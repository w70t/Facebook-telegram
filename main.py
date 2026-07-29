"""
بوت نسخ ونشر: تلغرام -> مراجعة بالأزرار -> فيسبوك
كل شيء يُدار من داخل تلغرام (تسجيل الدخول، فيسبوك، القنوات، الأدمنون، التحديث).

شغّل مرة واحدة:  python configure.py   (يحفظ api_id/api_hash/bot_token)
ثم:             python main.py        وأرسل /start للبوت وأكمل من هناك.
"""
import asyncio
import glob
import hashlib
import logging
import os
import random
import re
import secrets
import stat
import subprocess
import sys
import time
from urllib.parse import urljoin, urlsplit

import requests
from telethon import Button, TelegramClient, events
from telethon.errors import (
    FloodWaitError,
    MessageNotModifiedError,
    SessionPasswordNeededError,
)
from telethon.utils import get_peer_id

from facebook import MAX_ALBUM_PHOTOS, FacebookAuthError, FacebookError, FacebookPublisher
from settings import BASE_DIR, Settings
from store import PendingStore
from twitter import XReader
from util import (
    CAPTION_LIMIT,
    TEXT_LIMIT,
    human_size,
    media_summary,
    normalize_phone,
    preview,
    review_body,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("tg2fb")

S = Settings()

if not S.bootstrap_ready():
    print("❌ لم يتم الإعداد الأولي بعد. شغّل أولاً:  python configure.py")
    sys.exit(1)

# ملفات الحالة (التنزيلات + المنشورات المعلّقة) تعيش بجوار settings.json
STATE_DIR = os.path.dirname(os.path.abspath(S.path)) or BASE_DIR
_download_dir = S.get("download_dir", "downloads")
DOWNLOAD_DIR = (
    _download_dir if os.path.isabs(_download_dir)
    else os.path.join(STATE_DIR, _download_dir)
)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# عميلان على نفس حلقة asyncio: حساب شخصي + بوت
user = TelegramClient(
    os.path.join(BASE_DIR, "user_session"), S.get("api_id"), S.get("api_hash")
)
bot = TelegramClient(
    os.path.join(BASE_DIR, "bot_session"), S.get("api_id"), S.get("api_hash")
)

# المنشورات المعلّقة تُحفظ على القرص: معرّفات عشوائية تنجو من إعادة التشغيل
PENDING = PendingStore(
    os.path.join(STATE_DIR, "pending.json"),
    DOWNLOAD_DIR,
    ttl_hours=S.get_int("pending_ttl_hours", 48),
)

state: dict[int, dict] = {}        # user_id -> {"action": ..., "ts": ...}
source_ids: set[int] = set()
STATE_TTL = 600                    # محادثة إعداد مهجورة تنتهي بعد 10 دقائق
HOUSEKEEPING_SECONDS = 3600

MAX_CLAIM_ATTEMPTS = 5
_claim_code = None
_claim_attempts = 0

xreader = XReader(S)


# ============ حالة محادثات الإعداد (بمهلة) ============
def _set_state(uid, data):
    state[uid] = {**data, "ts": time.time()}


def _get_state(uid):
    st = state.get(uid)
    if not st:
        return None
    if time.time() - st.get("ts", 0) > STATE_TTL:
        state.pop(uid, None)
        return None
    return st


def _clear_state(uid):
    state.pop(uid, None)


def _purge_states():
    cutoff = time.time() - STATE_TTL
    for uid in [u for u, st in state.items() if st.get("ts", 0) < cutoff]:
        state.pop(uid, None)


_SAFE_EXT = re.compile(r"^\.[A-Za-z0-9]{1,5}$")
# معرّف صفحة فيسبوك رقمي دائماً؛ يُدمج في مسار URL فلا نقبل غيره
_FB_PAGE_ID = re.compile(r"^\d{1,25}$")
# روابط وسائط X المشروعة فقط
X_MEDIA_HOSTS = ("twimg.com", "twitter.com", "x.com")
# بيانات اعتماد مضمّنة في رابط git (https://user:token@host) — لا تُرسل لتلغرام
_CRED_IN_URL = re.compile(r"(https?://)[^/\s:@]+(?::[^/\s@]*)?@")


def _redact(text):
    """يحجب أي بيانات اعتماد داخل روابط قبل عرض مخرجات git/pip في المحادثة."""
    return _CRED_IN_URL.sub(r"\1***@", text or "")


def _trusted_media_url(url):
    parts = urlsplit(url or "")
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in X_MEDIA_HOSTS)


def _harden_state_permissions():
    """
    ⚠️ أمان: Telethon ينشئ ملفات الجلسة عبر sqlite بصلاحيات umask الافتراضية
    (0644) ولا يضبطها أبداً — تحقّقنا من مصدر المكتبة. ملف الجلسة يعادل دخولاً
    كاملاً لحساب تلغرام، فأي مستخدم آخر على الجهاز يستطيع نسخه وانتحال الحساب.
    """
    for pattern in ("*.session", "*.session-journal"):
        for path in glob.glob(os.path.join(BASE_DIR, pattern)):
            try:
                if stat.S_IMODE(os.stat(path).st_mode) & 0o077:
                    os.chmod(path, 0o600)
                    log.info("ضُبطت صلاحيات %s إلى 600", os.path.basename(path))
            except OSError as e:
                log.warning("تعذّر ضبط صلاحيات %s: %s", path, e)


def _rebuild_ids():
    source_ids.clear()
    source_ids.update(S.source_ids())


def _max_media_bytes():
    return S.get_int("max_media_mb", 200) * 1024 * 1024


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


