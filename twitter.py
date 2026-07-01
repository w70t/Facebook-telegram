"""
قارئ X (تويتر) غير رسمي عبر مكتبة twikit.
يسجّل دخول حساب X ثانوي، ويجلب التغريدات الجديدة للحسابات المتابَعة.

ملاحظة: twikit غير رسمية وقد تتغيّر واجهتها؛ الكود مكتوب دفاعياً.
"""
import logging
import os

from settings import BASE_DIR

log = logging.getLogger("tg2fb.x")

COOKIES_FILE = os.path.join(BASE_DIR, "x_cookies.json")


class XReader:
    def __init__(self, settings):
        self.S = settings
        self.client = None
        self.ready = False

    def _new_client(self):
        from twikit import Client  # استيراد كسول حتى لا يفشل المشروع لو غير مثبّت

        return Client("en-US")

    async def login(self, username, email, password):
        """تسجيل دخول جديد وحفظ الكوكيز."""
        self.client = self._new_client()
        await self.client.login(
            auth_info_1=username, auth_info_2=email or username, password=password
        )
        self.client.save_cookies(COOKIES_FILE)
        self.ready = True
        log.info("تم تسجيل دخول X: %s", username)

    async def ensure_login(self):
        """يضمن جلسة صالحة: كوكيز محفوظة أو بيانات مخزّنة."""
        if self.ready and self.client:
            return True
        if self.client is None:
            self.client = self._new_client()
        if os.path.exists(COOKIES_FILE):
            self.client.load_cookies(COOKIES_FILE)
            self.ready = True
            return True
        u, e, p = self.S.get("x_username"), self.S.get("x_email"), self.S.get("x_password")
        if u and p:
            await self.login(u, e, p)
            return True
        return False

    async def resolve(self, screen_name):
        """يحوّل @اسم إلى (user_id, الاسم الظاهر)."""
        await self.ensure_login()
        user = await self.client.get_user_by_screen_name(screen_name.lstrip("@"))
        return str(user.id), getattr(user, "name", screen_name)

    async def fetch_new(self, account):
        """يرجّع التغريدات الأحدث من last_id (الأقدم أولاً)."""
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
            # نتجاهل الريتويت لتفادي التكرار
            if getattr(tw, "retweeted_tweet", None) is not None:
                continue
            fresh.append(tw)
        fresh.reverse()  # الأقدم أولاً
        return fresh

    @staticmethod
    def extract_media_urls(tweet):
        """يرجّع [(url, 'photo'|'video')] بشكل دفاعي لاختلاف نسخ twikit."""
        out = []
        media = getattr(tweet, "media", None) or []
        for m in media:
            mtype = getattr(m, "type", None) or (m.get("type") if isinstance(m, dict) else None)
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
                        best = max(
                            streams,
                            key=lambda s: getattr(s, "bitrate", 0) or 0,
                        )
                        url = getattr(best, "url", None)
                        if url:
                            out.append((url, "video"))
            except Exception:  # noqa: BLE001
                continue
        return out
