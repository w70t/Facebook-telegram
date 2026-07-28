import json

import pytest

from facebook import FacebookAuthError, FacebookError, FacebookPublisher


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)

    def json(self):
        if isinstance(self._payload, dict):
            return self._payload
        raise ValueError("not json")


def _handle(payload, status=200):
    return FacebookPublisher._handle(FakeResponse(payload, status))


def test_success_returns_payload():
    assert _handle({"id": "123_456"}) == {"id": "123_456"}


def test_expired_token_raises_auth_error():
    """توكن Graph Explorer ينتهي خلال ~ساعة — لازم يُميَّز عن الأخطاء العابرة."""
    with pytest.raises(FacebookAuthError, match="190"):
        _handle({"error": {"code": 190, "message": "Session has expired"}}, 400)


def test_missing_permission_raises_auth_error():
    with pytest.raises(FacebookAuthError, match="pages_manage_posts"):
        _handle({"error": {"code": 200, "message": "Permissions error"}}, 403)


def test_transient_error_is_marked_retryable():
    with pytest.raises(FacebookError) as exc:
        _handle({"error": {"code": 2, "message": "Service temporarily unavailable"}}, 500)
    assert exc.value.transient is True
    assert not isinstance(exc.value, FacebookAuthError)


def test_permanent_error_is_not_retryable():
    with pytest.raises(FacebookError) as exc:
        _handle({"error": {"code": 100, "message": "Invalid parameter"}}, 400)
    assert getattr(exc.value, "transient", False) is False


def test_non_json_server_error_is_retryable():
    with pytest.raises(FacebookError) as exc:
        _handle("<html>502 Bad Gateway</html>", 502)
    assert exc.value.transient is True


def test_graph_version_is_configurable():
    fb = FacebookPublisher("1", "tok", version="v24.0")
    assert fb.graph.endswith("/v24.0")
    assert fb.graph_video.startswith("https://graph-video.facebook.com/")
    assert FacebookPublisher("1", "tok").graph.endswith("/v23.0")


def test_retries_then_succeeds(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        if len(calls) < 3:
            return FakeResponse({"error": {"code": 2, "message": "temporary"}}, 500)
        return FakeResponse({"id": "ok"})

    monkeypatch.setattr("facebook.requests.post", fake_post)
    monkeypatch.setattr("facebook.time.sleep", lambda *_: None)

    assert FacebookPublisher("1", "tok").post_text("مرحبا") == {"id": "ok"}
    assert len(calls) == 3


def test_auth_error_is_not_retried(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return FakeResponse({"error": {"code": 190, "message": "expired"}}, 401)

    monkeypatch.setattr("facebook.requests.post", fake_post)
    monkeypatch.setattr("facebook.time.sleep", lambda *_: None)

    with pytest.raises(FacebookAuthError):
        FacebookPublisher("1", "tok").post_text("مرحبا")
    assert len(calls) == 1


def test_album_attaches_all_photos(tmp_path, monkeypatch):
    """ألبوم تلغرام كان يتحوّل إلى عدة منشورات منفصلة على فيسبوك."""
    paths = []
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        p = tmp_path / name
        p.write_bytes(b"img")
        paths.append(str(p))

    requests_made = []

    def fake_post(url, data=None, files=None, timeout=None):
        requests_made.append((url, data))
        if url.endswith("/photos"):
            return FakeResponse({"id": f"media{len(requests_made)}"})
        return FakeResponse({"id": "post_1"})

    monkeypatch.setattr("facebook.requests.post", fake_post)

    result = FacebookPublisher("PAGE", "tok").post_photos(paths, "التعليق")
    assert result == {"id": "post_1"}

    uploads = [r for r in requests_made if r[0].endswith("/photos")]
    assert len(uploads) == 3
    assert all(r[1]["published"] == "false" for r in uploads)

    feed_url, feed_data = requests_made[-1]
    assert feed_url.endswith("/PAGE/feed")
    assert feed_data["message"] == "التعليق"
    assert json.loads(feed_data["attached_media[0]"]) == {"media_fbid": "media1"}
    assert json.loads(feed_data["attached_media[2]"]) == {"media_fbid": "media3"}


def test_single_photo_uses_simple_upload(tmp_path, monkeypatch):
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"img")
    urls = []

    def fake_post(url, data=None, files=None, timeout=None):
        urls.append(url)
        return FakeResponse({"id": "1"})

    monkeypatch.setattr("facebook.requests.post", fake_post)
    FacebookPublisher("PAGE", "tok").post_photos([str(photo)], "نص")
    assert urls == ["https://graph.facebook.com/v23.0/PAGE/photos"]


def test_oversized_video_rejected_before_upload(tmp_path, monkeypatch):
    video = tmp_path / "big.mp4"
    video.write_bytes(b"0")
    monkeypatch.setattr("facebook.MAX_VIDEO_BYTES", 0)
    with pytest.raises(FacebookError, match="حد الرفع"):
        FacebookPublisher("PAGE", "tok").post_video(str(video))
