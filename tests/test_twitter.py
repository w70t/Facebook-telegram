import asyncio
import os
import stat
import sys
import types

import httpx
import pytest

import twitter
import xbrowser
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


class FakeCookieJar:
    def __init__(self, client):
        self.client = client
        self.values = {}

    def clear(self):
        self.values.clear()

    def set(self, name, value, *, domain, path):
        assert domain == ".x.com"
        assert path == "/"
        self.values[name] = value
        self.client.cookies = dict(self.values)


class FakeClient:
    def __init__(
        self, events=None, verify_error=None, save_error=None,
        authenticated_username="account",
    ):
        self.events = events if events is not None else []
        self.verify_error = verify_error
        self.save_error = save_error
        self.loaded = []
        self.saved = []
        self.password_login_calls = 0
        self.cookies = None
        self.authenticated_username = authenticated_username
        self.http = types.SimpleNamespace(cookies=FakeCookieJar(self))

    def load_cookies(self, path):
        self.events.append("load")
        self.loaded.append(path)

    async def user(self):
        self.events.append("verify")
        if self.verify_error:
            raise self.verify_error
        return types.SimpleNamespace(screen_name=self.authenticated_username)

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

    def set_cookies(self, cookies, clear_cookies=False):
        self.events.append("set")
        self.cookies = dict(cookies)
        assert clear_cookies is True

    def get_cookies(self):
        return dict(self.cookies or {})


def test_new_client_installs_maintained_transaction_adapter(monkeypatch, tmp_path):
    created = []

    class Client:
        def __init__(self, language):
            self.language = language
            self.client_transaction = object()
            created.append(self)

    monkeypatch.setitem(sys.modules, "twikit", types.SimpleNamespace(Client=Client))
    reader = XReader(FakeSettings([]))

    client = reader._new_client()

    from xtransaction import TwikitTransactionAdapter

    assert client is created[0]
    assert client.language == "en-US"
    assert isinstance(client.client_transaction, TwikitTransactionAdapter)


def test_x_session_cookies_are_never_sent_to_non_x_hosts():
    client = types.SimpleNamespace(
        http=types.SimpleNamespace(cookies=httpx.Cookies())
    )

    XReader._set_x_cookies(client, {
        "auth_token": "session-secret",
        "ct0": "csrf-secret",
    })

    x_request = httpx.Request("GET", "https://x.com/i/api/test")
    client.http.cookies.set_cookie_header(x_request)
    assert "session-secret" in x_request.headers.get("cookie", "")

    for url in (
        "https://abs.twimg.com/responsive-web/client-web/app.js",
        "https://evil.example/collect",
    ):
        request = httpx.Request("GET", url)
        client.http.cookies.set_cookie_header(request)
        assert "cookie" not in request.headers


