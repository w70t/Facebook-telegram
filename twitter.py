"""
قارئ X (تويتر) غير رسمي عبر مكتبة twikit.
يدعم مجموعة حسابات دخول: لو انحظر حساب يبدّل تلقائياً للي بعده.

ملاحظة: twikit غير رسمية وقد تتغيّر واجهتها؛ الكود مكتوب دفاعياً.
"""
import logging
import os
import re

from settings import BASE_DIR

log = logging.getLogger("tg2fb.x")

# كلمات تدل على مشكلة مصادقة/حظر (لتمييزها عن أخطاء الشبكة العابرة).
# \b ضرورية: بدونها كان "bandwidth" و"banner" يطابقان "ban" فيُحرق حساب سليم.
AUTH_HINTS = re.compile(
    r"\b(?:401|403|unauthoriz\w*|forbidden|suspend\w*|banned|ban|locked|"
    r"could not authenticate|denied|blocked|not\s+authorized|"
    r"invalid\s+token|bad\s+token|login\s+required|session\s+expired)\b",
    re.I,
)

# أسماء استثناءات twikit التي تعني قطعاً مشكلة حساب لا مشكلة شبكة
AUTH_EXC_NAMES = {
    "Unauthorized", "Forbidden", "AccountSuspended", "AccountLocked",
    "UserUnavailable", "CouldNotTweet",
}


def _cookies_path(username):
    safe = re.sub(r"\W+", "_", username or "x")
    return os.path.join(BASE_DIR, f"x_cookies_{safe}.json")


def _drop_cookies(username):
    """
    يحذف ملف الكوكيز. بدون هذا كانت الكوكيز المنتهية تُحمّل مجدداً إلى ما لا نهاية
    فيبقى الحساب "فاشلاً" حتى بعد الضغط على زر ♻️ إعادة التفعيل.
    """
    path = _cookies_path(username)
    try:
        os.remove(path)
        log.info("حُذفت كوكيز X المنتهية لـ %s", username)
    except OSError:
        pass


def is_auth_error(exc):
    if type(exc).__name__ in AUTH_EXC_NAMES:
        return True
    return bool(AUTH_HINTS.search(str(exc)))


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

    async def _activate(self, cred):
        username = cred["username"]
        client = self._new_client()
        cpath = _cookies_path(username)
        authenticated = False

        if os.path.exists(cpath):
            try:
                client.load_cookies(cpath)
                await self._verify(client, username)
                authenticated = True
            except Exception as e:  # noqa: BLE001
                log.warning("كوكيز X لـ %s غير صالحة (%s) — إعادة تسجيل دخول", username, e)
                _drop_cookies(username)
                client = self._new_client()

        if not authenticated:
            await client.login(
                auth_info_1=username,
                auth_info_2=cred.get("email") or username,
                password=cred["password"],
            )
            client.save_cookies(cpath)
            try:
                os.chmod(cpath, 0o600)   # الكوكيز = جلسة دخول كاملة
            except OSError:
                pass

        self.client = client
        self.active = username
        self.ready = True
        log.info("حساب X النشط: %s", username)

    async def ensure_login(self):
        """يضمن جلسة صالحة، ويتنقّل بين الحسابات عند فشل الدخول."""
        if self.ready and self.client:
            return True
        for cred in self.S.x_logins():
            if cred.get("failed"):
                continue
            try:
                await self._activate(cred)
                return True
            except Exception as e:  # noqa: BLE001
                log.warning("فشل دخول X %s: %s", cred["username"], e)
                _drop_cookies(cred["username"])
                self.S.mark_x_login_failed(cred["username"], True)
                self.invalidate()
        return False

    def report_failure(self, exc):
        """يُستدعى عند خطأ أثناء الجلب: يعلّم الحساب النشط كمحظور إن كان خطأ مصادقة."""
        if self.active and is_auth_error(exc):
            _drop_cookies(self.active)
            self.S.mark_x_login_failed(self.active, True)
            self.invalidate()
            return True
        return False

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
            log.warning("تعذّر جلب أحدث تغريدة لـ %s: %s", user_id, e)
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
