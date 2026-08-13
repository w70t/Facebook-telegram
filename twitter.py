"""
قارئ X (تويتر) غير رسمي عبر مكتبة twikit.
يدعم مجموعة حسابات دخول: لو انحظر حساب يبدّل تلقائياً للي بعده.

ملاحظة: twikit غير رسمية وقد تتغيّر واجهتها؛ الكود مكتوب دفاعياً.
"""
import logging
import os
import re
import tempfile

from settings import SETTINGS_FILE

BASE_DIR = os.path.dirname(os.path.abspath(SETTINGS_FILE))

log = logging.getLogger("tg2fb.x")

# لا نعلّم الحساب failed إلا عند دليل قطعي على بيانات اعتماد خاطئة أو تعطيل الحساب.
# 403/Forbidden وblocked/denied قد تكون قيود endpoint أو rate-limit وليست دليلاً.
AUTH_HINTS = re.compile(
    r"\b(?:401\s+unauthoriz\w*|invalid\s+(?:credentials|password)|"
    r"incorrect\s+password|wrong\s+password|could not authenticate|"
    r"account\s+(?:(?:is|has\s+been)\s+)?(?:suspend\w*|locked)|"
    r"suspended\s+account)\b",
    re.I,
)

# أسماء استثناءات twikit التي تعني قطعاً مشكلة اعتماد/حساب لا مشكلة endpoint.
AUTH_EXC_NAMES = {
    "Unauthorized", "AccountSuspended", "AccountLocked",
}

ACCOUNT_DISABLED_EXC_NAMES = {"AccountSuspended", "AccountLocked"}
ACCOUNT_DISABLED_HINTS = re.compile(
    r"\b(?:account\s+(?:(?:is|has\s+been)\s+)?(?:suspend\w*|locked)|"
    r"suspended\s+account)\b",
    re.I,
)
SESSION_INVALID_HINTS = re.compile(
    r"\b(?:401|unauthoriz\w*|invalid\s+token|bad\s+token|"
    r"login\s+required|session\s+expired|could not authenticate)\b",
    re.I,
)


def _cookies_path(username):
    safe = re.sub(r"\W+", "_", username or "x")
    return os.path.join(BASE_DIR, f"x_cookies_{safe}.json")


def _restrict(path, *, required=False):
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        if required:
            log.warning("تعذّر تقييد ملف كوكيز X (%s)", type(e).__name__)
            raise