def test_real_twikit_duplicate_cookie_cleanup_preserves_x_domain_scope(
    monkeypatch,
):
    from twikit import Client

    reader = XReader(FakeSettings([]))
    monkeypatch.setattr(
        "xtransaction.TwikitTransactionAdapter",
        lambda: types.SimpleNamespace(home_page_response=object()),
    )
    client = reader._new_client()
    assert isinstance(client, Client)
    reader._set_x_cookies(client, {
        "auth_token": "session-secret",
        "ct0": "csrf-secret",
    })

    client._remove_duplicate_ct0_cookie()

    for url in (
        "https://abs.twimg.com/responsive-web/client-web/app.js",
        "https://evil.example/collect",
    ):
        request = httpx.Request("GET", url)
        client.http.cookies.set_cookie_header(request)
        assert "cookie" not in request.headers


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
    monkeypatch.setattr(
        twitter, "_legacy_cookies_path",
        lambda username: str(tmp_path / "cookies.json"),
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
    assert client.events == ["verify"]
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
        client = FakeClient(authenticated_username="ready")
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

    async def obtain_cookies(credentials, challenge_handler, **kwargs):
        events.append("login")
        received.update({
            "credentials": dict(credentials),
            "challenge_handler": challenge_handler,
            **kwargs,
        })
        return {"auth_token": "auth", "ct0": "csrf"}

    monkeypatch.setattr(xbrowser, "obtain_cookies", obtain_cookies)
    cred = settings.credentials[0]

    assert asyncio.run(reader.login_interactive(cred, handler)) is True
    assert events == ["login", "verify", "save"]
    assert received == {
        "credentials": {
            "username": "account",
            "email": None,
            "password": "top-secret",
        },
        "challenge_handler": handler,
    }
    assert settings.marked == []
    assert reader.client is client
    assert reader.active == "account"
    cookie = tmp_path / "cookies.json"
    assert cookie.exists()
    if os.name == "posix":
        assert stat.S_IMODE(cookie.stat().st_mode) == 0o600


def test_interactive_login_forwards_progress_callback(monkeypatch, tmp_path):
    client = FakeClient()
    reader, settings = _reader(monkeypatch, tmp_path, client)
    received = {}
    cred = settings.credentials[0]

    async def obtain_cookies(credentials, challenge_handler, **kwargs):
        # The caller mapping and XReader's password local are already cleared
        # before a browser progress callback can await Telegram.
        assert cred == {}
        assert credentials["password"] == "top-secret"
        assert callable(challenge_handler)
        received.update(kwargs)
        return {"auth_token": "auth", "ct0": "csrf"}

    async def progress(_stage):
        return None

    monkeypatch.setattr(xbrowser, "obtain_cookies", obtain_cookies)
    assert asyncio.run(reader.login_interactive(
        cred, lambda value: value,
        progress_handler=progress,
    )) is True
    assert cred == {}
    assert received == {"progress_handler": progress}


def test_interactive_challenge_timeout_does_not_mark_failed_or_save_secret(
    monkeypatch, tmp_path, caplog,
):
    class ChallengeTimeout(Exception):
        pass

    client = FakeClient()
    reader, settings = _reader(monkeypatch, tmp_path, client)

    async def obtain_cookies(*_args, **_kwargs):
        raise ChallengeTimeout("top-secret")

    monkeypatch.setattr(xbrowser, "obtain_cookies", obtain_cookies)

    with pytest.raises(ChallengeTimeout):
        asyncio.run(reader.login_interactive(settings.credentials[0], lambda x: x))
    assert settings.marked == []
    assert not (tmp_path / "cookies.json").exists()
    assert "top-secret" not in caplog.text


def test_interactive_raw_unauthorized_does_not_mark_credentials_failed(
    monkeypatch, tmp_path,
):
    class Unauthorized(Exception):
        pass

    client = FakeClient()
    reader, settings = _reader(monkeypatch, tmp_path, client)

    async def obtain_cookies(*_args, **_kwargs):
        raise Unauthorized("bad credentials")

    monkeypatch.setattr(xbrowser, "obtain_cookies", obtain_cookies)

    with pytest.raises(Unauthorized):
        asyncio.run(reader.login_interactive(settings.credentials[0], lambda x: x))
    assert settings.marked == []
    assert not (tmp_path / "cookies.json").exists()


def test_interactive_explicit_credentials_rejection_preserves_stored_account(
    monkeypatch, tmp_path,
):
    class XBrowserCredentialsRejected(Exception):
        pass

    client = FakeClient()
    reader, settings = _reader(monkeypatch, tmp_path, client)
    cookie = tmp_path / "cookies.json"
    cookie.write_text('{"old": "session"}', encoding="utf-8")

    async def obtain_cookies(*_args, **_kwargs):
        raise XBrowserCredentialsRejected("redacted")

    monkeypatch.setattr(xbrowser, "obtain_cookies", obtain_cookies)

    with pytest.raises(XBrowserCredentialsRejected):
        asyncio.run(reader.login_interactive(settings.credentials[0], lambda x: x))
    assert settings.marked == []
    assert cookie.read_text(encoding="utf-8") == '{"old": "session"}'


def test_interactive_session_for_different_account_is_never_saved_or_activated(
    monkeypatch, tmp_path,
):
    client = FakeClient(authenticated_username="different-account")
    reader, settings = _reader(monkeypatch, tmp_path, client)

    async def obtain_cookies(*_args, **_kwargs):
        return {"auth_token": "auth", "ct0": "csrf"}

    monkeypatch.setattr(xbrowser, "obtain_cookies", obtain_cookies)

    with pytest.raises(twitter.XSessionAccountMismatch):
        asyncio.run(reader.login_interactive(settings.credentials[0], lambda x: x))
    assert client.saved == []
    assert reader.ready is False
    assert settings.marked == []
    assert not (tmp_path / "cookies.json").exists()


def test_interactive_verify_failure_never_saves_cookies(monkeypatch, tmp_path):
    client = FakeClient(verify_error=ConnectionError("offline"))
    reader, settings = _reader(monkeypatch, tmp_path, client)

    async def obtain_cookies(*_args, **_kwargs):
        return {"auth_token": "auth", "ct0": "csrf"}

    monkeypatch.setattr(xbrowser, "obtain_cookies", obtain_cookies)

    with pytest.raises(twitter.XSessionVerificationError):
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

    async def obtain_cookies(*_args, **_kwargs):
        return {"auth_token": "auth", "ct0": "csrf"}

    monkeypatch.setattr(xbrowser, "obtain_cookies", obtain_cookies)

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
    session = reader.capture_session()

    assert reader.report_failure(Exception("401 Unauthorized"), session=session) is True
    assert settings.marked == []
    assert not cookie.exists()
    assert reader.active is None


def test_report_failure_disables_only_locked_account(monkeypatch, tmp_path):
    class AccountLocked(Exception):
        pass

    reader, settings = _reader(monkeypatch, tmp_path, FakeClient())
    reader.active = "account"
    reader.client = object()
    reader.ready = True
    session = reader.capture_session()

    assert reader.report_failure(AccountLocked("redacted"), session=session) is True
    assert settings.marked == [("account", True)]


def test_report_failure_disables_explicit_bad_credentials(monkeypatch, tmp_path):
    reader, settings = _reader(monkeypatch, tmp_path, FakeClient())
    reader.active = "account"
    reader.client = object()
    reader.ready = True
    session = reader.capture_session()

    assert reader.report_failure(Exception("incorrect password"), session=session) is True
    assert settings.marked == [("account", True)]


def test_report_failure_ambiguous_forbidden_is_transient(monkeypatch, tmp_path):
    reader, settings = _reader(monkeypatch, tmp_path, FakeClient())
    reader.active = "account"
    reader.client = object()
    reader.ready = True
    session = reader.capture_session()

    assert reader.report_failure(Exception("403 Forbidden"), session=session) is False
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


def test_discard_during_cookie_verify_cannot_reactivate_deleted_session(
    monkeypatch, tmp_path,
):
    verify_started = None
    release_verify = None

    class BarrierClient(FakeClient):
        async def user(self):
            verify_started.set()
            await release_verify.wait()
            return types.SimpleNamespace(screen_name="account")

    async def scenario():
        nonlocal verify_started, release_verify
        verify_started = asyncio.Event()
        release_verify = asyncio.Event()
        client = BarrierClient()
        reader, settings = _reader(monkeypatch, tmp_path, client)
        cookie = tmp_path / "cookies.json"
        cookie.write_text("{}", encoding="utf-8")

        task = asyncio.create_task(reader._activate_cookie(settings.credentials[0]))
        await verify_started.wait()
        assert reader.discard_session("account") is True
        release_verify.set()

        assert await task is False
        assert reader.ready is False
        assert reader.active is None
        assert reader.client is None
        assert not cookie.exists()

    asyncio.run(scenario())


def test_switch_during_interactive_login_cannot_save_or_replace_new_session(
    monkeypatch, tmp_path,
):
    login_started = None
    release_login = None
    old_client = FakeClient()
    reader, settings = _reader(monkeypatch, tmp_path, old_client)

    async def obtain_cookies(*_args, **_kwargs):
        login_started.set()
        await release_login.wait()
        return {"auth_token": "auth", "ct0": "csrf"}

    monkeypatch.setattr(xbrowser, "obtain_cookies", obtain_cookies)

    async def scenario():
        nonlocal login_started, release_login
        login_started = asyncio.Event()
        release_login = asyncio.Event()
        task = asyncio.create_task(reader.login_interactive(
            settings.credentials[0], lambda *_args: None,
        ))
        await login_started.wait()
        replacement = object()
        reader._set_active(replacement, "second")
        replacement_session = reader.capture_session()
        release_login.set()

        with pytest.raises(twitter.XSessionSuperseded):
            await task
        assert reader.capture_session() == replacement_session
        assert old_client.saved == []
        assert not (tmp_path / "cookies.json").exists()

    asyncio.run(scenario())


def test_stale_interactive_auth_error_cannot_disable_replacement_credential(
    monkeypatch, tmp_path,
):
    class XBrowserCredentialsRejected(Exception):
        pass

    login_started = None
    release_login = None
    old_client = FakeClient()
    reader, settings = _reader(monkeypatch, tmp_path, old_client)

    async def obtain_cookies(*_args, **_kwargs):
        login_started.set()
        await release_login.wait()
        raise XBrowserCredentialsRejected("old attempt rejected")

    monkeypatch.setattr(xbrowser, "obtain_cookies", obtain_cookies)

    async def scenario():
        nonlocal login_started, release_login
        login_started = asyncio.Event()
        release_login = asyncio.Event()
        task = asyncio.create_task(reader.login_interactive(
            settings.credentials[0], lambda *_args: None,
        ))
        await login_started.wait()
        replacement = object()
        reader._set_active(replacement, "account")
        replacement_session = reader.capture_session()
        release_login.set()

        with pytest.raises(XBrowserCredentialsRejected):
            await task
        assert reader.capture_session() == replacement_session
        assert settings.marked == []

    asyncio.run(scenario())


def test_ensure_login_interactive_requires_explicit_handler(monkeypatch, tmp_path):
    reader, _settings = _reader(monkeypatch, tmp_path, FakeClient())
    with pytest.raises(TypeError, match="challenge_handler"):
        asyncio.run(reader.ensure_login(interactive=True))


def test_stale_fetch_failure_cannot_destroy_replacement_session(
    monkeypatch, tmp_path,
):
    class AccountLocked(Exception):
        pass

    old_started = None
    release_old = None

    class OldClient(FakeClient):
        async def get_user_tweets(self, *_args, **_kwargs):
            old_started.set()
            await release_old.wait()
            raise AccountLocked("old request")

    async def scenario():
        nonlocal old_started, release_old
        old_started = asyncio.Event()
        release_old = asyncio.Event()
        old_client = OldClient()
        reader, settings = _reader(monkeypatch, tmp_path, old_client)
        reader._set_active(old_client, "account")
        old_session = reader.capture_session()
        task = asyncio.create_task(reader.fetch_new(
            {"user_id": "1", "last_id": None}, session=old_session,
        ))
        await old_started.wait()

        reader.invalidate()
        new_client = FakeClient()
        reader._set_active(new_client, "account")
        new_session = reader.capture_session()
        cookie = tmp_path / "cookies.json"
        cookie.write_text('{"new": true}', encoding="utf-8")

        release_old.set()
        with pytest.raises(AccountLocked) as raised:
            await task
        assert reader.report_failure(raised.value, session=old_session) is False
        assert reader.capture_session() == new_session
        assert cookie.read_text(encoding="utf-8") == '{"new": true}'
        assert settings.marked == []

    asyncio.run(scenario())


def test_session_generation_changes_on_invalidate_and_activation(
    monkeypatch, tmp_path,
):
    reader, _settings = _reader(monkeypatch, tmp_path, FakeClient())
    initial = reader._session_generation
    reader.invalidate()
    after_invalidate = reader._session_generation
    reader._set_active(object(), "account")

    assert after_invalidate > initial
    assert reader._session_generation > after_invalidate


def test_cookie_path_is_case_insensitive_and_drops_legacy_case(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(twitter, "BASE_DIR", str(tmp_path))
    canonical = twitter._cookies_path("Reader")
    legacy = twitter._legacy_cookies_path("Reader")
    assert canonical == twitter._cookies_path("reader")
    with open(canonical, "w", encoding="utf-8") as cookie_file:
        cookie_file.write("canonical")
    if os.path.normcase(legacy) != os.path.normcase(canonical):
        with open(legacy, "w", encoding="utf-8") as cookie_file:
            cookie_file.write("legacy")

    assert twitter._drop_cookies("Reader") is True
    assert not os.path.exists(canonical)
    assert not os.path.exists(legacy)


def test_drop_cookie_finds_old_alias_even_when_stored_casing_changed(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(twitter, "BASE_DIR", str(tmp_path))
    old_alias = tmp_path / "x_cookies_Reader.json"
    old_alias.write_text('{"session": "old"}', encoding="utf-8")

    assert twitter._drop_cookies("reader") is True
    assert not old_alias.exists()


def test_cookie_migration_keeps_canonical_and_removes_all_case_aliases(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(twitter, "BASE_DIR", str(tmp_path))
    canonical = tmp_path / "x_cookies_reader.json"
    old_alias = tmp_path / "x_cookies_Reader.json"
    if os.path.normcase(str(canonical)) == os.path.normcase(str(old_alias)):
        pytest.skip("case aliases cannot coexist on this filesystem")
    canonical.write_text('{"session": "new"}', encoding="utf-8")
    old_alias.write_text('{"session": "old"}', encoding="utf-8")

    assert twitter._canonicalize_cookie_path("reader") == str(canonical)
    assert canonical.read_text(encoding="utf-8") == '{"session": "new"}'
    assert not old_alias.exists()


def test_drop_cookie_does_not_follow_or_unlink_symlink(monkeypatch, tmp_path):
    monkeypatch.setattr(twitter, "BASE_DIR", str(tmp_path))
    victim = tmp_path / "victim.json"
    alias = tmp_path / "x_cookies_Reader.json"
    victim.write_text("keep", encoding="utf-8")
    try:
        alias.symlink_to(victim)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    assert twitter._drop_cookies("reader") is False
    assert alias.is_symlink()
    assert victim.read_text(encoding="utf-8") == "keep"


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
