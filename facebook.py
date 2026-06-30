"""نشر على صفحة فيسبوك عبر Graph API."""
import requests

GRAPH = "https://graph.facebook.com/v19.0"
GRAPH_VIDEO = "https://graph-video.facebook.com/v19.0"


class FacebookPublisher:
    def __init__(self, page_id: str, access_token: str, timeout: int = 120):
        self.page_id = page_id
        self.token = access_token
        self.timeout = timeout

    def post_text(self, message: str) -> dict:
        url = f"{GRAPH}/{self.page_id}/feed"
        r = requests.post(
            url,
            data={"message": message, "access_token": self.token},
            timeout=self.timeout,
        )
        return self._handle(r)

    def post_photo(self, photo_path: str, message: str = "") -> dict:
        url = f"{GRAPH}/{self.page_id}/photos"
        with open(photo_path, "rb") as f:
            r = requests.post(
                url,
                data={"caption": message, "access_token": self.token},
                files={"source": f},
                timeout=self.timeout,
            )
        return self._handle(r)

    def post_video(self, video_path: str, message: str = "") -> dict:
        url = f"{GRAPH_VIDEO}/{self.page_id}/videos"
        with open(video_path, "rb") as f:
            r = requests.post(
                url,
                data={"description": message, "access_token": self.token},
                files={"source": f},
                timeout=self.timeout,
            )
        return self._handle(r)

    @staticmethod
    def _handle(resp: requests.Response) -> dict:
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise
        if resp.status_code >= 400 or "error" in data:
            err = data.get("error", {})
            msg = err.get("message", resp.text)
            raise RuntimeError(f"Facebook API error: {msg}")
        return data
