"""
مخزن إعدادات واحد لكل المشروع (settings.json).
كل شيء يُضبط من داخل تلغرام ويُحفظ هنا — لا حاجة لملف .env.
تبقى القيم الثلاث الأساسية (api_id/api_hash/bot_token) فقط عبر setup.py مرة واحدة.
"""
import json
import os
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", os.path.join(BASE_DIR, "settings.json"))

DEFAULTS = {
    # الأساسيات (من setup.py)
    "api_id": None,
    "api_hash": None,
    "bot_token": None,
    # يُضبط من داخل تلغرام
    "owner_id": None,
    "admin_ids": [],
    "review_chat_id": None,
    "fb_page_id": None,
    "fb_page_token": None,
    "default_cc": None,      # رمز الدولة الافتراضي مثل "966"
    "user_phone": None,
    "sources": [],           # قنوات تلغرام: [{"id","title","input"}]
    "download_dir": "downloads",
    # X (تويتر) — طريقة غير رسمية عبر twikit
    "x_logins": [],          # حسابات الدخول: [{"username","email","password","failed"}]
    "x_accounts": [],        # الحسابات المتابَعة: [{"screen_name","user_id","last_id"}]
    "x_poll_seconds": 120,
    "x_skip_replies": True,  # X: انسخ التغريدات فقط لا الردود
    "filter_words": [],      # كلمات ممنوعة: أي منشور يحتويها يُتجاهل
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
        self._lock = threading.Lock()
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except (json.JSONDecodeError, OSError):
                pass
        # سماح بأخذ الأساسيات من متغيرات البيئة كبديل (اختياري)
        self.data["api_id"] = self.data.get("api_id") or _env_int("API_ID")
        self.data["api_hash"] = self.data.get("api_hash") or os.environ.get("API_HASH")
        self.data["bot_token"] = self.data.get("bot_token") or os.environ.get("BOT_TOKEN")

    def save(self):
        with self._lock:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            # الملف يحتوي أسرارًا (توكن فيسبوك/كلمات مرور X) — اقصر القراءة على المالك
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    # --- وصول عام ---
    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()

    # --- بوت جاهز للتشغيل؟ ---
    def bootstrap_ready(self):
        return all(self.data.get(k) for k in ("api_id", "api_hash", "bot_token"))

    # --- الأدمنون ---
    def is_admin(self, uid):
        return uid == self.data.get("owner_id") or uid in (self.data.get("admin_ids") or [])

    def add_admin(self, uid):
        ids = list(self.data.get("admin_ids") or [])
        if uid not in ids:
            ids.append(uid)
            self.data["admin_ids"] = ids
            self.save()

    def remove_admin(self, uid):
        ids = [i for i in (self.data.get("admin_ids") or []) if i != uid]
        self.data["admin_ids"] = ids
        self.save()

    # --- القنوات المصدر ---
    def sources(self):
        return list(self.data.get("sources") or [])

    def source_ids(self):
        return {s["id"] for s in self.sources()}

    def add_source(self, peer_id, title, raw):
        srcs = self.sources()
        if any(s["id"] == peer_id for s in srcs):
            return False
        srcs.append({"id": peer_id, "title": title, "input": raw})
        self.data["sources"] = srcs
        self.save()
        return True

    def remove_source(self, peer_id=None, raw=None):
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
        word = word.strip()
        if not word or word.lower() in [w.lower() for w in self.filter_words()]:
            return False
        words = self.filter_words()
        words.append(word)
        self.data["filter_words"] = words
        self.save()
        return True

    def remove_filter_word(self, word):
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
        logins = [lg for lg in self.x_logins() if lg["username"].lower() != username.lower()]
        logins.insert(0, {
            "username": username, "email": email, "password": password, "failed": False,
        })
        self.data["x_logins"] = logins
        self.save()

    def remove_x_login(self, username):
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
        for lg in self.x_logins():
            if lg["username"].lower() == username.lower():
                lg["failed"] = failed
        self.data["x_logins"] = self.x_logins()
        self.save()

    def reset_x_failures(self):
        logins = self.x_logins()
        for lg in logins:
            lg["failed"] = False
        self.data["x_logins"] = logins
        self.save()

    # --- الحسابات المتابَعة ---
    def x_accounts(self):
        return list(self.data.get("x_accounts") or [])

    def add_x_account(self, screen_name, user_id):
        accs = self.x_accounts()
        if any(a["screen_name"].lower() == screen_name.lower() for a in accs):
            return False
        accs.append({"screen_name": screen_name, "user_id": user_id, "last_id": None})
        self.data["x_accounts"] = accs
        self.save()
        return True

    def remove_x_account(self, screen_name):
        accs = self.x_accounts()
        kept = [a for a in accs if a["screen_name"].lower() != screen_name.lower()]
        removed = len(accs) - len(kept)
        self.data["x_accounts"] = kept
        self.save()
        return removed

    def set_x_last_id(self, screen_name, last_id):
        accs = self.x_accounts()
        for a in accs:
            if a["screen_name"].lower() == screen_name.lower():
                a["last_id"] = last_id
        self.data["x_accounts"] = accs
        self.save()
