import asyncio
import types

import pytest

import xtransaction


class Response:
    def __init__(self, status_code=200, *, content=b"html", text="javascript"):
        self.status_code = status_code
        self.content = content
        self.text = text


class Session:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    async def request(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


class Parser:
    def __init__(self, home, script):
        self.home = home
        self.script = script

    def generate_transaction_id(self, method, path):
        return f"{method}:{path}"


def make_adapter(
    url="https://abs.twimg.com/responsive-web/client-web/ondemand.s.abc.js",
    session=None,
):
    session = session or Session([Response(content=b"home"), Response(text="asset")])

    async def public_get(request_url, headers, stage):
        return await session.request(
            method="GET", url=request_url, headers=headers,
            follow_redirects=False, stage=stage,
        )

    return xtransaction.TwikitTransactionAdapter(
        parser_factory=Parser,
        soup_factory=lambda content, parser: types.SimpleNamespace(
            content=content, parser=parser
        ),
        url_resolver=lambda _home: url,
        public_get=public_get,
    )


def test_adapter_bootstraps_from_home_and_restricts_asset_host():
    session = Session([Response(content=b"home"), Response(text="asset")])
    adapter = make_adapter(session=session)

    asyncio.run(adapter.init(session, {"User-Agent": "browser"}))

    assert [call["url"] for call in session.calls] == [
        "https://x.com/home",
        "https://abs.twimg.com/responsive-web/client-web/ondemand.s.abc.js",
    ]
    assert all(call["follow_redirects"] is False for call in session.calls)
    assert adapter.generate_transaction_id("GET", "/api/path") == "GET:/api/path"


@pytest.mark.parametrize(
    "url",
    [
        "http://abs.twimg.com/responsive-web/client-web/ondemand.s.a.js",
        "https://evil.example/responsive-web/client-web/ondemand.s.a.js",
        "https://abs.twimg.com/other.js",
        "https://user@abs.twimg.com/responsive-web/client-web/ondemand.s.a.js",
        "https://abs.twimg.com:444/responsive-web/client-web/ondemand.s.a.js",
    ],
)
def test_adapter_rejects_untrusted_asset_urls_without_request(url):
    session = Session([Response()])
    adapter = make_adapter(url, session=session)

    with pytest.raises(xtransaction.XTransactionCompatibilityError):
        asyncio.run(adapter.init(session, {}))

    assert len(session.calls) == 1
    assert adapter.home_page_response is None


@pytest.mark.parametrize("status,stage", [(503, "home"), (404, "asset")])
def test_adapter_rejects_http_failures_without_exposing_response(status, stage):
    responses = (
        [Response(status_code=status)]
        if stage == "home"
        else [Response(), Response(status_code=status, text="secret body")]
    )
    adapter = make_adapter(session=Session(responses))

    with pytest.raises(xtransaction.XTransactionNetworkError) as caught:
        asyncio.run(adapter.init(Session([]), {}))

    assert stage in str(caught.value)
    assert "secret body" not in str(caught.value)
    assert adapter.home_page_response is None


def test_adapter_normalizes_parser_changes_and_never_marks_ready():
    def broken_parser(_home, _script):
        raise ValueError("raw asset detail")

    session = Session([Response(), Response()])

    async def public_get(url, headers, stage):
        return await session.request(url=url, headers=headers, stage=stage)

    adapter = xtransaction.TwikitTransactionAdapter(
        parser_factory=broken_parser,
        soup_factory=lambda content, parser: object(),
        url_resolver=lambda _home: (
            "https://abs.twimg.com/responsive-web/client-web/ondemand.s.a.js"
        ),
        public_get=public_get,
    )

    with pytest.raises(
        xtransaction.XTransactionCompatibilityError,
        match="asset format changed",
    ) as caught:
        asyncio.run(adapter.init(Session([]), {}))

    assert "raw asset detail" not in str(caught.value)
    assert adapter.home_page_response is None


def test_generate_before_init_fails_closed():
    with pytest.raises(xtransaction.XTransactionCompatibilityError, match="not ready"):
        make_adapter().generate_transaction_id("GET", "/")


def test_public_bootstrap_never_uses_authenticated_session_or_secret_headers():
    authenticated = Session(error=AssertionError("authenticated session used"))
    public = Session([Response(), Response()])
    adapter = make_adapter(session=public)

    asyncio.run(adapter.init(authenticated, {
        "User-Agent": "browser",
        "Authorization": "Bearer SECRET",
        "Cookie": "auth_token=SECRET; ct0=CSRF",
        "X-Csrf-Token": "CSRF",
    }))

    assert authenticated.calls == []
    assert len(public.calls) == 2
    for call in public.calls:
        assert call["follow_redirects"] is False
        assert call["headers"] == {"User-Agent": "browser"}
