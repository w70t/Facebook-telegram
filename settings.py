"""
مخزن إعدادات واحد لكل المشروع (settings.json).
كل شيء يُضبط من داخل تلغرام ويُحفظ هنا — لا حاجة لملف .env.
تبقى القيم الثلاث الأساسية (api_id/api_hash/bot_token) فقط عبر configure.py مرة واحدة.
"""
import copy
import logging
import os
import shutil
import threading

from jsonio import atomic_write_json, read_json_resilient

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
    "review_chat_id": None,
    "fb_page_id": None,
    "fb_page_token": None,
    "fb_api_version": "v23.0",   # إصدار Graph API — ميتا تُنهي دعم الإصدارات بعد ~سنتين
    "default_cc": None,          # رمز الدولة الافتراضي مثل "966"
    "user_phone": None,
    "sources": [],               # قنوات تلغرام: [{"id","title","input"}]
    "download_dir": "downloads",
    # X (تويتر) — طريقة غير رسمية عبر twikit
    "x_logins": [],              # حسابات الدخول: [{"username","email","password","failed"}]
    "x_accounts": [],            # الحسابات المتابَعة: [{"screen_name","user_id","last_id"}]
    "x_poll_seconds": 120,
    "x_skip_replies": True,      # X: انسخ التغريدات فقط لا الردود
    "x_max_per_cycle": 5,        # سقف التغريدات لكل دورة (الباقي يأتي في الدورة التالية)
    "filter_words": [],          # كلمات ممنوعة: أي منشور يحتويها يُتجاهل
    # حدود التشغيل — تحمي بطاقة الـ SD من الامتلاء
    "max_media_mb": 200,         # أقصى حجم وسيط يُنزَّل
    "pending_ttl_hours": 48,     # عمر المنشور المعلّق قبل حذفه تلقائياً
}


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
        data, status = read_json_resilient(self.path)
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

    def save(self):
        with self._lock:
            # نحتفظ بآخر نسخة سليمة قبل الاستبدال — تُستخدم للاسترجاع لو تلف الملف
            if os.path.exists(self.path):
                try:
                    shutil.copy2(self.path, self.path + ".bak")
                    os.chmod(self.path + ".bak", 0o600)
                except OSError:
                    pass
            # الملف يحتوي أسرارًا (توكن فيسبوك/كلمات مرور X) — قراءة المالك فقط
            atomic_write_json(self.path, self.data, mode=0o600)

    # --- وصول عام ---
    def get(self, key, default=None):
        return self.data.get(key, default)

    def get_int(self, key, default):
        """يقرأ قيمة رقمية بأمان — قيمة تالفة في الملف لا توقف البوت."""
        try:
            value = int(self.data.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    def set(self, key, value):
        with self._lock:
            self.data[key] = value
            self.save()

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
                self.data["admin_ids"] = ids
                self.save()

    def remove_admin(self, uid):
        with self._lock:
            self.data["admin_ids"] = [
                i for i in (self.data.get("admin_ids") or []) if i != uid
            ]
            self.save()

    # --- القنوات المصدر ---
    def sources(self):
        return list(self.data.get("sources") or [])

    def source_ids(self):
        return {s["id"] for s in self.sources()}

    def add_source(self, peer_id, title, raw):
        with self._lock:
            srcs = self.sources()
            if any(s["id"] == peer_id for s in srcs):
                return False
            srcs.append({"id": peer_id, "title": title, "input": raw})
            self.data["sources"] = srcs
            self.save()
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
            self.data["sources"] = kept
            self.save()
            return removed

    # --- فلترة الكلمات ---
    def filter_words(self):
        return list(self.data.get("filter_words") or [])

    def add_filter_word(self, word):
        with self._lock:
            word = word.strip()
            if not word or word.lower() in [w.lower() for w in self.filter_words()]:
                return False
            words = self.filter_words()
            words.append(word)
            self.data["filter_words"] = words
            self.save()
            return True

    def remove_filter_word(self, word):
        with self._lock:
            words = self.filter_words()
            kept = [w for w in words if w.lower() != word.strip().lower()]
            removed = len(words) - len(kept)
            self.data["filter_words"] = kept
            self.save()
            return removed

    def is_filtered(self, text):
        low = (text or "").lower()
        return any(w.lower() in low for w in self.filter_words())

    # --- فيسبوك ---
    def facebook_ready(self):
        return bool(self.data.get("fb_page_id") and self.data.get("fb_page_token"))

    # --- حسابات دخول X (مجموعة، مع تبديل تلقائي عند الحظر) ---
    def x_logins(self):
        return list(self.data.get("x_logins") or [])

    def x_login_ready(self):
        return any(not lg.get("failed") for lg in self.x_logins())

    def add_x_login(self, username, email, password):
        """يضيف حساب دخول ويجعله النشط (في المقدمة). يحدّث لو موجوداً."""
        with self._lock:
            logins = [
                lg for lg in self.x_logins() if lg["username"].lower() != username.lower()
            ]
            logins.insert(0, {
                "username": username, "email": email, "password": password, "failed": False,
            })
            self.data["x_logins"] = logins
            self.save()

    def remove_x_login(self, username):
        with self._lock:
            logins = self.x_logins()
            kept = [lg for lg in logins if lg["username"].lower() != username.lower()]
            removed = len(logins) - len(kept)
            self.data["x_logins"] = kept
            self.save()
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
            self.data["x_logins"] = chosen + rest
            self.save()
            return True

    def mark_x_login_failed(self, username, failed=True):
        with self._lock:
            logins = self.x_logins()
            for lg in logins:
                if lg["username"].lower() == username.lower():
                    lg["failed"] = failed
            self.data["x_logins"] = logins
            self.save()

    def reset_x_failures(self):
        with self._lock:
            logins = self.x_logins()
            for lg in logins:
                lg["failed"] = False
            self.data["x_logins"] = logins
            self.save()

    # --- الحسابات المتابَعة ---
    def x_accounts(self):
        return list(self.data.get("x_accounts") or [])

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
            self.data["x_accounts"] = accs
            self.save()
            return True

    def remove_x_account(self, screen_name):
        with self._lock:
            accs = self.x_accounts()
            kept = [a for a in accs if a["screen_name"].lower() != screen_name.lower()]
            removed = len(accs) - len(kept)
            self.data["x_accounts"] = kept
            self.save()
            return removed

    def set_x_last_id(self, screen_name, last_id):
        with self._lock:
            accs = self.x_accounts()
            for a in accs:
                if a["screen_name"].lower() == screen_name.lower():
                    a["last_id"] = str(last_id)
            self.data["x_accounts"] = accs
            self.save()