# ============ نداءات تلغرام مع احتواء FloodWait ============
async def _tg_call(fn, *args, **kwargs):
    """
    تلغرام يفرض انتظاراً إجبارياً عند كثرة الإرسال. بدون معالجته كانت أي قناة نشطة
    تُسقط المنشورات صامتةً.
    """
    for attempt in range(3):
        try:
            return await fn(*args, **kwargs)
        except FloodWaitError as e:
            wait = int(getattr(e, "seconds", 5))
            if wait > 300 or attempt == 2:
                raise
            log.warning("FloodWait %ss — أنتظر ثم أعيد المحاولة", wait)
            await asyncio.sleep(wait + 1)
    raise RuntimeError("تعذّر إرسال الرسالة بعد عدة محاولات")


# ============ استقبال منشورات القنوات المصدر ============
def _media_kind(msg):
    """
    نوع الوسيط. الملفات المرسلة كمستند (image/video mime) تُعامل كصورة/فيديو بدل
    أن تُسقط بصمت عند النشر.
    """
    if getattr(msg, "photo", None):
        return "photo"
    if getattr(msg, "video", None) or getattr(msg, "video_note", None) or getattr(msg, "gif", None):
        return "video"
    doc = getattr(msg, "document", None)
    if doc is not None:
        mime = getattr(doc, "mime_type", "") or ""
        if mime.startswith("image/"):
            return "photo"
        if mime.startswith("video/"):
            return "video"
        return "document"
    return None


DEFAULT_EXT = {"photo": ".jpg", "video": ".mp4", "document": ".bin"}


def _is_inside(path, directory):
    """هل المسار داخل المجلد فعلاً (بعد حلّ الروابط الرمزية و..)؟"""
    try:
        root = os.path.realpath(directory)
        return os.path.commonpath([os.path.realpath(path), root]) == root
    except (ValueError, OSError):
        return False


def _safe_media_path(msg, kind):
    """
    اسم ملف نولّده نحن داخل مجلد التنزيل.

    ⚠️ أمان: اسم الملف القادم من تلغرام (DocumentAttributeFilename) يتحكّم به
    **صاحب القناة المصدر** — وأنت تتابع قنوات لا تملكها. لو مُرّر مجلد إلى
    download_media فإن Telethon قبل 1.42 كان يدمج ذلك الاسم كما هو، فاسم مثل
    "../../venv/lib/python3.11/site-packages/x.pth" يكتب ملفاً خارج المجلد
    (وملف .pth داخل الـ venv يعني تنفيذ كود عند أول تشغيل).
    بتمرير مسار كامل نختاره نحن، لا يُستخدم اسم المُرسِل أصلاً — بأي إصدار.
    """
    ext = getattr(getattr(msg, "file", None), "ext", "") or ""
    if not _SAFE_EXT.match(ext):
        ext = DEFAULT_EXT.get(kind, ".bin")
    return os.path.join(DOWNLOAD_DIR, f"tg_{secrets.token_hex(8)}{ext}")


async def _download_tg_media(msg):
    """يُرجع {"path","type"} أو None. يتخطّى الملفات الأكبر من الحد المسموح."""
    kind = _media_kind(msg)
    if not kind:
        return None
    max_bytes = _max_media_bytes()
    size = getattr(getattr(msg, "file", None), "size", None) or 0
    if max_bytes and size > max_bytes:
        log.warning(
            "تخطّي وسيط بحجم %s (الحد %s)", human_size(size), human_size(max_bytes)
        )
        return None
    try:
        path = await msg.download_media(file=_safe_media_path(msg, kind))
    except Exception as e:  # noqa: BLE001
        log.warning("فشل تنزيل الوسائط: %s", e)
        return None
    if not path:
        return None
    if not _is_inside(path, DOWNLOAD_DIR):     # حزام أمان ثانٍ
        log.error("تنزيل خارج مجلد الوسائط — رُفض: %r", path)
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    return {"path": path, "type": kind}


async def _queue_for_review(text, media, origin=""):
    """يضيف منشوراً للمخزن ويرسله للمراجعة. يرجع item_id أو None."""
    item_id = PENDING.add(text, media, origin)
    try:
        await _send_for_review(item_id)
        return item_id
    except Exception as e:  # noqa: BLE001
        log.error("فشل إرسال المنشور للمراجعة: %s", e)
        PENDING.remove(item_id)
        await _notify_owner(f"⚠️ وصل منشور جديد لكن تعذّر عرضه للمراجعة:\n{e}")
        return None


@user.on(events.NewMessage)
async def on_source_message(event):
    if event.chat_id not in source_ids or not S.get("review_chat_id"):
        return
    if event.message.grouped_id:
        return                      # جزء من ألبوم — يتكفّل به on_source_album

    msg = event.message
    text = msg.message or ""
    if S.is_filtered(text):
        log.info("تجاهل منشور تلغرام (فلتر كلمات)")
        return

    media = []
    if msg.media:
        item = await _download_tg_media(msg)
        if item:
            media.append(item)

    log.info("منشور جديد من %s (%s)", event.chat_id, media_summary(media) or "نص")
    await _queue_for_review(text, media)


@user.on(events.Album)
async def on_source_album(event):
    """
    ألبوم تلغرام (عدة صور برسالة واحدة) يصل كرسائل منفصلة. بدون هذا المعالج كان
    كل عنصر يصبح منشوراً مستقلاً على فيسبوك.
    """
    if event.chat_id not in source_ids or not S.get("review_chat_id"):
        return

    text = next((m.message for m in event.messages if m.message), "") or ""
    if S.is_filtered(text):
        log.info("تجاهل ألبوم تلغرام (فلتر كلمات)")
        return

    media = []
    for msg in event.messages[:MAX_ALBUM_PHOTOS]:
        item = await _download_tg_media(msg)
        if item:
            media.append(item)

    log.info("ألبوم جديد من %s (%s)", event.chat_id, media_summary(media) or "نص")
    await _queue_for_review(text, media)


