"""
بوت نسخ ونشر: تلغرام -> مراجعة بالأزرار -> فيسبوك
كل شيء يُدار من داخل تلغرام (تسجيل الدخول، فيسبوك، القنوات، الأدمنون، التحديث).

شغّل مرة واحدة:  python setup.py      (يحفظ api_id/api_hash/bot_token)
ثم:             python main.py       وأرسل /start للبوت وأكمل من هناك.
"""
import asyncio
import logging
import os
import re
import secrets
import subprocess
import sys

from telethon import Button, TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.utils import get_peer_id

from facebook import FacebookPublisher
from settings import BASE_DIR, Settings

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("tg2fb")

S = Settings()

if not S.bootstrap_ready():
    print("❌ لم يتم الإعداد الأولي بعد. شغّل أولاً:  python setup.py")
    sys.exit(1)

os.makedirs(S.get("download_dir", "downloads"), exist_ok=True)

# عميلان على نفس حلقة asyncio: حساب شخصي + بوت
user = TelegramClient(
    os.path.join(BASE_DIR, "user_session"), S.get("api_id"), S.get("api_hash")
)
bot = TelegramClient(
    os.path.join(BASE_DIR, "bot_session"), S.get("api_id"), S.get("api_hash")
)

# حالة قيد الانتظار: منشورات للمراجعة + محادثات إدخال جارية
pending: dict[str, dict] = {}
state: dict[int, dict] = {}        # user_id -> {"action": ..., ...}
source_ids: set[int] = set()
_counter = 0
_claim_code = None


def _new_id() -> str:
    global _counter
    _counter += 1
    return str(_counter)


def _rebuild_ids():
    source_ids.clear()
    source_ids.update(S.source_ids())


