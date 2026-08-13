"""
مخزن إعدادات واحد لكل المشروع (settings.json).
كل شيء يُضبط من داخل تلغرام ويُحفظ هنا — لا حاجة لملف .env.
تبقى القيم الثلاث الأساسية (api_id/api_hash/bot_token) فقط عبر configure.py مرة واحدة.
"""
import copy
import json
import logging
import math
import os
import secrets
import threading
import time
import unicodedata

from jsonio import atomic_write_json, read_json

log = logging.getLogger("tg2fb.settings")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", os.path.join(BASE_DIR, "settings.json"))

DEFAULTS = {
    # الأساسيات (من configure.py)
    "api_id": None,
    "api_hash": None,
    "bot_token": None,
    # يُضبط من داخل تلغرام
    "owner_id": None,
    "admin_ids": [],
    # إصدار لوحة أوامر Telegram التي أُرسلت لكل أدمن؛ يمنع تكرار الرسالة عند كل restart.
    "reply_keyboard_versions": {},
    "review_chat_id": None,
    "fb_page_id": None,
    "fb_page_token": None,
    "fb_api_version": "v23.0",   # إصدار Graph API — ميتا تُنهي دعم الإصدارات بعد ~سنتين
    "default_cc": None,          # رمز الدولة الافتراضي مثل "966"
    "user_phone": None,
    "sources": [],               # قنوات تلغرام: [{"id","title","input"}]
    "download_dir": "downloads",
    "filter_words": [],          # كلمات ممنوعة: أي منشور يحتويها يُتجاهل
    # عبارات حرفية تُحذف من النص قبل عرضه/نشره؛ لا تُفسر كتعبيرات منتظمة.
    "text_cleanup_phrases": [],
    # حدود التشغيل — تحمي بطاقة الـ SD من الامتلاء
    "max_media_mb": 200,         # أقصى حجم وسيط يُنزَّل
    "pending_ttl_hours": 48,     # عمر المنشور المعلّق قبل حذفه تلقائياً
}

# X أُوقف من المنتج. القائمة ثابتة كي يستطيع التشغيل/النشر حذف كل بياناته
# القديمة من primary وbackup من دون تخمين أسماء المفاتيح.
REMOVED_X_SETTING_KEYS = frozenset({
    "x_logins",
    "x_accounts",
    "x_poll_seconds",
    "x_skip_replies",
    "x_max_per_cycle",
    "x_login_cooldown",
    "x_login_cooldown_emergency",
    "x_cooldown_key_id",
    "x_login_cooldowns",
})

_BACKUP_FORMAT = 2
_BACKUP_PREPARED = "prepared"
_BACKUP_COMMITTED = "committed"
_PRIMARY_TXID = "__settings_txid"
_MAX_UNIX_TIMESTAMP = 253402300799  # 9999-12-31T23:59:59Z
_MAX_X_LOGIN_COOLDOWN_SECONDS = 24 * 60 * 60
_MAX_X_LOGIN_COOLDOWNS = 32
_X_LOGIN_SCOPE_HEX_LENGTH = 64
_MAX_CLEANUP_PHRASES = 100
_MAX_CLEANUP_PHRASE_LENGTH = 200

_ARABIC_ALEF_VARIANTS = str.maketrans({
    "آ": "ا",
    "أ": "ا",
    "إ": "ا",
    "ٱ": "ا",
    "ٲ": "ا",
    "ٳ": "ا",
    "ٵ": "ا",
})


def _is_arabic_codepoint(char):
    codepoint = ord(char)
    return (
        0x0600 <= codepoint <= 0x06FF
        or 0x0750 <= codepoint <= 0x077F
        or 0x08A0 <= codepoint <= 0x08FF
        or 0xFB50 <= codepoint <= 0xFDFF
        or 0xFE70 <= codepoint <= 0xFEFF
    )


