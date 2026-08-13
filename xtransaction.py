"""Safe compatibility adapter for Twikit's X-Client-Transaction generator.

Twikit 2.3.3 initializes its transaction data from ``https://x.com``.  X now
serves the required ``ondemand.s`` mapping on ``/home`` instead, so every API
request fails before it can use an otherwise valid session.  Keep this small
adapter separate from the login implementation: browser login obtains the
session, while this class lets the existing reader use that session.
"""

from urllib.parse import urlparse

import httpx


HOME_URL = "https://x.com/home"
ASSET_HOST = "abs.twimg.com"
ASSET_PATH_PREFIX = "/responsive-web/client-web/ondemand.s."


class XTransactionCompatibilityError(RuntimeError):
    """X changed the web assets needed to generate request transaction IDs."""


class XTransactionNetworkError(RuntimeError):
    """The public X bootstrap assets could not be downloaded safely."""


def _safe_asset_url(url):
    parsed = urlparse(str(url or ""))
    if (
        parsed.scheme != "https"
        or parsed.hostname != ASSET_HOST
        or not parsed.path.startswith(ASSET_PATH_PREFIX)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise XTransactionCompatibilityError("unexpected X transaction asset URL")
    return parsed.geturl()


def _require_success(response, stage):
    status = getattr(response, "status_code", None)
    if not isinstance(status, int) or not 200 <= status < 300:
        raise XTransactionNetworkError(f"X transaction bootstrap failed at {stage}")


def _public_headers(headers):
    """Copy only non-credential headers needed to retrieve public web assets."""
    source = headers or {}
    allowed = {}
    for name in ("User-Agent", "Accept", "Accept-Language"):
        value = source.get(name) or source.get(name.lower())
        if isinstance(value, str) and value:
            allowed[name] = value
    return allowed


async def _public_get(url, headers, stage):
    """Fetch one allowlisted URL using a cookie-free, no-redirect client."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            cookies=None,
            timeout=20,
            trust_env=False,
        ) as public:
            response = await public.get(url, headers=_public_headers(headers))
    except Exception as exc:  # noqa: BLE001 - never expose URLs/bodies
        raise XTransactionNetworkError(
            f"X transaction bootstrap failed at {stage}"
        ) from exc
    _require_success(response, stage)
    return response


class TwikitTransactionAdapter:
    """Implements the tiny interface consumed by ``twikit.Client.request``.

    Parser dependencies are imported lazily so Telegram/Facebook startup and
    cookie-only housekeeping do not fail merely because the optional X reader
    dependency is unavailable.
    """

    def __init__(
        self, *, parser_factory=None, soup_factory=None, url_resolver=None,
        public_get=None,
    ):
        self.home_page_response = None
        self._inner = None
        self._parser_factory = parser_factory
        self._soup_factory = soup_factory
        self._url_resolver = url_resolver
        self._public_get = public_get or _public_get

    @staticmethod
    def _defaults():
        try:
            from bs4 import BeautifulSoup
            from x_client_transaction import ClientTransaction
            from x_client_transaction.utils import get_ondemand_file_url
        except (ImportError, AttributeError) as exc:
            raise XTransactionCompatibilityError(
                "X transaction compatibility dependency is unavailable"
            ) from exc
        return ClientTransaction, BeautifulSoup, get_ondemand_file_url

    async def init(self, _session, headers):
        parser_factory = self._parser_factory
        soup_factory = self._soup_factory
        url_resolver = self._url_resolver
        if parser_factory is None or soup_factory is None or url_resolver is None:
            default_parser, default_soup, default_resolver = self._defaults()
            parser_factory = parser_factory or default_parser
            soup_factory = soup_factory or default_soup
            url_resolver = url_resolver or default_resolver

        public_headers = _public_headers(headers)
        home_response = await self._public_get(HOME_URL, public_headers, "home")
        _require_success(home_response, "home")
        home = soup_factory(home_response.content, "html.parser")

        try:
            asset_url = _safe_asset_url(url_resolver(home))
        except XTransactionCompatibilityError:
            raise
        except Exception as exc:  # noqa: BLE001 - parser implementations vary
            raise XTransactionCompatibilityError(
                "X transaction asset mapping changed"
            ) from exc

        asset_response = await self._public_get(asset_url, public_headers, "asset")
        _require_success(asset_response, "asset")

        try:
            inner = parser_factory(home, asset_response.text)
        except Exception as exc:  # noqa: BLE001
            raise XTransactionCompatibilityError(
                "X transaction asset format changed"
            ) from exc
        self._inner = inner
        # Twikit checks this attribute before every request.  Assign it only
        # after the complete bootstrap succeeds so a partial object is never used.
        self.home_page_response = home

    def generate_transaction_id(self, method, path):
        if self._inner is None or self.home_page_response is None:
            raise XTransactionCompatibilityError("X transaction adapter is not ready")
        try:
            return self._inner.generate_transaction_id(method=method, path=path)
        except Exception as exc:  # noqa: BLE001
            raise XTransactionCompatibilityError(
                "X transaction ID generation failed"
            ) from exc