def _preview(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "(بدون نص)"
    return text if len(text) <= 3500 else text[:3500] + "…"


async def _resolve(identifier):
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


# ============ استقبال منشورات القنوات المصدر ============
@user.on(events.NewMessage)
async def on_source_message(event):
    if event.chat_id not in source_ids:
        return
    review_chat = S.get("review_chat_id")
    if not review_chat:
        return

    msg = event.message
    text = msg.message or ""
    media_path = None
    media_type = None
    if msg.media:
        media_type = "video" if msg.video else "photo" if msg.photo else "document"
        try:
            media_path = await msg.download_media(file=S.get("download_dir") + "/")
        except Exception as e:  # noqa: BLE001
            log.warning("فشل تنزيل الوسائط: %s", e)
            media_path = media_type = None

    item_id = _new_id()
    pending[item_id] = {"text": text, "media_path": media_path, "media_type": media_type}
    log.info("منشور جديد %s (%s)", item_id, media_type or "نص")
    try:
        await _send_for_review(item_id)
    except Exception as e:  # noqa: BLE001
        log.error("فشل إرسال المنشور للمراجعة: %s", e)
        _cleanup(item_id)


def _build_buttons(item_id, item):
    rows = [[Button.inline("✅ نشر", f"pub:{item_id}".encode())]]
    if item.get("media_path"):
        rows.append([Button.inline("📄 نشر النص فقط", f"pubtext:{item_id}".encode())])
    rows.append(
        [
            Button.inline("✏️ تعديل النص", f"edit:{item_id}".encode()),
            Button.inline("❌ تجاهل", f"skip:{item_id}".encode()),
        ]
    )
    return rows


def _cleanup(item_id):
    item = pending.pop(item_id, None)
    if item and item.get("media_path"):
        try:
            os.remove(item["media_path"])
        except OSError:
            pass


async def _send_for_review(item_id):
    item = pending[item_id]
    caption = f"📥 منشور جديد للمراجعة:\n\n{_preview(item['text'])}"
    buttons = _build_buttons(item_id, item)
    chat = S.get("review_chat_id")
    if item.get("media_path") and item.get("media_type") in ("photo", "video"):
        await bot.send_file(chat, item["media_path"], caption=caption, buttons=buttons)
    else:
        await bot.send_message(chat, caption, buttons=buttons)


# ============ لوحة التحكم (الأزرار) ============
def _panel_markup():
    login = "✅" if S.get("user_phone") else "❌"
    fb = "✅" if S.facebook_ready() else "❌"
    rev = "✅" if S.get("review_chat_id") else "❌"
    return [
        [Button.inline(f"🔐 تسجيل دخول الحساب {login}", b"m:login")],
        [Button.inline(f"📘 إعداد فيسبوك {fb}", b"m:fb")],
        [Button.inline(f"📍 تعيين قروب المراجعة {rev}", b"m:review")],
        [Button.inline("📡 القنوات المصدر", b"m:sources")],
        [Button.inline("👤 الأدمنون", b"m:admins")],
        [Button.inline("🌍 رمز الدولة الافتراضي", b"m:cc")],
        [Button.inline("🔄 تحديث من GitHub", b"m:update")],
        [Button.inline("ℹ️ الحالة", b"m:status")],
    ]


async def _show_panel(event):
    await event.respond("⚙️ لوحة التحكم:", buttons=_panel_markup())


@bot.on(events.NewMessage(pattern=r"^/(panel|start)"))
async def cmd_panel(event):
    uid = event.sender_id
    # أول شخص يطالب بالملكية عبر رمز يظهر في سجل الـ Raspberry
    if not S.get("owner_id"):
        await event.respond(
            "👋 أهلاً! لتصبح المالك، أرسل:\n`/claim الرمز`\n"
            "الرمز يظهر في سجل التشغيل على جهاز Raspberry."
        )
        return
    if not S.is_admin(uid):
        await event.respond("هذا البوت خاص. لست ضمن الأدمنين.")
        return
    await _show_panel(event)


@bot.on(events.NewMessage(pattern=r"^/claim(?:\s+(\S+))?"))
async def cmd_claim(event):
    global _claim_code
    if S.get("owner_id"):
        return
    code = (event.pattern_match.group(1) or "").strip()
    if _claim_code and code == _claim_code:
        S.set("owner_id", event.sender_id)
        S.add_admin(event.sender_id)
        _claim_code = None
        await event.respond("✅ أصبحت المالك والأدمن. أرسل /panel للمتابعة.")
        log.info("تم تعيين المالك: %s", event.sender_id)
    else:
        await event.respond("❌ الرمز غير صحيح.")


@bot.on(events.NewMessage(pattern=r"^/id"))
async def cmd_id(event):
    await event.respond(f"chat id: `{event.chat_id}`\nyour id: `{event.sender_id}`")


# ============ أزرار اللوحة ============
@bot.on(events.CallbackQuery(pattern=rb"^m:"))
async def on_menu(event):
    if not S.is_admin(event.sender_id):
        await event.answer("غير مصرّح لك.", alert=True)
        return
    what = event.data.decode().split(":", 1)[1]

    if what == "login":
        if S.get("user_phone"):
            await event.respond(
                f"الحساب مسجّل حالياً: {S.get('user_phone')}\n"
                "لإعادة تسجيل الدخول أرسل الرقم مرة أخرى."
            )
        state[event.sender_id] = {"action": "login_phone"}
        await event.respond(
            "🔐 أرسل رقم هاتف الحساب الشخصي:\n"
            "• مع رمز الدولة: `+9665xxxxxxxx`\n"
            "• أو بدونه وسنضيف الرمز الافتراضي (اضبطه من 🌍 رمز الدولة)."
        )
    elif what == "fb":
        state[event.sender_id] = {"action": "fb_page_id"}
        await event.respond("📘 أرسل **معرّف صفحة فيسبوك** (FB_PAGE_ID):")
    elif what == "review":
        S.set("review_chat_id", event.chat_id)
        await event.respond("📍 تم تعيين هذه المحادثة كقروب المراجعة ✅")
    elif what == "sources":
        await _show_sources(event)
    elif what == "admins":
        await _show_admins(event)
    elif what == "cc":
        state[event.sender_id] = {"action": "set_cc"}
        await event.respond("🌍 أرسل رمز الدولة الافتراضي بالأرقام فقط، مثل: `966`")
    elif what == "update":
        await _self_update(event)
    elif what == "status":
        await _show_status(event)
    await event.answer()


# ============ القنوات المصدر ============
async def _show_sources(event):
    srcs = S.sources()
    text = "📡 القنوات المصدر:\n" + (
        "\n".join(f"• {s['title']} (`{s['id']}`)" for s in srcs)
        if srcs else "(لا توجد قنوات بعد)"
    )
    await event.respond(
        text,
        buttons=[
            [Button.inline("➕ إضافة قناة", b"src:add")],
            [Button.inline("➖ حذف قناة", b"src:del")],
        ],
    )


@bot.on(events.CallbackQuery(pattern=rb"^src:"))
async def on_src(event):
    if not S.is_admin(event.sender_id):
        await event.answer("غير مصرّح لك.", alert=True)
        return
    action = event.data.decode().split(":", 1)[1]
    if action == "add":
        if not await user.is_user_authorized():
            await event.respond("سجّل دخول الحساب أولاً من 🔐.")
        else:
            state[event.sender_id] = {"action": "add_source"}
            await event.respond("أرسل @يوزر_القناة أو رابطها أو معرّفها الرقمي:")
    elif action == "del":
        state[event.sender_id] = {"action": "del_source"}
        await event.respond("أرسل @يوزر_القناة أو معرّفها الرقمي لحذفها:")
    await event.answer()


# ============ الأدمنون ============
async def _show_admins(event):
    ids = S.get("admin_ids") or []
    owner = S.get("owner_id")
    lines = [f"• `{i}`" + (" (المالك)" if i == owner else "") for i in ids]
    await event.respond(
        "👤 الأدمنون:\n" + ("\n".join(lines) or "(لا أحد)"),
        buttons=[
            [Button.inline("➕ إضافة أدمن", b"adm:add")],
            [Button.inline("➖ حذف أدمن", b"adm:del")],
        ],
    )


@bot.on(events.CallbackQuery(pattern=rb"^adm:"))
async def on_adm(event):
    if event.sender_id != S.get("owner_id"):
        await event.answer("للمالك فقط.", alert=True)
        return
    action = event.data.decode().split(":", 1)[1]
    if action == "add":
        state[event.sender_id] = {"action": "add_admin"}
        await event.respond("أرسل المعرّف الرقمي للأدمن الجديد (يعرفه بأمر /id):")
    elif action == "del":
        state[event.sender_id] = {"action": "del_admin"}
        await event.respond("أرسل المعرّف الرقمي للأدمن المراد حذفه:")
    await event.answer()


# ============ التحديث الذاتي على Raspberry ============
async def _self_update(event):
    await event.respond("🔄 جاري السحب من GitHub…")
    try:
        out = subprocess.run(
            ["git", "-C", BASE_DIR, "pull", "--ff-only"],
            capture_output=True, text=True, timeout=120,
        )
        msg = (out.stdout + out.stderr).strip()[:1500]
        await event.respond(f"```\n{msg}\n```\n♻️ إعادة تشغيل البوت…")
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ فشل التحديث: {e}")
        return
    await asyncio.sleep(1)
    try:
        await bot.disconnect()
        await user.disconnect()
    finally:
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])


