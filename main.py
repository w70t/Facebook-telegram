"""
بوت نسخ ونشر: تلغرام -> مراجعة بالأزرار -> فيسبوك
كل شيء يُدار من داخل تلغرام (تسجيل الدخول، فيسبوك، القنوات، الأدمنون، التحديث).

شغّل مرة واحدة:  python configure.py   (يحفظ api_id/api_hash/bot_token)
ثم:             python main.py        وأرسل /start للبوت وأكمل من هناك.
"""
import asyncio
import glob
import hashlib
import hmac
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

from facebook import (
    MAX_ALBUM_PHOTOS,
    FacebookAuthError,
    FacebookError,
    FacebookPublisher,
    FacebookUncertainError,
)
from settings import BASE_DIR, Settings
from store import PendingStore
from twitter import XReader, XSessionAccountMismatch, XSessionVerificationError
from xbrowser import (
    XBrowserCleanupError,
    XBrowserChallengeRejected,
    XBrowserCredentialsRejected,
    XBrowserPageChanged,
    XBrowserRateLimited,
    XBrowserSessionError,
    XBrowserUnavailable,
    XBrowserUnsupportedChallenge,
)
from xtransaction import XTransactionCompatibilityError, XTransactionNetworkError
from jsonio import atomic_write_json, read_json
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

try:
    _scrubbed_x_passwords = S.scrub_x_login_passwords()
    if _scrubbed_x_passwords:
        log.info("حُذفت %d كلمة مرور X قديمة من الإعدادات", _scrubbed_x_passwords)
except OSError as exc:
    # لا تُستخدم كلمة المرور القديمة في الخلفية أو مسار المتصفح، لكن نعيد محاولة
    # حذفها في التشغيل التالي إذا كان القرص مؤقتاً للقراءة فقط/ممتلئاً.
    log.error("تعذّر حذف كلمات مرور X القديمة من القرص (%s)", type(exc).__name__)

if not S.bootstrap_ready():
    print("❌ لم يتم الإعداد الأولي بعد. شغّل أولاً:  python configure.py")
    sys.exit(1)

# ملفات الحالة (التنزيلات + المنشورات المعلّقة) تعيش بجوار settings.json
STATE_DIR = os.path.dirname(os.path.abspath(S.path)) or BASE_DIR
SESSION_DIR = STATE_DIR


def _resolve_download_dir(value, state_dir=STATE_DIR):
    """يقبل فقط مجلد تنزيل فرعياً حقيقياً داخل مجلد الحالة."""
    state_root = os.path.normcase(os.path.realpath(os.path.abspath(state_dir)))
    try:
        raw = os.fspath(value) if value else "downloads"
    except TypeError:
        raw = "downloads"
    candidate = raw if os.path.isabs(raw) else os.path.join(state_root, raw)
    candidate = os.path.normcase(os.path.realpath(os.path.abspath(candidate)))
    try:
        managed = (
            candidate != state_root
            and os.path.commonpath([state_root, candidate]) == state_root
        )
    except ValueError:  # أقراص مختلفة على Windows
        managed = False
    if managed:
        return candidate
    fallback = os.path.join(state_root, "downloads")
    log.error(
        "download_dir خارج مجلد الحالة ورُفض (%r)؛ سيُستخدم %s",
        value, fallback,
    )
    return fallback


_download_dir = S.get("download_dir", "downloads")
DOWNLOAD_DIR = _resolve_download_dir(_download_dir)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def _harden_state_permissions(directory=None):
    """
    يقيد جلسات Telethon قبل أي اتصال بالشبكة؛ ملف الجلسة يعادل دخولاً كاملاً
    لحساب تلغرام ولا يجوز أن يبقى بصلاحيات umask الافتراضية (0644).
    """
    directory = directory or SESSION_DIR
    for pattern in ("*.session", "*.session-journal"):
        for path in glob.glob(os.path.join(directory, pattern)):
            try:
                if stat.S_IMODE(os.stat(path).st_mode) & 0o077:
                    os.chmod(path, 0o600)
                    log.info("ضُبطت صلاحيات %s إلى 600", os.path.basename(path))
            except OSError as e:
                log.warning("تعذّر ضبط صلاحيات %s: %s", path, e)


# عميلان على نفس حلقة asyncio: حساب شخصي + بوت
user = TelegramClient(
    os.path.join(SESSION_DIR, "user_session"), S.get("api_id"), S.get("api_hash")
)
bot = TelegramClient(
    os.path.join(SESSION_DIR, "bot_session"), S.get("api_id"), S.get("api_hash")
)
_harden_state_permissions()

# المنشورات المعلّقة تُحفظ على القرص: معرّفات عشوائية تنجو من إعادة التشغيل
PENDING = PendingStore(
    os.path.join(STATE_DIR, "pending.json"),
    DOWNLOAD_DIR,
    ttl_hours=S.get_int("pending_ttl_hours", 48),
)

state: dict[int, dict] = {}        # user_id -> {"action": ..., "ts": ...}
source_ids: set[int] = set()
# حجز ذري داخل حلقة asyncio: يمنع ضغطتي نشر متزامنتين، ويمنع skip/edit من
# حذف العنصر أو وسائطه بينما يرفعها طلب نشر جارٍ.
_publishing: set[str] = set()
# حزام داخل العملية إذا نجح Facebook ثم تعذّر تثبيت/حذف pending. الحالة الدائمة
# publish_state أدناه هي الحماية الأساسية عبر إعادة التشغيل.
_published: set[str] = set()
STATE_TTL = 600                    # محادثة إعداد مهجورة تنتهي بعد 10 دقائق
HOUSEKEEPING_SECONDS = 3600
X_CHALLENGE_TIMEOUT = 180          # رمز X مؤقت؛ لا نحتفظ به أكثر من 3 دقائق
X_SECRET_DELETE_TIMEOUT = 15       # لا نحتفظ بكلمة المرور بسبب RPC معلّق بلا حد
X_SECRET_TOMBSTONE_TTL = STATE_TTL # يغطي مهلة كل محادثة إعداد/رمز متأخر
X_LOGIN_SHUTDOWN_TIMEOUT = 55      # يسمح بإغلاق Chromium المحمي قبل restart/exit
X_LOGIN_COOLDOWN_SECONDS = 60 * 60 # مدة احترازية؛ X لا يرسل مدة دقيقة في صفحة الحظر
X_COOLDOWN_UPDATE_SECONDS = 60     # تحديث عداد الرسالة بلا إغراق Telegram
X_COOLDOWN_NOTIFY_RETRY_SECONDS = 60
X_COOLDOWN_SHUTDOWN_TIMEOUT = 5    # مهمة العداد لا تحمل أسراراً ولا تعطل restart

# محاولة تسجيل X تبقى داخل الذاكرة فقط. لا نضع كلمة المرور أو رمز الاستخدام
# الواحد في state، ولا نكتب رمز Authenticator إلى settings.json.
_x_login_tasks: dict[int, asyncio.Task] = {}
_x_challenges: dict[tuple[int, int], asyncio.Future] = {}
_x_login_deleting: set[int] = set()
_x_login_cancelled: set[int] = set()
_x_secret_tombstones: dict[tuple[int, int], float] = {}
_x_secret_tasks: set[asyncio.Task] = set()
_x_cooldown_task = None
_x_cooldown_memory = None          # بديل مؤقت فقط إذا تعذرت كتابة settings
_x_cooldown_notifying: set[str] = set()
_x_cooldown_notified_memory: set[str] = set()
_x_cooldown_lock = None
_x_cooldown_lock_loop = None
_x_cooldown_runtime = None         # deadline monotonic للجيل الحالي
# العدّاد القديم أعلاه بلا هوية حساب، لذلك يبقى للإشعار فقط ولا يمنع حساباً
# جديداً. العدادات الجديدة منفصلة ببصمة HMAC ولا تحفظ اسم مستخدم X.
_x_account_cooldown_tasks: dict[str, asyncio.Task] = {}
_x_account_cooldown_memory: dict[str, dict] = {}
_x_account_cooldown_runtime: dict[str, dict] = {}
_x_account_cooldown_notifying: set[tuple[str, str]] = set()
_x_account_cooldown_notified_memory: set[tuple[str, str]] = set()
_x_cooldown_hmac_key = None
_x_cooldown_key_error = False
_x_browser_cleanup_unconfirmed = False
_x_secret_delete_unconfirmed = False
_restarting = False
_restart_task = None

MAX_CLAIM_ATTEMPTS = 5
_claim_code = None
_claim_attempts = 0

BTN_PANEL = "⚙️ لوحة التحكم"
BTN_ID = "🆔 معرّفي"
BTN_CANCEL = "🛑 إلغاء"
BTN_CLAIM = "🔑 تفعيل الملكية"
REPLY_KEYBOARD_VERSION = 1
_REPLY_ACTIONS = {
    BTN_PANEL: "panel",
    BTN_ID: "id",
    BTN_CANCEL: "cancel",
    BTN_CLAIM: "claim",
}
_RESERVED_COMMANDS = {"/start", "/panel", "/id", "/cancel", "/claim"}
X_SETUP_ACTIONS = {
    "x_user", "x_email", "x_pass_pending", "x_pass",
    "x_login_running", "x_auth_code",
}

xreader = XReader(S)

X_COOLDOWN_KEY_FILE = os.path.join(STATE_DIR, "x_cooldown_key.json")
_X_COOLDOWN_KEY_CONTEXT = b"tg2fb/x-cooldown/v1\0"
_X_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_XLOGIN_SAFE_STAGES = {
    "login_started",
    "challenge_requested_alternate_identifier",
    "challenge_requested_two_factor",
    "challenge_requested_verification_code",
    "challenge_received_alternate_identifier",
    "challenge_received_two_factor",
    "challenge_received_verification_code",
    "session_verified",
    "rate_limited",
    "attempt_finished",
}


def _log_xlogin_stage(stage):
    """يسجل مرحلة allowlisted فقط؛ لا اسم حساب ولا سر ولا prompt من X."""
    if stage in _XLOGIN_SAFE_STAGES:
        log.info("XLOGIN_STAGE %s", stage)


def _canonical_x_username(value):
    if not isinstance(value, str):
        return None
    username = value.strip()
    if username.startswith("@"):
        username = username[1:]
    if not _X_USERNAME_RE.fullmatch(username):
        return None
    return username.casefold()