def _save_cookies_atomic(client, path):
    """يحفظ جلسة X كاملة ذرّياً، مع إبقاء الجلسة السابقة عند فشل الكتابة."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, temp_path = tempfile.mkstemp(
        dir=directory,
        prefix=".x-cookies-",
        suffix=".tmp",
    )
    os.close(fd)
    try:
        # mkstemp خاص افتراضياً على POSIX، ونطبّق القيد صراحة قبل وبعد كتابة Twikit.
        _restrict(temp_path, required=True)
        client.save_cookies(temp_path)
        _restrict(temp_path, required=True)
        # Windows يتطلب مقبضاً قابلاً للكتابة كي يقبل fsync.
        with open(temp_path, "r+b") as cookie_file:
            os.fsync(cookie_file.fileno())
        os.replace(temp_path, path)
        _restrict(path)

        # ثبّت إدخال الدليل على Raspberry Pi قدر الإمكان. بعض المنصات (Windows)
        # لا تسمح بفتح الدليل، لذا لا نحول نجاح الاستبدال الذري إلى فشل كاذب.
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


def _cleanup_cookie_temps(directory=None):
    """يحذف بقايا كتابة جلسات X بعد crash، دون اتباع روابط رمزية."""
    directory = os.path.abspath(directory or BASE_DIR)
    try:
        entries = os.scandir(directory)
    except OSError as e:
        log.warning("تعذّر فحص بقايا جلسات X (%s)", type(e).__name__)
        return 0

    removed = 0
    with entries:
        for entry in entries:
            if not (
                entry.name.startswith(".x-cookies-")
                and entry.name.endswith(".tmp")
            ):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                os.remove(entry.path)
                removed += 1
            except OSError as e:
                log.warning("تعذّر حذف بقايا جلسة X (%s)", type(e).__name__)
    if removed:
        log.info("حُذفت %d من بقايا ملفات جلسات X المؤقتة", removed)
    return removed


def _drop_cookies(username):
    """
    يحذف ملف الكوكيز. بدون هذا كانت الكوكيز المنتهية تُحمّل مجدداً إلى ما لا نهاية
    فيبقى الحساب "فاشلاً" حتى بعد الضغط على زر ♻️ إعادة التفعيل.
    """
    path = _cookies_path(username)
    try:
        os.remove(path)
        log.info("حُذفت كوكيز X غير الصالحة")
        return True
    except FileNotFoundError:
        return True
    except OSError as e:
        log.warning("تعذّر حذف ملف جلسة X (%s)", type(e).__name__)
        return False


def is_auth_error(exc):
    if type(exc).__name__ in AUTH_EXC_NAMES:
        return True
    return bool(AUTH_HINTS.search(str(exc)))


def _is_account_disabled(exc):
    return (
        type(exc).__name__ in ACCOUNT_DISABLED_EXC_NAMES
        or bool(ACCOUNT_DISABLED_HINTS.search(str(exc)))
    )


def _is_session_invalid(exc):
    return (
        type(exc).__name__ in AUTH_EXC_NAMES
        or bool(SESSION_INVALID_HINTS.search(str(exc)))
    )


def is_newer(tweet_id, last_id):
    """
    مقارنة رقمية: معرّفات X تصاعدية زمنياً.
    المقارنة بالتساوي (==) كانت تفشل لو حُذفت التغريدة المرجعية أو خرجت من
    نافذة آخر 20 تغريدة، فتُعاد كل التغريدات من جديد.
    """
    if last_id in (None, ""):
        return True
    try:
        return int(tweet_id) > int(last_id)
    except (TypeError, ValueError):
        return str(tweet_id) != str(last_id)


class XReader:
    def __init__(self, settings):
        _cleanup_cookie_temps()
        self.S = settings
        self.client = None
        self.active = None      # اسم الحساب النشط حالياً
        self.ready = False

    def _new_client(self):
        from twikit import Client  # استيراد كسول

        return Client("en-US")

    def invalidate(self):
        """يُبطل الجلسة الحالية ليُعاد اختيار حساب نشط."""
        self.client = None
        self.active = None
        self.ready = False

    def discard_session(self, username):
        """يحذف جلسة دخول لم نستطع تثبيت إعداداتها محلياً."""
        removed = _drop_cookies(username)
        if self.active and self.active.lower() == str(username).lower():
            self.invalidate()
        return removed

    @staticmethod
    async def _verify(client, username):
        """
        نداء خفيف للتأكد أن الكوكيز المحمّلة ما زالت صالحة.
        بدونه كنا نضع ready=True بكوكيز ميتة، فيفشل كل جلب لاحق ويُعلَّم الحساب محظوراً.
        """
        probe = getattr(client, "user", None)
        if callable(probe):
            await probe()
            return
        await client.get_user_by_screen_name(username)

    def _set_active(self, client, username):
        self.client = client
        self.active = username
        self.ready = True
        log.info("تم تفعيل جلسة X")

    def _mark_failed_if_auth(self, username, exc, *, drop_cookies=False):
        """لا يحرق حساباً بسبب timeout/challenge/network عابر."""
        if not is_auth_error(exc):
            return False
        if drop_cookies:
            _drop_cookies(username)
        self.S.mark_x_login_failed(username, True)
        return True

    async def _activate_cookie(self, cred):
        """يحمّل جلسة موجودة فقط؛ لا يلمس كلمة المرور أو مدخلات Twikit."""
        username = cred["username"]
        cpath = _cookies_path(username)
        if not os.path.exists(cpath):
            return False
        client = self._new_client()
        _restrict(cpath)              # يصلح ملفات أُنشئت بصلاحيات مفتوحة سابقاً
        client.load_cookies(cpath)
        await self._verify(client, username)
        self._set_active(client, username)
        return True

    @staticmethod
    def _validate_credential(cred):
        if not isinstance(cred, dict):
            raise TypeError("بيانات دخول X يجب أن تكون قاموساً")
        username = str(cred.get("username") or "").strip().lstrip("@")
        password = cred.get("password")
        if not username or not password:
            raise ValueError("اسم مستخدم X وكلمة المرور مطلوبان")
        return username, cred.get("email") or username, password

    async def login_interactive(self, cred, challenge_handler):
        """
        تسجيل صريح يتيح للواجهة طلب 2FA/challenges من المستخدم عبر callback.

        لا تُحفظ الكوكيز ولا تصبح الجلسة نشطة إلا بعد نجاح تسجيل الدخول ثم نداء
        تحقق مستقل. استيراد xauth كسول كي يبقى poller خالياً تماماً من أي مسار
        قد يطلب password/input.
        """
        if not callable(challenge_handler):
            raise TypeError("challenge_handler يجب أن يكون callable")
        username, auth_info_2, password = self._validate_credential(cred)
        client = self._new_client()
        try:
            from xauth import login_with_challenges

            await login_with_challenges(
                client,
                auth_info_1=username,
                auth_info_2=auth_info_2,
                password=password,
                challenge_handler=challenge_handler,
            )
            await self._verify(client, username)
        except Exception as e:  # noqa: BLE001
            self._mark_failed_if_auth(username, e)
            log.warning("فشل تسجيل X التفاعلي (%s)", type(e).__name__)
            raise

        cpath = _cookies_path(username)
        try:
            # الكوكيز = جلسة دخول X كاملة. نكتب ملفاً مؤقتاً خاصاً بعد verify ثم
            # نستبدل الملف النهائي ذرّياً، فلا نفقد جلسة سابقة عند فشل القرص.
            _save_cookies_atomic(client, cpath)
        except Exception as e:  # noqa: BLE001
            log.warning("تعذّر حفظ جلسة X المتحققة (%s)", type(e).__name__)
            raise

        self._set_active(client, username)
        return True

    async def ensure_login(self, interactive=False, challenge_handler=None):
        """
        يفعّل كوكيز صالحة في الخلفية دون تسجيل بكلمة مرور أو طلب إدخال.

        ``interactive=True`` متاح للتوافق فقط، ويتطلب callback صريحاً ويمر عبر
        login_interactive؛ الاستدعاء الافتراضي الذي يستخدمه poller cookie-only.
        """
        if self.ready and self.client:
            return True
        credentials = self.S.x_logins()
        disabled = set()
        for cred in credentials:
            if cred.get("failed"):
                continue
            try:
                if await self._activate_cookie(cred):
                    return True
            except Exception as e:  # noqa: BLE001
                # 401/session-expired يبطل الجلسة فقط؛ لا يثبت أن كلمة المرور
                # خاطئة. قفل/تعليق الحساب وحده يبرر failed في مسار الخلفية.
                if _is_session_invalid(e):
                    _drop_cookies(cred["username"])
                if _is_account_disabled(e):
                    self.S.mark_x_login_failed(cred["username"], True)
                    disabled.add(cred["username"])
                log.warning("فشل التحقق من كوكيز X (%s)", type(e).__name__)
                self.invalidate()

        if interactive:
            if not callable(challenge_handler):
                raise TypeError(
                    "challenge_handler مطلوب عند interactive=True"
                )
            for cred in credentials:
                if not cred.get("failed") and cred["username"] not in disabled:
                    return await self.login_interactive(cred, challenge_handler)
        return False

    def report_failure(self, exc):
        """يعالج فشل جلسة الجلب دون الخلط بين cookie ميتة وحساب معطّل."""
        session_invalid = _is_session_invalid(exc)
        definitive_auth = is_auth_error(exc)
        if not self.active or not (session_invalid or definitive_auth):
            return False
        username = self.active
        _drop_cookies(username)
        # 401/session-expired أثناء الجلب يثبت بطلان cookie لا بطلان كلمة
        # المرور. أما قفل الحساب أو رسالة credentials/password الصريحة فقط
        # فتسمح بتعطيل بيانات الاعتماد المخزنة.
        if _is_account_disabled(exc) or (definitive_auth and not session_invalid):
            self.S.mark_x_login_failed(username, True)
        self.invalidate()
        return True

    async def resolve(self, screen_name):
        if not await self.ensure_login():
            raise RuntimeError("لا يوجد حساب دخول X صالح")
        user = await self.client.get_user_by_screen_name(screen_name.lstrip("@"))
        return str(user.id), getattr(user, "name", screen_name)

    async def latest_tweet_id(self, user_id):
        """
        أحدث معرّف تغريدة الآن — يُستخدم كنقطة بداية عند إضافة حساب جديد حتى لا
        تُرسل عشرون تغريدة قديمة دفعة واحدة إلى قروب المراجعة.
        """
        try:
            tweets = await self.client.get_user_tweets(user_id, "Tweets", count=1)
        except Exception as e:  # noqa: BLE001
            log.warning("تعذّر جلب أحدث تغريدة من X (%s)", type(e).__name__)
            return None
        for tw in tweets:
            return str(tw.id)
        return None

    @staticmethod
    def _is_reply(tweet):
        """يكتشف إن كانت التغريدة رداً (بأشكال twikit المختلفة)."""
        for attr in ("in_reply_to", "in_reply_to_status_id", "in_reply_to_user_id"):
            if getattr(tweet, attr, None):
                return True
        text = getattr(tweet, "full_text", None) or getattr(tweet, "text", "") or ""
        return text.lstrip().startswith("@")

    @staticmethod
    def select_new(tweets, account, skip_replies=True, limit=5):
        """
        يفرز التغريدات الجديدة (الأقدم أولاً) بحد أقصى limit لكل دورة.
        الفرز بالمقارنة لا بالتوقف عند أول تطابق — أمتن لو تغيّر ترتيب النتائج أو
        حُذفت تغريدة. ما يتجاوز الحد يُلتقط في الدورة التالية لأننا نعالج الأقدم أولاً.
        """
        last_id = account.get("last_id")
        fresh = []
        for tw in tweets:
            if not is_newer(tw.id, last_id):
                continue
            if getattr(tw, "retweeted_tweet", None) is not None:
                continue                              # نتجاهل الريتويت
            if skip_replies and XReader._is_reply(tw):
                continue                              # نتجاهل الردود — تغريدات فقط
            fresh.append(tw)

        def sort_key(tw):
            try:
                return int(tw.id)
            except (TypeError, ValueError):
                return 0

        fresh.sort(key=sort_key)                      # الأقدم أولاً
        return fresh[:limit] if limit else fresh

    async def fetch_new(self, account):
        if not await self.ensure_login():
            raise RuntimeError("لا يوجد حساب دخول X صالح")
        tweets = await self.client.get_user_tweets(account["user_id"], "Tweets", count=20)
        return self.select_new(
            tweets,
            account,
            skip_replies=self.S.get("x_skip_replies", True),
            limit=self.S.get_int("x_max_per_cycle", 5),
        )

    @staticmethod
    def extract_media_urls(tweet):
        """كل وسائط التغريدة — لا الأولى فقط."""
        out = []
        media = getattr(tweet, "media", None) or []
        for m in media:
            mtype = getattr(m, "type", None) or (
                m.get("type") if isinstance(m, dict) else None
            )
            try:
                if mtype == "photo":
                    url = getattr(m, "media_url", None) or (
                        m.get("media_url_https") if isinstance(m, dict) else None
                    )
                    if url:
                        out.append((url, "photo"))
                elif mtype in ("video", "animated_gif"):
                    streams = getattr(m, "streams", None)
                    if streams:
                        best = max(streams, key=lambda s: getattr(s, "bitrate", 0) or 0)
                        url = getattr(best, "url", None)
                        if url:
                            out.append((url, "video"))
            except Exception:  # noqa: BLE001
                continue
        return out