# ============ الحالة ============
async def _show_status(event):
    await event.respond(
        "ℹ️ الحالة:\n"
        f"• تسجيل الحساب: {'✅ ' + str(S.get('user_phone')) if S.get('user_phone') else '❌'}\n"
        f"• فيسبوك: {'✅' if S.facebook_ready() else '❌'}\n"
        f"• قروب المراجعة: {'✅' if S.get('review_chat_id') else '❌'}\n"
        f"• عدد القنوات: {len(S.sources())}\n"
        f"• عدد الأدمنين: {len(S.get('admin_ids') or [])}\n"
        f"• رمز الدولة الافتراضي: {S.get('default_cc') or '—'}"
    )


# ============ نشر المنشورات ============
@bot.on(events.CallbackQuery(pattern=rb"^(pub|pubtext|edit|skip):"))
async def on_post_action(event):
    if not S.is_admin(event.sender_id):
        await event.answer("غير مصرّح لك.", alert=True)
        return
    action, _, item_id = event.data.decode().partition(":")
    item = pending.get(item_id)
    if not item:
        await event.answer("انتهت صلاحية هذا المنشور.", alert=True)
        return

    if action == "edit":
        state[event.sender_id] = {"action": "edit_text", "item_id": item_id}
        await event.respond("✏️ أرسل الآن النص الجديد.")
        await event.answer()
    elif action == "skip":
        _cleanup(item_id)
        await event.edit("🚫 تم التجاهل.")
    else:
        await _publish(event, item_id, include_media=(action == "pub"))