def _load_x_cooldown_hmac_key():
    """مفتاح منفصل 0600؛ لا يُطبع ولا يُحفظ داخل settings.json."""
    global _x_cooldown_hmac_key, _x_cooldown_key_error
    if _x_cooldown_hmac_key is not None:
        return _x_cooldown_hmac_key
    if _x_cooldown_key_error:
        return None
    try:
        if os.path.lexists(X_COOLDOWN_KEY_FILE):
            info = os.stat(X_COOLDOWN_KEY_FILE, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("cooldown key is not a regular file")
            payload = read_json(X_COOLDOWN_KEY_FILE)
            if not isinstance(payload, dict):
                raise OSError("invalid cooldown key document")
            raw = bytes.fromhex(payload.get("key", ""))
            if payload.get("version") != 1 or len(raw) != 32:
                raise OSError("invalid cooldown key")
        else:
            # إذا وُجدت عدادات scoped وفُقد مفتاحها، توليد مفتاح جديد سيفتحها
            # خطأً. نفشل مغلقاً بدلاً من كسر الربط بصمت.
            existing = getattr(S, "x_login_cooldowns", lambda: {})()
            if existing:
                raise OSError("cooldown key missing while scoped records exist")
            raw = secrets.token_bytes(32)
            atomic_write_json(
                X_COOLDOWN_KEY_FILE,
                {"version": 1, "key": raw.hex()},
                mode=0o600,
            )
        try:
            os.chmod(X_COOLDOWN_KEY_FILE, 0o600)
        except OSError as exc:
            raise OSError("could not protect cooldown key") from exc
        if os.name == "posix":
            protected = os.stat(X_COOLDOWN_KEY_FILE, follow_symlinks=False)
            if protected.st_mode & 0o077:
                raise OSError("cooldown key permissions are too broad")
            if hasattr(os, "getuid") and protected.st_uid != os.getuid():
                raise OSError("cooldown key has the wrong owner")
        key_id = hashlib.sha256(_X_COOLDOWN_KEY_CONTEXT + raw).hexdigest()
        stored_key_id = S.get("x_cooldown_key_id")
        existing_scoped = getattr(S, "x_login_cooldowns", lambda: {})()
        if stored_key_id is None:
            if existing_scoped:
                raise OSError("cooldown key identity missing for scoped records")
            S.set("x_cooldown_key_id", key_id)
        elif (
            not isinstance(stored_key_id, str)
            or len(stored_key_id) != 64
            or not hmac.compare_digest(stored_key_id, key_id)
        ):
            raise OSError("cooldown key identity mismatch")
        _x_cooldown_hmac_key = raw
        return raw
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        _x_cooldown_key_error = True
        log.error("تعذّر تحميل مفتاح فصل عدادات X (%s)", type(exc).__name__)
        return None


def _x_cooldown_scope(username):
    canonical = _canonical_x_username(username)
    key = _load_x_cooldown_hmac_key()
    if canonical is None or key is None:
        return None
    return hmac.new(
        key, _X_COOLDOWN_KEY_CONTEXT + canonical.encode("ascii"), hashlib.sha256,
    ).hexdigest()


# ============ حالة محادثات الإعداد (بمهلة) ============
def _set_state(uid, data):
    # لا نمسح tombstone لمحاولة سرية ملغاة هنا: قد تكون كلمة المرور/الرمز القديم
    # ما زالت في الطريق. أول رسالة نصية لاحقة تُحذف fail-closed، ثم يستطيع
    # المستخدم إعادة إدخال قيمة الخطوة الجديدة بأمان.
    state[uid] = {**data, "ts": time.time()}


def _get_state(uid):
    st = state.get(uid)
    if not st:
        return None
    if time.time() - st.get("ts", 0) > STATE_TTL:
        _remember_x_secret_tombstone(uid, st)
        state.pop(uid, None)
        return None
    return st


def _clear_state(uid):
    state.pop(uid, None)


def _purge_states():
    cutoff = time.time() - STATE_TTL
    for uid in [u for u, st in state.items() if st.get("ts", 0) < cutoff]:
        _remember_x_secret_tombstone(uid, state.get(uid))
        state.pop(uid, None)


async def _delete_secret_message(event):
    """يحذف كلمة المرور/الرمز ويعيد ما إذا ضمن Telegram نجاح الحذف."""
    global _x_secret_delete_unconfirmed
    # The deletion is its own tracked task.  Telethon cancels event-handler
    # tasks while disconnecting; shielding the RPC lets the restart
    # coordinator wait for this child before replacing the process.
    async def delete_once():
        try:
            await event.delete()
            return True
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - caller decides whether to warn/abort
            return False

    delete_task = asyncio.create_task(delete_once())
    _x_secret_tasks.add(delete_task)
    try:
        try:
            deleted = await asyncio.wait_for(
                asyncio.shield(delete_task), timeout=X_SECRET_DELETE_TIMEOUT,
            )
            _x_secret_tasks.discard(delete_task)
            if not deleted:
                _x_secret_delete_unconfirmed = True
                log.warning("تعذّر حذف رسالة سرية من Telegram")
            return deleted
        except asyncio.TimeoutError:
            delete_task.cancel()
            await asyncio.gather(delete_task, return_exceptions=True)
            _x_secret_tasks.discard(delete_task)
            _x_secret_delete_unconfirmed = True
            log.warning("انتهت مهلة حذف رسالة سرية من Telegram")
            return False
    except asyncio.CancelledError:
        # Do not cancel the Telegram deletion.  It remains in
        # _x_secret_tasks and is drained by the restart/shutdown coordinator.
        raise


def _remember_x_secret_tombstone(uid, st):
    if not st or st.get("action") not in {
        "x_email", "x_pass_pending", "x_pass", "x_auth_code", "claim_code",
    }:
        return
    chat_id = st.get("x_chat_id", st.get("claim_chat_id"))
    if chat_id is None:
        return
    _x_secret_tombstones[(uid, chat_id)] = time.monotonic() + X_SECRET_TOMBSTONE_TTL


async def _delete_late_x_secret(event):
    """يحذف أول سر يصل بعد إلغاء محاولة X، مربوطاً بالمرسل والمحادثة."""
    now = time.monotonic()
    for key, expires in list(_x_secret_tombstones.items()):
        if expires <= now:
            _x_secret_tombstones.pop(key, None)
    key = (event.sender_id, event.chat_id)
    # لا نستثني النصوص التي تبدأ /: كلمة مرور X نفسها قد تبدأ بشرطة مائلة.
    # أول رسالة بعد الإلغاء/انتهاء المهلة تُعامل كسر محتمل fail-closed.
    if key not in _x_secret_tombstones:
        return False
    _x_secret_tombstones.pop(key, None)
    deleted = await _delete_secret_message(event)
    await event.respond(
        "🛑 أُهملت هذه الرسالة لأن عملية الإدخال السرية أُلغيت."
        + ("" if deleted else " احذف الرسالة الظاهرة يدوياً فوراً.")
    )
    return True


def _has_secret_tombstone(event):
    now = time.monotonic()
    for key, expires in list(_x_secret_tombstones.items()):
        if expires <= now:
            _x_secret_tombstones.pop(key, None)
    return (event.sender_id, event.chat_id) in _x_secret_tombstones


def _is_private_chat(event):
    """يتحقق من أن الحدث داخل محادثة المستخدم الخاصة مع البوت."""
    explicit = getattr(event, "is_private", None)
    if explicit is not None:
        return bool(explicit)
    return (
        getattr(event, "chat_id", None) is not None
        and event.chat_id == getattr(event, "sender_id", None)
    )


def _x_challenge_key(event):
    return event.sender_id, event.chat_id


def _x_private_context_matches(event, st=None):
    if not _is_private_chat(event):
        return False
    expected = (st or {}).get("x_chat_id")
    return expected is None or expected == event.chat_id


def _cancel_x_login(uid, *, cancel_task=True, remember_secret=True):
    """يلغي محاولة X لمستخدم وينظف كل future/state مرتبطة بها."""
    task = _x_login_tasks.get(uid)
    # أثناء حذف password الحالية لا توجد رسالة متأخرة ننتظرها؛ مسار الحذف نفسه
    # سيحذر عند الفشل. tombstone تخص السر الذي لم يصل بعد فقط.
    if remember_secret and uid not in _x_login_deleting:
        _remember_x_secret_tombstone(uid, state.get(uid))
    if task is not None:
        _x_login_cancelled.add(uid)
    for key, future in list(_x_challenges.items()):
        if key[0] != uid:
            continue
        _x_challenges.pop(key, None)
        if not future.done():
            future.cancel()
    _clear_state(uid)
    if (
        cancel_task
        and task is not None
        and uid not in _x_login_deleting
        and task is not asyncio.current_task()
        and not task.done()
    ):
        task.cancel()
    return task


async def _shutdown_x_logins(timeout=X_LOGIN_SHUTDOWN_TIMEOUT):
    """Cancel every interactive X login and wait for browser cleanup."""
    global _x_secret_delete_unconfirmed
    current_task = asyncio.current_task()
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        login_pending = set()
        for uid, task in list(_x_login_tasks.items()):
            _cancel_x_login(uid)
            if task is not None and task is not current_task and not task.done():
                login_pending.add(task)
        # Include completed orphan deletions too: their boolean result must be
        # inspected before restart, not discarded merely because the RPC ended.
        secret_pending = {
            task for task in _x_secret_tasks if task is not current_task
        }
        pending = login_pending | secret_pending
        if not pending and not any(
            task is not current_task and task is not None and not task.done()
            for task in _x_login_tasks.values()
        ) and not any(
            task is not current_task and not task.done()
            for task in _x_secret_tasks
        ):
            return not (
                _x_browser_cleanup_unconfirmed
                or _x_secret_delete_unconfirmed
            )
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            log.error("انتهت مهلة إغلاق متصفح تسجيل X؛ أُلغي restart الآمن")
            return False
        try:
            tasks = list(pending)
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=remaining,
            )
            result_by_task = dict(zip(tasks, results))
            if any(
                isinstance(result_by_task.get(task), XBrowserCleanupError)
                for task in login_pending
            ):
                return False
            secret_ok = True
            for task in secret_pending:
                result = result_by_task.get(task)
                _x_secret_tasks.discard(task)
                if result is not True:
                    secret_ok = False
            if not secret_ok:
                _x_secret_delete_unconfirmed = True
                log.error("تعذّر تأكيد حذف رسالة سرية؛ أُلغي restart الآمن")
                return False
        except asyncio.TimeoutError:
            log.error("انتهت مهلة إغلاق متصفح تسجيل X؛ أُلغي restart الآمن")
            return False


def _cancel_setup_for_navigation(uid):
    """يلغي خطوة إدخال قبل الانتقال إلى لوحة/معرّفات أخرى."""
    current = state.get(uid)
    if (current or {}).get("action", "").startswith("x_") or uid in _x_login_tasks:
        _cancel_x_login(uid)
    elif current:
        _remember_x_secret_tombstone(uid, current)
        _clear_state(uid)


def _navigation_context_matches(event, current=None):
    """يمنع زر/أمر في مجموعة من إلغاء سر يجري إدخاله في الخاص."""
    current = current or state.get(event.sender_id)
    action = (current or {}).get("action", "")
    if action.startswith("x_"):
        return _x_private_context_matches(event, current)
    if action == "claim_code":
        return (
            _is_private_chat(event)
            and current.get("claim_chat_id") == event.chat_id
        )
    if event.sender_id in _x_login_tasks:
        return _is_private_chat(event)
    return True


async def _reject_callback_during_x_setup(event):
    """يمنع أي Inline button آخر من استبدال إدخال X سري جارٍ."""
    uid = event.sender_id
    current = _get_state(uid)
    task = _x_login_tasks.get(uid)
    if not (
        (current or {}).get("action") in X_SETUP_ACTIONS
        or (task is not None and not task.done())
    ):
        return False
    if not _navigation_context_matches(event, current):
        text = "استخدم أزرار المحاولة في محادثة البوت الخاصة الأصلية."
    else:
        text = "ألغِ محاولة X أولاً بزر «🛑 إلغاء» الظاهر فيها."
    await event.answer(text, alert=True)
    return True


def _new_x_setup_id():
    """معرّف قصير يربط كل زر بمحاولة X التي أنشأته."""
    return secrets.token_hex(6)


def _x_setup_buttons(setup_id, *, allow_email_skip=False):
    rows = []
    if allow_email_skip:
        rows.append([Button.inline(
            "⏭️ تخطي البريد",
            f"xsetup:{setup_id}:skip_email".encode(),
        )])
    rows.append([Button.inline(
        "🛑 إلغاء",
        f"xsetup:{setup_id}:cancel".encode(),
    )])
    return rows


def _set_x_password_pending(uid, st, email):
    next_state = {
        "action": "x_pass_pending",
        "x_username": st.get("x_username"),
        "x_cooldown_scope": st.get("x_cooldown_scope"),
        "x_email": email,
        "x_chat_id": st.get("x_chat_id"),
        "x_setup_id": st.get("x_setup_id"),
    }
    _set_state(uid, next_state)
    return state[uid]


async def _prompt_x_password(event, pending):
    """يعرض طلب كلمة المرور ثم يفعّله فقط إن بقيت المحاولة نفسها حيّة."""
    uid = event.sender_id
    if state.get(uid) is not pending:
        return False
    if not S.is_admin(uid):
        _cancel_x_login(uid)
        return False
    await event.respond(
        "أرسل **كلمة مرور** حساب X:",
        buttons=_x_setup_buttons(pending["x_setup_id"]),
    )
    if state.get(uid) is not pending:
        return False
    if not S.is_admin(uid):
        _cancel_x_login(uid)
        return False
    _set_state(uid, {
        **{key: value for key, value in pending.items() if key != "ts"},
        "action": "x_pass",
    })
    return True


def _require_x_admin(uid):
    if not S.is_admin(uid):
        _cancel_x_login(uid)
        raise PermissionError("X login administrator permission was revoked")


async def _wait_for_x_challenge(event, kind, _prompt="", setup_id=None):
    """يربط تحدي X برسالة الأدمن التالية في المحادثة الخاصة نفسها."""
    if not _is_private_chat(event):
        raise RuntimeError("X login challenges require a private Telegram chat")
    uid = event.sender_id
    _require_x_admin(uid)
    key = _x_challenge_key(event)
    previous = _x_challenges.get(key)
    if previous is not None and not previous.done():
        raise RuntimeError("يوجد تحدي X آخر قيد الانتظار")

    future = asyncio.get_running_loop().create_future()
    _x_challenges[key] = future
    setup_id = setup_id or _new_x_setup_id()
    _set_state(uid, {
        "action": "x_auth_code",
        "x_challenge_kind": kind,
        "x_chat_id": event.chat_id,
        "x_setup_id": setup_id,
    })
    challenge_state = state[uid]
    if kind == "two_factor":
        text = (
            "🔐 افتح تطبيق Authenticator وأرسل **الرمز الحالي من 6 أرقام**.\n"
            "سأحذف الرسالة فوراً، والرمز لا يُحفظ. تنتهي المهلة بعد 3 دقائق."
        )
    elif kind == "alternate_identifier":
        text = (
            "👤 طلب X معلومة ثانية للتأكد من الحساب. أرسل **البريد أو رقم الهاتف "
            "أو اسم المستخدم** المرتبط بالحساب. هذا ليس رمز Authenticator.\n"
            "سأحذف الرسالة فوراً ولن أخزنها. تنتهي المهلة بعد 3 دقائق."
        )
    else:
        text = (
            "📩 أرسل **رمز التحقق المؤقت** الذي أرسله X إلى البريد أو الهاتف.\n"
            "لا ترسل كلمة المرور أو مفتاح Authenticator السري. سأحذف الرسالة "
            "فوراً ولن أخزنها. تنتهي المهلة بعد 3 دقائق."
        )
    try:
        await event.respond(
            text + "\nللإلغاء اضغط الزر الظاهر أدناه.",
            buttons=_x_setup_buttons(setup_id),
        )
        try:
            response = await asyncio.wait_for(future, timeout=X_CHALLENGE_TIMEOUT)
        except asyncio.TimeoutError:
            _remember_x_secret_tombstone(uid, state.get(uid))
            raise
        _require_x_admin(uid)
        return response
    finally:
        if _x_challenges.get(key) is future:
            _x_challenges.pop(key, None)
        current = state.get(uid)
        if current is challenge_state:
            task = _x_login_tasks.get(uid)
            if task is not None and not task.done() and uid not in _x_login_cancelled:
                _set_state(uid, {
                    "action": "x_login_running",
                    "x_chat_id": event.chat_id,
                    "x_setup_id": setup_id,
                })
            else:
                _clear_state(uid)


async def _submit_x_challenge_code(event, st, raw):
    """Submit a code; secret deletion itself is tracked independently."""
    return await _submit_x_challenge_code_impl(event, st, raw)


async def _submit_x_challenge_code_impl(event, st, raw):
    """يسلّم رمزاً مؤقتاً لمحاولة X دون تسجيله أو تخزينه."""
    uid = event.sender_id
    context_ok = _x_private_context_matches(event, st)
    key = _x_challenge_key(event)
    original_future = _x_challenges.get(key)
    deleted = await _delete_secret_message(event)
    if _restarting:
        if not deleted:
            await event.respond(
                "⚠️ لم أستخدم الرمز لأن البوت قيد التحديث، وتعذّر حذفه. احذف الرسالة يدوياً فوراً."
            )
        return
    # لا نسلّم رمزاً التُقط من محاولة قديمة إلى challenge أحدث في نفس chat.
    if state.get(uid) is not st or _x_challenges.get(key) is not original_future:
        if not deleted:
            await event.respond(
                "⚠️ لم أستخدم رمز المحاولة القديمة، لكن تعذّر حذفه من Telegram. "
                "احذف الرسالة الظاهرة يدوياً فوراً."
            )
        return
    if not context_ok:
        await event.respond(
            "🔒 لم أستخدم هذه الرسالة. أرسل الرمز في المحادثة الخاصة الأصلية مع البوت."
            + ("" if deleted else " احذف الرسالة الظاهرة يدوياً فوراً.")
        )
        return

    future = original_future
    if not deleted:
        if future is not None and not future.done():
            future.cancel()
        if _x_challenges.get(key) is future:
            _x_challenges.pop(key, None)
        if state.get(uid) is st:
            _clear_state(uid)
        await event.respond(
            "❌ لم أستطع حذف رسالة الرمز بأمان، فألغيت محاولة دخول X. "
            "احذف الرسالة يدوياً ثم ابدأ مجدداً."
        )
        return
    if future is None or future.done():
        if state.get(uid) is st:
            _clear_state(uid)
        await event.respond("⌛ انتهت محاولة تسجيل X. ابدأها من لوحة التحكم مجدداً.")
        return

    kind = st.get("x_challenge_kind")
    if kind == "two_factor":
        code = re.sub(r"[\s-]+", "", raw)
        valid = bool(re.fullmatch(r"\d{6}", code))
        hint = "رمز Authenticator يجب أن يكون 6 أرقام. افتح التطبيق وأرسل الرمز الحالي."
    else:
        code = raw.strip()
        # LoginAcid في X مدخل نصي عام وقد يطلب بريداً أو هاتفاً بتنسيق محلي؛
        # لا نفرض صيغة أضيق من X، ونمنع فقط الفراغ/محارف التحكم/الحجم المفرط.
        valid = bool(code) and len(code) <= 254 and not any(
            ord(char) < 32 or ord(char) == 127 for char in code
        )
        hint = "قيمة التحقق غير صالحة. أرسل الرمز أو البريد/الهاتف كما يطلبه X."
    if not valid:
        await event.respond(f"❌ {hint}")
        return

    future.set_result(code)
    await event.respond("⏳ جاري التحقق من الرمز…")


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
        # المسار الخارج عن مجلدنا غير مملوك لنا. قد يكون Telethon (أو بديل
        # اختباري/مخترق) أعاد مسار ملف موجود، لذلك لا نحذفه أبداً.
        return None
    return {"path": path, "type": kind}


