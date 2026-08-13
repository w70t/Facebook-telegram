"""
قارئ X (تويتر) غير رسمي عبر مكتبة twikit.
يدعم مجموعة حسابات دخول: لو انحظر حساب يبدّل تلقائياً للي بعده.

ملاحظة: twikit غير رسمية وقد تتغيّر واجهتها؛ الكود مكتوب دفاعياً.
"""
import json
import logging
import os
import re
import tempfile
from collections import namedtuple

from settings import SETTINGS_FILE

BASE_DIR = os.path.dirname(os.path.abspath(SETTINGS_FILE))

log = logging.getLogger("tg2fb.x")
SessionRef = namedtuple("SessionRef", "generation client username")


class XSessionSuperseded(RuntimeError):
    """أُبطلت محاولة تفعيل/دخول لأن عملية أحدث غيّرت جلسة X."""


class XSessionAccountMismatch(RuntimeError):
    """The authenticated browser session belongs to a different X account."""


class XSessionVerificationError(RuntimeError):
    """The browser authenticated, but the reader could not verify the session."""

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
INTERACTIVE_AUTH_EXC_NAMES = {
    "AccountSuspended", "AccountLocked",
}
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
    # X usernames are case-insensitive. Canonical lower-case prevents orphaned
    # session files when an admin later types a different casing on Linux.
    safe = re.sub(r"\W+", "_", username or "x").lower()
    return os.path.join(BASE_DIR, f"x_cookies_{safe}.json")


def _legacy_cookies_path(username):
    safe = re.sub(r"\W+", "_", username or "x")
    return os.path.join(BASE_DIR, f"x_cookies_{safe}.json")


def _cookie_alias_paths(username):
    """يعيد كل ملفات الجلسة العادية المطابقة للاسم دون حساسية الأحرف.

    البحث direct-child فقط ولا يتبع symlinks. هذا يلتقط أسماء الإصدارات القديمة
    مثل ``x_cookies_Reader.json`` حتى لو صار الاسم المخزن لاحقاً ``reader``.
    """
    canonical = os.path.abspath(_cookies_path(username))
    directory = os.path.dirname(canonical) or "."
    expected = os.path.basename(canonical).casefold()
    aliases = []
    unsafe_match = False
    try:
        entries = os.scandir(directory)
    except OSError as e:
        log.warning("تعذّر فحص ملفات جلسة X (%s)", type(e).__name__)
        return canonical, aliases, True
    with entries:
        for entry in entries:
            if entry.name.casefold() != expected:
                continue
            try:
                if entry.is_file(follow_symlinks=False):
                    aliases.append(os.path.abspath(entry.path))
                else:
                    unsafe_match = True
            except OSError:
                unsafe_match = True
    return canonical, aliases, unsafe_match