async def _publish(event, item_id, include_media):
    if not S.facebook_ready():
        await event.answer("أعدّ فيسبوك أولاً من /panel.", alert=True)
        return
    item = pending.get(item_id)
    if not item:
        await event.answer("المنشور لم يعد متاحاً.", alert=True)
        return

    await event.answer("⏳ جاري النشر…")
    fb = FacebookPublisher(S.get("fb_page_id"), S.get("fb_page_token"))
    text, path, mtype = item["text"], item.get("media_path"), item.get("media_type")
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
    await event.edit(f"✅ تم النشر على فيسبوك.\n\n{_preview(text)}")
    _cleanup(item_id)


# ============ موجّه الإدخالات النصية (محادثات الإعداد) ============
@bot.on(events.NewMessage)
async def on_text(event):
    uid = event.sender_id
    st = state.get(uid)
    if not st or not event.text or event.text.startswith("/"):
        return
    action = st["action"]
    text = event.text.strip()

    if action == "login_phone":
        await _login_phone(event, text)
    elif action == "login_code":
        await _login_code(event, st, text)
    elif action == "login_password":
        await _login_password(event, text)
    elif action == "set_cc":
        S.set("default_cc", re.sub(r"\D", "", text))
        state.pop(uid, None)
        await event.respond(f"✅ رمز الدولة الافتراضي: {S.get('default_cc')}")
    elif action == "fb_page_id":
        S.set("fb_page_id", text)
        state[uid] = {"action": "fb_token"}
        await event.respond("الآن أرسل **توكن الصفحة** (FB_PAGE_TOKEN):")
    elif action == "fb_token":
        S.set("fb_page_token", text)
        state.pop(uid, None)
        await event.respond("✅ تم حفظ إعداد فيسبوك.")
    elif action == "add_source":
        await _add_source(event, text)
    elif action == "del_source":
        rid = int(text) if text.lstrip("-").isdigit() else None
        removed = S.remove_source(peer_id=rid, raw=text)
        _rebuild_ids()
        state.pop(uid, None)
        await event.respond(f"🗑️ حُذف {removed} قناة." if removed else "لم أجد قناة مطابقة.")
    elif action == "add_admin":
        if text.isdigit():
            S.add_admin(int(text))
            await event.respond(f"✅ أضيف الأدمن `{text}`")
        else:
            await event.respond("أرسل معرّفاً رقمياً صحيحاً.")
        state.pop(uid, None)
    elif action == "del_admin":
        if text.isdigit():
            S.remove_admin(int(text))
            await event.respond(f"🗑️ حُذف الأدمن `{text}`")
        else:
            await event.respond("أرسل معرّفاً رقمياً صحيحاً.")
        state.pop(uid, None)
    elif action == "edit_text":
        item = pending.get(st["item_id"])
        state.pop(uid, None)
        if not item:
            await event.respond("المنشور لم يعد متاحاً.")
            return
        item["text"] = event.text
        await event.respond("✅ تم تحديث النص. المعاينة الجديدة:")
        await _send_for_review(st["item_id"])