def _filter_match_key(value):
    """يطبع المقارنة فقط، مع إبقاء النص المحفوظ كما أدخله المستخدم."""
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = normalized.translate(_ARABIC_ALEF_VARIANTS).replace("ـ", "")
    return "".join(
        char
        for char in normalized
        if not (
            unicodedata.category(char).startswith("C")
            or (
                _is_arabic_codepoint(char)
                and unicodedata.category(char) in {"Mn", "Me"}
            )
        )
    )


def _normalize_cleanup_phrase(value):
    """يطبع عبارة حذف حرفية، ويرفض المحارف الخفية أو متعددة الأسطر."""
    if not isinstance(value, str):
        raise ValueError("cleanup phrase must be text")
    if any(
        char in "\r\n"
        or unicodedata.category(char).startswith("C")
        or unicodedata.category(char) in {"Zl", "Zp"}
        for char in value
    ):
        raise ValueError("cleanup phrase contains control or newline characters")
    normalized = unicodedata.normalize("NFKC", value)
    if any(
        unicodedata.category(char).startswith("C")
        or unicodedata.category(char) in {"Zl", "Zp"}
        for char in normalized
    ):
        raise ValueError("cleanup phrase contains control or newline characters")
    # بعد رفض المحارف التحكمية، لا يبقى من whitespace إلا فواصل آمنة مرئية.
    normalized = " ".join(normalized.split())
    if not normalized:
        raise ValueError("cleanup phrase must not be empty")
    if len(normalized) > _MAX_CLEANUP_PHRASE_LENGTH:
        raise ValueError("cleanup phrase is too long")
    return normalized


def _valid_txid(value):
    return isinstance(value, str) and bool(value)


def _settings_data(value):
    """يعيد بيانات الإعدادات المنطقية من دون حقل بروتوكول المعاملة المحجوز."""
    data = copy.deepcopy(value)
    data.pop(_PRIMARY_TXID, None)
    return data


def _primary_document(data, txid):
    document = _settings_data(data)
    document[_PRIMARY_TXID] = txid
    return document


def _backup_envelope(data, state, txid):
    return {
        "format": _BACKUP_FORMAT,
        "state": state,
        "txid": txid,
        "data": _settings_data(data),
    }


def _decode_committed_backup(value):
    if not isinstance(value, dict):
        return None
    if (
        value.get("format") != _BACKUP_FORMAT
        or value.get("state") != _BACKUP_COMMITTED
        or not _valid_txid(value.get("txid"))
        or not isinstance(value.get("data"), dict)
        or _PRIMARY_TXID in value["data"]
    ):
        return None
    return value["txid"], copy.deepcopy(value["data"])


def _read_dict(path):
    try:
        value = read_json(path)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return None, exc
    if not isinstance(value, dict):
        return None, TypeError("JSON root is not an object")
    return value, None


def _quarantine_corrupt(path):
    quarantine = f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        os.replace(path, quarantine)
    except OSError:
        return None
    log.error("نُقل ملف الإعدادات التالف إلى %s", quarantine)
    return quarantine


def _repair_committed_backup(path, data, txid):
    """يجعل backup مرجعاً committed لنفس primary؛ الفشل لا يبطل primary صالحاً."""
    backup = path + ".bak"
    current, error = _read_dict(backup)
    if error is None and _decode_committed_backup(current) == (txid, data):
        return True
    try:
        atomic_write_json(
            backup, _backup_envelope(data, _BACKUP_COMMITTED, txid), mode=0o600
        )
    except OSError as exc:
        log.warning("الإعدادات سليمة لكن تعذّر إصلاح النسخة الاحتياطية: %s", exc)
        return False
    return True


def _migrate_legacy_primary(path, data):
    """يربط primary قديمة بمعاملة قبل أن يمنح backup القديمة أي ثقة."""
    txid = secrets.token_hex(16)
    try:
        atomic_write_json(path, _primary_document(data, txid), mode=0o600)
    except OSError as exc:
        # primary القديمة ما زالت المرجع الصالح. لا نلمس backup القديمة كي تظل
        # غير موثوقة إن فُقد primary لاحقاً.
        log.warning("قُرئت الإعدادات القديمة لكن تعذّرت هجرتها: %s", exc)
        return False
    _repair_committed_backup(path, data, txid)
    return True