async def _queue_for_review(text, media, origin="", on_persisted=None):
    """
    يضيف منشوراً للمخزن ويرسله للمراجعة. يرجع item_id أو None.

    ``on_persisted`` خطوة متزامنة اختيارية تُنفّذ بعد تثبيت pending وقبل أول
    إرسال Telegram. يستخدمها X لتثبيت المؤشر بلا نافذة crash تنشئ عنصراً ثانياً.
    """
    try:
        item_id = PENDING.add(text, media, origin)
    except OSError as e:
        log.error("فشل حفظ المنشور قبل المراجعة: %s", e)
        await _notify_owner(
            "⚠️ وصل منشور جديد لكن تعذّر حفظه على القرص؛ لم يُرسل للمراجعة.\n"
            f"{e}"
        )
        return None
    if on_persisted is not None:
        try:
            on_persisted()
        except Exception as e:  # noqa: BLE001
            # لا نحذف العنصر: أصبح outbox دائماً، والدورة التالية ستعثر عليه
            # عبر origin وتحاول تثبيت المؤشر مجدداً قبل أي إرسال.
            log.error("حُفظ المنشور لكن تعذّر تثبيت مؤشر مصدره: %s", e)
            await _notify_owner(
                "⚠️ حُفظ منشور جديد، لكن تعذّر تثبيت مؤشر مصدره على القرص؛ "
                "لم يُرسل للمراجعة وسأعيد المحاولة دون إنشاء نسخة أخرى.\n"
                f"{e}"
            )
            return None
    try:
        await _send_for_review(item_id)
        return item_id
    except Exception as e:  # noqa: BLE001
        log.error("فشل إرسال المنشور للمراجعة: %s", e)
        # العنصر صار outbox دائماً؛ لا نحذفه بسبب عطل Telegram مؤقت. سيعيد
        # startup إرساله عبر _replay_unreviewed ثم يثبت review في pending.json.
        await _notify_owner(
            "⚠️ وصل منشور جديد وحُفظ، لكن تعذّر عرضه للمراجعة الآن. "
            "سأعيد إرساله بعد إعادة التشغيل.\n"
            f"{e}"
        )
        return item_id


_X_ORIGIN_RE = re.compile(
    r"^https://x\.com/([A-Za-z0-9_]{1,15})/status/([0-9]+)$",
    re.ASCII,
)


def _checkpoint_x_origin(item):
    """يثبّت مؤشر X المضمّن في origin قبل كشف العنصر في Telegram."""
    match = _X_ORIGIN_RE.fullmatch(str(item.get("origin") or ""))
    if not match:
        return
    screen_name, last_id = match.groups()
    # set_x_last_id transactional وidempotent. إن نجح ثم مات التشغيل قبل
    # الإرسال، تكراره لا يضر؛ وإن فشل يجب أن يبقى review=None.
    S.set_x_last_id(screen_name, last_id)


async def _replay_unreviewed():
    """يعيد إرسال عناصر حُفظت ثم انقطع التشغيل قبل إنشاء رسالة المراجعة."""
    if not S.get("review_chat_id"):
        return 0
    replayed = 0
    uncertain = 0
    cleanup_failed = 0
    for item_id, item in list(PENDING.items.items()):
        publish_state = item.get("publish_state")
        if publish_state == "published":
            try:
                PENDING.remove(item_id)
            except OSError as e:
                cleanup_failed += 1
                log.error("تعذّر تنظيف المنشور المنشور %s: %s", item_id, e)
            continue
        if publish_state == "publishing":
            uncertain += 1
            log.error(
                "لن أعيد المنشور %s للمراجعة: حالة النشر %s قد تعني أنه وصل "
                "إلى Facebook قبل انقطاع التشغيل.",
                item_id, publish_state,
            )
            continue
        if item.get("review"):
            continue
        try:
            _checkpoint_x_origin(item)
        except Exception as e:  # noqa: BLE001
            log.error(
                "تعذّر تثبيت مؤشر X قبل استعادة المنشور %s؛ سيبقى في outbox: %s",
                item_id, e,
            )
            continue
        try:
            await _send_for_review(item_id)
        except Exception as e:  # noqa: BLE001
            # نبقي العنصر على القرص ليُعاد في التشغيل التالي؛ حذفه هنا يحوّل
            # عطل تلغرام المؤقت إلى فقد دائم للمحتوى.
            log.error("فشل استعادة المنشور المعلّق %s للمراجعة: %s", item_id, e)
            continue
        replayed += 1
    if replayed:
        log.info("أُعيد إرسال %d منشور محفوظ للمراجعة", replayed)
    if uncertain:
        await _notify_owner(
            f"⚠️ يوجد {uncertain} منشور بحالة نشر غير محسومة بعد إعادة التشغيل. "
            "حُظر نشره تلقائياً لمنع التكرار. اضغط زر النشر القديم، ثم اختر "
            "هل المنشور موجود على Facebook أم لا."
        )
    if cleanup_failed:
        await _notify_owner(
            f"⚠️ تعذّر تنظيف {cleanup_failed} منشور مؤكد النشر من القرص. "
            "لن يُعاد نشره، وسأحاول تنظيفه دورياً."
        )
    return replayed


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
def _command_keyboard(*, claim=False):
    """لوحة Reply Keyboard ثابتة تظهر تحت خانة الكتابة في المحادثة الخاصة."""
    primary = BTN_CLAIM if claim else BTN_PANEL
    options = {
        "resize": True,
        "persistent": True,
        "placeholder": "اختر أمراً",
    }
    return [
        [Button.text(primary, **options), Button.text(BTN_ID, **options)],
        [Button.text(BTN_CANCEL, **options)],
    ]


def _keyboard_versions():
    raw = S.get("reply_keyboard_versions") or {}
    return raw if isinstance(raw, dict) else {}


def _is_reserved_command(text):
    command = (text or "").split(maxsplit=1)[0].split("@", 1)[0].lower()
    return command in _RESERVED_COMMANDS


def _remember_keyboard_version(uid):
    versions = _keyboard_versions()
    key = str(uid)
    if versions.get(key) == REPLY_KEYBOARD_VERSION:
        return
    versions[key] = REPLY_KEYBOARD_VERSION
    S.set("reply_keyboard_versions", versions)


def _forget_keyboard_version(uid):
    versions = _keyboard_versions()
    if versions.pop(str(uid), None) is not None:
        S.set("reply_keyboard_versions", versions)


async def _send_command_keyboard(event, *, claim=False):
    if not _is_private_chat(event):
        return False
    await event.respond(
        "اختر من الأزرار أسفل خانة الكتابة:",
        buttons=_command_keyboard(claim=claim),
    )
    if not claim and S.is_admin(event.sender_id):
        try:
            _remember_keyboard_version(event.sender_id)
        except OSError as exc:
            log.warning("أُرسلت لوحة أوامر Telegram لكن تعذّر حفظ إصدارها: %s", exc)
    return True