# ============ عرض المنشور للمراجعة ============
def _build_buttons(item_id, item):
    playable = [m for m in item.get("media") or [] if m.get("type") in ("photo", "video")]
    rows = [[Button.inline("✅ نشر", f"pub:{item_id}".encode())]]
    if playable:
        rows.append([Button.inline("📄 نشر النص فقط", f"pubtext:{item_id}".encode())])
    rows.append(
        [
            Button.inline("✏️ تعديل النص", f"edit:{item_id}".encode()),
            Button.inline("❌ تجاهل", f"skip:{item_id}".encode()),
        ]
    )
    return rows


def _review_header(item):
    header = "📥 منشور جديد للمراجعة:"
    summary = media_summary(item.get("media"))
    if summary:
        header += f"\n📎 {summary}"
    if any(m.get("type") == "document" for m in item.get("media") or []):
        header += " (الملفات لا تُنشر على فيسبوك)"
    if item.get("origin"):
        header += f"\n🔗 {item['origin']}"
    return header


async def _send_for_review(item_id, refresh=False):
    item = PENDING.get(item_id)
    chat = S.get("review_chat_id")
    if not item or not chat:
        return

    header = _review_header(item)
    buttons = _build_buttons(item_id, item)
    files = PENDING.media_paths(item_id)
    review = item.get("review") or {}

    # بعد التعديل نحدّث نفس الرسالة بدل ترك رسالة قديمة بأزرار حيّة
    if refresh and review.get("msg"):
        limit = CAPTION_LIMIT if review.get("has_media") else TEXT_LIMIT
        try:
            await _tg_call(
                bot.edit_message,
                review["chat"],
                review["msg"],
                review_body(item["text"], header, limit),
                buttons=buttons,
            )
            return
        except MessageNotModifiedError:
            return
        except Exception as e:  # noqa: BLE001
            log.warning("تعذّر تحديث رسالة المراجعة، سأرسل جديدة: %s", e)

    if len(files) > 1:
        # الألبوم لا يقبل أزراراً في تلغرام — نرسله ثم رسالة تحكّم منفصلة
        await _tg_call(bot.send_file, chat, files)
        msg = await _tg_call(
            bot.send_message, chat,
            review_body(item["text"], header, TEXT_LIMIT), buttons=buttons,
        )
        has_media = False
    elif len(files) == 1:
        msg = await _tg_call(
            bot.send_file, chat, files[0],
            caption=review_body(item["text"], header, CAPTION_LIMIT),
            buttons=buttons,
        )
        has_media = True
    else:
        msg = await _tg_call(
            bot.send_message, chat,
            review_body(item["text"], header, TEXT_LIMIT), buttons=buttons,
        )
        has_media = False

    PENDING.update(item_id, review={"chat": chat, "msg": msg.id, "has_media": has_media})