def read_settings_resilient(path):
    """قراءة fail-closed: لا تستعيد إلا backup معلّمة committed صراحةً."""
    primary_exists = os.path.exists(path)
    if primary_exists:
        primary, error = _read_dict(path)
        if error is None:
            # أي primary سليمة هي المرجع دائماً؛ ولا يصل حقل المعاملة إلى المستهلكين.
            data = _settings_data(primary)
            txid = primary.get(_PRIMARY_TXID)
            if _valid_txid(txid):
                # لا تكفي مساواة البيانات: يجب أن تنتمي backup لنفس المعاملة.
                _repair_committed_backup(path, data, txid)
            else:
                _migrate_legacy_primary(path, data)
            return data, None
        log.error("ملف الإعدادات %s تالف (%s)", path, type(error).__name__)
        _quarantine_corrupt(path)

    backup = path + ".bak"
    if not os.path.exists(backup):
        return (None, "corrupt") if primary_exists else (None, None)
    envelope, backup_error = _read_dict(backup)
    decoded = None if backup_error is not None else _decode_committed_backup(envelope)
    if decoded is None:
        # PREPARED قد تكون عملية فشلت قبل commit؛ والنسخة القديمة بلا marker قد
        # تحتوي صلاحيات/أسراراً أُلغيَت، لذلك لا نطبق أياً منهما تلقائياً.
        log.error("رُفضت نسخة إعدادات احتياطية غير committed")
        return None, "corrupt"
    txid, recovered = decoded
    try:
        atomic_write_json(path, _primary_document(recovered, txid), mode=0o600)
    except OSError as exc:
        log.error("استُعيدت الإعدادات لكن تعذّر إصلاح الملف الأساسي: %s", exc)
    else:
        log.warning("استُعيدت الإعدادات من نسخة committed وأُصلح الملف الأساسي")
    return recovered, "recovered"


def _env_int(name):
    v = os.environ.get(name)
    try:
        return int(v) if v else None
    except ValueError:
        return None