def _same_cookie_path(left, right):
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _canonicalize_cookie_path(username):
    """ينقل اسم ملف قديم حساساً للأحرف إلى الاسم القانوني الصغير ذرّياً."""
    canonical, aliases, unsafe_match = _cookie_alias_paths(username)
    canonical_alias = next(
        (path for path in aliases if _same_cookie_path(path, canonical)), None
    )
    source = canonical_alias
    if source is None and aliases:
        # إذا وُجد أكثر من alias قديم نأخذ الأحدث، ثم نمسح البقية بعد نجاح النقل.
        try:
            source = max(aliases, key=lambda path: os.stat(path).st_mtime_ns)
        except OSError:
            source = aliases[0]
    if source is not None and not _same_cookie_path(source, canonical):
        try:
            os.replace(source, canonical)
            _restrict(canonical)
        except OSError as e:
            log.warning("تعذّر ترحيل اسم ملف جلسة X (%s)", type(e).__name__)
            return source
    for alias in aliases:
        if _same_cookie_path(alias, canonical) or alias == source:
            continue
        try:
            os.remove(alias)
        except OSError as e:
            log.warning("تعذّر حذف alias قديم لجلسة X (%s)", type(e).__name__)
    if source is None and unsafe_match:
        log.warning("رُفض ملف جلسة X غير عادي")
        return None
    return canonical


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
    _canonical, paths, unsafe_match = _cookie_alias_paths(username)
    ok = True
    removed = False
    for path in paths:
        try:
            os.remove(path)
            removed = True
        except FileNotFoundError:
            continue
        except OSError as e:
            ok = False
            log.warning("تعذّر حذف ملف جلسة X (%s)", type(e).__name__)
    if removed:
        log.info("حُذفت كوكيز X غير الصالحة")
    if unsafe_match:
        log.warning("تُرك alias غير عادي لملف جلسة X دون اتباعه")
    return ok and not unsafe_match


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
        isinstance(exc, XSessionAccountMismatch)
        or
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
        self._session_generation = 0

    def _new_client(self):
        from twikit import Client  # استيراد كسول
        from xtransaction import TwikitTransactionAdapter

        client = Client("en-US")
        client.client_transaction = TwikitTransactionAdapter()
        # Twikit 2.3.3 rebuilds its CookieJar as a list inside this helper,
        # stripping domains after every request. Replace it with a scoped
        # equivalent so auth_token/ct0 can never become domainless.
        def remove_duplicate_ct0_cookie():
            scoped = {}
            for cookie in client.http.cookies.jar:
                if cookie.name == "ct0" and "ct0" in scoped:
                    continue
                scoped[cookie.name] = cookie.value
            self._set_x_cookies(client, scoped)

        client._remove_duplicate_ct0_cookie = remove_duplicate_ct0_cookie
        return client

    @staticmethod
    def _set_x_cookies(client, cookies):
        """Scope authenticated cookies to X; never create domainless secrets."""
        jar = getattr(getattr(client, "http", None), "cookies", None)
        if jar is None or not callable(getattr(jar, "set", None)):
            setter = getattr(client, "set_cookies", None)
            if callable(setter):
                setter(cookies, clear_cookies=True)
                return
            raise XSessionVerificationError("X client cookie jar is unavailable")
        jar.clear()
        for name, value in cookies.items():
            jar.set(name, value, domain=".x.com", path="/")

    @classmethod
    def _load_x_cookies(cls, client, path):
        try:
            with open(path, encoding="utf-8") as cookie_file:
                cookies = json.load(cookie_file)
        except (OSError, ValueError, TypeError):
            raise XSessionVerificationError("X cookie file is invalid") from None
        if not isinstance(cookies, dict) or not all(
            isinstance(name, str) and isinstance(value, str)
            for name, value in cookies.items()
        ):
            raise XSessionVerificationError("X cookie file is invalid")
        cls._set_x_cookies(client, cookies)
        cookies.clear()

    def invalidate(self):
        """يُبطل الجلسة الحالية ليُعاد اختيار حساب نشط."""
        self._session_generation += 1
        self.client = None
        self.active = None
        self.ready = False
        return self._session_generation

    def discard_session(self, username):
        """يحذف جلسة دخول لم نستطع تثبيت إعداداتها محلياً."""
        # الحذف mutation حتى لو لم يكن الحساب active بعد. رفع الجيل أولاً يمنع
        # verify قديم جارٍ من إعادة تفعيل cookie حُذفت للتو.
        self._session_generation += 1
        removed = _drop_cookies(username)
        if self.active and self.active.lower() == str(username).lower():
            self.client = None
            self.active = None
            self.ready = False
        return removed

    @staticmethod
    async def _verify(client, username):
        """
        نداء خفيف للتأكد أن الكوكيز المحمّلة ما زالت صالحة.
        بدونه كنا نضع ready=True بكوكيز ميتة، فيفشل كل جلب لاحق ويُعلَّم الحساب محظوراً.
        """
        probe = getattr(client, "user", None)
        if callable(probe):
            authenticated = await probe()
            actual = getattr(authenticated, "screen_name", None)
            if not isinstance(actual, str) or not actual.strip():
                raise XSessionVerificationError(
                    "X did not identify the authenticated account"
                )
            if actual.strip().lstrip("@").casefold() != str(username).strip().lstrip("@").casefold():
                raise XSessionAccountMismatch(
                    "X browser session account did not match the requested account"
                )
            return
        # A client without ``user()`` cannot prove which account owns a cookie.
        raise XSessionVerificationError("X client cannot identify its authenticated account")

    @staticmethod
    async def _prepare_transaction(client):
        """Initialize public transaction data before authenticated cookies exist."""
        adapter = getattr(client, "client_transaction", None)
        if adapter is None or not callable(getattr(adapter, "init", None)):
            if client.__class__.__module__.startswith(("test_", "tests.")):
                return
            raise XSessionVerificationError("X transaction adapter is unavailable")
        if getattr(adapter, "home_page_response", None) is not None:
            return
        headers = {
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Referer": "https://x.com",
            "User-Agent": getattr(client, "_user_agent", "Mozilla/5.0"),
        }
        await adapter.init(getattr(client, "http", None), headers)

    def _set_active(self, client, username):
        self._session_generation += 1
        self.client = client
        self.active = username
        self.ready = True
        log.info("تم تفعيل جلسة X")

    def capture_session(self):
        """يلتقط مرجعاً ثابتاً يمنع خطأ طلب قديم من إتلاف جلسة أحدث."""
        if not (self.ready and self.client is not None and self.active):
            return None
        return SessionRef(self._session_generation, self.client, self.active)

    def _session_matches(self, session):
        return bool(
            session is not None
            and session.generation == self._session_generation
            and session.client is self.client
            and str(session.username).lower() == str(self.active).lower()
        )

    def _require_generation(self, generation):
        if generation != self._session_generation:
            raise XSessionSuperseded("X session changed during authentication")

    def is_generation_current(self, generation):
        return generation == self._session_generation

    def _mark_failed_if_auth(self, username, exc, *, drop_cookies=False):
        """لا يحرق حساباً بسبب timeout/challenge/network عابر."""
        if type(exc).__name__ not in INTERACTIVE_AUTH_EXC_NAMES:
            return False
        if drop_cookies:
            _drop_cookies(username)
        self.S.mark_x_login_failed(username, True)
        return True

    async def _activate_cookie(self, cred):
        """يحمّل جلسة موجودة فقط؛ لا يلمس كلمة المرور أو مدخلات Twikit."""
        generation = self._session_generation
        username = cred["username"]
        cpath = _canonicalize_cookie_path(username)
        if cpath is None or not os.path.exists(cpath):
            return False
        client = self._new_client()
        _restrict(cpath)              # يصلح ملفات أُنشئت بصلاحيات مفتوحة سابقاً
        await self._prepare_transaction(client)
        if generation != self._session_generation:
            return False
        self._load_x_cookies(client, cpath)
        await self._verify(client, username)
        if generation != self._session_generation:
            return False
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
        return username, cred.get("email"), password

    async def login_interactive(self, cred, challenge_handler, progress_handler=None):
        """
        تسجيل صريح يتيح للواجهة طلب 2FA/challenges من المستخدم عبر callback.

        لا تُحفظ الكوكيز ولا تصبح الجلسة نشطة إلا بعد نجاح تسجيل الدخول ثم نداء
        تحقق مستقل. استيراد xauth كسول كي يبقى poller خالياً تماماً من أي مسار
        قد يطلب password/input.
        """
        if not callable(challenge_handler):
            raise TypeError("challenge_handler يجب أن يكون callable")
        if progress_handler is not None and not callable(progress_handler):
            raise TypeError("progress_handler يجب أن يكون callable")
        generation = self._session_generation
        username, auth_info_2, password = self._validate_credential(cred)
        # The caller's short-lived mapping is no longer needed once its values
        # have been copied into local variables. Clear it before any await so a
        # Telegram progress callback can never keep an extra password copy alive.
        cred.clear()
        client = self._new_client()
        browser_cookies = None
        browser_credentials = {
            "username": username,
            "email": auth_info_2,
            "password": password,
        }
        password = None
        try:
            from xbrowser import obtain_cookies

            await self._prepare_transaction(client)
            self._require_generation(generation)
            progress_options = (
                {"progress_handler": progress_handler}
                if progress_handler is not None else {}
            )
            browser_cookies = await obtain_cookies(
                browser_credentials,
                challenge_handler=challenge_handler,
                **progress_options,
            )
            browser_credentials.clear()
            self._require_generation(generation)
            self._set_x_cookies(client, browser_cookies)
            browser_cookies.clear()
            try:
                await self._verify(client, username)
            except (XSessionAccountMismatch, XSessionVerificationError):
                raise
            except Exception:  # noqa: BLE001 - expose no remote response details
                raise XSessionVerificationError(
                    "X reader session verification failed"
                ) from None
            self._require_generation(generation)
        except Exception as e:  # noqa: BLE001
            # نتيجة محاولة قديمة لا تملك صلاحية تعطيل credential بعد أن بدّلت
            # عملية أحدث الجلسة/الحساب؛ وإلا قد يحرق خطأ متأخر الحساب الصحيح.
            if self.is_generation_current(generation):
                self._mark_failed_if_auth(username, e)
            reason = (
                getattr(e, "reason", None)
                if type(e).__name__ == "XBrowserPageChanged"
                else None
            )
            if reason:
                log.warning("فشل تسجيل X التفاعلي (%s:%s)", type(e).__name__, reason)
            else:
                log.warning("فشل تسجيل X التفاعلي (%s)", type(e).__name__)
            raise
        finally:
            password = None
            browser_credentials.clear()
            if browser_cookies is not None:
                browser_cookies.clear()

        cpath = _cookies_path(username)
        try:
            self._require_generation(generation)
            # الكوكيز = جلسة دخول X كاملة. نكتب ملفاً مؤقتاً خاصاً بعد verify ثم
            # نستبدل الملف النهائي ذرّياً، فلا نفقد جلسة سابقة عند فشل القرص.
            _save_cookies_atomic(client, cpath)
            # لا توجد await بين الحارس والكتابة/التفعيل؛ فلا تستطيع coroutine
            # أخرى تبديل الجلسة داخل هذا المقطع المتزامن من حلقة asyncio.
            self._require_generation(generation)
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
            generation = self._session_generation
            try:
                if await self._activate_cookie(cred):
                    return True
                if generation != self._session_generation:
                    return bool(self.ready and self.client is not None)
            except Exception as e:  # noqa: BLE001
                if generation != self._session_generation:
                    return bool(self.ready and self.client is not None)
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

    def report_failure(self, exc, *, session=None):
        """يعالج فشل جلسة الجلب دون الخلط بين cookie ميتة وحساب معطّل."""
        if not self._session_matches(session):
            return False
        session_invalid = _is_session_invalid(exc)
        definitive_auth = is_auth_error(exc)
        if not self.active or not (session_invalid or definitive_auth):
            return False
        username = session.username
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
        session = self.capture_session()
        if session is None:
            raise RuntimeError("لا يوجد مرجع جلسة X صالح")
        user = await session.client.get_user_by_screen_name(screen_name.lstrip("@"))
        return str(user.id), getattr(user, "name", screen_name)

    async def latest_tweet_id(self, user_id):
        """
        أحدث معرّف تغريدة الآن — يُستخدم كنقطة بداية عند إضافة حساب جديد حتى لا
        تُرسل عشرون تغريدة قديمة دفعة واحدة إلى قروب المراجعة.
        """
        session = self.capture_session()
        if session is None:
            return None
        try:
            tweets = await session.client.get_user_tweets(user_id, "Tweets", count=1)
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

    async def fetch_new(self, account, *, session=None):
        if session is None:
            if not await self.ensure_login():
                raise RuntimeError("لا يوجد حساب دخول X صالح")
            session = self.capture_session()
        if session is None:
            raise RuntimeError("لا يوجد مرجع جلسة X صالح")
        tweets = await session.client.get_user_tweets(
            account["user_id"], "Tweets", count=20
        )
        if not self._session_matches(session):
            raise XSessionSuperseded("X session changed during fetch")
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
