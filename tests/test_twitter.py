import asyncio
import os
import stat
import sys
import types

import pytest

import twitter
from twitter import XReader, is_auth_error, is_newer


class FakeTweet:
    def __init__(self, tid, text="نص", reply=False, retweet=False):
        self.id = tid
        self.full_text = text
        self.in_reply_to = tid if reply else None
        self.retweeted_tweet = object() if retweet else None


# --- تمييز أخطاء المصادقة عن أخطاء الشبكة ---
@pytest.mark.parametrize("message", [
    "401 Unauthorized",
    "Your account has been suspended",
    "Account is locked",
    "Could not authenticate you",
    "invalid credentials",
    "incorrect password",
])
def test_real_auth_errors_detected(message):
    assert is_auth_error(Exception(message))


@pytest.mark.parametrize("message", [
    "bandwidth limit exceeded",       # كان يطابق "ban" ويحرق حساباً سليماً
    "banner image failed to load",
    "Connection aborted, timeout",
    "Rate limit exceeded (429)",
    "Temporary failure in name resolution",
    "403 Forbidden",
    "request was blocked by this endpoint",
    "session expired",
])
def test_transient_errors_are_not_auth_errors(message):
    assert not is_auth_error(Exception(message))


def test_auth_error_detected_by_exception_class():
    class Unauthorized(Exception):
        pass

    assert is_auth_error(Unauthorized("مشكلة"))


# --- فصل جلسات الخلفية عن تسجيل الدخول التفاعلي ---
class FakeSettings:
    def __init__(self, credentials):
        self.credentials = credentials
        self.marked = []

    def x_logins(self):
        return [dict(cred) for cred in self.credentials]

    def mark_x_login_failed(self, username, failed=True):
        self.marked.append((username, failed))


class FakeClient:
    def __init__(self, events=None, verify_error=None, save_error=None):
        self.events = events if events is not None else []
        self.verify_error = verify_error
        self.save_error = save_error
        self.loaded = []
        self.saved = []
        self.password_login_calls = 0

    def load_cookies(self, path):
        self.events.append("load")
        self.loaded.append(path)

    async def user(self):
        self.events.append("verify")
        if self.verify_error:
            raise self.verify_error

    async def login(self, **kwargs):
        self.password_login_calls += 1
        raise AssertionError("background must never call Twikit.login")

    def save_cookies(self, path):
        self.events.append("save")
        self.saved.append(path)
        if self.save_error:
            raise self.save_error
        with open(path, "w", encoding="utf-8") as cookie_file:
            cookie_file.write("{}")


def _reader(monkeypatch, tmp_path, client, credentials=None):
    credentials = credentials or [{
        "username": "account", "email": None, "password": "top-secret",
        "failed": False,
    }]
    settings = FakeSettings(credentials)
    reader = XReader(settings)
    monkeypatch.setattr(reader, "_new_client", lambda: client)
    monkeypatch.setattr(
        twitter, "_cookies_path", lambda username: str(tmp_path / "cookies.json")
    )
    return reader, settings


