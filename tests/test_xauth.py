import asyncio
import builtins
import types

import pytest

import xauth


class Cookies:
    def __init__(self):
        self.cleared = False

    def clear(self):
        self.cleared = True


class Client:
    def __init__(self):
        self.http = types.SimpleNamespace(cookies=Cookies())
        self.v11 = object()
        self._user_id = None

    async def _get_guest_token(self):
        return "guest"

    async def _ui_metrics(self):
        return "javascript"


def _task(task_id, **extra):
    subtask = {"subtask_id": task_id, **extra} if task_id else None
    return {
        "flow_token": "flow",
        "subtasks": [subtask] if subtask else [],
    }


class Flow:
    """محاكاة صغيرة لتسلسل onboarding في Twikit 2.3.3."""

    after_password = ["AccountDuplicationCheck"]
    after_identifier = ["LoginEnterAlternateIdentifierSubtask"]
    challenge_results = []
    instances = []

    def __init__(self, client, guest_token):
        assert guest_token == "guest"
        self.client = client
        self.response = _task(None)
        self.calls = []
        self._challenge_results = list(type(self).challenge_results)
        type(self).instances.append(self)

    @property
    def task_id(self):
        subtasks = self.response.get("subtasks") or []
        return subtasks[0]["subtask_id"] if subtasks else None

    async def sso_init(self, provider):
        self.calls.append(("sso", provider))

    async def execute_task(self, *inputs, **kwargs):
        if kwargs.get("params"):
            self.calls.append(("start", kwargs["params"]))
            self.response = _task("LoginJsInstrumentationSubtask")
            return
        payload = inputs[0]
        task_id = payload["subtask_id"]
        self.calls.append((task_id, payload))
        if task_id == "LoginJsInstrumentationSubtask":
            next_task = "LoginEnterUserIdentifierSSO"
        elif task_id == "LoginEnterUserIdentifierSSO":
            next_task = type(self).after_identifier[0]
        elif task_id == "LoginEnterAlternateIdentifierSubtask":
            next_task = "LoginEnterPassword"
        elif task_id == "LoginEnterPassword":
            next_task = type(self).after_password[0]
        elif task_id in ("LoginAcid", "LoginTwoFactorAuthChallenge"):
            next_task = self._challenge_results.pop(0)
        elif task_id == "AccountDuplicationCheck":
            self.response = {
                "flow_token": "done",
                "subtasks": [{"subtask_id": "OpenAccount", "id_str": "42"}],
            }
            return
        else:
            raise AssertionError(f"unexpected task: {task_id}")
        self.response = _task(next_task)


@pytest.fixture(autouse=True)
def fake_twikit(monkeypatch):
    Flow.after_password = ["AccountDuplicationCheck"]
    Flow.after_identifier = ["LoginEnterAlternateIdentifierSubtask"]
    Flow.challenge_results = []
    Flow.instances = []
    monkeypatch.setattr(
        xauth,
        "_load_twikit_api",
        lambda: (Flow, lambda *_args, **_kwargs: ["42"], lambda _js: "metrics"),
    )


def run_login(client, handler, **kwargs):
    values = {
        "auth_info_1": "reader",
        "auth_info_2": "reader@example.test",
        "password": "password",
        "challenge_handler": handler,
    }
    values.update(kwargs)
    return asyncio.run(xauth.login_with_challenges(client, **values))


def test_login_without_challenge_never_reads_stdin(monkeypatch):
    monkeypatch.setattr(
        builtins, "input", lambda *_args: (_ for _ in ()).throw(AssertionError("stdin"))
    )
    client = Client()
    called = []

    async def handler(kind, prompt):
        called.append((kind, prompt))
        return "unused"

    result = run_login(client, handler)
    assert result["subtasks"][0]["subtask_id"] == "OpenAccount"
    assert client.http.cookies.cleared is True
    assert client._user_id == "42"
    assert called == []


def test_verification_then_authenticator_and_wrong_code_retry(monkeypatch):
    Flow.after_password = ["LoginAcid"]
    Flow.challenge_results = [
        "LoginTwoFactorAuthChallenge",
        "LoginTwoFactorAuthChallenge",
        "AccountDuplicationCheck",
    ]
    monkeypatch.setattr(
        builtins, "input", lambda *_args: (_ for _ in ()).throw(AssertionError("stdin"))
    )
    replies = iter(("EMAIL1", "111111", "222222"))
    calls = []

    async def handler(kind, prompt):
        calls.append((kind, prompt))
        return next(replies)

    run_login(Client(), handler)
    assert calls == [
        ("verification", ""),
        ("two_factor", ""),
        ("two_factor", ""),
    ]
    flow = Flow.instances[0]
    submitted = [
        payload["enter_text"]["text"]
        for task, payload in flow.calls
        if task in ("LoginAcid", "LoginTwoFactorAuthChallenge")
    ]
    assert submitted == ["EMAIL1", "111111", "222222"]


def test_password_denial_is_classified_as_credentials_rejected():
    Flow.after_password = ["DenyLoginSubtask"]

    async def handler(_kind, _prompt):
        return "unused"

    with pytest.raises(xauth.XCredentialsRejected, match="invalid credentials"):
        run_login(Client(), handler)


def test_denial_after_authenticator_does_not_classify_account_as_bad_password():
    Flow.after_password = ["LoginTwoFactorAuthChallenge"]
    Flow.challenge_results = ["DenyLoginSubtask"]

    async def handler(_kind, _prompt):
        return "123456"

    with pytest.raises(xauth.XChallengeRejected, match="verification code"):
        run_login(Client(), handler)


def test_unsupported_challenge_fails_closed():
    Flow.after_password = ["LoginEnterRecaptcha"]

    async def handler(_kind, _prompt):
        return "unused"

    with pytest.raises(xauth.XUnsupportedChallenge, match="LoginEnterRecaptcha"):
        run_login(Client(), handler)


def test_unexpected_task_before_password_fails_closed_without_sending_password():
    Flow.after_identifier = ["LoginEnterRecaptcha"]

    async def handler(_kind, _prompt):
        return "unused"

    with pytest.raises(xauth.XUnsupportedChallenge, match="LoginEnterRecaptcha"):
        run_login(Client(), handler)
    flow = Flow.instances[0]
    assert all(task != "LoginEnterPassword" for task, _payload in flow.calls)
    assert "password" not in repr(flow.calls)


def test_empty_challenge_response_is_rejected():
    Flow.after_password = ["LoginTwoFactorAuthChallenge"]

    async def handler(_kind, _prompt):
        return "   "

    with pytest.raises(xauth.XChallengeResponseError):
        run_login(Client(), handler)


def test_challenge_loop_has_hard_limit():
    Flow.after_password = ["LoginTwoFactorAuthChallenge"]
    Flow.challenge_results = ["LoginTwoFactorAuthChallenge"] * 10

    async def handler(_kind, _prompt):
        return "123456"

    with pytest.raises(xauth.XChallengeRejected, match="too many"):
        run_login(Client(), handler)


def test_wrong_twikit_version_is_rejected_before_private_import(monkeypatch):
    monkeypatch.setattr(xauth, "version", lambda _name: "9.9.9")
    with pytest.raises(xauth.XAuthCompatibilityError, match="9.9.9"):
        xauth._require_supported_twikit()
