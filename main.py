"""
بوت نسخ ونشر: تلغرام -> مراجعة بالأزرار -> فيسبوك

التدفّق:
  1) حساب شخصي (user) يستمع للقنوات المصدر.
  2) عند أي منشور جديد يرسله بوت المراجعة إلى محادثة الأدمنين مع أزرار.
  3) الأدمن يضغط: نشر / نشر بدون وسائط / تعديل النص / تجاهل.
  4) عند الموافقة يُنشر على صفحة فيسبوك.
"""
import asyncio
import logging
import os

from telethon import Button, TelegramClient, events
from telethon.utils import get_peer_id

import config
import sources as sources_store
from facebook import FacebookPublisher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("tg2fb")

os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)

# عميلان على نفس حلقة asyncio: حساب شخصي + بوت
user = TelegramClient("user_session", config.API_ID, config.API_HASH)
bot = TelegramClient("bot_session", config.API_ID, config.API_HASH)

fb = FacebookPublisher(config.FB_PAGE_ID, config.FB_PAGE_TOKEN)

# المنشورات المعلّقة بانتظار قرار الأدمن
pending: dict[str, dict] = {}
# الأدمنون الذين هم في وضع "تعديل النص الآن"
edit_state: dict[int, str] = {}
_counter = 0

# القنوات المصدر (تُدار من داخل البوت وتُحفظ في sources.json)
source_entries: list[dict] = []
source_ids: set[int] = set()


def _rebuild_ids():
    source_ids.clear()
    source_ids.update(e["id"] for e in source_entries)


async def _resolve(identifier):
    """يحوّل @username / رابط / معرّف إلى (id, العنوان) عبر الحساب الشخصي."""
    if isinstance(identifier, str) and identifier.lstrip("-").isdigit():
        identifier = int(identifier)
    entity = await user.get_entity(identifier)
    peer_id = get_peer_id(entity)
    title = (
        getattr(entity, "title", None)
        or getattr(entity, "username", None)
        or str(peer_id)
    )
    return peer_id, title


async def init_sources():
    global source_entries
    source_entries = sources_store.load()
    # أول تشغيل: انسخ القنوات من .env إن وُجدت
    if not source_entries and config.SOURCE_CHANNELS:
        for ch in config.SOURCE_CHANNELS:
            try:
                peer_id, title = await _resolve(ch)
                source_entries.append({"id": peer_id, "title": title, "input": str(ch)})
            except Exception as e:  # noqa: BLE001
                log.warning("تعذّر حل القناة %s: %s", ch, e)
        sources_store.save(source_entries)
    _rebuild_ids()


def _new_id() -> str:
    global _counter
    _counter += 1
    return str(_counter)