# ============ لوحة التحكم (الأزرار) ============
def _panel_markup():
    login = "✅" if S.get("user_phone") else "❌"
    fb = "✅" if S.facebook_ready() else "❌"
    rev = "✅" if S.get("review_chat_id") else "❌"
    return [
        [Button.inline(f"🔐 تسجيل دخول الحساب {login}", b"m:login")],
        [Button.inline(f"📘 إعداد فيسبوك {fb}", b"m:fb")],
        [Button.inline(f"📍 تعيين قروب المراجعة {rev}", b"m:review")],
        [Button.inline("📡 قنوات تلغرام المصدر", b"m:sources")],
        [Button.inline(
            f"🐦 حسابات دخول X ({len(S.x_logins())}) "
            f"{'✅' if S.x_login_ready() else '❌'}", b"m:xlogins"
        )],
        [Button.inline("🐦 حسابات X المتابَعة", b"m:xaccounts")],
        [Button.inline(f"🚫 فلترة الكلمات ({len(S.filter_words())})", b"m:filter")],
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
    """
    الملكية = كل الأسرار (توكن فيسبوك، كلمات مرور X). رمز من ستة أرقام بلا حد
    للمحاولات كان قابلاً للتخمين، فصار رمزاً طويلاً مع سقف محاولات.
    """
    global _claim_code, _claim_attempts
    if S.get("owner_id") or not _claim_code:
        return
    if _claim_attempts >= MAX_CLAIM_ATTEMPTS:
        await event.respond("🚫 تجاوزت عدد المحاولات. أعد تشغيل البوت لتوليد رمز جديد.")
        return

    code = (event.pattern_match.group(1) or "").strip()
    if code and secrets.compare_digest(code, _claim_code):
        S.set("owner_id", event.sender_id)
        S.add_admin(event.sender_id)
        _claim_code = None
        await event.respond("✅ أصبحت المالك والأدمن. أرسل /panel للمتابعة.")
        log.info("تم تعيين المالك: %s", event.sender_id)
        return

    _claim_attempts += 1
    left = MAX_CLAIM_ATTEMPTS - _claim_attempts
    log.warning("محاولة /claim فاشلة من %s (المتبقي %d)", event.sender_id, left)
    if left <= 0:
        _claim_code = None
        await event.respond("🚫 تجاوزت عدد المحاولات. أعد تشغيل البوت لتوليد رمز جديد.")
    else:
        await event.respond(f"❌ الرمز غير صحيح. المحاولات المتبقية: {left}")


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
        _set_state(event.sender_id, {"action": "login_phone"})
        await event.respond(
            "🔐 أرسل رقم هاتف الحساب الشخصي:\n"
            "• مع رمز الدولة: `+9665xxxxxxxx`\n"
            "• أو بدونه وسنضيف الرمز الافتراضي (اضبطه من 🌍 رمز الدولة)."
        )
    elif what == "fb":
        _set_state(event.sender_id, {"action": "fb_page_id"})
        await event.respond("📘 أرسل **معرّف صفحة فيسبوك** (FB_PAGE_ID):")
    elif what == "review":
        S.set("review_chat_id", event.chat_id)
        await event.respond("📍 تم تعيين هذه المحادثة كقروب المراجعة ✅")
    elif what == "sources":
        await _show_sources(event)
    elif what == "xlogins":
        await _show_x_logins(event)
    elif what == "xaccounts":
        await _show_x_accounts(event)
    elif what == "filter":
        await _show_filter(event)
    elif what == "admins":
        await _show_admins(event)
    elif what == "cc":
        _set_state(event.sender_id, {"action": "set_cc"})
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
            _set_state(event.sender_id, {"action": "add_source"})
            await event.respond("أرسل @يوزر_القناة أو رابطها أو معرّفها الرقمي:")
    elif action == "del":
        _set_state(event.sender_id, {"action": "del_source"})
        await event.respond("أرسل @يوزر_القناة أو معرّفها الرقمي لحذفها:")
    await event.answer()


# ============ حسابات دخول X (مجموعة مع تبديل) ============
async def _notify_owner(text):
    chat = S.get("review_chat_id") or S.get("owner_id")
    if not chat:
        return
    try:
        await _tg_call(bot.send_message, chat, text)
    except Exception as e:  # noqa: BLE001
        log.warning("تعذّر تنبيه المالك: %s", e)


async def _show_x_logins(event):
    logins = S.x_logins()
    active = S.active_x_login()

    def label(lg):
        if lg.get("failed"):
            mark = "🚫"
        elif active and lg["username"].lower() == active["username"].lower():
            mark = "⭐"
        else:
            mark = "•"
        return f"{mark} @{lg['username']}"

    text = "🐦 حسابات دخول X:\n" + (
        "\n".join(label(lg) for lg in logins) if logins else "(لا يوجد)"
    )
    text += "\n\n⭐ النشط | 🚫 محظور/فشل\nلو انحظر حساب، أضف غيره وسيبدّل تلقائياً."
    await event.respond(
        text,
        buttons=[
            [Button.inline("➕ إضافة حساب دخول", b"xlog:add")],
            [
                Button.inline("🔁 تبديل النشط", b"xlog:switch"),
                Button.inline("➖ حذف حساب", b"xlog:del"),
            ],
            [Button.inline("♻️ إعادة تفعيل المحظورة", b"xlog:reset")],
        ],
    )


@bot.on(events.CallbackQuery(pattern=rb"^xlog:"))
async def on_xlogin(event):
    if not S.is_admin(event.sender_id):
        await event.answer("غير مصرّح لك.", alert=True)
        return
    action = event.data.decode().split(":", 1)[1]
    if action == "add":
        _set_state(event.sender_id, {"action": "x_user"})
        await event.respond(
            "🐦 استخدم حساب X ثانوياً.\nأرسل **اسم المستخدم** (بدون @):"
        )
    elif action == "switch":
        _set_state(event.sender_id, {"action": "x_switch"})
        await event.respond("أرسل @اسم الحساب الذي تريد تفعيله:")
    elif action == "del":
        _set_state(event.sender_id, {"action": "x_login_del"})
        await event.respond("أرسل @اسم حساب الدخول لحذفه:")
    elif action == "reset":
        S.reset_x_failures()
        xreader.invalidate()
        await event.respond(
            "♻️ أُعيد تفعيل كل حسابات الدخول.\n"
            "الكوكيز المنتهية تُحذف تلقائياً ليعاد تسجيل الدخول بكلمة المرور."
        )
    await event.answer()


# ============ حسابات X المتابَعة ============
async def _show_x_accounts(event):
    accs = S.x_accounts()
    text = "🐦 حسابات X المتابَعة:\n" + (
        "\n".join(f"• @{a['screen_name']}" for a in accs)
        if accs else "(لا توجد حسابات بعد)"
    )
    replies = "مُتجاهَلة (تغريدات فقط)" if S.get("x_skip_replies", True) else "مشمولة"
    await event.respond(
        text,
        buttons=[
            [Button.inline("➕ إضافة حساب", b"xacc:add")],
            [Button.inline("➖ حذف حساب", b"xacc:del")],
            [Button.inline(f"↩️ الردود: {replies}", b"xacc:replies")],
        ],
    )


@bot.on(events.CallbackQuery(pattern=rb"^xacc:"))
async def on_xacc(event):
    if not S.is_admin(event.sender_id):
        await event.answer("غير مصرّح لك.", alert=True)
        return
    action = event.data.decode().split(":", 1)[1]
    if action == "add":
        if not S.x_login_ready():
            await event.respond("سجّل دخول X أولاً من 🐦 حسابات دخول X.")
        else:
            _set_state(event.sender_id, {"action": "x_add"})
            await event.respond("أرسل @اسم_الحساب المراد متابعته:")
    elif action == "del":
        _set_state(event.sender_id, {"action": "x_del"})
        await event.respond("أرسل @اسم_الحساب المراد حذفه:")
    elif action == "replies":
        S.set("x_skip_replies", not S.get("x_skip_replies", True))
        st = "تغريدات فقط (تجاهل الردود)" if S.get("x_skip_replies") else "التغريدات والردود"
        await event.respond(f"↩️ الوضع الآن: {st}")
    await event.answer()


async def _add_x_account(event, raw):
    name = raw.lstrip("@").strip()
    _clear_state(event.sender_id)
    try:
        user_id, _disp = await xreader.resolve(name)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ تعذّر إيجاد الحساب. تأكد من تسجيل دخول X.\n{e}")
        return
    # نقطة البداية = أحدث تغريدة الآن، وإلا وصلت آخر 20 تغريدة دفعة واحدة
    last_id = await xreader.latest_tweet_id(user_id)
    added = S.add_x_account(name, user_id, last_id)
    await event.respond(
        f"✅ أُضيف حساب X: @{name}\nسأنقل التغريدات الجديدة من الآن فصاعداً."
        if added else f"ℹ️ موجود مسبقاً: @{name}"
    )


# ============ فلترة الكلمات ============
async def _show_filter(event):
    words = S.filter_words()
    text = "🚫 كلمات الفلترة (أي منشور يحتويها يُتجاهل):\n" + (
        "\n".join(f"• {w}" for w in words) if words else "(لا توجد كلمات)"
    )
    text += "\n\nتُطبّق على منشورات تلغرام و X معاً."
    await event.respond(
        text,
        buttons=[
            [Button.inline("➕ إضافة كلمة", b"flt:add")],
            [Button.inline("➖ حذف كلمة", b"flt:del")],
        ],
    )


@bot.on(events.CallbackQuery(pattern=rb"^flt:"))
async def on_flt(event):
    if not S.is_admin(event.sender_id):
        await event.answer("غير مصرّح لك.", alert=True)
        return
    action = event.data.decode().split(":", 1)[1]
    if action == "add":
        _set_state(event.sender_id, {"action": "add_filter"})
        await event.respond("أرسل الكلمة/العبارة الممنوعة:")
    elif action == "del":
        _set_state(event.sender_id, {"action": "del_filter"})
        await event.respond("أرسل الكلمة المراد حذفها:")
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
        _set_state(event.sender_id, {"action": "add_admin"})
        await event.respond("أرسل المعرّف الرقمي للأدمن الجديد (يعرفه بأمر /id):")
    elif action == "del":
        _set_state(event.sender_id, {"action": "del_admin"})
        await event.respond("أرسل المعرّف الرقمي للأدمن المراد حذفه:")
    await event.answer()


# ============ التحديث الذاتي على Raspberry ============
def _requirements_fingerprint():
    try:
        with open(os.path.join(BASE_DIR, "requirements.txt"), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def _run(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


async def _self_update(event):
    """
    يسحب آخر نسخة ثم يعيد التشغيل — لكن فقط عند نجاح السحب فعلاً.
    كان يعيد التشغيل حتى لو فشل git pull، ولا يثبّت الاعتماديات الجديدة.
    """
    await event.respond("🔄 جاري السحب من GitHub…")
    before = _requirements_fingerprint()
    try:
        out = await asyncio.to_thread(
            _run, ["git", "-C", BASE_DIR, "pull", "--ff-only"], 180
        )
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ فشل التحديث: {e}")
        return

    # المخرجات تُرسل إلى محادثة تلغرام؛ رابط origin قد يحمل توكن وصول مضمّناً
    msg = _redact((out.stdout + out.stderr).strip())[:1500] or "(بلا مخرجات)"
    if out.returncode != 0:
        await event.respond(
            f"❌ فشل السحب — **لن أعيد التشغيل**:\n```\n{msg}\n```\n"
            "غالباً بسبب تعديلات محلية. جرّب: `git -C . status`"
        )
        return

    await event.respond(f"```\n{msg}\n```")

    if _requirements_fingerprint() != before:
        await event.respond("📦 تغيّرت الاعتماديات — جاري التثبيت…")
        try:
            pip = await asyncio.to_thread(
                _run,
                [sys.executable, "-m", "pip", "install", "-r",
                 os.path.join(BASE_DIR, "requirements.txt"), "--quiet"],
                600,
            )
            if pip.returncode != 0:
                await event.respond(
                    f"⚠️ فشل تثبيت الاعتماديات — لن أعيد التشغيل:\n"
                    f"```\n{_redact((pip.stdout + pip.stderr).strip())[:1000]}\n```"
                )
                return
        except Exception as e:  # noqa: BLE001
            await event.respond(f"⚠️ تعذّر تشغيل pip: {e}\nلن أعيد التشغيل.")
            return

    await event.respond("♻️ إعادة تشغيل البوت…")
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
        f"• فيسبوك: {'✅' if S.facebook_ready() else '❌'} (Graph {S.get('fb_api_version')})\n"
        f"• قروب المراجعة: {'✅' if S.get('review_chat_id') else '❌'}\n"
        f"• قنوات تلغرام: {len(S.sources())}\n"
        f"• حسابات دخول X: {len(S.x_logins())} (النشط: "
        f"{('@' + xreader.active) if xreader.active else '—'})\n"
        f"• حسابات X المتابَعة: {len(S.x_accounts())}\n"
        f"• منشورات بانتظار المراجعة: {len(PENDING.items)}\n"
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
    item = PENDING.get(item_id)
    if not item:
        await event.answer("انتهت صلاحية هذا المنشور.", alert=True)
        return

    if action == "edit":
        _set_state(event.sender_id, {"action": "edit_text", "item_id": item_id})
        await event.respond("✏️ أرسل الآن النص الجديد.")
        await event.answer()
    elif action == "skip":
        PENDING.remove(item_id)
        await event.edit("🚫 تم التجاهل.")
    else:
        await _publish(event, item_id, include_media=(action == "pub"))


async def _publish(event, item_id, include_media):
    if not S.facebook_ready():
        await event.answer("أعدّ فيسبوك أولاً من /panel.", alert=True)
        return
    item = PENDING.get(item_id)
    if not item:
        await event.answer("المنشور لم يعد متاحاً.", alert=True)
        return

    text = item["text"]
    photos = PENDING.media_paths(item_id, ("photo",))
    videos = PENDING.media_paths(item_id, ("video",))
    if not text.strip() and not (include_media and (photos or videos)):
        await event.answer("لا يوجد نص ولا وسائط للنشر.", alert=True)
        return

    await event.answer("⏳ جاري النشر…")
    fb = FacebookPublisher(
        S.get("fb_page_id"), S.get("fb_page_token"),
        version=S.get("fb_api_version"),
    )
    note = ""
    try:
        if include_media and videos:
            if len(videos) > 1 or photos:
                note = "\nℹ️ فيسبوك يقبل فيديو واحداً لكل منشور — نُشر الأول فقط."
            await asyncio.to_thread(fb.post_video, videos[0], text)
        elif include_media and photos:
            if len(photos) > MAX_ALBUM_PHOTOS:
                note = f"\nℹ️ نُشرت أول {MAX_ALBUM_PHOTOS} صور فقط."
            await asyncio.to_thread(fb.post_photos, photos, text)
        else:
            await asyncio.to_thread(fb.post_text, text)
    except FacebookAuthError as e:
        log.error("مشكلة مصادقة فيسبوك %s: %s", item_id, e)
        await event.respond(
            f"🔑 مشكلة في توكن فيسبوك:\n{e}\n\n"
            "المنشور محفوظ — أعِد ضبط 📘 فيسبوك من /panel ثم اضغط نشر مجدداً."
        )
        await _notify_owner("🔑 توكن فيسبوك لم يعد صالحاً — النشر متوقف حتى تحديثه.")
        return
    except (FacebookError, OSError) as e:
        log.error("فشل النشر %s: %s", item_id, e)
        await event.respond(f"❌ فشل النشر على فيسبوك:\n{e}\nالمنشور محفوظ، جرّب مجدداً.")
        return

    await event.edit(f"✅ تم النشر على فيسبوك.{note}\n\n{preview(text, 800)}")
    PENDING.remove(item_id)


# ============ موجّه الإدخالات النصية (محادثات الإعداد) ============
@bot.on(events.NewMessage)
async def on_text(event):
    uid = event.sender_id
    st = _get_state(uid)
    if not st or not event.text or event.text.startswith("/"):
        return
    # ⚠️ أمان: الصلاحية تُفحص عند فتح المحادثة فقط، فمن أُزيل من الأدمنين بعدها
    # كان بإمكانه إكمال خطوة معلّقة (تغيير توكن فيسبوك مثلاً). نعيد الفحص هنا.
    if not S.is_admin(uid):
        _clear_state(uid)
        log.warning("أُهملت محادثة إعداد لمستخدم لم يعد أدمن: %s", uid)
        return
    action = st["action"]
    text = event.text.strip()

    if action == "login_phone":
        await _login_phone(event, text)
    elif action == "login_code":
        await _login_code(event, st, text)
    elif action == "login_password":
        await _login_password(event, st, text)
    elif action == "set_cc":
        S.set("default_cc", re.sub(r"\D", "", text))
        _clear_state(uid)
        await event.respond(f"✅ رمز الدولة الافتراضي: {S.get('default_cc')}")
    elif action == "fb_page_id":
        # يُدمج في مسار Graph API — لا نقبل إلا أرقاماً
        if not _FB_PAGE_ID.match(text):
            await event.respond(
                "❌ معرّف الصفحة يجب أن يكون أرقاماً فقط (مثل `123456789012345`).\n"
                "تجده عبر `GET /me/accounts` في Graph Explorer."
            )
            return
        S.set("fb_page_id", text)
        _set_state(uid, {"action": "fb_token"})
        await event.respond("الآن أرسل **توكن الصفحة** (FB_PAGE_TOKEN):")
    elif action == "fb_token":
        await _save_fb_token(event, text)
    elif action == "add_source":
        await _add_source(event, text)
    elif action == "del_source":
        rid = int(text) if text.lstrip("-").isdigit() else None
        removed = S.remove_source(peer_id=rid, raw=text)
        _rebuild_ids()
        _clear_state(uid)
        await event.respond(f"🗑️ حُذف {removed} قناة." if removed else "لم أجد قناة مطابقة.")
    elif action == "x_user":
        _set_state(uid, {"action": "x_email", "x_username": text.lstrip("@")})
        await event.respond("أرسل بريد الحساب الإلكتروني (أو أرسل `-` لتخطّيه):")
    elif action == "x_email":
        _set_state(uid, {
            "action": "x_pass",
            "x_username": st.get("x_username"),
            "x_email": None if text == "-" else text,
        })
        await event.respond("أرسل **كلمة مرور** حساب X:")
    elif action == "x_pass":
        await _save_x_login(event, st, text)
    elif action == "x_switch":
        await _switch_x_login(event, text)
    elif action == "x_login_del":
        _clear_state(uid)
        removed = S.remove_x_login(text.lstrip("@"))
        if removed:
            xreader.invalidate()
        await event.respond(
            f"🗑️ حُذف {removed} حساب دخول." if removed else "لم أجد الحساب."
        )
    elif action == "x_add":
        await _add_x_account(event, text)
    elif action == "x_del":
        removed = S.remove_x_account(text.lstrip("@"))
        _clear_state(uid)
        await event.respond(f"🗑️ حُذف {removed} حساب." if removed else "لم أجد الحساب.")
    elif action == "add_filter":
        added = S.add_filter_word(text)
        _clear_state(uid)
        await event.respond(f"✅ أُضيفت الكلمة: {text}" if added else "ℹ️ موجودة مسبقاً.")
    elif action == "del_filter":
        removed = S.remove_filter_word(text)
        _clear_state(uid)
        await event.respond("🗑️ حُذفت الكلمة." if removed else "لم أجد الكلمة.")
    elif action == "add_admin":
        if text.isdigit():
            S.add_admin(int(text))
            await event.respond(f"✅ أضيف الأدمن `{text}`")
        else:
            await event.respond("أرسل معرّفاً رقمياً صحيحاً.")
        _clear_state(uid)
    elif action == "del_admin":
        if text.isdigit():
            S.remove_admin(int(text))
            await event.respond(f"🗑️ حُذف الأدمن `{text}`")
        else:
            await event.respond("أرسل معرّفاً رقمياً صحيحاً.")
        _clear_state(uid)
    elif action == "edit_text":
        await _apply_edit(event, st)


async def _save_fb_token(event, token):
    _clear_state(event.sender_id)
    S.set("fb_page_token", token)
    fb = FacebookPublisher(
        S.get("fb_page_id"), token, version=S.get("fb_api_version")
    )
    try:
        info = await asyncio.to_thread(fb.check_token)
    except FacebookAuthError as e:
        await event.respond(
            f"⚠️ حُفظ التوكن لكنه لا يعمل:\n{e}\n\n"
            "تذكّر: توكن Graph Explorer صالح ~ساعة فقط — بدّله بتوكن طويل الأجل "
            "(انظر SETUP_AR.md)."
        )
        return
    except FacebookError as e:
        await event.respond(f"⚠️ حُفظ التوكن لكن تعذّر التحقق الآن: {e}")
        return
    await event.respond(
        f"✅ تم حفظ إعداد فيسبوك والتحقق منه.\nالصفحة: {info.get('name', '؟')}"
    )


async def _save_x_login(event, st, password):
    username = st.get("x_username")
    email = st.get("x_email")
    _clear_state(event.sender_id)
    try:
        await event.delete()  # حذف رسالة كلمة المرور للخصوصية
    except Exception:  # noqa: BLE001
        pass
    S.add_x_login(username, email, password)
    xreader.invalidate()
    await event.respond("⏳ جاري تسجيل الدخول إلى X…")
    try:
        if await xreader.ensure_login():
            await event.respond(f"✅ تم الدخول. الحساب النشط: @{xreader.active}")
        else:
            await event.respond("❌ تعذّر الدخول بأي حساب. تحقق من البيانات.")
    except Exception as e:  # noqa: BLE001
        await event.respond(
            f"❌ فشل تسجيل الدخول إلى X: {e}\n"
            "قد يطلب تأكيداً أمنياً؛ سجّل الدخول من المتصفح مرة ثم أعد المحاولة."
        )


async def _switch_x_login(event, text):
    _clear_state(event.sender_id)
    if not S.set_active_x_login(text.lstrip("@")):
        await event.respond("لم أجد حساب دخول بهذا الاسم.")
        return
    xreader.invalidate()
    try:
        ok = await xreader.ensure_login()
        await event.respond(
            f"✅ الحساب النشط الآن: @{xreader.active}" if ok
            else "❌ تعذّر الدخول بهذا الحساب."
        )
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ فشل: {e}")


async def _apply_edit(event, st):
    item_id = st["item_id"]
    _clear_state(event.sender_id)
    if not PENDING.get(item_id):
        await event.respond("المنشور لم يعد متاحاً.")
        return
    PENDING.update(item_id, text=event.text)
    await _send_for_review(item_id, refresh=True)
    await event.respond("✅ تم تحديث النص في رسالة المراجعة.")


# ============ تسجيل الدخول ============
async def _login_phone(event, raw):
    phone = normalize_phone(raw, S.get("default_cc"))
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
    _set_state(event.sender_id, {
        "action": "login_code", "phone": phone, "hash": sent.phone_code_hash
    })
    await event.respond(
        "📩 وصلك رمز داخل تلغرام. أرسله **مع فواصل** حتى لا يُلغى تلقائياً، مثل:\n"
        "`1 2 3 4 5`"
    )


async def _login_code(event, st, text):
    code = re.sub(r"\D", "", text)
    try:
        await user.sign_in(phone=st["phone"], code=code, phone_code_hash=st["hash"])
    except SessionPasswordNeededError:
        _set_state(event.sender_id, {"action": "login_password", "phone": st["phone"]})
        await event.respond("🔒 الحساب محمي بكلمة مرور (تحقق بخطوتين). أرسلها الآن:")
        return
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ رمز غير صحيح أو منتهٍ: {e}\nأعد المحاولة من 🔐.")
        _clear_state(event.sender_id)
        return
    await _login_done(event, st["phone"])


async def _login_password(event, st, password):
    try:
        await user.sign_in(password=password)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ كلمة المرور غير صحيحة: {e}")
        return
    try:
        await event.delete()   # كلمة مرور الحساب لا تبقى في المحادثة
    except Exception:  # noqa: BLE001
        pass
    await _login_done(event, st.get("phone") or S.get("user_phone"))


async def _login_done(event, phone):
    me = await user.get_me()
    if phone:
        S.set("user_phone", phone)
    _clear_state(event.sender_id)
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
    if peer_id == S.get("review_chat_id"):
        await event.respond("⚠️ لا يمكن جعل قروب المراجعة نفسه مصدراً (حلقة لا نهائية).")
        return
    added = S.add_source(peer_id, title, raw)
    _rebuild_ids()
    _clear_state(event.sender_id)
    await event.respond(
        f"✅ أُضيفت: {title} (`{peer_id}`)" if added else f"ℹ️ موجودة مسبقاً: {title}"
    )


# ============ قارئ X: التنزيل والمعالجة والدوران ============
def _open_media_stream(url, max_hops=3):
    """
    يفتح رابط وسائط X بعد التأكد أنه يشير إلى نطاق موثوق — في كل تحويلة أيضاً.

    ⚠️ أمان (SSRF): الروابط تأتي من ردود twikit وليست من عندنا. بلا تقييد، ردٌّ
    مُلاعَب (أو تحويلة) يجعل البوت يجلب عناوين داخل الشبكة المحلية للـ Pi.
    """
    for _ in range(max_hops):
        if not _trusted_media_url(url):
            raise ValueError(f"رابط وسائط غير موثوق: {url[:80]}")
        resp = requests.get(url, stream=True, timeout=90, allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                raise ValueError("تحويلة بلا وجهة")
            url = urljoin(url, location)
            continue
        return resp
    raise ValueError("تجاوز عدد التحويلات المسموح")


def _download_url(url, dest_dir, max_bytes=0):
    """ينزّل وسيطاً بحد أقصى للحجم — بدونه فيديو ضخم واحد يملأ بطاقة الـ SD."""
    ext = os.path.splitext(urlsplit(url).path)[1]
    if not _SAFE_EXT.match(ext):      # امتداد من رابط خارجي — لا نثق به كما هو
        ext = ".bin"
    path = os.path.join(dest_dir, f"x_{secrets.token_hex(8)}{ext}")
    try:
        with _open_media_stream(url) as r:
            r.raise_for_status()
            declared = int(r.headers.get("Content-Length") or 0)
            if max_bytes and declared > max_bytes:
                raise ValueError(
                    f"حجم الوسيط {human_size(declared)} يتجاوز الحد "
                    f"{human_size(max_bytes)}"
                )
            written = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(8192):
                    written += len(chunk)
                    if max_bytes and written > max_bytes:
                        raise ValueError(
                            f"الوسيط تجاوز الحد {human_size(max_bytes)} أثناء التنزيل"
                        )
                    f.write(chunk)
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return path


async def handle_x_tweet(account, tweet):
    text = getattr(tweet, "full_text", None) or getattr(tweet, "text", "") or ""
    if S.is_filtered(text):
        log.info("تجاهل تغريدة (فلتر كلمات)")
        S.set_x_last_id(account["screen_name"], str(tweet.id))
        return

    media = []
    max_bytes = _max_media_bytes()
    for url, kind in XReader.extract_media_urls(tweet)[:MAX_ALBUM_PHOTOS]:
        try:
            path = await asyncio.to_thread(_download_url, url, DOWNLOAD_DIR, max_bytes)
        except Exception as e:  # noqa: BLE001
            log.warning("فشل تنزيل وسيط X: %s", e)
            continue
        media.append({"path": path, "type": kind})

    origin = f"https://x.com/{account['screen_name']}/status/{tweet.id}"
    log.info("تغريدة جديدة من @%s (%s)", account["screen_name"], media_summary(media) or "نص")
    await _queue_for_review(text, media, origin)
    S.set_x_last_id(account["screen_name"], str(tweet.id))


_x_alerted = False


async def x_poller():
    global _x_alerted
    await asyncio.sleep(5)
    while True:
        try:
            if S.x_accounts() and S.get("review_chat_id"):
                if await xreader.ensure_login():
                    _x_alerted = False
                    active = xreader.active
                    for i, acc in enumerate(S.x_accounts()):
                        # تباعد عشوائي بسيط بين الحسابات (سلوك أقل آلية)
                        if i:
                            await asyncio.sleep(random.uniform(3, 10))
                        try:
                            for tw in await xreader.fetch_new(acc):
                                await handle_x_tweet(acc, tw)
                        except Exception as e:  # noqa: BLE001
                            if xreader.report_failure(e):
                                await _notify_owner(
                                    f"⚠️ حساب X @{active} تعذّر (قد يكون محظوراً). "
                                    "سأجرّب حساباً آخر في الدورة القادمة."
                                )
                                break
                            log.warning("قراءة X @%s: %s", acc["screen_name"], e)
                elif S.x_logins() and not _x_alerted:
                    _x_alerted = True
                    await _notify_owner(
                        "🚫 كل حسابات دخول X محظورة/فاشلة.\n"
                        "أضف حساباً جديداً من /panel ← 🐦 حسابات دخول X."
                    )
        except Exception as e:  # noqa: BLE001
            log.warning("دورة X: %s", e)
        # فاصل عشوائي بين الدورات حتى لا يبان الإيقاع ساعةً منتظمة
        base = S.get_int("x_poll_seconds", 120)
        await asyncio.sleep(random.uniform(base * 0.7, base * 1.6))


# ============ التنظيف الدوري ============
async def housekeeping():
    """
    بدون هذا كانت المنشورات غير المُراجَعة وملفاتها تتراكم للأبد حتى تمتلئ
    بطاقة الـ SD على الـ Pi.
    """
    while True:
        await asyncio.sleep(HOUSEKEEPING_SECONDS)
        try:
            expired = PENDING.purge_expired()
            orphans = PENDING.sweep_orphans()
            _purge_states()
            _harden_state_permissions()   # الجلسات تُعاد كتابتها أحياناً
            if expired or orphans:
                log.info("تنظيف: %d منشور منتهٍ، %d ملف يتيم", expired, orphans)
        except Exception as e:  # noqa: BLE001
            log.warning("فشل التنظيف الدوري: %s", e)


# ============ التشغيل ============
async def main():
    global _claim_code
    await bot.start(bot_token=S.get("bot_token"))
    await user.connect()
    _harden_state_permissions()   # ملفات الجلسة تُنشأ الآن — قيّدها فوراً

    if S.recovery == "recovered":
        log.warning("تم استرجاع الإعدادات من النسخة الاحتياطية بعد تلف الملف.")
    elif S.recovery == "corrupt":
        log.error("ملف الإعدادات كان تالفاً — راجع الملفات بلاحقة .corrupt-*")

    if not S.get("owner_id"):
        _claim_code = secrets.token_urlsafe(9)
        log.warning("=" * 56)
        log.warning("لا يوجد مالك بعد. أرسل للبوت في تلغرام:  /claim %s", _claim_code)
        log.warning("=" * 56)

    _rebuild_ids()
    PENDING.purge_expired()
    PENDING.sweep_orphans()

    authed = await user.is_user_authorized()
    bot_me = await bot.get_me()
    log.info("البوت: @%s | الحساب الشخصي مسجّل: %s", bot_me.username, authed)
    log.info("منشورات بانتظار المراجعة: %d", len(PENDING.items))
    if not authed:
        log.info("الحساب غير مسجّل — سجّل الدخول من زر 🔐 داخل البوت.")

    await asyncio.gather(
        user.run_until_disconnected(),
        bot.run_until_disconnected(),
        x_poller(),
        housekeeping(),
    )


if __name__ == "__main__":
    asyncio.run(main())