class Settings:
    def __init__(self, path=SETTINGS_FILE):
        self.path = path
        self._lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)   # deepcopy: لئلا تُشارَك قوائم DEFAULTS
        self.recovery = None                  # None | "recovered" | "corrupt"
        self.load()

    def load(self):
        data, status = read_settings_resilient(self.path)
        self.recovery = status
        if data:
            self.data.update(data)
        elif status == "corrupt":
            log.error(
                "تعذّر قراءة %s وأي نسخة احتياطية — سأبدأ بإعدادات فارغة. "
                "الملف التالف محفوظ بلاحقة .corrupt-* بجانبه.", self.path
            )
        # سماح بأخذ الأساسيات من متغيرات البيئة كبديل (اختياري)
        self.data["api_id"] = self.data.get("api_id") or _env_int("API_ID")
        self.data["api_hash"] = self.data.get("api_hash") or os.environ.get("API_HASH")
        self.data["bot_token"] = self.data.get("bot_token") or os.environ.get("BOT_TOKEN")

    def _commit(self, data):
        """معاملة: PREPARED ثم primary كنقطة commit ثم COMMITTED بأفضل جهد."""
        with self._lock:
            candidate = _settings_data(data)
            txid = secrets.token_hex(16)
            prepared = _backup_envelope(candidate, _BACKUP_PREPARED, txid)
            committed = _backup_envelope(candidate, _BACKUP_COMMITTED, txid)
            primary = _primary_document(candidate, txid)
            # إذا فشلت هذه أو كتابة primary لا تتغير الذاكرة، وPREPARED لا تُستعاد.
            atomic_write_json(self.path + ".bak", prepared, mode=0o600)
            atomic_write_json(self.path, primary, mode=0o600)
            # primary أصبح commit point؛ لا نُرجع فشلاً بعد هذه النقطة.
            self.data = candidate
            try:
                atomic_write_json(self.path + ".bak", committed, mode=0o600)
            except OSError as exc:
                log.warning(
                    "حُفظت الإعدادات لكن تعذّر إنهاء النسخة الاحتياطية: %s", exc
                )

    def save(self):
        """يحفظ تعديلات مباشرة متعمّدة (يستخدمه configure.py)."""
        self._commit(self.data)

    def scrub_x_integration_data(self):
        """يحذف إعدادات X القديمة بمعاملة واحدة، ولا ينشئ مفاتيح فارغة."""
        with self._lock:
            present = REMOVED_X_SETTING_KEYS.intersection(self.data)
            if not present:
                return 0
            candidate = copy.deepcopy(self.data)
            for key in present:
                candidate.pop(key, None)
            self._commit(candidate)
            return len(present)

    def _replace(self, key, value):
        candidate = copy.deepcopy(self.data)
        candidate[key] = value
        self._commit(candidate)

    # --- وصول عام ---
    def get(self, key, default=None):
        return copy.deepcopy(self.data.get(key, default))

    def get_int(self, key, default):
        """يقرأ قيمة رقمية بأمان — قيمة تالفة في الملف لا توقف البوت."""
        try:
            value = int(self.data.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    def set(self, key, value):
        with self._lock:
            self._replace(key, value)

    def set_many(self, values):
        """يثبّت عدة مفاتيح معاً؛ إمّا كلها أو لا شيء."""
        with self._lock:
            candidate = copy.deepcopy(self.data)
            candidate.update(values)
            self._commit(candidate)

    # --- بوت جاهز للتشغيل؟ ---
    def bootstrap_ready(self):
        return all(self.data.get(k) for k in ("api_id", "api_hash", "bot_token"))

    # --- الأدمنون ---
    def is_admin(self, uid):
        return uid == self.data.get("owner_id") or uid in (self.data.get("admin_ids") or [])

    def add_admin(self, uid):
        with self._lock:
            ids = list(self.data.get("admin_ids") or [])
            if uid not in ids:
                ids.append(uid)
                self._replace("admin_ids", ids)

    def remove_admin(self, uid):
        with self._lock:
            ids = [
                i for i in (self.data.get("admin_ids") or []) if i != uid
            ]
            self._replace("admin_ids", ids)

    # --- القنوات المصدر ---
    def sources(self):
        return copy.deepcopy(self.data.get("sources") or [])

    def source_ids(self):
        return {s["id"] for s in self.sources()}

    def add_source(self, peer_id, title, raw):
        with self._lock:
            srcs = self.sources()
            if any(s["id"] == peer_id for s in srcs):
                return False
            srcs.append({"id": peer_id, "title": title, "input": raw})
            self._replace("sources", srcs)
            return True

    def remove_source(self, peer_id=None, raw=None):
        with self._lock:
            srcs = self.sources()
            kept = [
                s for s in srcs
                if not ((peer_id is not None and s["id"] == peer_id)
                        or (raw is not None and (s["input"] == raw or s["title"] == raw)))
            ]
            removed = len(srcs) - len(kept)
            self._replace("sources", kept)
            return removed

    # --- فلترة الكلمات ---
    def filter_words(self):
        return copy.deepcopy(self.data.get("filter_words") or [])

    def add_filter_word(self, word):
        with self._lock:
            word = word.strip()
            identity = _filter_match_key(word)
            if not identity or identity in {
                _filter_match_key(existing)
                for existing in self.filter_words()
            }:
                return False
            words = self.filter_words()
            words.append(word)
            self._replace("filter_words", words)
            return True

    def remove_filter_word(self, word):
        with self._lock:
            words = self.filter_words()
            identity = _filter_match_key(word.strip())
            kept = [w for w in words if _filter_match_key(w) != identity]
            removed = len(words) - len(kept)
            self._replace("filter_words", kept)
            return removed

    def is_filtered(self, text):
        normalized = _filter_match_key(text or "")
        return any(
            identity and identity in normalized
            for identity in map(_filter_match_key, self.filter_words())
        )

    # --- حذف عبارات حرفية من النص ---
    def cleanup_phrases(self):
        raw_phrases = self.data.get("text_cleanup_phrases")
        if not isinstance(raw_phrases, list):
            return []
        phrases = []
        identities = set()
        for raw_phrase in raw_phrases:
            try:
                phrase = _normalize_cleanup_phrase(raw_phrase)
            except ValueError:
                continue
            identity = phrase.casefold()
            if identity in identities:
                continue
            identities.add(identity)
            phrases.append(phrase)
            if len(phrases) == _MAX_CLEANUP_PHRASES:
                break
        return phrases

    def add_cleanup_phrase(self, phrase):
        with self._lock:
            phrase = _normalize_cleanup_phrase(phrase)
            phrases = self.cleanup_phrases()
            identity = phrase.casefold()
            if identity in {existing.casefold() for existing in phrases}:
                return False
            if len(phrases) >= _MAX_CLEANUP_PHRASES:
                raise ValueError("too many cleanup phrases")
            phrases.append(phrase)
            self._replace("text_cleanup_phrases", phrases)
            return True

    def remove_cleanup_phrase(self, phrase):
        with self._lock:
            identity = _normalize_cleanup_phrase(phrase).casefold()
            phrases = self.cleanup_phrases()
            kept = [item for item in phrases if item.casefold() != identity]
            removed = len(phrases) - len(kept)
            if removed:
                self._replace("text_cleanup_phrases", kept)
            return removed

    # --- فيسبوك ---
    def facebook_ready(self):
        return bool(self.data.get("fb_page_id") and self.data.get("fb_page_token"))

    # --- حسابات دخول X (مجموعة، مع تبديل تلقائي عند الحظر) ---
    def x_logins(self):
        return copy.deepcopy(self.data.get("x_logins") or [])

    def x_login_ready(self):
        return any(not lg.get("failed") for lg in self.x_logins())

    def add_x_login(self, username, email, password):
        """يضيف جلسة دخول ويجعلها النشطة؛ كلمة المرور لا تُحفظ بعد التحقق."""
        with self._lock:
            logins = [
                lg for lg in self.x_logins() if lg["username"].lower() != username.lower()
            ]
            logins.insert(0, {
                "username": username, "email": email, "password": None, "failed": False,
            })
            self._replace("x_logins", logins)

    def scrub_x_login_passwords(self):
        """يحذف كلمات مرور X القديمة من primary والنسخة الاحتياطية ذرّياً."""
        with self._lock:
            logins = self.x_logins()
            changed = 0
            for login in logins:
                if login.get("password") is not None:
                    login["password"] = None
                    changed += 1
            if changed:
                self._replace("x_logins", logins)
            return changed

    def remove_x_login(self, username):
        with self._lock:
            logins = self.x_logins()
            kept = [lg for lg in logins if lg["username"].lower() != username.lower()]
            removed = len(logins) - len(kept)
            self._replace("x_logins", kept)
            return removed

    def active_x_login(self):
        for lg in self.x_logins():
            if not lg.get("failed"):
                return lg
        return None

    def set_active_x_login(self, username):
        with self._lock:
            logins = self.x_logins()
            chosen = [lg for lg in logins if lg["username"].lower() == username.lower()]
            if not chosen:
                return False
            rest = [lg for lg in logins if lg["username"].lower() != username.lower()]
            chosen[0]["failed"] = False
            self._replace("x_logins", chosen + rest)
            return True

    def mark_x_login_failed(self, username, failed=True):
        with self._lock:
            logins = self.x_logins()
            for lg in logins:
                if lg["username"].lower() == username.lower():
                    lg["failed"] = failed
            self._replace("x_logins", logins)

    def reset_x_failures(self):
        with self._lock:
            logins = self.x_logins()
            for lg in logins:
                lg["failed"] = False
            self._replace("x_logins", logins)

    # --- مهلة محاولات دخول X ---
    @staticmethod
    def _valid_x_login_cooldown(value):
        """ينقّي سجل المهلة كيلا تُستخدم بيانات تالفة أو حقول اعتماد دخيلة."""
        if not isinstance(value, dict):
            return None
        generation = value.get("generation")
        started_at = value.get("started_at")
        until = value.get("until")
        duration_seconds = value.get("duration_seconds")
        chat_id = value.get("chat_id")
        message_id = value.get("message_id")
        notified = value.get("notified", False)
        if (
            not isinstance(generation, str)
            or not generation
            or len(generation) > 128
            or isinstance(started_at, bool)
            or not isinstance(started_at, (int, float))
            or not math.isfinite(started_at)
            or started_at < 0
            or started_at > _MAX_UNIX_TIMESTAMP
            or isinstance(until, bool)
            or not isinstance(until, (int, float))
            or not math.isfinite(until)
            or until <= 0
            or until > _MAX_UNIX_TIMESTAMP
            or isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, (int, float))
            or not math.isfinite(duration_seconds)
            or duration_seconds < 1
            or duration_seconds > _MAX_X_LOGIN_COOLDOWN_SECONDS
            or abs((until - started_at) - duration_seconds) > 1
            or isinstance(chat_id, bool)
            or not isinstance(chat_id, int)
            or chat_id == 0
            or (
                message_id is not None
                and (
                    isinstance(message_id, bool)
                    or not isinstance(message_id, int)
                    or message_id <= 0
                )
            )
            or not isinstance(notified, bool)
        ):
            return None
        return {
            "generation": generation,
            "started_at": float(started_at),
            "until": float(until),
            "duration_seconds": int(duration_seconds),
            "chat_id": chat_id,
            "message_id": message_id,
            "notified": notified,
        }

    def x_login_cooldown(self):
        with self._lock:
            return copy.deepcopy(
                self._valid_x_login_cooldown(self.data.get("x_login_cooldown"))
            )

    def start_x_login_cooldown(
        self, until, chat_id, *, generation=None, message_id=None,
        duration_seconds=3600,
    ):
        """يستبدل المهلة ذرّياً ويعيد سجلها المنقّى (من دون أي اعتماد X)."""
        record = self._valid_x_login_cooldown({
            "generation": generation or secrets.token_hex(16),
            "started_at": float(until) - float(duration_seconds),
            "until": until,
            "duration_seconds": duration_seconds,
            "chat_id": chat_id,
            "message_id": message_id,
            "notified": False,
        })
        if record is None:
            raise ValueError("invalid X login cooldown")
        with self._lock:
            self._replace("x_login_cooldown", record)
        return copy.deepcopy(record)

    def set_x_login_cooldown_message(self, generation, message_id):
        """يربط رسالة العداد بالجيل نفسه؛ لا يستطيع مؤقّت قديم لمس جيل أحدث."""
        with self._lock:
            current = self._valid_x_login_cooldown(
                self.data.get("x_login_cooldown")
            )
            if current is None or current["generation"] != generation:
                return False
            updated = dict(current)
            updated["message_id"] = message_id
            updated = self._valid_x_login_cooldown(updated)
            if updated is None:
                raise ValueError("invalid Telegram message id")
            self._replace("x_login_cooldown", updated)
            return True

    def mark_x_login_cooldown_notified(self, generation):
        """يثبّت نجاح إشعار الانتهاء ذرّياً ويمنع الإشعارات المكررة بعد restart."""
        with self._lock:
            current = self._valid_x_login_cooldown(
                self.data.get("x_login_cooldown")
            )
            if current is None or current["generation"] != generation:
                return False
            if current["notified"]:
                return True
            updated = dict(current)
            updated["notified"] = True
            self._replace("x_login_cooldown", updated)
            return True

    # --- Account-scoped X login cooldowns ---
    @staticmethod
    def _valid_x_login_cooldown_scope(value):
        """Return a canonical opaque fingerprint, never a raw account identity."""
        if (
            not isinstance(value, str)
            or len(value) != _X_LOGIN_SCOPE_HEX_LENGTH
            or any(character not in "0123456789abcdefABCDEF" for character in value)
        ):
            return None
        return value.lower()

    @classmethod
    def _valid_x_login_cooldowns(cls, value):
        """Sanitize each scoped record independently and enforce a hard size cap."""
        if not isinstance(value, dict):
            return {}
        cooldowns = {}
        for raw_scope, raw_record in value.items():
            scope = cls._valid_x_login_cooldown_scope(raw_scope)
            record = cls._valid_x_login_cooldown(raw_record)
            if scope is None or record is None or scope in cooldowns:
                continue
            cooldowns[scope] = record
            if len(cooldowns) == _MAX_X_LOGIN_COOLDOWNS:
                break
        return cooldowns

    @classmethod
    def _require_x_login_cooldown_scope(cls, value):
        scope = cls._valid_x_login_cooldown_scope(value)
        if scope is None:
            raise ValueError("invalid X login cooldown scope")
        return scope

    def x_login_cooldowns(self):
        """List valid cooldowns by opaque account fingerprint."""
        with self._lock:
            return copy.deepcopy(
                self._valid_x_login_cooldowns(
                    self.data.get("x_login_cooldowns")
                )
            )

    def x_login_cooldown_for(self, scope):
        """Get one account's cooldown without exposing another account's state."""
        scope = self._require_x_login_cooldown_scope(scope)
        with self._lock:
            return copy.deepcopy(self._valid_x_login_cooldowns(
                self.data.get("x_login_cooldowns")
            ).get(scope))

    def start_x_login_cooldown_for(
        self, scope, until, chat_id, *, generation=None, message_id=None,
        duration_seconds=3600,
    ):
        """Atomically start or replace only the cooldown for ``scope``."""
        scope = self._require_x_login_cooldown_scope(scope)
        record = self._valid_x_login_cooldown({
            "generation": generation or secrets.token_hex(16),
            "started_at": float(until) - float(duration_seconds),
            "until": until,
            "duration_seconds": duration_seconds,
            "chat_id": chat_id,
            "message_id": message_id,
            "notified": False,
        })
        if record is None:
            raise ValueError("invalid X login cooldown")
        with self._lock:
            cooldowns = self._valid_x_login_cooldowns(
                self.data.get("x_login_cooldowns")
            )
            if scope not in cooldowns and len(cooldowns) >= _MAX_X_LOGIN_COOLDOWNS:
                removable = sorted(
                    (
                        (key, value) for key, value in cooldowns.items()
                        if value["notified"]
                    ),
                    key=lambda item: item[1]["until"],
                )
                while removable and len(cooldowns) >= _MAX_X_LOGIN_COOLDOWNS:
                    old_scope, _old_record = removable.pop(0)
                    cooldowns.pop(old_scope, None)
                if len(cooldowns) >= _MAX_X_LOGIN_COOLDOWNS:
                    raise ValueError("too many active X login cooldowns")
            cooldowns[scope] = record
            self._replace("x_login_cooldowns", cooldowns)
        return copy.deepcopy(record)

    def set_x_login_cooldown_message_for(self, scope, generation, message_id):
        """CAS-bind a Telegram countdown message to one account and generation."""
        scope = self._require_x_login_cooldown_scope(scope)
        with self._lock:
            cooldowns = self._valid_x_login_cooldowns(
                self.data.get("x_login_cooldowns")
            )
            current = cooldowns.get(scope)
            if current is None or current["generation"] != generation:
                return False
            updated = dict(current)
            updated["message_id"] = message_id
            updated = self._valid_x_login_cooldown(updated)
            if updated is None:
                raise ValueError("invalid Telegram message id")
            cooldowns[scope] = updated
            self._replace("x_login_cooldowns", cooldowns)
            return True

    def mark_x_login_cooldown_notified_for(self, scope, generation):
        """CAS-mark one account generation notified, idempotently."""
        scope = self._require_x_login_cooldown_scope(scope)
        with self._lock:
            cooldowns = self._valid_x_login_cooldowns(
                self.data.get("x_login_cooldowns")
            )
            current = cooldowns.get(scope)
            if current is None or current["generation"] != generation:
                return False
            if current["notified"]:
                return True
            updated = dict(current)
            updated["notified"] = True
            cooldowns[scope] = updated
            self._replace("x_login_cooldowns", cooldowns)
            return True

    def remove_x_login_cooldown_for(self, scope, generation):
        """CAS-remove one generation without letting stale workers remove a newer one."""
        scope = self._require_x_login_cooldown_scope(scope)
        with self._lock:
            cooldowns = self._valid_x_login_cooldowns(
                self.data.get("x_login_cooldowns")
            )
            current = cooldowns.get(scope)
            if current is None or current["generation"] != generation:
                return False
            del cooldowns[scope]
            self._replace("x_login_cooldowns", cooldowns)
            return True

    def bind_legacy_x_login_cooldown_to(self, scope):
        """Atomically move the identity-less legacy timer to a confirmed scope."""
        scope = self._require_x_login_cooldown_scope(scope)
        with self._lock:
            legacy = self._valid_x_login_cooldown(
                self.data.get("x_login_cooldown")
            )
            if legacy is None:
                return None
            cooldowns = self._valid_x_login_cooldowns(
                self.data.get("x_login_cooldowns")
            )
            existing = cooldowns.get(scope)
            if existing is not None and existing["generation"] != legacy["generation"]:
                raise ValueError("scope already has a different X login cooldown")
            if existing is None and len(cooldowns) >= _MAX_X_LOGIN_COOLDOWNS:
                raise ValueError("too many X login cooldowns")
            cooldowns[scope] = legacy
            candidate = copy.deepcopy(self.data)
            candidate["x_login_cooldowns"] = cooldowns
            candidate["x_login_cooldown"] = None
            candidate["x_login_cooldown_emergency"] = False
            self._commit(candidate)
            return copy.deepcopy(legacy)

    # --- الحسابات المتابَعة ---
    def x_accounts(self):
        return copy.deepcopy(self.data.get("x_accounts") or [])

    def add_x_account(self, screen_name, user_id, last_id=None):
        """
        last_id = أحدث تغريدة وقت الإضافة. بدونه تُعتبر كل التغريدات القديمة
        "جديدة" وتُغرق قروب المراجعة بعشرين تغريدة دفعة واحدة.
        """
        with self._lock:
            accs = self.x_accounts()
            if any(a["screen_name"].lower() == screen_name.lower() for a in accs):
                return False
            accs.append({
                "screen_name": screen_name,
                "user_id": user_id,
                "last_id": str(last_id) if last_id is not None else None,
            })
            self._replace("x_accounts", accs)
            return True

    def remove_x_account(self, screen_name):
        with self._lock:
            accs = self.x_accounts()
            kept = [a for a in accs if a["screen_name"].lower() != screen_name.lower()]
            removed = len(accs) - len(kept)
            self._replace("x_accounts", kept)
            return removed

    def set_x_last_id(self, screen_name, last_id):
        with self._lock:
            accs = self.x_accounts()
            for a in accs:
                if a["screen_name"].lower() == screen_name.lower():
                    current = a.get("last_id")
                    try:
                        # معرّفات X الرقمية تصاعدية؛ replay لتغريدة قديمة لا
                        # يجوز أن يرجع المؤشر للخلف فيعيد تغريدات أحدث.
                        if current is not None and int(last_id) <= int(current):
                            continue
                    except (TypeError, ValueError):
                        if str(last_id) == str(current):
                            continue
                    a["last_id"] = str(last_id)
            self._replace("x_accounts", accs)