def _preview(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "(بدون نص)"
    return text if len(text) <= 3500 else text[:3500] + "…"


def _build_buttons(item_id: str, item: dict):
    rows = [[Button.inline("✅ نشر", f"pub:{item_id}".encode())]]
    if item.get("media_path"):
        rows.append(
            [Button.inline("📄 نشر النص فقط (بدون وسائط)", f"pubtext:{item_id}".encode())]
        )
    rows.append(
        [
            Button.inline("✏️ تعديل النص", f"edit:{item_id}".encode()),
            Button.inline("❌ تجاهل", f"skip:{item_id}".encode()),
        ]
    )
    return rows


def _cleanup(item_id: str):
    item = pending.pop(item_id, None)
    if item and item.get("media_path"):
        try:
            os.remove(item["media_path"])
        except OSError:
            pass


async def _send_for_review(item_id: str):
    item = pending[item_id]
    caption = f"📥 منشور جديد للمراجعة:\n\n{_preview(item['text'])}"
    buttons = _build_buttons(item_id, item)
    if item.get("media_path") and item.get("media_type") in ("photo", "video"):
        await bot.send_file(
            config.REVIEW_CHAT_ID, item["media_path"], caption=caption, buttons=buttons
        )
    else:
        await bot.send_message(config.REVIEW_CHAT_ID, caption, buttons=buttons)


# ---------- الحساب الشخصي: يقرأ القنوات المصدر ----------
@user.on(events.NewMessage)
async def on_source_message(event):
    if event.chat_id not in source_ids:
        return
    msg = event.message
    text = msg.message or ""
    media_path = None
    media_type = None

    if msg.media:
        media_type = "video" if msg.video else "photo" if msg.photo else "document"
        try:
            media_path = await msg.download_media(file=config.DOWNLOAD_DIR + "/")
        except Exception as e:  # noqa: BLE001
            log.warning("فشل تنزيل الوسائط: %s", e)
            media_path = None
            media_type = None

    item_id = _new_id()
    pending[item_id] = {"text": text, "media_path": media_path, "media_type": media_type}
    log.info("منشور جديد %s (نوع: %s)", item_id, media_type or "نص")
    try:
        await _send_for_review(item_id)
    except Exception as e:  # noqa: BLE001
        log.error("فشل إرسال المنشور للمراجعة: %s", e)
        _cleanup(item_id)


# ---------- بوت المراجعة: الأزرار ----------
@bot.on(events.CallbackQuery)
async def on_callback(event):
    if event.sender_id not in config.ADMIN_IDS:
        await event.answer("غير مصرّح لك.", alert=True)
        return

    action, _, item_id = event.data.decode().partition(":")
    item = pending.get(item_id)
    if not item:
        await event.answer("انتهت صلاحية هذا المنشور أو تمت معالجته.", alert=True)
        return

    if action == "edit":
        edit_state[event.sender_id] = item_id
        await event.respond("✏️ أرسل الآن النص الجديد للمنشور (كرسالة عادية).")
        await event.answer()
        return

    if action == "skip":
        _cleanup(item_id)
        await event.edit("🚫 تم التجاهل ولن يُنشر.")
        return

    if action in ("pub", "pubtext"):
        await _publish(event, item_id, include_media=(action == "pub"))


async def _publish(event, item_id: str, include_media: bool):
    item = pending.get(item_id)
    if not item:
        await event.answer("المنشور لم يعد متاحاً.", alert=True)
        return

    await event.answer("⏳ جاري النشر على فيسبوك…")
    text = item["text"]
    path = item.get("media_path")
    mtype = item.get("media_type")

    try:
        if include_media and path and mtype == "photo":
            await asyncio.to_thread(fb.post_photo, path, text)
        elif include_media and path and mtype == "video":
            await asyncio.to_thread(fb.post_video, path, text)
        else:
            await asyncio.to_thread(fb.post_text, text)
    except Exception as e:  # noqa: BLE001
        log.error("فشل النشر %s: %s", item_id, e)
        await event.respond(f"❌ فشل النشر على فيسبوك:\n{e}")
        return

    log.info("تم نشر %s على فيسبوك", item_id)
    await event.edit(f"✅ تم النشر على فيسبوك.\n\n{_preview(text)}")
    _cleanup(item_id)


# ---------- بوت المراجعة: استقبال النص المعدّل + أوامر ----------
@bot.on(events.NewMessage(pattern=r"^/id"))
async def cmd_id(event):
    await event.respond(
        f"chat id: `{event.chat_id}`\nyour id: `{event.sender_id}`"
    )


@bot.on(events.NewMessage(pattern=r"^/start"))
async def cmd_start(event):
    await event.respond(
        "بوت مراجعة المنشورات يعمل ✅\n\n"
        "الأوامر:\n"
        "/id — معرّف المحادثة ومعرّفك\n"
        "/sources — عرض القنوات المصدر\n"
        "/addsource @قناة — إضافة قناة\n"
        "/delsource @قناة — حذف قناة"
    )


# ---------- إدارة القنوات المصدر من داخل البوت (للأدمنين) ----------
@bot.on(events.NewMessage(pattern=r"^/sources"))
async def cmd_sources(event):
    if event.sender_id not in config.ADMIN_IDS:
        return
    if not source_entries:
        await event.respond("لا توجد قنوات مصدر بعد.\nأضف بـ: /addsource @القناة")
        return
    lines = [f"• {e['title']}  (`{e['id']}`)" for e in source_entries]
    await event.respond("📡 القنوات المصدر:\n" + "\n".join(lines))


@bot.on(events.NewMessage(pattern=r"^/addsource(?:@\w+)?(?:\s+(.+))?$"))
async def cmd_addsource(event):
    if event.sender_id not in config.ADMIN_IDS:
        return
    arg = (event.pattern_match.group(1) or "").strip()
    if not arg:
        await event.respond("الاستخدام: `/addsource @channel`\nأو رابط أو معرّف رقمي.")
        return
    try:
        peer_id, title = await _resolve(arg)
    except Exception as e:  # noqa: BLE001
        await event.respond(
            "❌ تعذّر الوصول للقناة. تأكد أن حسابك الشخصي **عضو فيها**.\n" f"{e}"
        )
        return
    if any(e["id"] == peer_id for e in source_entries):
        await event.respond(f"ℹ️ القناة مضافة مسبقاً: {title}")
        return
    source_entries.append({"id": peer_id, "title": title, "input": arg})
    sources_store.save(source_entries)
    _rebuild_ids()
    await event.respond(f"✅ تمت إضافة: {title}  (`{peer_id}`)")


@bot.on(events.NewMessage(pattern=r"^/delsource(?:@\w+)?(?:\s+(.+))?$"))
async def cmd_delsource(event):
    if event.sender_id not in config.ADMIN_IDS:
        return
    arg = (event.pattern_match.group(1) or "").strip()
    if not arg:
        await event.respond("الاستخدام: `/delsource @channel` أو المعرّف الرقمي.")
        return

    target_id = None
    if arg.lstrip("-").isdigit():
        target_id = int(arg)
    else:
        try:
            target_id, _ = await _resolve(arg)
        except Exception:  # noqa: BLE001
            target_id = None

    kept = [
        e
        for e in source_entries
        if not (e["id"] == target_id or e["input"] == arg or e["title"] == arg)
    ]
    removed = len(source_entries) - len(kept)
    if removed:
        source_entries[:] = kept
        sources_store.save(source_entries)
        _rebuild_ids()
        await event.respond(f"🗑️ تم حذف {removed} قناة. استخدم /sources للعرض.")
    else:
        await event.respond("لم أجد قناة مطابقة. استخدم /sources للعرض.")


@bot.on(events.NewMessage)
async def on_edit_text(event):
    # نلتقط النص الجديد فقط من أدمن في وضع التعديل
    if event.sender_id not in edit_state:
        return
    if not event.text or event.text.startswith("/"):
        return

    item_id = edit_state.pop(event.sender_id)
    item = pending.get(item_id)
    if not item:
        await event.respond("المنشور لم يعد متاحاً.")
        return

    item["text"] = event.text
    await event.respond("✅ تم تحديث النص. هذه المعاينة الجديدة:")
    await _send_for_review(item_id)


async def main():
    await bot.start(bot_token=config.BOT_TOKEN)
    # أول تشغيل سيطلب رقم الهاتف ورمز التحقق لإنشاء جلسة الحساب الشخصي
    await user.start()

    await init_sources()

    me = await user.get_me()
    bot_me = await bot.get_me()
    log.info("الحساب الشخصي: %s | البوت: @%s", me.id, bot_me.username)
    log.info("يستمع لـ %d قناة مصدر", len(source_entries))

    await asyncio.gather(
        user.run_until_disconnected(),
        bot.run_until_disconnected(),
    )


if __name__ == "__main__":
    asyncio.run(main())