def _normalize_phone(raw):
    raw = raw.strip().replace(" ", "")
    if raw.startswith("+"):
        return raw
    if raw.startswith("00"):
        return "+" + raw[2:]
    cc = S.get("default_cc")
    if cc:
        return "+" + cc + raw.lstrip("0")
    return None  # يحتاج رمز الدولة


async def _login_phone(event, raw):
    phone = _normalize_phone(raw)
    if not phone:
        await event.respond(
            "⚠️ الرقم بدون رمز دولة. إمّا أرسله كـ `+9665...`\n"
            "أو اضبط رمز الدولة الافتراضي من زر 🌍 ثم أعد المحاولة."
        )
        return
    if not user.is_connected():
        await user.connect()
    try:
        sent = await user.send_code_request(phone)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ تعذّر إرسال الرمز: {e}")
        return
    state[event.sender_id] = {
        "action": "login_code", "phone": phone, "hash": sent.phone_code_hash
    }
    await event.respond(
        "📩 وصلك رمز داخل تلغرام. أرسله **مع فواصل** حتى لا يُلغى تلقائياً، مثل:\n"
        "`1 2 3 4 5`"
    )


async def _login_code(event, st, text):
    code = re.sub(r"\D", "", text)
    try:
        await user.sign_in(phone=st["phone"], code=code, phone_code_hash=st["hash"])
    except SessionPasswordNeededError:
        state[event.sender_id] = {"action": "login_password"}
        await event.respond("🔒 الحساب محمي بكلمة مرور (تحقق بخطوتين). أرسلها الآن:")
        return
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ رمز غير صحيح أو منتهٍ: {e}\nأعد المحاولة من 🔐.")
        state.pop(event.sender_id, None)
        return
    await _login_done(event, st["phone"])


async def _login_password(event, password):
    try:
        await user.sign_in(password=password)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ كلمة المرور غير صحيحة: {e}")
        return
    await _login_done(event, S.get("user_phone"))


async def _login_done(event, phone):
    me = await user.get_me()
    if phone:
        S.set("user_phone", phone)
    state.pop(event.sender_id, None)
    await event.respond(
        f"✅ تم تسجيل الدخول: {me.first_name} (id `{me.id}`)\n"
        "الآن أضف القنوات من 📡."
    )
    log.info("تم تسجيل دخول الحساب الشخصي: %s", me.id)


async def _add_source(event, raw):
    try:
        peer_id, title = await _resolve(raw)
    except Exception as e:  # noqa: BLE001
        await event.respond(
            "❌ تعذّر الوصول للقناة. تأكد أن حسابك الشخصي **عضو فيها**.\n" f"{e}"
        )
        return
    added = S.add_source(peer_id, title, raw)
    _rebuild_ids()
    state.pop(event.sender_id, None)
    await event.respond(
        f"✅ أُضيفت: {title} (`{peer_id}`)" if added else f"ℹ️ موجودة مسبقاً: {title}"
    )


# ============ التشغيل ============
async def main():
    global _claim_code
    await bot.start(bot_token=S.get("bot_token"))
    await user.connect()

    if not S.get("owner_id"):
        _claim_code = f"{secrets.randbelow(1000000):06d}"
        log.warning("=" * 48)
        log.warning("لا يوجد مالك بعد. أرسل للبوت في تلغرام:  /claim %s", _claim_code)
        log.warning("=" * 48)

    _rebuild_ids()
    authed = await user.is_user_authorized()
    bot_me = await bot.get_me()
    log.info("البوت: @%s | الحساب الشخصي مسجّل: %s", bot_me.username, authed)
    if not authed:
        log.info("الحساب غير مسجّل — سجّل الدخول من زر 🔐 داخل البوت.")

    await asyncio.gather(
        user.run_until_disconnected(), bot.run_until_disconnected()
    )


if __name__ == "__main__":
    asyncio.run(main())
