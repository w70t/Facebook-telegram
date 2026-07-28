"""نشر على صفحة فيسبوك عبر Graph API."""
import json
import logging
import os
import time

import requests

log = logging.getLogger("tg2fb.fb")

DEFAULT_VERSION = "v23.0"
MAX_ALBUM_PHOTOS = 10          # حد فيسبوك للوسائط المرفقة بمنشور واحد
MAX_VIDEO_BYTES = 1024 ** 3    # رفع بسيط (غير مجزّأ) لا يصلح لأكبر من ذلك

# أكواد أخطاء فيسبوك التي تعني "تدخّل المالك مطلوب" لا "أعد المحاولة"
AUTH_ERROR_CODES = {102, 190}          # جلسة/توكن غير صالح أو منتهٍ
PERMISSION_ERROR_CODES = {10, 200, 3, 803}   # صلاحيات ناقصة
# أخطاء عابرة تستحق إعادة المحاولة
TRANSIENT_ERROR_CODES = {1, 2, 4, 17, 32, 341, 368, 613}


class FacebookError(RuntimeError):
    """خطأ نشر عام."""


class FacebookAuthError(FacebookError):
    """
    التوكن منتهٍ أو ناقص الصلاحيات — لا فائدة من إعادة المحاولة.
    توكنات Graph Explorer قصيرة الأجل (~ساعة)؛ لازم تبديلها بتوكن طويل الأجل.
    """


class FacebookPublisher:
    def __init__(self, page_id, access_token, timeout=120, version=DEFAULT_VERSION,
                 retries=3):
        self.page_id = page_id
        self.token = access_token
        self.timeout = timeout
        self.version = version or DEFAULT_VERSION
        self.retries = max(1, retries)

    @property
    def graph(self):
        return f"https://graph.facebook.com/{self.version}"

    @property
    def graph_video(self):
        return f"https://graph-video.facebook.com/{self.version}"

    # --- النشر ---
    def post_text(self, message):
        return self._request(
            f"{self.graph}/{self.page_id}/feed",
            data={"message": message, "access_token": self.token},
        )

    def post_photo(self, photo_path, message=""):
        return self._request(
            f"{self.graph}/{self.page_id}/photos",
            data={"caption": message, "access_token": self.token},
            file_path=photo_path,
        )

    def post_photos(self, photo_paths, message=""):
        """
        عدة صور في منشور واحد (ألبوم): تُرفع كل صورة غير منشورة ثم تُرفق بمنشور
        واحد على الحائط. بدون هذا كان ألبوم تلغرام يتحوّل إلى عدة منشورات منفصلة.
        """
        paths = [p for p in photo_paths if p]
        if not paths:
            return self.post_text(message)
        if len(paths) == 1:
            return self.post_photo(paths[0], message)

        media_ids = []
        for path in paths[:MAX_ALBUM_PHOTOS]:
            result = self._request(
                f"{self.graph}/{self.page_id}/photos",
                data={"published": "false", "access_token": self.token},
                file_path=path,
            )
            if result.get("id"):
                media_ids.append(result["id"])

        if not media_ids:
            raise FacebookError("تعذّر رفع أي صورة للألبوم")

        data = {"message": message, "access_token": self.token}
        for index, media_id in enumerate(media_ids):
            data[f"attached_media[{index}]"] = json.dumps({"media_fbid": media_id})
        return self._request(f"{self.graph}/{self.page_id}/feed", data=data)

    def post_video(self, video_path, message=""):
        size = os.path.getsize(video_path)
        if size > MAX_VIDEO_BYTES:
            raise FacebookError(
                f"حجم الفيديو {size / 1024 ** 3:.1f} GB أكبر من حد الرفع البسيط (1 GB)."
            )
        return self._request(
            f"{self.graph_video}/{self.page_id}/videos",
            data={"description": message, "access_token": self.token},
            file_path=video_path,
        )

    # --- الفحص ---
    def check_token(self):
        """يتحقق أن التوكن ما زال صالحاً — يرمي FacebookAuthError لو انتهى."""
        return self._request(
            f"{self.graph}/{self.page_id}",
            data={"fields": "id,name", "access_token": self.token},
            method="GET",
        )

    # --- الطبقة الدنيا ---
    def _request(self, url, data, file_path=None, method="POST"):
        last_error = None
        for attempt in range(self.retries):
            try:
                if method == "GET":
                    resp = requests.get(url, params=data, timeout=self.timeout)
                elif file_path:
                    with open(file_path, "rb") as f:
                        resp = requests.post(
                            url, data=data, files={"source": f}, timeout=self.timeout
                        )
                else:
                    resp = requests.post(url, data=data, timeout=self.timeout)
            except requests.RequestException as e:
                last_error = FacebookError(f"تعذّر الاتصال بفيسبوك: {e}")
            else:
                try:
                    return self._handle(resp)
                except FacebookAuthError:
                    raise                       # لا فائدة من إعادة المحاولة
                except FacebookError as e:
                    if not getattr(e, "transient", False):
                        raise
                    last_error = e

            if attempt < self.retries - 1:
                delay = 2 ** attempt
                log.warning("فشل مؤقت مع فيسبوك (%s) — إعادة بعد %ss", last_error, delay)
                time.sleep(delay)
        raise last_error

    @staticmethod
    def _handle(resp):
        try:
            data = resp.json()
        except ValueError:
            error = FacebookError(
                f"رد غير متوقع من فيسبوك ({resp.status_code}): {resp.text[:200]}"
            )
            error.transient = resp.status_code >= 500
            raise error

        if resp.status_code < 400 and "error" not in data:
            return data

        err = data.get("error", {}) or {}
        code = err.get("code")
        message = err.get("message") or resp.text[:200]

        if code in AUTH_ERROR_CODES:
            raise FacebookAuthError(
                f"توكن الصفحة منتهٍ أو غير صالح (code {code}): {message}"
            )
        if code in PERMISSION_ERROR_CODES:
            raise FacebookAuthError(
                f"صلاحيات ناقصة (code {code}): {message}\n"
                "أعد توليد التوكن مع pages_manage_posts و pages_read_engagement."
            )

        error = FacebookError(f"Facebook API error ({code}): {message}")
        error.transient = code in TRANSIENT_ERROR_CODES or resp.status_code >= 500
        raise error