async def _offer_command_keyboard(uid):
    """يرسل اللوحة مرة واحدة للأدمن بعد تشغيل البوت، بأفضل جهد."""
    if _keyboard_versions().get(str(uid)) == REPLY_KEYBOARD_VERSION:
        return False
    try:
        await _tg_call(
            bot.send_message,
            uid,
            "⌨️ أصبحت أوامر البوت أزراراً ثابتة أسفل خانة الكتابة:",
            buttons=_command_keyboard(),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("تعذّر إرسال لوحة أوامر Telegram للأدمن %s: %s", uid, exc)
        return False
    try:
        _remember_keyboard_version(uid)
    except OSError as exc:
        # الرسالة وصلت بالفعل؛ فشل الحفظ يعني فقط أنها قد تُعرض مجدداً بعد restart.
        log.warning("أُرسلت لوحة أوامر Telegram لكن تعذّر حفظ إصدارها: %s", exc)
    return True


async def _remove_command_keyboard(uid):
    """يخفي اللوحة عند سحب صلاحية الأدمن ويتيح إعادة إرسالها إن أُعيد لاحقاً."""
    try:
        await _tg_call(
            bot.send_message,
            uid,
            "🔒 سُحبت صلاحية إدارة البوت.",
            buttons=Button.clear(),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("تعذّر إخفاء لوحة أوامر Telegram للأدمن السابق %s: %s", uid, exc)
    try:
        _forget_keyboard_version(uid)
    except OSError as exc:
        log.warning("تعذّر نسيان إصدار لوحة الأدمن السابق %s: %s", uid, exc)


async def _offer_command_keyboards(uids):
    """مهمة خلفية؛ FloodWait هنا لا يؤخر استعادة outbox أو تشغيل المصادر."""
    if not uids:
        return
    await asyncio.gather(*(_offer_command_keyboard(uid) for uid in uids))


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


async def cmd_panel(event):
    uid = event.sender_id
    current = state.get(uid)
    if (current or uid in _x_login_tasks) and not _navigation_context_matches(
        event, current
    ):
        await event.respond("🔒 أكمل أو ألغِ العملية من محادثة البوت الخاصة الأصلية.")
        return
    _cancel_setup_for_navigation(uid)
    # أول شخص يطالب بالملكية عبر رمز يظهر في سجل الـ Raspberry
    if not S.get("owner_id"):
        if not _is_private_chat(event):
            await event.respond("🔒 افتح محادثة البوت الخاصة واضغط زر تفعيل الملكية.")
            return
        await event.respond(
            "👋 أهلاً! اضغط «🔑 تفعيل الملكية»، ثم أرسل الرمز الذي يظهر "
            "في سجل التشغيل على جهاز Raspberry.",
            buttons=_command_keyboard(claim=True),
        )
        return
    if not S.is_admin(uid):
        await event.respond(
            "هذا البوت خاص. لست ضمن الأدمنين.",
            buttons=Button.clear() if _is_private_chat(event) else None,
        )
        return
    await _send_command_keyboard(event)
    await _show_panel(event)


@bot.on(events.NewMessage(pattern=r"^/(?:panel|start)(?:@\w+)?$"))
async def on_panel_command(event):
    await cmd_panel(event)
    raise events.StopPropagation


async def _attempt_claim(event, code):
    """
    الملكية = كل الأسرار (توكن فيسبوك، كلمات مرور X). الرمز طويل وعشوائي
    وله سقف محاولات حتى لا يكون قابلاً للتخمين.
    """
    global _claim_code, _claim_attempts
    if S.get("owner_id") or not _claim_code:
        return False
    if _claim_attempts >= MAX_CLAIM_ATTEMPTS:
        await event.respond("🚫 تجاوزت عدد المحاولات. أعد تشغيل البوت لتوليد رمز جديد.")
        return False

    code = (code or "").strip()
    if code and secrets.compare_digest(code, _claim_code):
        S.set("owner_id", event.sender_id)
        S.add_admin(event.sender_id)
        _claim_code = None
        _clear_state(event.sender_id)
        await event.respond(
            "✅ أصبحت المالك والأدمن. الأوامر الآن أزرار أسفل خانة الكتابة.",
            buttons=_command_keyboard(),
        )
        try:
            _remember_keyboard_version(event.sender_id)
        except OSError as exc:
            log.warning("تعذّر حفظ إصدار لوحة الأوامر بعد تفعيل الملكية: %s", exc)
        await _show_panel(event)
        log.info("تم تعيين المالك: %s", event.sender_id)
        return True

    _claim_attempts += 1
    left = MAX_CLAIM_ATTEMPTS - _claim_attempts
    log.warning("محاولة /claim فاشلة من %s (المتبقي %d)", event.sender_id, left)
    if left <= 0:
        _claim_code = None
        await event.respond("🚫 تجاوزت عدد المحاولات. أعد تشغيل البوت لتوليد رمز جديد.")
    else:
        await event.respond(f"❌ الرمز غير صحيح. المحاولات المتبقية: {left}")
    return False


async def cmd_claim(event):
    """مسار توافق قديم؛ زر تفعيل الملكية هو المسار الظاهر للمستخدم."""
    raw = event.text or ""
    parts = raw.split(maxsplit=1)
    tail = parts[1] if len(parts) == 2 else ""
    code_parts = tail.split()
    code = code_parts[0] if len(code_parts) == 1 else ""
    if S.get("owner_id") or not _claim_code:
        return
    if not _is_private_chat(event):
        deleted = await _delete_secret_message(event) if tail else True
        await event.respond(
            "🔒 أُهمل رمز الملكية؛ أرسله في محادثة البوت الخاصة فقط."
            + ("" if deleted else " احذف الرسالة الظاهرة يدوياً فوراً.")
        )
        return
    if tail and not code:
        deleted = await _delete_secret_message(event)
        await event.respond(
            "❌ صيغة رمز الملكية غير صحيحة. اضغط «🔑 تفعيل الملكية» ثم أرسل "
            "الرمز وحده في الرسالة التالية."
            + ("" if deleted else " احذف الرسالة الظاهرة يدوياً فوراً.")
        )
        return
    if not code:
        await event.respond(
            "اضغط «🔑 تفعيل الملكية» ثم أرسل الرمز الظاهر في سجل Raspberry."
        )
        return

    # احجز المحاولة قبل حذف الرسالة؛ زر الإلغاء/اللوحة يستطيع إبطالها أثناء
    # Telegram RPC، ثم يفشل فحص الهوية أدناه ولا تُفعّل ملكية قديمة.
    _set_state(event.sender_id, {
        "action": "claim_code",
        "claim_chat_id": event.chat_id,
    })
    attempt = state[event.sender_id]
    deleted = await _delete_secret_message(event)
    if not deleted:
        if state.get(event.sender_id) is attempt:
            _clear_state(event.sender_id)
        await event.respond(
            "❌ لم أستطع حذف رسالة رمز الملكية بأمان. احذفها يدوياً "
            "واستخدم زر «🔑 تفعيل الملكية» ثم حاول مجدداً."
        )
        return
    if state.get(event.sender_id) is not attempt:
        return
    await _attempt_claim(event, code)


@bot.on(events.NewMessage(pattern=r"^/claim(?:@\w+)?(?:\s|$)"))
async def on_claim_command(event):
    await cmd_claim(event)
    raise events.StopPropagation


async def cmd_id(event):
    current = state.get(event.sender_id)
    if (
        current or event.sender_id in _x_login_tasks
    ) and not _navigation_context_matches(event, current):
        await event.respond("🔒 استخدم زر المعرّف في محادثة البوت الخاصة الأصلية.")
        return
    _cancel_setup_for_navigation(event.sender_id)
    buttons = None
    if _is_private_chat(event):
        if S.is_admin(event.sender_id):
            buttons = _command_keyboard()
        elif not S.get("owner_id"):
            buttons = _command_keyboard(claim=True)
        else:
            buttons = Button.clear()
    await event.respond(
        f"معرّف المحادثة: `{event.chat_id}`\nمعرّفك: `{event.sender_id}`",
        buttons=buttons,
    )


@bot.on(events.NewMessage(pattern=r"^/id(?:@\w+)?$"))
async def on_id_command(event):
    await cmd_id(event)
    raise events.StopPropagation


async def cmd_cancel(event):
    """يلغي محادثة الإعداد ومحاولة تسجيل X الخاصة بالمرسل فقط."""
    uid = event.sender_id
    # من سُحبت صلاحيته يظل مسموحاً له بتنظيف محاولته المعلّقة فقط.
    current = state.get(uid)
    if (
        not S.is_admin(uid)
        and uid not in _x_login_tasks
        and (current or {}).get("action") != "claim_code"
    ):
        return
    had_state = uid in state
    task = _x_login_tasks.get(uid)
    if (current or task is not None) and not _navigation_context_matches(
        event, current
    ):
        await event.respond("🔒 اضغط «🛑 إلغاء» في محادثة البوت الخاصة الأصلية.")
        return
    _cancel_x_login(uid)
    await event.respond(
        "🛑 أُلغيت محاولة تسجيل X." if task is not None
        else ("🛑 أُلغيت عملية الإعداد." if had_state else "لا توجد عملية معلّقة.")
    )


@bot.on(events.NewMessage(pattern=r"^/cancel(?:@\w+)?$"))
async def on_cancel_command(event):
    await cmd_cancel(event)
    raise events.StopPropagation


async def _open_reply_panel(event):
    """يفتح اللوحة من Reply Keyboard دون إعادة إرسال لوحة المفاتيح نفسها."""
    if not S.get("owner_id"):
        if not _is_private_chat(event):
            await event.respond("🔒 افتح محادثة البوت الخاصة لتفعيل الملكية.")
            return
        await event.respond(
            "اضغط «🔑 تفعيل الملكية» أولاً.",
            buttons=_command_keyboard(claim=True),
        )
        return
    if not S.is_admin(event.sender_id):
        await event.respond(
            "هذا البوت خاص. لست ضمن الأدمنين.",
            buttons=Button.clear() if _is_private_chat(event) else None,
        )
        return
    await _show_panel(event)


async def _start_claim_from_button(event):
    if S.get("owner_id"):
        await _open_reply_panel(event)
        return
    if not _is_private_chat(event):
        await event.respond("🔒 فعّل الملكية داخل محادثة البوت الخاصة فقط.")
        return
    if not _claim_code:
        await event.respond("⌛ لا يوجد رمز تفعيل صالح الآن. أعد تشغيل البوت لتوليد رمز جديد.")
        return
    _clear_state(event.sender_id)
    _set_state(event.sender_id, {
        "action": "claim_code",
        "claim_chat_id": event.chat_id,
    })
    await event.respond(
        "🔑 أرسل الآن رمز تفعيل الملكية الظاهر في سجل تشغيل Raspberry. "
        "سأحذف رسالة الرمز فوراً."
    )


async def _handle_reply_button(event, action, st=None, *, already_deleted=False):
    """يوجّه ضغطات Reply Keyboard قبل أن تصل إلى حقول كلمات المرور/الرموز."""
    uid = event.sender_id
    current = st or state.get(uid)
    sensitive = (current or {}).get("action") in {
        "x_email", "x_pass_pending", "x_pass",
        "x_login_running", "x_auth_code", "claim_code",
    }
    should_delete = sensitive or _has_secret_tombstone(event)

    # زر وصل من مجموعة لا يملك حق إلغاء محاولة سرية مربوطة بمحادثة البوت
    # الخاصة. نحذف نص الزر احتياطياً، لكن لا نغيّر state/task الأصلية.
    if (current or uid in _x_login_tasks) and not _navigation_context_matches(
        event, current
    ):
        deleted = True
        if should_delete and not already_deleted:
            deleted = await _delete_secret_message(event)
        await event.respond(
            "🔒 استخدم أزرار الأوامر في محادثة البوت الخاصة الأصلية."
            + ("" if deleted else " احذف الرسالة الظاهرة يدوياً فوراً.")
        )
        return

    # ثبّت الإلغاء قبل أول await؛ فلا يستطيع رمز/كلمة مرور متزامنة إكمال
    # الملكية أو تسجيل X بينما Telegram يتأخر في حذف رسالة الزر.
    if action == "cancel":
        await cmd_cancel(event)
    elif current or uid in _x_login_tasks:
        _cancel_setup_for_navigation(uid)

    if should_delete and not already_deleted:
        deleted = await _delete_secret_message(event)
        if not deleted:
            await event.respond(
                "⚠️ تعذّر حذف الرسالة من Telegram. احذفها يدوياً فوراً."
            )

    if action == "cancel":
        return

    if action == "panel":
        await _open_reply_panel(event)
    elif action == "id":
        await cmd_id(event)
    elif action == "claim":
        await _start_claim_from_button(event)


# ============ أزرار اللوحة ============
@bot.on(events.CallbackQuery(pattern=rb"^m:"))
async def on_menu(event):
    if not S.is_admin(event.sender_id):
        await event.answer("غير مصرّح لك.", alert=True)
        return
    if await _reject_callback_during_x_setup(event):
        return
    what = event.data.decode().split(":", 1)[1]

    if what == "login":
        if S.get("user_phone"):
            await event.respond(
                f"الحساب مسجّل حالياً: {S.get('user_phone')}\n"
                "لإعادة تسجيل الدخول أرسل الرقم مرة أخرى."
            )
            if await _reject_callback_during_x_setup(event):
                return
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
    if await _reject_callback_during_x_setup(event):
        return
    action = event.data.decode().split(":", 1)[1]
    if action == "add":
        if not await user.is_user_authorized():
            if await _reject_callback_during_x_setup(event):
                return
            await event.respond("سجّل دخول الحساب أولاً من 🔐.")
        else:
            if await _reject_callback_during_x_setup(event):
                return
            _set_state(event.sender_id, {"action": "add_source"})
            await event.respond("أرسل @يوزر_القناة أو رابطها أو معرّفها الرقمي:")
    elif action == "del":
        _set_state(event.sender_id, {"action": "del_source"})
        await event.respond("أرسل @يوزر_القناة أو معرّفها الرقمي لحذفها:")
    await event.answer()


# ============ حسابات دخول X (مجموعة مع تبديل) ============
def _x_cooldown_record():
    """يعيد أحدث مهلة؛ البديل الذاكري يُستخدم فقط عند تعذر الحفظ على القرص."""
    global _x_cooldown_runtime
    if _x_cooldown_memory is not None:
        record = dict(_x_cooldown_memory)
    else:
        record = S.x_login_cooldown()
    if not record:
        return None
    now = time.time()
    duration = record["duration_seconds"]
    # قد يبدأ Raspberry Pi بعد انقطاع الكهرباء بساعة نظام قديمة قبل NTP.
    # لا نرمي السجل (fail-open)، بل نعيد ساعة محدودة من لحظة الاكتشاف.
    runtime_is_current = (
        _x_cooldown_runtime is not None
        and _x_cooldown_runtime.get("generation") == record["generation"]
    )
    clock_behind = not runtime_is_current and record["until"] - now > duration + 5
    if clock_behind:
        # لا نعيد كتابة epoch المحفوظ بساعة boot القديمة؛ بعد NTP يبقى الأصل
        # مرجعاً صحيحاً. الحارس monotonic أدناه يفرض ساعة كاملة داخل العملية.
        log.warning("حُمي عداد X من انحراف ساعة النظام")
    if not runtime_is_current:
        wall_remaining = duration if clock_behind else max(
            0, min(duration, record["until"] - time.time())
        )
        _x_cooldown_runtime = {
            "generation": record["generation"],
            "deadline": time.monotonic() + wall_remaining,
        }
    return record


def _x_cooldown_remaining(record=None, now=None):
    record = record or _x_cooldown_record()
    if not record or record.get("notified"):
        return 0
    if (
        now is None
        and _x_cooldown_runtime is not None
        and _x_cooldown_runtime.get("generation") == record["generation"]
    ):
        return max(0, min(
            record["duration_seconds"],
            int(_x_cooldown_runtime["deadline"] - time.monotonic() + 0.999999),
        ))
    now = time.time() if now is None else now
    return max(0, min(
        record["duration_seconds"],
        int(record["until"] - now + 0.999999),
    ))


def _format_x_cooldown(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _x_cooldown_text(record=None, now=None):
    record = record or _x_cooldown_record()
    remaining = _x_cooldown_remaining(record, now=now)
    end_at = time.strftime("%H:%M", time.localtime(record["until"]))
    return (
        "⏳ أوقف X محاولات الدخول مؤقتاً. أُغلق المتصفح ولم تُحفظ جلسة.\n\n"
        f"⏱ الوقت الاحترازي المتبقي: `{_format_x_cooldown(remaining)}`\n"
        f"🕒 ينتهي تقريباً عند الساعة `{end_at}` حسب وقت Raspberry Pi.\n"
        "هذه مدة احترازية يضعها البوت (ساعة من لحظة الحظر) لأن X لم يحدد "
        "مدة دقيقة. يُحدّث العداد كل دقيقة؛ انتظر حتى ينتهي وسأرسل لك زر "
        "إعادة المحاولة تلقائياً."
    )


def _x_cooldown_ready_buttons():
    return [[Button.inline("🔄 إعادة محاولة X", b"xlog:add")]]


def _get_x_cooldown_lock():
    """Lock خاص بحلقة التشغيل؛ الاختبارات قد تنشئ أكثر من event loop."""
    global _x_cooldown_lock, _x_cooldown_lock_loop
    loop = asyncio.get_running_loop()
    if _x_cooldown_lock is None or _x_cooldown_lock_loop is not loop:
        _x_cooldown_lock = asyncio.Lock()
        _x_cooldown_lock_loop = loop
    return _x_cooldown_lock


def _x_cooldown_task_done(task):
    global _x_cooldown_task
    if _x_cooldown_task is task:
        _x_cooldown_task = None
    if task.cancelled():
        return
    try:
        task.result()
    except Exception as exc:  # noqa: BLE001
        log.warning("توقفت مهمة عداد X (%s)", type(exc).__name__)


def _ensure_x_cooldown_scheduler(record=None):
    """ينشئ مهمة واحدة فقط للجيل الحالي؛ الجيل الجديد يلغي القديمة."""
    global _x_cooldown_task
    record = record or _x_cooldown_record()
    if not record or record.get("notified"):
        return None
    current = _x_cooldown_task
    generation = record["generation"]
    if (
        current is not None
        and not current.done()
        and getattr(current, "_x_cooldown_generation", None) == generation
    ):
        return current
    if current is not None and not current.done():
        current.cancel()
    task = asyncio.create_task(_run_x_cooldown(record["generation"]))
    task._x_cooldown_generation = generation
    task.add_done_callback(_x_cooldown_task_done)
    _x_cooldown_task = task
    return task


async def _edit_x_cooldown_message(record, text, buttons=None):
    if not record.get("message_id"):
        return False
    try:
        await _tg_call(
            bot.edit_message,
            record["chat_id"],
            record["message_id"],
            text,
            buttons=buttons,
        )
        return True
    except MessageNotModifiedError:
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("تعذّر تحديث رسالة عداد X (%s)", type(exc).__name__)
        return False


async def _create_x_cooldown_message(generation):
    """ينشئ رسالة عداد مفقودة بعد restart/preseed، مرة واحدة وبـCAS."""
    global _x_cooldown_memory
    async with _get_x_cooldown_lock():
        record = _x_cooldown_record()
        if (
            not record
            or record["generation"] != generation
            or record.get("notified")
            or record.get("message_id")
            or _x_cooldown_remaining(record) <= 0
        ):
            return True
        if not S.is_admin(record["chat_id"]):
            # سحب صلاحية مستلم الرسالة لا يعني أن حظر X انتهى. نبقي المؤقت
            # نافذاً ونحاول لاحقاً؛ وسمه notified هنا كان يفتح الدخول مبكراً.
            return False
        try:
            message = await _tg_call(
                bot.send_message, record["chat_id"], _x_cooldown_text(record),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("تعذّر إنشاء رسالة عداد X (%s)", type(exc).__name__)
            return False
        message_id = getattr(message, "id", None)
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            return False
        current = _x_cooldown_record()
        if not current or current["generation"] != generation:
            return True
        try:
            return S.set_x_login_cooldown_message(generation, message_id)
        except OSError as exc:
            current = dict(current)
            current["message_id"] = message_id
            _x_cooldown_memory = current
            log.warning("تعذّر حفظ رسالة عداد X المستأنفة (%s)", type(exc).__name__)
            return True


def _mark_x_cooldown_notified(generation):
    """CAS على القرص، مع حارس ذاكرة إذا فشلت الكتابة بعد نجاح Telegram."""
    if _x_cooldown_memory is not None and (
        _x_cooldown_memory.get("generation") == generation
    ):
        _x_cooldown_memory["notified"] = True
        _x_cooldown_notified_memory.add(generation)
        return True
    try:
        return S.mark_x_login_cooldown_notified(generation)
    except OSError as exc:
        _x_cooldown_notified_memory.add(generation)
        log.error("تعذّر تثبيت انتهاء عداد X (%s)", type(exc).__name__)
        return False


async def _notify_x_cooldown_ready(generation):
    """إشعار at-least-once مع CAS؛ لا يغيّر مؤقّت قديم سجلاً أحدث."""
    if generation in _x_cooldown_notifying:
        return False
    _x_cooldown_notifying.add(generation)
    try:
        async with _get_x_cooldown_lock():
            record = _x_cooldown_record()
            if (
                not record
                or record["generation"] != generation
                or record.get("notified")
                or generation in _x_cooldown_notified_memory
                or _x_cooldown_remaining(record) > 0
            ):
                return True
            # chat_id هو محادثة البوت الخاصة التي بدأت منها العملية، ويساوي uid.
            # إذا سُحبت صلاحية الأدمن أثناء الساعة فلا نرسل له زر اعتماد جديداً.
            if not S.is_admin(record["chat_id"]):
                _mark_x_cooldown_notified(generation)
                return True
            ready_text = (
                "✅ انتهى عداد الانتظار الاحترازي لمحاولة دخول X.\n"
                "يمكنك الآن الضغط على الزر للبدء من جديد. لن يعيد البوت إرسال "
                "كلمة المرور تلقائياً."
            )
            await _edit_x_cooldown_message(
                record, ready_text, buttons=_x_cooldown_ready_buttons(),
            )
            # activation تستخدم القفل نفسه؛ لا يمكن أن تستبدل الجيل بين هذا
            # الفحص وsend_message، وبذلك لا يصل زر جاهزية قديم بعد حظر أحدث.
            current = _x_cooldown_record()
            if not current or current["generation"] != generation:
                return True
            try:
                await _tg_call(
                    bot.send_message,
                    record["chat_id"],
                    ready_text,
                    buttons=_x_cooldown_ready_buttons(),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "تعذّر إرسال إشعار انتهاء عداد X (%s)", type(exc).__name__,
                )
                return False

            # نثبت الإرسال بعد نجاح Telegram. إذا تعذر القرص نمنع التكرار في
            # هذه العملية، ويظل retry بعد restart ممكناً (at-least-once).
            _mark_x_cooldown_notified(generation)
            return True
    finally:
        _x_cooldown_notifying.discard(generation)


async def _consume_expired_x_cooldown(record):
    """ضغط المستخدم زر المحاولة بنفسه؛ أوقف إشعار الجاهزية القديم بلا تكرار."""
    if not record or record.get("notified"):
        return
    async with _get_x_cooldown_lock():
        current = _x_cooldown_record()
        if (
            current
            and current["generation"] == record["generation"]
            and _x_cooldown_remaining(current) <= 0
        ):
            _mark_x_cooldown_notified(current["generation"])


async def _run_x_cooldown(generation):
    while True:
        record = _x_cooldown_record()
        if (
            not record
            or record["generation"] != generation
            or record.get("notified")
            or generation in _x_cooldown_notified_memory
        ):
            return
        remaining = _x_cooldown_remaining(record)
        if remaining <= 0:
            if await _notify_x_cooldown_ready(generation):
                return
            await asyncio.sleep(X_COOLDOWN_NOTIFY_RETRY_SECONDS)
            continue
        if not record.get("message_id"):
            if await _create_x_cooldown_message(generation):
                continue
            await asyncio.sleep(X_COOLDOWN_NOTIFY_RETRY_SECONDS)
            continue
        await _edit_x_cooldown_message(record, _x_cooldown_text(record))
        await asyncio.sleep(min(X_COOLDOWN_UPDATE_SECONDS, remaining))


async def _activate_x_login_cooldown(event):
    # نفس القفل يحمي إرسال إشعار جيل منتهٍ واستبداله بجيل جديد.
    async with _get_x_cooldown_lock():
        return await _activate_x_login_cooldown_locked(event)


async def _activate_x_login_cooldown_locked(event):
    """يبدأ ساعة احترازية عالمية، ثم يعرض عداداً لا يحتفظ بأي اعتماد."""
    global _x_cooldown_memory, _x_cooldown_runtime
    generation = secrets.token_hex(16)
    record = {
        "generation": generation,
        "started_at": time.time(),
        "duration_seconds": X_LOGIN_COOLDOWN_SECONDS,
        "chat_id": int(event.chat_id),
        "message_id": None,
        "notified": False,
    }
    record["until"] = record["started_at"] + record["duration_seconds"]
    # يثبت مدة الساعة بالزمن الرتيب قبل أي await؛ قفزة NTP أثناء Telegram RPC
    # لا تستطيع إنهاء cooldown الجديد فوراً.
    _x_cooldown_runtime = {
        "generation": generation,
        "deadline": time.monotonic() + record["duration_seconds"],
    }
    try:
        record = S.start_x_login_cooldown(
            record["until"], record["chat_id"], generation=generation,
            duration_seconds=record["duration_seconds"],
        )
        _x_cooldown_memory = None
    except OSError as exc:
        # لا نفتح باب المحاولات إذا كان القرص عاطلاً؛ يبقى المنع والعداد حيّين
        # في العملية الحالية، مع تسجيل واضح أن الدوام عبر restart تعذّر.
        _x_cooldown_memory = dict(record)
        log.error("تعذّر حفظ عداد X على القرص (%s)", type(exc).__name__)
    try:
        message = await event.respond(_x_cooldown_text(record))
    except Exception as exc:  # noqa: BLE001
        log.warning("تعذّر إرسال رسالة بدء عداد X (%s)", type(exc).__name__)
        _ensure_x_cooldown_scheduler(record)
        return record
    message_id = getattr(message, "id", None)
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        _ensure_x_cooldown_scheduler(record)
        return record
    if _x_cooldown_memory is not None and (
        _x_cooldown_memory.get("generation") == generation
    ):
        _x_cooldown_memory["message_id"] = message_id
    else:
        try:
            S.set_x_login_cooldown_message(generation, message_id)
        except OSError as exc:
            record = dict(record)
            record["message_id"] = message_id
            _x_cooldown_memory = record
            log.warning("تعذّر حفظ معرّف رسالة عداد X (%s)", type(exc).__name__)
    _ensure_x_cooldown_scheduler(record)
    return record


async def _shutdown_x_cooldown(timeout=X_COOLDOWN_SHUTDOWN_TIMEOUT):
    global _x_cooldown_task
    task = _x_cooldown_task
    _x_cooldown_task = None
    if task is None or task.done():
        return
    task.cancel()
    _done, pending = await asyncio.wait({task}, timeout=timeout)
    if pending:
        # لا ننتظر بلا حد: المهمة لا تحمل credentials، والمهلة نفسها durable
        # وستُستأنف في العملية الجديدة.
        log.warning("لم تتوقف مهمة عداد X ضمن مهلة الإغلاق؛ سأكمل restart")


# ============ عدادات دخول X المنفصلة لكل حساب ============
def _x_account_cooldown_record(scope):
    if not scope:
        return None
    record = _x_account_cooldown_memory.get(scope)
    if record is None:
        record = S.x_login_cooldown_for(scope)
    if not record:
        return None
    record = dict(record)
    runtime = _x_account_cooldown_runtime.get(scope)
    duration = record["duration_seconds"]
    runtime_current = runtime and runtime.get("generation") == record["generation"]
    clock_behind = (
        not runtime_current and record["until"] - time.time() > duration + 5
    )
    if not runtime_current:
        wall_remaining = duration if clock_behind else max(
            0, min(duration, record["until"] - time.time())
        )
        _x_account_cooldown_runtime[scope] = {
            "generation": record["generation"],
            "deadline": time.monotonic() + wall_remaining,
        }
    return record


def _x_account_cooldown_remaining(scope, record=None, now=None):
    record = record or _x_account_cooldown_record(scope)
    if not record or record.get("notified"):
        return 0
    runtime = _x_account_cooldown_runtime.get(scope)
    if (
        now is None
        and runtime
        and runtime.get("generation") == record["generation"]
    ):
        return max(0, min(
            record["duration_seconds"],
            int(runtime["deadline"] - time.monotonic() + 0.999999),
        ))
    now = time.time() if now is None else now
    return max(0, min(
        record["duration_seconds"], int(record["until"] - now + 0.999999),
    ))


def _x_account_cooldown_text(scope, record=None):
    record = record or _x_account_cooldown_record(scope)
    remaining = _x_account_cooldown_remaining(scope, record)
    end_at = time.strftime("%H:%M", time.localtime(record["until"]))
    return (
        "⏳ أوقف X الدخول إلى هذا الحساب مؤقتاً. هذا العداد خاص بهذا الحساب فقط؛ "
        "يمكنك إضافة حساب X آخر أثناء الانتظار.\n\n"
        f"⏱ الوقت الاحترازي المتبقي: `{_format_x_cooldown(remaining)}`\n"
        f"🕒 ينتهي تقريباً عند الساعة `{end_at}` حسب وقت Raspberry Pi.\n"
        "انتظر انتهاء العداد لهذا الحساب. لن يحاول البوت الدخول تلقائياً، "
        "وسيحدّث هذه الرسالة كل دقيقة."
    )


def _x_account_cooldown_task_done(scope, task):
    if _x_account_cooldown_tasks.get(scope) is task:
        _x_account_cooldown_tasks.pop(scope, None)
    if task.cancelled():
        return
    try:
        task.result()
    except Exception as exc:  # noqa: BLE001
        log.warning("توقفت مهمة عداد حساب X (%s)", type(exc).__name__)


def _ensure_x_account_cooldown_scheduler(scope, record=None):
    record = record or _x_account_cooldown_record(scope)
    if not record or record.get("notified"):
        return None
    generation = record["generation"]
    current = _x_account_cooldown_tasks.get(scope)
    if (
        current is not None
        and not current.done()
        and getattr(current, "_x_cooldown_generation", None) == generation
    ):
        return current
    if current is not None and not current.done():
        current.cancel()
    task = asyncio.create_task(_run_x_account_cooldown(scope, generation))
    task._x_cooldown_generation = generation
    task.add_done_callback(lambda done, key=scope: _x_account_cooldown_task_done(key, done))
    _x_account_cooldown_tasks[scope] = task
    return task


async def _create_x_account_cooldown_message(scope, generation):
    async with _get_x_cooldown_lock():
        record = _x_account_cooldown_record(scope)
        if (
            not record or record["generation"] != generation
            or record.get("notified") or record.get("message_id")
            or _x_account_cooldown_remaining(scope, record) <= 0
        ):
            return True
        if not S.is_admin(record["chat_id"]):
            # فصل صلاحية Telegram عن صلاحية مؤقت X: لا نرسل للأدمن المسحوب،
            # لكن لا نحوّل المؤقت النشط إلى منتهٍ قبل موعده.
            return False
        try:
            message = await _tg_call(
                bot.send_message, record["chat_id"],
                _x_account_cooldown_text(scope, record),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("تعذّر إنشاء رسالة عداد حساب X (%s)", type(exc).__name__)
            return False
        message_id = getattr(message, "id", None)
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            return False
        current = _x_account_cooldown_record(scope)
        if not current or current["generation"] != generation:
            return True
        try:
            return S.set_x_login_cooldown_message_for(scope, generation, message_id)
        except OSError as exc:
            current["message_id"] = message_id
            _x_account_cooldown_memory[scope] = current
            log.warning("تعذّر حفظ رسالة عداد حساب X (%s)", type(exc).__name__)
            return True


def _mark_x_account_cooldown_notified(scope, generation):
    key = (scope, generation)
    memory = _x_account_cooldown_memory.get(scope)
    if memory and memory.get("generation") == generation:
        memory["notified"] = True
        _x_account_cooldown_notified_memory.add(key)
        return True
    try:
        return S.mark_x_login_cooldown_notified_for(scope, generation)
    except OSError as exc:
        _x_account_cooldown_notified_memory.add(key)
        log.error("تعذّر تثبيت انتهاء عداد حساب X (%s)", type(exc).__name__)
        return False


async def _notify_x_account_cooldown_ready(scope, generation):
    key = (scope, generation)
    if key in _x_account_cooldown_notifying:
        return False
    _x_account_cooldown_notifying.add(key)
    try:
        async with _get_x_cooldown_lock():
            record = _x_account_cooldown_record(scope)
            if (
                not record or record["generation"] != generation
                or record.get("notified") or key in _x_account_cooldown_notified_memory
                or _x_account_cooldown_remaining(scope, record) > 0
            ):
                return True
            if not S.is_admin(record["chat_id"]):
                _mark_x_account_cooldown_notified(scope, generation)
                return True
            text = (
                "✅ انتهى انتظار البوت لهذا الحساب. لا يضمن ذلك أن X رفع الحظر، "
                "لكن يمكنك بدء محاولة جديدة. لن تُعاد أي كلمة مرور تلقائياً."
            )
            await _edit_x_cooldown_message(
                record, text, buttons=_x_cooldown_ready_buttons(),
            )
            current = _x_account_cooldown_record(scope)
            if not current or current["generation"] != generation:
                return True
            try:
                await _tg_call(
                    bot.send_message, record["chat_id"], text,
                    buttons=_x_cooldown_ready_buttons(),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("تعذّر إرسال انتهاء عداد حساب X (%s)", type(exc).__name__)
                return False
            _mark_x_account_cooldown_notified(scope, generation)
            return True
    finally:
        _x_account_cooldown_notifying.discard(key)


async def _run_x_account_cooldown(scope, generation):
    while True:
        record = _x_account_cooldown_record(scope)
        key = (scope, generation)
        if (
            not record or record["generation"] != generation
            or record.get("notified") or key in _x_account_cooldown_notified_memory
        ):
            return
        remaining = _x_account_cooldown_remaining(scope, record)
        if remaining <= 0:
            if await _notify_x_account_cooldown_ready(scope, generation):
                return
            await asyncio.sleep(X_COOLDOWN_NOTIFY_RETRY_SECONDS)
            continue
        if not record.get("message_id"):
            if await _create_x_account_cooldown_message(scope, generation):
                continue
            await asyncio.sleep(X_COOLDOWN_NOTIFY_RETRY_SECONDS)
            continue
        await _edit_x_cooldown_message(
            record, _x_account_cooldown_text(scope, record),
        )
        await asyncio.sleep(min(X_COOLDOWN_UPDATE_SECONDS, remaining))


async def _activate_x_account_cooldown(event, scope):
    async with _get_x_cooldown_lock():
        generation = secrets.token_hex(16)
        record = {
            "generation": generation,
            "started_at": time.time(),
            "duration_seconds": X_LOGIN_COOLDOWN_SECONDS,
            "chat_id": int(event.chat_id),
            "message_id": None,
            "notified": False,
        }
        record["until"] = record["started_at"] + record["duration_seconds"]
        _x_account_cooldown_runtime[scope] = {
            "generation": generation,
            "deadline": time.monotonic() + record["duration_seconds"],
        }
        try:
            record = S.start_x_login_cooldown_for(
                scope, record["until"], record["chat_id"],
                generation=generation, duration_seconds=record["duration_seconds"],
            )
            _x_account_cooldown_memory.pop(scope, None)
        except (OSError, ValueError) as exc:
            _x_account_cooldown_memory[scope] = dict(record)
            log.error("تعذّر حفظ عداد حساب X (%s)", type(exc).__name__)
            if isinstance(exc, ValueError):
                # امتلاء خريطة العدادات مع 32 حساباً نشطاً حالة شاذة. نحفظ
                # حارساً عالمياً صريحاً كي لا يضيع المنع بعد restart؛ وهو مميز
                # عن legacy القديم الذي لا يجوز أن يمنع الحساب الثاني.
                try:
                    S.set_many({
                        "x_login_cooldown": dict(record),
                        "x_login_cooldown_emergency": True,
                    })
                except OSError as save_exc:
                    log.error(
                        "تعذّر حفظ حارس امتلاء عدادات X (%s)",
                        type(save_exc).__name__,
                    )
        try:
            message = await event.respond(_x_account_cooldown_text(scope, record))
        except Exception as exc:  # noqa: BLE001
            log.warning("تعذّر إرسال بدء عداد حساب X (%s)", type(exc).__name__)
            _ensure_x_account_cooldown_scheduler(scope, record)
            return record
        message_id = getattr(message, "id", None)
        if isinstance(message_id, int) and not isinstance(message_id, bool):
            memory = _x_account_cooldown_memory.get(scope)
            if memory and memory.get("generation") == generation:
                memory["message_id"] = message_id
            else:
                try:
                    S.set_x_login_cooldown_message_for(scope, generation, message_id)
                except OSError as exc:
                    record = dict(record)
                    record["message_id"] = message_id
                    _x_account_cooldown_memory[scope] = record
                    log.warning("تعذّر حفظ معرّف عداد حساب X (%s)", type(exc).__name__)
        _ensure_x_account_cooldown_scheduler(scope, record)
        return record


async def _consume_expired_x_account_cooldown(scope, record):
    if not record or record.get("notified"):
        return
    async with _get_x_cooldown_lock():
        current = _x_account_cooldown_record(scope)
        if (
            current and current["generation"] == record["generation"]
            and _x_account_cooldown_remaining(scope, current) <= 0
        ):
            _mark_x_account_cooldown_notified(scope, current["generation"])


async def _shutdown_x_account_cooldowns(timeout=X_COOLDOWN_SHUTDOWN_TIMEOUT):
    tasks = [task for task in _x_account_cooldown_tasks.values() if not task.done()]
    _x_account_cooldown_tasks.clear()
    for task in tasks:
        task.cancel()
    if not tasks:
        return
    _done, pending = await asyncio.wait(set(tasks), timeout=timeout)
    if pending:
        log.warning("لم تتوقف كل مهام عدادات حسابات X ضمن مهلة الإغلاق")


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
    if _restarting:
        await event.answer("البوت قيد التحديث؛ حاول إضافة حساب X بعد عودته.", alert=True)
        return
    if await _reject_callback_during_x_setup(event):
        return
    action = event.data.decode().split(":", 1)[1]
    if action == "add":
        if not _is_private_chat(event):
            await event.answer(
                "افتح محادثة البوت الخاصة واضغط «⚙️ لوحة التحكم» لإضافة حساب X بأمان.",
                alert=True,
            )
            return
        existing = _x_login_tasks.get(event.sender_id)
        if existing is not None and not existing.done():
            await event.answer(
                "لديك محاولة دخول X جارية. ألغها أولاً بالزر الظاهر في المحادثة.",
                alert=True,
            )
            return
        setup_id = _new_x_setup_id()
        _set_state(event.sender_id, {
            "action": "x_user",
            "x_chat_id": event.chat_id,
            "x_setup_id": setup_id,
        })
        await event.respond(
            "🐦 استخدم حساب X ثانوياً.\nأرسل **اسم المستخدم** (بدون @):",
            buttons=_x_setup_buttons(setup_id),
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
            "إذا انتهت جلسة أحدها فأعد إضافته؛ سيطلب البوت رمز Authenticator "
            "داخل Telegram عند الحاجة."
        )
    await event.answer()


@bot.on(events.CallbackQuery(pattern=rb"^xsetup:"))
async def on_xsetup(event):
    """أزرار خطوات إعداد X الظاهرة مباشرة تحت رسائل الإدخال."""
    uid = event.sender_id
    st = _get_state(uid)
    if not S.is_admin(uid):
        if st and st.get("action") in X_SETUP_ACTIONS:
            _cancel_x_login(uid)
        await event.answer("غير مصرّح لك.", alert=True)
        return
    if not st or st.get("action") not in X_SETUP_ACTIONS:
        await event.answer("انتهت هذه الخطوة.", alert=True)
        return
    if not _x_private_context_matches(event, st):
        await event.answer("استخدم الزر في المحادثة الخاصة الأصلية.", alert=True)
        return

    try:
        prefix, setup_id, action = event.data.decode().split(":", 2)
    except (AttributeError, UnicodeDecodeError, ValueError):
        await event.answer("زر غير صالح.", alert=True)
        return
    if _restarting and action != "cancel":
        await event.answer("البوت قيد التحديث؛ استخدم إلغاء أو حاول بعد عودته.", alert=True)
        return
    current_id = st.get("x_setup_id")
    if (
        prefix != "xsetup"
        or not current_id
        or not secrets.compare_digest(str(current_id), setup_id)
    ):
        await event.answer("هذا الزر من محاولة قديمة.", alert=True)
        return
    if action == "skip_email":
        if st.get("action") != "x_email":
            await event.answer("زر التخطي لم يعد صالحاً لهذه الخطوة.", alert=True)
            return
        # حالة انتقالية قبل أول await: أي رسالة بريد متزامنة تُرفض ولا يمكن أن
        # تُفسّر ككلمة مرور. كما تبقى هوية المحاولة قابلة للفحص بعد Telegram RPC.
        pending = _set_x_password_pending(uid, st, None)
        await event.answer("تم تخطي البريد")
        if state.get(uid) is not pending:
            return
        if not S.is_admin(uid):
            _cancel_x_login(uid)
            return
        await _prompt_x_password(event, pending)
        return
    if action == "cancel":
        task = _cancel_x_login(uid)
        await event.answer("أُلغيت العملية")
        await event.respond(
            "🛑 أُلغيت محاولة تسجيل X." if task is not None
            else "🛑 أُلغيت عملية إضافة حساب X."
        )
        return
    await event.answer("زر غير معروف.", alert=True)


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
    if await _reject_callback_during_x_setup(event):
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
    if await _reject_callback_during_x_setup(event):
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
    if await _reject_callback_during_x_setup(event):
        return
    action = event.data.decode().split(":", 1)[1]
    if action == "add":
        _set_state(event.sender_id, {"action": "add_admin"})
        await event.respond("أرسل المعرّف الرقمي للأدمن الجديد (يجده من زر «🆔 معرّفي»):")
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
    global _restarting, _restart_task
    if _restarting:
        await event.respond("⏳ يوجد تحديث أو إيقاف آمن جارٍ بالفعل.")
        return
    _restarting = True
    async def coordinator():
        global _restarting, _restart_task
        try:
            if not await _shutdown_x_logins():
                await event.respond(
                    "❌ لم أبدأ التحديث لأن متصفح X لم يُغلق بأمان. ألغِ المحاولة ثم أعد التحديث."
                )
                return
            return await _self_update_impl(event)
        finally:
            _restarting = False
            if _restart_task is asyncio.current_task():
                _restart_task = None

    # This task is deliberately separate from Telethon's handler task.
    # bot.disconnect() cancels handlers, but must not cancel the coordinator
    # before it has drained secret deletion/browser cleanup and exec'd.
    _restart_task = asyncio.create_task(coordinator())
    try:
        return await asyncio.shield(_restart_task)
    except asyncio.CancelledError:
        # The coordinator remains alive and owns the restart lifecycle.
        raise


async def _self_update_impl(event):
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
    # Stop dispatching new bot handlers before the final drain.  Existing
    # handlers can finish deleting any secret message while Telegram remains
    # connected.  The coordinator itself is not one of these handlers.
    handlers = list(bot.list_event_handlers())
    for callback, builder in handlers:
        bot.remove_event_handler(callback, builder)
    if not await _shutdown_x_logins():
        for callback, builder in handlers:
            bot.add_event_handler(callback, builder)
        await event.respond(
            "❌ لم أعد التشغيل لأن متصفح X أو حذف رسالة سرية لم يكتمل بأمان. حاول الإلغاء ثم أعد التحديث."
        )
        return
    # The safety gate already passed.  Attempt both disconnects independently;
    # one client failure must not strand a live process with all handlers
    # removed and prevent the other client from closing.
    for client in (bot, user):
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            log.exception("تعذّر فصل عميل Telegram أثناء restart")
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
def _publish_resolution_buttons(item_id):
    """أزرار حسم نتيجة POST غير المعروفة؛ callback_data يجب ألا يتجاوز 64 بايت."""
    return [
        [Button.inline(
            "✅ موجود على Facebook — إغلاق",
            f"pubfix:close:{item_id}".encode(),
        )],
        [Button.inline(
            "↩️ غير موجود — السماح بالمحاولة",
            f"pubfix:retry:{item_id}".encode(),
        )],
    ]


async def _show_publish_resolution(event, item_id, detail=""):
    text = (
        "⚠️ نتيجة النشر غير محسومة. قد يكون Facebook استلم المنشور رغم تعذّر "
        "استلام الرد. افحص الصفحة يدوياً ثم اختر:"
    )
    if detail:
        text += f"\n\n{detail}"
    await event.respond(text, buttons=_publish_resolution_buttons(item_id))


async def _cleanup_confirmed_publish(event, item_id):
    """يحاول حذف سجل مؤكد النشر فقط؛ لا يتصل بـFacebook إطلاقاً."""
    try:
        removed = PENDING.remove(item_id)
    except OSError as e:
        log.error("تعذّر تنظيف المنشور المؤكد %s: %s", item_id, e)
        await event.respond(
            f"⚠️ المنشور مؤكد النشر ولن يُنشر مجدداً، لكن تعذّر تنظيف سجله:\n{e}"
        )
        return False
    _published.discard(item_id)
    if not removed:
        await event.answer("✅ المنشور منشور ومنظّف مسبقاً.", alert=True)
        return True
    try:
        await event.edit("✅ تم تأكيد وجود المنشور على Facebook وإغلاق الطلب.")
    except Exception as e:  # noqa: BLE001
        log.warning("نُظّف المنشور %s لكن تعذّر تحديث رسالة المراجعة: %s", item_id, e)
    return True


def _publish_block_message(item_id, item=None):
    """سبب منع إجراء قد يعيد نشر عنصر وصل/قد يكون وصل إلى Facebook."""
    if item_id in _published:
        return "✅ نُشر هذا المنشور بالفعل؛ لن أعيد نشره."
    if item_id in _publishing:
        return "⏳ هذا المنشور قيد النشر بالفعل."
    publish_state = (item or {}).get("publish_state")
    if publish_state == "published":
        return "✅ نُشر هذا المنشور بالفعل؛ لن أعيد نشره."
    if publish_state == "publishing":
        return (
            "⚠️ حالة النشر غير محسومة بعد انقطاع سابق. "
            "راجع صفحة Facebook يدوياً لمنع منشور مكرر."
        )
    return None


@bot.on(events.CallbackQuery(pattern=rb"^(pub|pubtext|edit|skip):"))
async def on_post_action(event):
    if not S.is_admin(event.sender_id):
        await event.answer("غير مصرّح لك.", alert=True)
        return
    if await _reject_callback_during_x_setup(event):
        return
    action, _, item_id = event.data.decode().partition(":")
    item = PENDING.get(item_id)
    if item_id in _published or (item or {}).get("publish_state") == "published":
        await _cleanup_confirmed_publish(event, item_id)
        return
    if item_id not in _publishing and (item or {}).get("publish_state") == "publishing":
        await _show_publish_resolution(event, item_id)
        await event.answer()
        return
    blocked = _publish_block_message(item_id, item)
    if blocked:
        await event.answer(blocked, alert=True)
        return
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


@bot.on(events.CallbackQuery(pattern=rb"^pubfix:"))
async def on_publish_resolution(event):
    """حسم يدوي لحالة publishing؛ كلا الخيارين يغيّر التخزين فقط بلا POST."""
    if not S.is_admin(event.sender_id):
        await event.answer("غير مصرّح لك.", alert=True)
        return
    try:
        _prefix, action, item_id = event.data.decode().split(":", 2)
    except ValueError:
        await event.answer("طلب غير صالح.", alert=True)
        return

    item = PENDING.get(item_id)
    if not item:
        await event.answer("المنشور لم يعد متاحاً.", alert=True)
        return
    if item_id in _publishing:
        await event.answer("⏳ طلب النشر ما زال جارياً؛ انتظر نتيجته.", alert=True)
        return

    if action == "close":
        if item.get("publish_state") not in ("publishing", "published"):
            await event.answer("هذا المنشور ليس بحالة غير محسومة.", alert=True)
            return
        await _cleanup_confirmed_publish(event, item_id)
        return

    if action == "retry":
        if item.get("publish_state") != "publishing":
            await event.answer("هذا المنشور ليس بحالة غير محسومة.", alert=True)
            return
        try:
            updated = PENDING.update(
                item_id,
                publish_state=None,
                publishing_at=None,
                published_at=None,
            )
        except OSError as e:
            await event.respond(f"❌ تعذّر حفظ قرار السماح بالمحاولة:\n{e}")
            return
        if not updated:
            await event.answer("المنشور لم يعد متاحاً.", alert=True)
            return
        _published.discard(item_id)
        try:
            await event.edit(
                "↩️ تم تأكيد أن المنشور غير موجود على Facebook. "
                "يمكنك الآن الضغط على زر النشر الأصلي للمحاولة مجدداً."
            )
        except Exception as e:  # noqa: BLE001
            log.warning("فُتح المنشور %s للمحاولة لكن تعذّر تعديل الرسالة: %s", item_id, e)
        return

    await event.answer("طلب غير صالح.", alert=True)


async def _publish(event, item_id, include_media):
    if not S.facebook_ready():
        await event.answer("أعدّ فيسبوك أولاً من زر «⚙️ لوحة التحكم».", alert=True)
        return
    item = PENDING.get(item_id)
    blocked = _publish_block_message(item_id, item)
    if blocked:
        await event.answer(blocked, alert=True)
        return
    if not item:
        await event.answer("المنشور لم يعد متاحاً.", alert=True)
        return

    text = item["text"]
    photos = PENDING.media_paths(item_id, ("photo",))
    videos = PENDING.media_paths(item_id, ("video",))
    if not text.strip() and not (include_media and (photos or videos)):
        await event.answer("لا يوجد نص ولا وسائط للنشر.", alert=True)
        return

    _publishing.add(item_id)
    try:
        # يجب أن يسبق الحجز الدائم أول await: وإلا تستطيع ضغطة ثانية الدخول بين
        # الحجز في الذاكرة وحفظه، أو يموت التشغيل بلا أثر دائم للحالة.
        try:
            updated = PENDING.update(
                item_id, publish_state="publishing", publishing_at=time.time()
            )
        except OSError as e:
            log.error("تعذّر تثبيت حجز النشر %s: %s", item_id, e)
            await event.respond(
                f"❌ تعذّر حفظ حالة النشر على القرص؛ لم أتصل بفيسبوك.\n{e}"
            )
            return
        if not updated:
            await event.answer("المنشور لم يعد متاحاً.", alert=True)
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
        except FacebookUncertainError as e:
            log.error("نتيجة نشر فيسبوك غير محسومة %s: %s", item_id, e)
            # لا نمسح publish_state: الحجز الدائم يمنع أي retry إلى أن يحسم
            # الأدمن وجود المنشور عبر زري القرار، وكلاهما بلا اتصال بـFacebook.
            await _show_publish_resolution(event, item_id, str(e))
            await _notify_owner(
                "⚠️ نتيجة نشر على Facebook غير محسومة. افحص الصفحة ثم استخدم "
                "زري الحسم في رسالة المراجعة؛ لن أعيد POST تلقائياً."
            )
            return
        except FacebookAuthError as e:
            log.error("مشكلة مصادقة فيسبوك %s: %s", item_id, e)
            try:
                reopened = PENDING.update(
                    item_id, publish_state=None, publishing_at=None
                )
            except OSError as save_error:
                log.error("تعذّر إعادة فتح المنشور %s: %s", item_id, save_error)
                reopened = None
            if not reopened:
                await _show_publish_resolution(
                    event, item_id,
                    "فشل الطلب قطعاً، لكن تعذّر حفظ إعادة فتحه على القرص.",
                )
            await event.respond(
                f"🔑 مشكلة في توكن فيسبوك:\n{e}\n\n"
                "المنشور محفوظ — أعِد ضبط 📘 فيسبوك من «⚙️ لوحة التحكم» ثم اضغط نشر مجدداً."
            )
            await _notify_owner("🔑 توكن فيسبوك لم يعد صالحاً — النشر متوقف حتى تحديثه.")
            return
        except (FacebookError, OSError) as e:
            log.error("فشل النشر %s: %s", item_id, e)
            try:
                reopened = PENDING.update(
                    item_id, publish_state=None, publishing_at=None
                )
            except OSError as save_error:
                log.error("تعذّر إعادة فتح المنشور %s: %s", item_id, save_error)
                reopened = None
            if not reopened:
                await _show_publish_resolution(
                    event, item_id,
                    "فشل الطلب قطعاً، لكن تعذّر حفظ إعادة فتحه على القرص.",
                )
            await event.respond(
                f"❌ فشل النشر على فيسبوك:\n{e}\nالمنشور محفوظ، جرّب مجدداً."
            )
            return
        except Exception as e:  # noqa: BLE001
            # استثناء غير مصنّف قد يقع بعد إرسال الطلب؛ السياسة المحافظة تمنع
            # إعادة POST حتى يحسم الأدمن النتيجة يدوياً.
            log.exception("استثناء غير محسوم أثناء نشر %s", item_id)
            await _show_publish_resolution(event, item_id, str(e))
            return

        # Facebook نجح: الحماية داخل العملية تسبق أي I/O محلي. ثم نثبت published
        # على القرص قبل محاولة الحذف؛ حتى لو فشل الحذف يبقى الزر محظوراً بعد restart.
        _published.add(item_id)
        try:
            PENDING.update(
                item_id, publish_state="published", published_at=time.time()
            )
        except OSError as e:
            # publish_state="publishing" ثُبّت قبل الشبكة، ولذلك يبقى guard دائم
            # ومحافظ حتى إن فشل تحديثه إلى published.
            log.error("نُشر %s لكن تعذّر تثبيت حالة النجاح: %s", item_id, e)
        try:
            PENDING.remove(item_id)
        except OSError as e:
            log.error("نُشر %s لكن تعذّر حذف pending: %s", item_id, e)
        else:
            if PENDING.get(item_id) is None:
                _published.discard(item_id)
        try:
            await event.edit(f"✅ تم النشر على فيسبوك.{note}\n\n{preview(text, 800)}")
        except Exception as e:  # noqa: BLE001
            log.warning("نُشر %s لكن تعذّر تحديث رسالة المراجعة: %s", item_id, e)
    finally:
        _publishing.discard(item_id)


# ============ موجّه الإدخالات النصية (محادثات الإعداد) ============
@bot.on(events.NewMessage)
async def on_text(event):
    uid = event.sender_id
    if not event.text:
        return
    reply_action = _REPLY_ACTIONS.get(event.text)
    if reply_action:
        await _handle_reply_button(event, reply_action, _get_state(uid))
        return

    # معالجات الأوامر الخاصة ترفع StopPropagation. هذا الحاجز الإضافي يمنع
    # رسالة الأمر نفسها من استهلاك tombstone لو استُدعي on_text يدوياً/بترتيب مختلف.
    if _is_reserved_command(event.text):
        return
    if await _delete_late_x_secret(event):
        return
    st = _get_state(uid)
    # _get_state قد يحوّل state منتهية الآن إلى tombstone؛ احذف نفس الرسالة
    # المتأخرة بدلاً من انتظار رسالة ثانية.
    if not st:
        await _delete_late_x_secret(event)
        return
    # كلمة مرور X قد تبدأ بشرطة مائلة، لكن أسماء أوامر البوت تبقى محجوزة كيلا
    # يعمل handler الأمر ثم تُرسل الرسالة نفسها إلى X ككلمة مرور أيضاً.
    if event.text.startswith("/"):
        if st.get("action") == "x_pass" and not _is_reserved_command(event.text):
            pass
        else:
            if _is_reserved_command(event.text) and not event.text.lower().startswith(
                "/cancel"
            ):
                _cancel_setup_for_navigation(uid)
            return
    action = st["action"]
    text = event.text.strip()

    # تفعيل المالك يسبق فحص الأدمن لأن البوت لا يملك أي أدمن بعد. نربطه بنفس
    # المحادثة الخاصة ونحذف الرمز من Telegram قبل فحصه.
    if action == "claim_code":
        if (
            not _is_private_chat(event)
            or st.get("claim_chat_id") != event.chat_id
        ):
            deleted = await _delete_secret_message(event)
            await event.respond(
                "🔒 أُهمل رمز التفعيل. أرسله في محادثة البوت الخاصة الأصلية."
                + ("" if deleted else " احذف الرسالة الظاهرة يدوياً فوراً.")
            )
            return
        deleted = await _delete_secret_message(event)
        if not deleted:
            _clear_state(uid)
            await event.respond(
                "❌ لم أستطع حذف رسالة الرمز بأمان. احذفها يدوياً، ثم اضغط "
                "«🔑 تفعيل الملكية» وحاول مجدداً."
            )
            return
        # قد يضغط المستخدم إلغاء/لوحة التحكم بينما Telegram يحذف رسالة الرمز.
        # لا نستخدم snapshot قديمة بعد await؛ يجب أن تبقى نفس محاولة claim حية.
        if state.get(uid) is not st:
            return
        claimed = await _attempt_claim(event, text)
        if (
            state.get(uid) is st
            and not claimed
            and _claim_code
            and not S.get("owner_id")
        ):
            _set_state(uid, {
                "action": "claim_code",
                "claim_chat_id": event.chat_id,
            })
        elif state.get(uid) is st:
            _clear_state(uid)
        return

    # ⚠️ أمان: الصلاحية تُفحص عند فتح المحادثة فقط، فمن أُزيل من الأدمنين بعدها
    # كان بإمكانه إكمال خطوة معلّقة (تغيير توكن فيسبوك مثلاً). نعيد الفحص هنا.
    if not S.is_admin(uid):
        if st.get("action") in {
            "x_email", "x_pass_pending", "x_pass",
            "x_login_running", "x_auth_code",
        }:
            deleted = await _delete_secret_message(event)
            if not deleted:
                await event.respond("⚠️ احذف رسالة اعتماد X الظاهرة يدوياً فوراً.")
        _cancel_x_login(uid, remember_secret=False)
        log.warning("أُهملت محادثة إعداد لمستخدم لم يعد أدمن: %s", uid)
        return
    # كل خطوات اعتماد X تبدأ وتكتمل في المحادثة الخاصة نفسها. لو أرسل الأدمن
    # كلمة المرور/الرمز في مجموعة بالخطأ نحاول حذفها ولا نسلّمها لمحاولة الدخول.
    if action in X_SETUP_ACTIONS and not (
        _x_private_context_matches(event, st)
    ):
        deleted = True
        if action in {
            "x_email", "x_pass_pending", "x_pass",
            "x_login_running", "x_auth_code",
        }:
            deleted = await _delete_secret_message(event)
        await event.respond(
            "🔒 أكمل إعداد X في المحادثة الخاصة التي بدأت منها العملية."
            + ("" if deleted else " احذف الرسالة الظاهرة يدوياً فوراً.")
        )
        return

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
        username = _canonical_x_username(text)
        if username is None:
            await event.respond(
                "❌ اسم مستخدم X غير صالح. أرسل 1–15 حرفاً إنجليزياً أو رقماً "
                "أو شرطة سفلية، بدون رابط."
            )
            return
        scope = _x_cooldown_scope(username)
        if scope is None:
            _clear_state(uid)
            await event.respond(
                "❌ تعذّر فتح مخزن عدادات حسابات X بأمان. لم تبدأ محاولة دخول؛ "
                "افحص ملف حالة البوت ثم أعد المحاولة."
            )
            return
        cooldown = _x_account_cooldown_record(scope)
        remaining = _x_account_cooldown_remaining(scope, cooldown)
        if remaining > 0:
            _clear_state(uid)
            _ensure_x_account_cooldown_scheduler(scope, cooldown)
            await event.respond(
                "⏳ هذا الحساب وحده ما زال ضمن وقت الانتظار. المتبقي: "
                f"`{_format_x_cooldown(remaining)}`. يمكنك إضافة حساب X آخر الآن."
            )
            return
        if cooldown and not cooldown.get("notified"):
            await _consume_expired_x_account_cooldown(scope, cooldown)
        _set_state(uid, {
            "action": "x_email",
            "x_username": username,
            "x_cooldown_scope": scope,
            "x_chat_id": st.get("x_chat_id", event.chat_id),
            "x_setup_id": st.get("x_setup_id") or _new_x_setup_id(),
        })
        email_state = state[uid]
        await event.respond(
            "أرسل بريد الحساب الإلكتروني، أو اضغط زر التخطي:",
            buttons=_x_setup_buttons(
                email_state["x_setup_id"], allow_email_skip=True,
            ),
        )
    elif action == "x_email":
        pending = _set_x_password_pending(uid, st, None if text == "-" else text)
        await _prompt_x_password(event, pending)
    elif action in {"x_pass_pending", "x_login_running"}:
        # قد تصل رسالة البريد بالتزامن مع زر التخطي، أو يرسل المستخدم سراً قبل
        # اكتمال طلبه. نتعامل معها كسِر محتمل ولا نمررها مطلقاً إلى X.
        deleted = await _delete_secret_message(event)
        await event.respond(
            "⏳ انتظر ظهور خطوة الإدخال التالية، ثم أرسل القيمة المطلوبة مجدداً."
            + ("" if deleted else " احذف الرسالة الظاهرة يدوياً فوراً.")
        )
    elif action == "x_pass":
        # كلمات المرور حساسة لحروف المسافة؛ لا نمرر النسخة المقتطعة أعلاه.
        await _save_x_login(event, st, event.text)
    elif action == "x_auth_code":
        await _submit_x_challenge_code(event, st, text)
    elif action == "x_switch":
        await _switch_x_login(event, text)
    elif action == "x_login_del":
        _clear_state(uid)
        username = text.lstrip("@")
        stored_username = next(
            (
                login["username"]
                for login in S.x_logins()
                if login["username"].lower() == username.lower()
            ),
            username,
        )
        removed = S.remove_x_login(username)
        session_removed = True
        if removed:
            session_removed = xreader.discard_session(stored_username)
        await event.respond(
            (
                f"🗑️ حُذف {removed} حساب دخول."
                + (
                    ""
                    if session_removed
                    else "\n⚠️ تعذّر حذف ملف الجلسة من القرص؛ افحص صلاحيات مجلد الحالة."
                )
            )
            if removed else "لم أجد الحساب."
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
            added_uid = int(text)
            S.add_admin(added_uid)
            await event.respond(f"✅ أضيف الأدمن `{text}`")
            await _offer_command_keyboard(added_uid)
        else:
            await event.respond("أرسل معرّفاً رقمياً صحيحاً.")
        _clear_state(uid)
    elif action == "del_admin":
        if text.isdigit():
            removed_uid = int(text)
            S.remove_admin(removed_uid)
            if not S.is_admin(removed_uid):
                _cancel_x_login(removed_uid)
                await _remove_command_keyboard(removed_uid)
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
    global _x_browser_cleanup_unconfirmed
    username = st.get("x_username")
    email = st.get("x_email")
    setup_id = st.get("x_setup_id") or _new_x_setup_id()
    uid = event.sender_id
    scope = _x_cooldown_scope(username)
    if scope is None or (
        st.get("x_cooldown_scope") is not None
        and st.get("x_cooldown_scope") != scope
    ):
        deleted = await _delete_secret_message(event)
        _cancel_x_login(uid, cancel_task=False, remember_secret=False)
        await event.respond(
            "❌ لم أستخدم كلمة المرور لأن هوية محاولة X غير صالحة. ابدأ الإضافة من جديد."
            + ("" if deleted else " احذف رسالة كلمة المرور الظاهرة يدوياً فوراً.")
        )
        return
    cooldown = _x_account_cooldown_record(scope)
    remaining = _x_account_cooldown_remaining(scope, cooldown)
    emergency = _x_cooldown_record() if S.get("x_login_cooldown_emergency") else None
    emergency_remaining = _x_cooldown_remaining(emergency)
    if emergency_remaining > 0:
        deleted = await _delete_secret_message(event)
        _cancel_x_login(uid, cancel_task=False, remember_secret=False)
        _ensure_x_cooldown_scheduler(emergency)
        await event.respond(
            "⏳ لم أستخدم كلمة المرور لأن مخزن عدادات X وصل حد الأمان. "
            f"المتبقي: `{_format_x_cooldown(emergency_remaining)}`."
            + ("" if deleted else " احذف رسالة كلمة المرور الظاهرة يدوياً فوراً.")
        )
        return
    if remaining > 0:
        deleted = await _delete_secret_message(event)
        _cancel_x_login(uid, cancel_task=False, remember_secret=False)
        _ensure_x_account_cooldown_scheduler(scope, cooldown)
        await event.respond(
            "⏳ لم أستخدم كلمة المرور لأن هذا الحساب وحده ما زال ضمن الانتظار. "
            f"المتبقي: `{_format_x_cooldown(remaining)}`."
            " يمكنك إضافة حساب X آخر الآن."
            + ("" if deleted else " احذف رسالة كلمة المرور الظاهرة يدوياً فوراً.")
        )
        return
    if _restarting:
        deleted = await _delete_secret_message(event)
        _cancel_x_login(uid)
        await event.respond(
            "⏳ لم أستخدم كلمة المرور لأن البوت قيد التحديث. حاول بعد عودته."
            + ("" if deleted else " احذف رسالة كلمة المرور الظاهرة يدوياً فوراً.")
        )
        return
    if not _x_private_context_matches(event, st):
        deleted = await _delete_secret_message(event)
        await event.respond(
            "🔒 أُهملت كلمة المرور؛ تسجيل X مسموح فقط في المحادثة الخاصة الأصلية."
            + ("" if deleted else " احذف الرسالة الظاهرة يدوياً فوراً.")
        )
        return

    if not username or not password:
        deleted = await _delete_secret_message(event)
        _clear_state(uid)
        if not deleted:
            await event.respond("⚠️ احذف رسالة كلمة المرور الظاهرة يدوياً فوراً.")
        await event.respond("❌ اسم المستخدم وكلمة المرور مطلوبان.")
        return

    existing = _x_login_tasks.get(uid)
    if existing is not None and not existing.done():
        deleted = await _delete_secret_message(event)
        if not deleted:
            await event.respond("⚠️ احذف رسالة كلمة المرور الظاهرة يدوياً فوراً.")
        await event.respond("⏳ لديك محاولة تسجيل X جارية بالفعل. اضغط «🛑 إلغاء» لإلغائها.")
        return
    if any(
        owner != uid and pending is not None and not pending.done()
        for owner, pending in _x_login_tasks.items()
    ):
        # XReader عميل مشترك؛ محاولتان متزامنتان قد تخلطان cookies والحساب النشط.
        deleted = await _delete_secret_message(event)
        _clear_state(uid)
        if not deleted:
            await event.respond("⚠️ احذف رسالة كلمة المرور الظاهرة يدوياً فوراً.")
        await event.respond("⏳ توجد محاولة تسجيل X أخرى جارية. حاول بعد انتهائها.")
        return

    # No await is allowed between this final maintenance check and reservation.
    if _restarting:
        _cancel_x_login(uid)
        return
    # الحجز يحدث قبل أول await: يستطيع /cancel رؤية المحاولة حتى أثناء حذف السر.
    task = asyncio.current_task()
    _x_login_tasks[uid] = task
    _x_login_cancelled.discard(uid)
    _x_login_deleting.add(uid)
    _set_state(uid, {
        "action": "x_login_running",
        "x_chat_id": st.get("x_chat_id", event.chat_id),
        "x_setup_id": setup_id,
    })
    credentials = {"username": username, "email": email, "password": password}
    password = None

    async def challenge_handler(kind, prompt=""):
        _require_x_admin(uid)
        safe_kind = {
            "alternate_identifier": "alternate_identifier",
            "two_factor": "two_factor",
            "verification": "verification_code",
        }.get(kind)
        if safe_kind:
            _log_xlogin_stage(f"challenge_requested_{safe_kind}")
        response = await _wait_for_x_challenge(
            event, kind, prompt, setup_id=setup_id,
        )
        if safe_kind:
            _log_xlogin_stage(f"challenge_received_{safe_kind}")
        _require_x_admin(uid)
        return response

    try:
        try:
            deleted = await _delete_secret_message(event)
        finally:
            _x_login_deleting.discard(uid)
        # /cancel أثناء await الحذف لا يقطع عملية الحذف نفسها؛ يمنع الاتصال بـX
        # فور اكتمالها، ثم ينظف finally القفل ونسخة كلمة المرور.
        if uid in _x_login_cancelled:
            if deleted:
                return
        if not deleted:
            await event.respond(
                "❌ لم أستطع حذف رسالة كلمة المرور بأمان، لذلك لم أستخدمها. "
                "احذفها يدوياً ثم ابدأ إضافة الحساب مجدداً."
            )
            return
        # قد يبدأ عداد لهذا الحساب أثناء انتظار حذف رسالة السر. نفحصه مرة ثانية
        # قبل أي اتصال بالمتصفح، ولا نحتفظ بالاعتماد في مهمة انتظار طويلة.
        cooldown = _x_account_cooldown_record(scope)
        remaining = _x_account_cooldown_remaining(scope, cooldown)
        emergency = _x_cooldown_record() if S.get("x_login_cooldown_emergency") else None
        emergency_remaining = _x_cooldown_remaining(emergency)
        if emergency_remaining > 0:
            _ensure_x_cooldown_scheduler(emergency)
            await event.respond(
                "⏳ أُوقفت المحاولة قبل الاتصال بـ X لأن مخزن العدادات وصل حد الأمان. "
                f"المتبقي: `{_format_x_cooldown(emergency_remaining)}`."
            )
            return
        if remaining > 0:
            _ensure_x_account_cooldown_scheduler(scope, cooldown)
            await event.respond(
                "⏳ أُوقفت المحاولة قبل الاتصال بـ X لأن عداد هذا الحساب بدأ. "
                f"المتبقي: `{_format_x_cooldown(remaining)}`."
            )
            return
        _require_x_admin(uid)
        login_generation = xreader.invalidate()
        _log_xlogin_stage("login_started")
        await event.respond(
            "⏳ أحاول تسجيل الدخول إلى X عبر متصفح معزول على Raspberry Pi… "
            "إذا طلب X تحققاً إضافياً سأطلبه هنا."
        )
        _require_x_admin(uid)
        if not xreader.is_generation_current(login_generation):
            await event.respond("🛑 أُلغيت محاولة X لأن جلسة أحدث فُعّلت.")
            return
        ok = await xreader.login_interactive(credentials, challenge_handler)
        if not ok:
            await event.respond("❌ تعذّر تسجيل الدخول إلى X. تحقق من البيانات.")
            return
        if not S.is_admin(uid):
            discarded = xreader.discard_session(username)
            _cancel_x_login(uid, cancel_task=False)
            if not discarded:
                await _notify_owner(
                    "⚠️ سُحبت صلاحية أدمن أثناء دخول X، لكن تعذّر حذف ملف "
                    "الجلسة الجديدة. افحص مجلد الحالة واحذف جلسة الحساب يدوياً."
                )
            return
        # لا نحفظ كلمة المرور إلا بعد اكتمال كلمة المرور + التحديات + فحص الجلسة.
        try:
            # الجلسة المحققة تكفي للقراءة؛ لا نحتفظ بكلمة مرور X بعد الدخول.
            S.add_x_login(username, email, None)
        except OSError:
            # لا نترك cookie لا يمكن الوصول إليها بعد restart إذا فشل settings.
            discarded = xreader.discard_session(username)
            await event.respond(
                "❌ نجح تحقق X لكن تعذّر حفظ الإعدادات على القرص؛ "
                + (
                    "حُذفت الجلسة غير المثبتة."
                    if discarded
                    else "تعذّر أيضاً حذف ملف الجلسة، فافحص صلاحيات مجلد الحالة."
                )
            )
            return
        _log_xlogin_stage("session_verified")
        old_cooldown = _x_account_cooldown_record(scope)
        cooldown_task = _x_account_cooldown_tasks.pop(scope, None)
        if cooldown_task is not None and not cooldown_task.done():
            cooldown_task.cancel()
        _x_account_cooldown_memory.pop(scope, None)
        _x_account_cooldown_runtime.pop(scope, None)
        if old_cooldown:
            try:
                S.remove_x_login_cooldown_for(scope, old_cooldown["generation"])
            except OSError as exc:
                log.warning("تعذّر تنظيف عداد حساب X الناجح (%s)", type(exc).__name__)
        await event.respond(f"✅ تم الدخول. الحساب النشط: @{xreader.active}")
    except asyncio.TimeoutError:
        await event.respond("⌛ انتهت مهلة رمز X. ابدأ إضافة الحساب مجدداً.")
    except asyncio.CancelledError:
        return
    except XBrowserCredentialsRejected:
        await event.respond(
            "❌ رفض X اسم المستخدم أو كلمة المرور. لم تُحفظ جلسة جديدة."
        )
    except XBrowserChallengeRejected:
        await event.respond(
            "❌ رفض X معلومات التحقق عدة مرات. أُغلقت جلسة المتصفح ولم يُحفظ شيء."
        )
    except XBrowserUnsupportedChallenge:
        await event.respond(
            "⚠️ طلب X CAPTCHA أو مفتاح أمان/تأكيداً غير مدعوم. لن يحاول البوت "
            "تجاوزه؛ أكمله من موقع X الرسمي ثم أعد المحاولة."
        )
    except XBrowserRateLimited:
        # لا نُبقي كلمة المرور في ذاكرة مهمة العداد أو أثناء RPC Telegram.
        credentials.clear()
        _log_xlogin_stage("rate_limited")
        await _activate_x_account_cooldown(event, scope)
    except XBrowserUnavailable:
        await event.respond(
            "❌ متصفح تسجيل X غير متوفر أو تعذّر الوصول إلى صفحة X الآن. "
            "أُغلقت المحاولة ولم تُحفظ جلسة جديدة."
        )
    except XBrowserCleanupError:
        _x_browser_cleanup_unconfirmed = True
        await event.respond(
            "❌ تعذّر تأكيد إغلاق متصفح X بأمان، لذلك رُفضت الجلسة ولم تُحفظ. "
            "أعد تشغيل الخدمة قبل محاولة جديدة."
        )
    except (XBrowserPageChanged, XBrowserSessionError):
        await event.respond(
            "⚠️ تغيّرت صفحة دخول X أو لم تُنشئ جلسة كاملة. لم تُرفض بياناتك "
            "ولم تُحفظ جلسة جديدة."
        )
    except XSessionAccountMismatch:
        await event.respond(
            "❌ فتح X جلسة لحساب مختلف عن اسم المستخدم الذي أدخلته. أُغلقت الجلسة ولم يُحفظ شيء؛ "
            "تحقق من اسم المستخدم ثم أعد المحاولة."
        )
    except XSessionVerificationError:
        await event.respond(
            "⚠️ نجح تسجيل المتصفح، لكن تعذّر التحقق من جلسة قارئ X الآن. لم تُحفظ الجلسة؛ "
            "هذه ليست رسالة رفض لكلمة المرور، فحاول لاحقاً."
        )
    except (XTransactionCompatibilityError, XTransactionNetworkError):
        await event.respond(
            "⚠️ نجح مسار المتصفح، لكن تعذّر التحقق من جلسة القراءة مع X. "
            "لم تُحفظ الجلسة؛ حاول لاحقاً."
        )
    except Exception as e:  # noqa: BLE001
        log.warning("فشل دخول X التفاعلي (%s)", type(e).__name__)
        await event.respond(
            "❌ فشل تسجيل الدخول إلى X. تحقق من اسم المستخدم والبريد وكلمة المرور، "
            "ثم أعد المحاولة."
        )
    finally:
        _log_xlogin_stage("attempt_finished")
        _x_login_deleting.discard(uid)
        _x_login_cancelled.discard(uid)
        credentials.clear()
        if _x_login_tasks.get(uid) is task:
            _x_login_tasks.pop(uid, None)
        challenge = _x_challenges.pop(_x_challenge_key(event), None)
        if challenge is not None and not challenge.done():
            challenge.cancel()
        current = state.get(uid)
        if current and current.get("x_setup_id") == setup_id:
            _clear_state(uid)


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
            else "❌ لا توجد جلسة X صالحة لهذا الحساب؛ أعد إضافته لتسجيل الدخول."
        )
    except Exception as e:  # noqa: BLE001
        log.warning("فشل تبديل جلسة X (%s)", type(e).__name__)
        await event.respond("❌ تعذّر تفعيل جلسة X. أعد إضافة الحساب إذا انتهت جلسته.")


async def _apply_edit(event, st):
    item_id = st["item_id"]
    _clear_state(event.sender_id)
    item = PENDING.get(item_id)
    blocked = _publish_block_message(item_id, item)
    if blocked:
        await event.respond(blocked)
        return
    if not item:
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
    origin = f"https://x.com/{account['screen_name']}/status/{tweet.id}"
    existing = next(
        (
            (item_id, item)
            for item_id, item in PENDING.items.items()
            if item.get("origin") == origin
        ),
        None,
    )
    if existing:
        # انقطع التشغيل/فشل settings بعد حفظ pending. ثبّت المؤشر أولاً؛ ثم
        # أرسل نفس العنصر فقط إن لم تكن له رسالة مراجعة، بلا تنزيل أو add جديد.
        item_id, item = existing
        try:
            S.set_x_last_id(account["screen_name"], str(tweet.id))
        except Exception as e:  # noqa: BLE001
            log.error("تعذّر استئناف مؤشر X للمنشور %s: %s", item_id, e)
            await _notify_owner(
                "⚠️ تعذّر تثبيت مؤشر X لمنشور محفوظ؛ سأعيد المحاولة دون "
                f"إنشاء نسخة أخرى.\n{e}"
            )
            return False
        if item.get("review") is None:
            try:
                await _send_for_review(item_id)
            except Exception as e:  # noqa: BLE001
                log.error("ثُبّت مؤشر X لكن تعذّر إرسال المنشور %s للمراجعة: %s", item_id, e)
                await _notify_owner(
                    "⚠️ ثُبّت مؤشر X والمنشور محفوظ، لكن تعذّر عرضه للمراجعة. "
                    f"سيحاول outbox إرساله دورياً.\n{e}"
                )
        return True

    text = getattr(tweet, "full_text", None) or getattr(tweet, "text", "") or ""
    if S.is_filtered(text):
        log.info("تجاهل تغريدة (فلتر كلمات)")
        S.set_x_last_id(account["screen_name"], str(tweet.id))
        return True

    media = []
    max_bytes = _max_media_bytes()
    for url, kind in XReader.extract_media_urls(tweet)[:MAX_ALBUM_PHOTOS]:
        try:
            path = await asyncio.to_thread(_download_url, url, DOWNLOAD_DIR, max_bytes)
        except Exception as e:  # noqa: BLE001
            log.warning("فشل تنزيل وسيط X: %s", e)
            continue
        media.append({"path": path, "type": kind})

    log.info("تغريدة جديدة من @%s (%s)", account["screen_name"], media_summary(media) or "نص")
    item_id = await _queue_for_review(
        text,
        media,
        origin,
        on_persisted=lambda: S.set_x_last_id(
            account["screen_name"], str(tweet.id)
        ),
    )
    if item_id is None:
        # لا نتجاوز تغريدة لم تصل للمراجعة؛ ستظهر مجدداً في الدورة التالية.
        return False
    return True


_x_alerted = False


async def x_poller():
    global _x_alerted
    await asyncio.sleep(5)
    while True:
        try:
            if S.x_accounts() and S.get("review_chat_id"):
                if await xreader.ensure_login():
                    _x_alerted = False
                    for i, acc in enumerate(S.x_accounts()):
                        # تباعد عشوائي بسيط بين الحسابات (سلوك أقل آلية)
                        if i:
                            await asyncio.sleep(random.uniform(3, 10))
                        session = xreader.capture_session()
                        if session is None:
                            break
                        try:
                            for tw in await xreader.fetch_new(acc, session=session):
                                if not await handle_x_tweet(acc, tw):
                                    log.warning(
                                        "توقفت معالجة @%s لأن إرسال المراجعة فشل؛ "
                                        "سأعيد المحاولة في الدورة القادمة.",
                                        acc["screen_name"],
                                    )
                                    break
                        except Exception as e:  # noqa: BLE001
                            if xreader.report_failure(e, session=session):
                                await _notify_owner(
                                    f"⚠️ حساب X @{session.username} تعذّر (قد يكون محظوراً). "
                                    "سأجرّب حساباً آخر في الدورة القادمة."
                                )
                                break
                            log.warning("قراءة X @%s: %s", acc["screen_name"], e)
                elif S.x_logins() and not _x_alerted:
                    _x_alerted = True
                    await _notify_owner(
                        "🔐 لا توجد جلسة X صالحة حالياً.\n"
                        "أعد إضافة حساب الدخول من «⚙️ لوحة التحكم» ← 🐦 حسابات دخول X؛ "
                        "سيطلب البوت رمز Authenticator عند الحاجة."
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
            # outbox أولاً: لا نترك فشل Telegram ينتظر إعادة تشغيل قد لا تحدث.
            await _replay_unreviewed()
            # PendingStore يستثني outbox وحالات publishing/published بنفسه.
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
    global _claim_code, _restarting
    await bot.start(bot_token=S.get("bot_token"))
    # يعيد تشغيل العداد المحفوظ بعد restart، أو يرسل إشعار الانتهاء فوراً إذا
    # انقضى الموعد والبوت كان متوقفاً.
    _ensure_x_cooldown_scheduler()
    for scope, record in S.x_login_cooldowns().items():
        _ensure_x_account_cooldown_scheduler(scope, record)

    # بعد ترقية نسخة قديمة، تصل لوحة الأزرار للأدمنين تلقائياً مرة واحدة؛
    # لا يحتاج المستخدم إلى كتابة /start أو /panel كي تظهر له.
    owner = S.get("owner_id")
    keyboard_recipients = {
        uid for uid in ([owner] + (S.get("admin_ids") or []))
        if isinstance(uid, int)
    }
    await user.connect()
    _harden_state_permissions()   # ملفات الجلسة تُنشأ الآن — قيّدها فوراً

    if S.recovery == "recovered":
        log.warning("تم استرجاع الإعدادات من النسخة الاحتياطية بعد تلف الملف.")
    elif S.recovery == "corrupt":
        log.error("ملف الإعدادات كان تالفاً — راجع الملفات بلاحقة .corrupt-*")

    if not S.get("owner_id"):
        _claim_code = secrets.token_urlsafe(9)
        log.warning("=" * 56)
        log.warning(
            "لا يوجد مالك بعد. اضغط في البوت «🔑 تفعيل الملكية» ثم أرسل الرمز: %s",
            _claim_code,
        )
        log.warning("=" * 56)

    _rebuild_ids()
    # استعد outbox والحالات المؤكدة/غير المحسومة قبل أي تنظيف بالـTTL.
    await _replay_unreviewed()
    PENDING.purge_expired()
    PENDING.sweep_orphans()

    authed = await user.is_user_authorized()
    bot_me = await bot.get_me()
    log.info("البوت: @%s | الحساب الشخصي مسجّل: %s", bot_me.username, authed)
    log.info("منشورات بانتظار المراجعة: %d", len(PENDING.items))
    if not authed:
        log.info("الحساب غير مسجّل — سجّل الدخول من زر 🔐 داخل البوت.")

    try:
        await asyncio.gather(
            user.run_until_disconnected(),
            bot.run_until_disconnected(),
            x_poller(),
            housekeeping(),
            _offer_command_keyboards(keyboard_recipients),
        )
    finally:
        _restarting = True
        await _shutdown_x_cooldown()
        await _shutdown_x_account_cooldowns()
        if not await _shutdown_x_logins():
            log.critical("تعذّر تأكيد إغلاق متصفح X أثناء إيقاف البوت")


if __name__ == "__main__":
    asyncio.run(main())