def test_startup_removes_only_regular_atomic_cookie_temps(tmp_path):
    stale = tmp_path / ".x-cookies-deadbeef.tmp"
    unrelated = tmp_path / "notes.tmp"
    stale.write_text("full-session-secret", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")

    assert twitter._cleanup_cookie_temps(str(tmp_path)) == 1
    assert not stale.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_background_missing_cookies_never_password_logs_in(monkeypatch, tmp_path):
    client = FakeClient()
    reader, settings = _reader(monkeypatch, tmp_path, client)

    assert asyncio.run(reader.ensure_login()) is False
    assert client.password_login_calls == 0
    assert client.events == []
    assert settings.marked == []


def test_background_valid_cookies_are_verified(monkeypatch, tmp_path):
    cookie = tmp_path / "cookies.json"
    cookie.write_text("{}", encoding="utf-8")
    client = FakeClient()
    reader, settings = _reader(monkeypatch, tmp_path, client)

    assert asyncio.run(reader.ensure_login()) is True
    assert client.events == ["load", "verify"]
    assert client.password_login_calls == 0
    assert settings.marked == []
    assert reader.client is client
    assert reader.active == "account"


def test_background_invalid_session_drops_cookie_without_marking_failed(
    monkeypatch, tmp_path,
):
    class Unauthorized(Exception):
        pass

    cookie = tmp_path / "cookies.json"
    cookie.write_text("{}", encoding="utf-8")
    client = FakeClient(verify_error=Unauthorized("redacted"))
    reader, settings = _reader(monkeypatch, tmp_path, client)

    assert asyncio.run(reader.ensure_login()) is False
    assert settings.marked == []
    assert not cookie.exists()
    assert client.password_login_calls == 0


@pytest.mark.parametrize("error", [
    TimeoutError("challenge timed out"),
    ConnectionError("network unavailable"),
    Exception("403 Forbidden"),
])
def test_background_transient_error_keeps_account_and_cookies(
    monkeypatch, tmp_path, error,
):
    cookie = tmp_path / "cookies.json"
    cookie.write_text("{}", encoding="utf-8")
    client = FakeClient(verify_error=error)
    reader, settings = _reader(monkeypatch, tmp_path, client)

    assert asyncio.run(reader.ensure_login()) is False
    assert settings.marked == []
    assert cookie.exists()
    assert client.password_login_calls == 0


def test_background_checks_next_cookie_account_without_password_login(
    monkeypatch, tmp_path,
):
    credentials = [
        {"username": "missing", "password": "one", "failed": False},
        {"username": "ready", "password": "two", "failed": False},
    ]
    clients = []
    paths = {
        "missing": tmp_path / "missing.json",
        "ready": tmp_path / "ready.json",
    }
    paths["ready"].write_text("{}", encoding="utf-8")
    settings = FakeSettings(credentials)
    reader = XReader(settings)

    def factory():
        client = FakeClient()
        clients.append(client)
        return client

    monkeypatch.setattr(reader, "_new_client", factory)
    monkeypatch.setattr(twitter, "_cookies_path", lambda username: str(paths[username]))

    assert asyncio.run(reader.ensure_login()) is True
    assert reader.active == "ready"
    assert len(clients) == 1
    assert clients[0].password_login_calls == 0


def test_interactive_login_uses_challenge_callback_then_verifies_and_saves(
    monkeypatch, tmp_path,
):
    events = []
    client = FakeClient(events=events)
    reader, settings = _reader(monkeypatch, tmp_path, client)

    def handler(challenge):
        return challenge

    received = {}

    async def login_with_challenges(target, **kwargs):
        events.append("login")
        received.update(kwargs)
        assert target is client

    monkeypatch.setitem(
        sys.modules, "xauth",
        types.SimpleNamespace(login_with_challenges=login_with_challenges),
    )
    cred = settings.credentials[0]

    assert asyncio.run(reader.login_interactive(cred, handler)) is True
    assert events == ["login", "verify", "save"]
    assert received == {
        "auth_info_1": "account",
        "auth_info_2": "account",
        "password": "top-secret",
        "challenge_handler": handler,
    }
    assert settings.marked == []
    assert reader.client is client
    assert reader.active == "account"
    cookie = tmp_path / "cookies.json"
    assert cookie.exists()
    if os.name == "posix":
        assert stat.S_IMODE(cookie.stat().st_mode) == 0o600


def test_interactive_challenge_timeout_does_not_mark_failed_or_save_secret(
    monkeypatch, tmp_path, caplog,
):
    class ChallengeTimeout(Exception):
        pass

    client = FakeClient()
    reader, settings = _reader(monkeypatch, tmp_path, client)

    async def login_with_challenges(target, **kwargs):
        raise ChallengeTimeout("top-secret")

    monkeypatch.setitem(
        sys.modules, "xauth",
        types.SimpleNamespace(login_with_challenges=login_with_challenges),
    )

    with pytest.raises(ChallengeTimeout):
        asyncio.run(reader.login_interactive(settings.credentials[0], lambda x: x))
    assert settings.marked == []
    assert not (tmp_path / "cookies.json").exists()
    assert "top-secret" not in caplog.text


def test_interactive_definitive_auth_error_marks_failed_without_saving(
    monkeypatch, tmp_path,
):
    class Unauthorized(Exception):
        pass

    client = FakeClient()
    reader, settings = _reader(monkeypatch, tmp_path, client)

    async def login_with_challenges(target, **kwargs):
        raise Unauthorized("bad credentials")

    monkeypatch.setitem(
        sys.modules, "xauth",
        types.SimpleNamespace(login_with_challenges=login_with_challenges),
    )

    with pytest.raises(Unauthorized):
        asyncio.run(reader.login_interactive(settings.credentials[0], lambda x: x))
    assert settings.marked == [("account", True)]
    assert not (tmp_path / "cookies.json").exists()


def test_interactive_verify_failure_never_saves_cookies(monkeypatch, tmp_path):
    client = FakeClient(verify_error=ConnectionError("offline"))
    reader, settings = _reader(monkeypatch, tmp_path, client)

    async def login_with_challenges(target, **kwargs):
        return None

    monkeypatch.setitem(
        sys.modules, "xauth",
        types.SimpleNamespace(login_with_challenges=login_with_challenges),
    )

    with pytest.raises(ConnectionError):
        asyncio.run(reader.login_interactive(settings.credentials[0], lambda x: x))
    assert client.saved == []
    assert settings.marked == []
    assert not (tmp_path / "cookies.json").exists()


def test_interactive_save_failure_does_not_activate_session(monkeypatch, tmp_path):
    cookie = tmp_path / "cookies.json"
    cookie.write_text('{"old": "valid"}', encoding="utf-8")
    client = FakeClient()

    def partial_save(path):
        client.events.append("save")
        client.saved.append(path)
        with open(path, "w", encoding="utf-8") as cookie_file:
            cookie_file.write('{"partial":')
        raise OSError("secret filesystem detail")

    client.save_cookies = partial_save
    reader, settings = _reader(monkeypatch, tmp_path, client)

    async def login_with_challenges(target, **kwargs):
        return None

    monkeypatch.setitem(
        sys.modules, "xauth",
        types.SimpleNamespace(login_with_challenges=login_with_challenges),
    )

    with pytest.raises(OSError):
        asyncio.run(reader.login_interactive(settings.credentials[0], lambda x: x))
    assert reader.ready is False
    assert reader.client is None
    assert reader.active is None
    assert settings.marked == []
    assert cookie.read_text(encoding="utf-8") == '{"old": "valid"}'
    assert list(tmp_path.glob(".x-cookies-*.tmp")) == []


def test_report_failure_invalid_session_does_not_disable_credentials(
    monkeypatch, tmp_path,
):
    cookie = tmp_path / "cookies.json"
    cookie.write_text("{}", encoding="utf-8")
    reader, settings = _reader(monkeypatch, tmp_path, FakeClient())
    reader.active = "account"
    reader.client = object()
    reader.ready = True

    assert reader.report_failure(Exception("401 Unauthorized")) is True
    assert settings.marked == []
    assert not cookie.exists()
    assert reader.active is None


def test_report_failure_disables_only_locked_account(monkeypatch, tmp_path):
    class AccountLocked(Exception):
        pass

    reader, settings = _reader(monkeypatch, tmp_path, FakeClient())
    reader.active = "account"

    assert reader.report_failure(AccountLocked("redacted")) is True
    assert settings.marked == [("account", True)]


def test_report_failure_disables_explicit_bad_credentials(monkeypatch, tmp_path):
    reader, settings = _reader(monkeypatch, tmp_path, FakeClient())
    reader.active = "account"

    assert reader.report_failure(Exception("incorrect password")) is True
    assert settings.marked == [("account", True)]


def test_report_failure_ambiguous_forbidden_is_transient(monkeypatch, tmp_path):
    reader, settings = _reader(monkeypatch, tmp_path, FakeClient())
    reader.active = "account"

    assert reader.report_failure(Exception("403 Forbidden")) is False
    assert reader.active == "account"
    assert settings.marked == []


def test_discard_session_removes_cookie_and_invalidates_matching_account(
    monkeypatch, tmp_path,
):
    cookie = tmp_path / "cookies.json"
    cookie.write_text("{}", encoding="utf-8")
    reader, _settings = _reader(monkeypatch, tmp_path, FakeClient())
    reader.active = "Account"
    reader.client = object()
    reader.ready = True

    reader.discard_session("account")

    assert not cookie.exists()
    assert reader.active is None
    assert reader.client is None
    assert reader.ready is False


def test_ensure_login_interactive_requires_explicit_handler(monkeypatch, tmp_path):
    reader, _settings = _reader(monkeypatch, tmp_path, FakeClient())
    with pytest.raises(TypeError, match="challenge_handler"):
        asyncio.run(reader.ensure_login(interactive=True))


# --- مقارنة معرّفات التغريدات ---
def test_is_newer_numeric():
    assert is_newer(200, 100)
    assert not is_newer(100, 200)
    assert not is_newer(100, 100)


def test_is_newer_when_no_last_id():
    assert is_newer(100, None)
    assert is_newer(100, "")


def test_is_newer_handles_non_numeric_ids():
    assert is_newer("abc", "xyz")
    assert not is_newer("abc", "abc")


# --- اختيار التغريدات الجديدة ---
def test_deleted_reference_tweet_does_not_resend_everything():
    """
    البق الأصلي: المقارنة بالتساوي + break. لو حُذفت التغريدة رقم 105 لم يتحقق
    الشرط أبداً فتُعاد كل التغريدات. المقارنة الرقمية تحلّها.
    """
    tweets = [FakeTweet(i) for i in (110, 109, 108, 107, 106)]
    account = {"last_id": "105"}          # 105 لم تعد موجودة في القائمة
    fresh = XReader.select_new(tweets, account, limit=100)
    assert [t.id for t in fresh] == [106, 107, 108, 109, 110]

    account = {"last_id": "108"}
    fresh = XReader.select_new(tweets, account, limit=100)
    assert [t.id for t in fresh] == [109, 110]


def test_results_are_oldest_first():
    tweets = [FakeTweet(i) for i in (5, 4, 3)]
    fresh = XReader.select_new(tweets, {"last_id": "2"}, limit=100)
    assert [t.id for t in fresh] == [3, 4, 5]


def test_per_cycle_limit_takes_oldest_first():
    """الحد يمنع الإغراق؛ الباقي يصل في الدورة التالية لأننا نبدأ بالأقدم."""
    tweets = [FakeTweet(i) for i in range(20, 0, -1)]
    fresh = XReader.select_new(tweets, {"last_id": "0"}, limit=5)
    assert [t.id for t in fresh] == [1, 2, 3, 4, 5]


def test_replies_and_retweets_skipped():
    tweets = [
        FakeTweet(10),
        FakeTweet(11, reply=True),
        FakeTweet(12, retweet=True),
        FakeTweet(13, text="@someone رد"),
    ]
    fresh = XReader.select_new(tweets, {"last_id": "9"}, skip_replies=True, limit=100)
    assert [t.id for t in fresh] == [10]


def test_replies_included_when_enabled():
    tweets = [FakeTweet(10), FakeTweet(11, reply=True)]
    fresh = XReader.select_new(tweets, {"last_id": "9"}, skip_replies=False, limit=100)
    assert [t.id for t in fresh] == [10, 11]


# --- استخراج الوسائط ---
class FakePhoto:
    type = "photo"

    def __init__(self, url):
        self.media_url = url


class FakeStream:
    def __init__(self, url, bitrate):
        self.url = url
        self.bitrate = bitrate


class FakeVideo:
    type = "video"

    def __init__(self, streams):
        self.streams = streams


def test_extract_all_photos_not_just_first():
    tweet = FakeTweet(1)
    tweet.media = [FakePhoto("a.jpg"), FakePhoto("b.jpg"), FakePhoto("c.jpg")]
    assert XReader.extract_media_urls(tweet) == [
        ("a.jpg", "photo"), ("b.jpg", "photo"), ("c.jpg", "photo"),
    ]


def test_extract_video_picks_highest_bitrate():
    tweet = FakeTweet(1)
    tweet.media = [FakeVideo([FakeStream("low.mp4", 100), FakeStream("hi.mp4", 900)])]
    assert XReader.extract_media_urls(tweet) == [("hi.mp4", "video")]


def test_extract_media_handles_missing_media():
    assert XReader.extract_media_urls(FakeTweet(1)) == []
