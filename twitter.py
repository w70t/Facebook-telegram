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

# كلمات تدل على مشكلة مصادقة/حظر (لتمييزها عن أخطاء الشبكة العابرة)
AUTH_HINTS = re.compile(
    r"unauthoriz|401|403|forbidden|suspend|ban|locked|could not authenticate|"
    r"denied|blocked|not authorized",
    re.I,
)


def _cookies_path(username):
    safe = re.sub(r"\W+", "_", username or "x")
    return os.path.join(BASE_DIR, f"x_cookies_{safe}.json")


def is_auth_error(exc):
    return bool(AUTH_HINTS.search(str(exc)))


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

    async def _activate(self, cred):
        username = cred["username"]
        client = self._new_client()
        cpath = _cookies_path(username)
        if os.path.exists(cpath):
            client.load_cookies(cpath)
        else:
            await client.login(
                auth_info_1=username,
                auth_info_2=cred.get("email") or username,
                password=cred["password"],
            )
            client.save_cookies(cpath)
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
                self.S.mark_x_login_failed(cred["username"], True)
                self.invalidate()
        return False

    def report_failure(self, exc):
        """يُستدعى عند خطأ أثناء الجلب: يعلّم الحساب النشط كمحظور إن كان خطأ مصادقة."""
        if self.active and is_auth_error(exc):
            self.S.mark_x_login_failed(self.active, True)
            self.invalidate()
            return True
        return False

    async def resolve(self, screen_name):
        await self.ensure_login()
        user = await self.client.get_user_by_screen_name(screen_name.lstrip("@"))
        return str(user.id), getattr(user, "name", screen_name)

    async def fetch_new(self, account):
        await self.ensure_login()
        tweets = await self.client.get_user_tweets(
            account["user_id"], "Tweets", count=20
        )
        last_id = account.get("last_id")
        fresh = []
        for tw in tweets:
            tid = str(tw.id)
            if last_id is not None and tid == str(last_id):
                break
            if getattr(tw, "retweeted_tweet", None) is not None:
                continue
            fresh.append(tw)
        fresh.reverse()
        return fresh

    @staticmethod
    def extract_media_urls(tweet):
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
