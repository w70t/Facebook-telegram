"""Acquire an X web session through an isolated Chromium context.

This module has one deliberately narrow job: drive X's real login page and
return an in-memory cookie mapping suitable for ``twikit.Client.set_cookies``.
It never writes a browser profile, storage state, screenshot, trace, download,
password, or one-time verification value to disk.
"""

import asyncio
import os
import time
from urllib.parse import urlparse


LOGIN_URL = "https://x.com/i/flow/login"
ALLOWED_TOP_LEVEL_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
ALLOWED_COOKIE_DOMAINS = {"x.com", ".x.com", "twitter.com", ".twitter.com"}
TOTAL_TIMEOUT = 420
STEP_TIMEOUT_MS = 30_000
MAX_CHALLENGE_ATTEMPTS = 3
MAX_COOKIES = 80
MAX_COOKIE_NAME = 128
MAX_COOKIE_VALUE = 8192

USERNAME_SELECTOR = (
    'input[name="username_or_email"]:visible, '
    'input[autocomplete~="username"]:visible'
)
PASSWORD_SELECTOR = (
    'input[name="password"]:visible, input[autocomplete="current-password"]:visible'
)
# The OCF selector covers the older flow. JF renders either its dedicated
# one-time-code field or a typed input whose username/password variants are
# explicitly excluded. Body classification and actionability checks must still
# identify a reviewed challenge before anything is filled.
GENERIC_INPUT_SELECTOR = (
    'input[data-testid="ocfEnterTextTextInput"]:visible, '
    'input.jf-code-input-field[autocomplete="one-time-code"]:visible, '
    'input.jf-input:not([autocomplete~="username"]):not([type="password"]):visible, '
    'input.jf-float-input:not([autocomplete~="username"]):not([type="password"]):visible'
)
NEXT_SELECTOR = '[data-testid="ocfEnterTextNextButton"]:visible'
LOGIN_SELECTOR = (
    '[data-testid="LoginForm_Login_Button"]:visible, '
    'button[type="submit"]:visible, input[type="submit"]:visible'
)
COOKIE_REJECT_SELECTORS = (
    '[data-testid="cookie-policy-manage-dialog-reject-button"]:visible',
    'button:has-text("Refuse non-essential cookies"):visible',
)


class XBrowserError(RuntimeError):
    """Base class for fixed, secret-free browser-login failures."""


class XBrowserUnavailable(XBrowserError):
    """Chromium or its Playwright driver is unavailable."""


class XBrowserRateLimited(XBrowserError):
    """X temporarily limited interactive login attempts."""


class XBrowserPageChanged(XBrowserError):
    """X's login page no longer matches the reviewed state machine."""


class XBrowserCredentialsRejected(XBrowserError):
    """X explicitly rejected the supplied account credentials."""


class XBrowserChallengeRejected(XBrowserError):
    """X rejected too many one-time verification responses."""


class XBrowserUnsupportedChallenge(XBrowserError):
    """X requested CAPTCHA, a security key, or another unsupported step."""


class XBrowserSessionError(XBrowserError):
    """The page did not produce a complete reusable X session."""


class XBrowserCleanupError(XBrowserError):
    """The authenticated browser context could not be confirmed closed."""


class XBrowserCancelled(XBrowserError):
    """The caller cancelled the browser attempt."""


def _unsafe_debug_environment(env=None):
    """Reject diagnostics which can print fill values (passwords/OTP codes)."""
    env = os.environ if env is None else env
    if "DEBUGP" in env:
        return True
    if str(env.get("PWDEBUG") or "").strip():
        return True
    debug = str(env.get("DEBUG") or "").lower()
    return bool(debug and ("*" in debug or "pw:" in debug or "playwright" in debug))


def _validate_credentials(credentials):
    if not isinstance(credentials, dict):
        raise TypeError("X browser credentials must be a mapping")
    username = str(credentials.get("username") or "").strip().lstrip("@")
    password = credentials.get("password")
    alternate = credentials.get("email")
    if alternate is not None:
        alternate = str(alternate).strip()
        if alternate in ("", "-"):
            alternate = None
    if not username or not isinstance(password, str) or not password:
        raise ValueError("X username and password are required")
    if any(ord(char) < 32 for char in username):
        raise ValueError("X username contains control characters")
    return username, password, alternate


def _top_level_allowed(url):
    parsed = urlparse(str(url or ""))
    return (
        parsed.scheme == "https"
        and parsed.hostname in ALLOWED_TOP_LEVEL_HOSTS
        and parsed.username is None
        and parsed.password is None
        and parsed.port in (None, 443)
    )


def playwright_to_twikit(cookies, *, now=None):
    """Validate and flatten Playwright cookies without mixing domain aliases."""
    if not isinstance(cookies, list) or len(cookies) > MAX_COOKIES:
        raise XBrowserSessionError("X browser returned an invalid cookie set")
    now = time.time() if now is None else now
    result = {}
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise XBrowserSessionError("X browser returned an invalid cookie entry")
        name = cookie.get("name")
        value = cookie.get("value")
        domain = str(cookie.get("domain") or "").lower()
        path = cookie.get("path")
        expires = cookie.get("expires", -1)
        if domain not in ALLOWED_COOKIE_DOMAINS or path != "/":
            continue
        if not isinstance(name, str) or not isinstance(value, str):
            raise XBrowserSessionError("X browser returned a malformed cookie")
        if not name or len(name) > MAX_COOKIE_NAME or len(value) > MAX_COOKIE_VALUE:
            raise XBrowserSessionError("X browser returned an oversized cookie")
        if any(ord(char) < 33 or ord(char) > 126 for char in name):
            raise XBrowserSessionError("X browser returned an unsafe cookie name")
        if not value:
            if name in {"auth_token", "ct0"}:
                raise XBrowserSessionError("X browser session is incomplete")
            continue
        try:
            expired = float(expires) > 0 and float(expires) <= float(now)
        except (TypeError, ValueError):
            raise XBrowserSessionError("X browser returned an invalid cookie expiry") from None
        if expired:
            if name in {"auth_token", "ct0"}:
                raise XBrowserSessionError("X browser session has expired")
            continue
        previous = result.get(name)
        if previous is not None and previous != value:
            raise XBrowserSessionError("X browser returned conflicting cookies")
        result[name] = value
    if not result.get("auth_token") or not result.get("ct0"):
        raise XBrowserSessionError("X browser session is incomplete")
    return result


async def _is_visible(locator):
    try:
        return await locator.count() > 0 and await locator.first.is_visible()
    except Exception:  # noqa: BLE001 - DOM may change between count and visibility
        return False


async def _is_actionable(locator):
    """Reject geometrically visible decoys that cannot receive user input.

    X keeps future-step inputs in the DOM with a non-zero bounding box while
    setting ``opacity: 0`` and ``pointer-events: none``.  Playwright therefore
    considers them visible even though a user cannot interact with them.
    """
    try:
        eligible = bool(await locator.evaluate("""node => {
            const rect = node.getBoundingClientRect();
            if (!node.isConnected || rect.width <= 0 || rect.height <= 0
                    || node.matches(':disabled') || node.readOnly
                    || node.getAttribute('aria-disabled') === 'true') return false;
            const codeProxy = node.matches(
                'input.jf-code-input-field[autocomplete="one-time-code"]'
            );
            for (let current = node; current; current = current.parentElement) {
                const style = window.getComputedStyle(current);
                const opacity = Number.parseFloat(style.opacity || '1');
                if (style.display === 'none' || style.visibility === 'hidden'
                        || (current === node && style.pointerEvents === 'none')
                        || (Number.isFinite(opacity) && opacity <= 0
                            && !(codeProxy && current === node))) {
                    return false;
                }
            }
            return true;
        }"""))
        if not eligible:
            return False
        # Trial mode performs Playwright's hit-target/actionability checks but
        # never dispatches the click.  It distinguishes the active responsive
        # form from the covered duplicate currently rendered by X.
        await locator.click(trial=True, timeout=1_000)
        return True
    except Exception:  # noqa: BLE001 - ambiguous DOM must fail closed
        return False


async def _actionable_candidates(locator, *, limit=10):
    try:
        count = await locator.count()
    except Exception:  # noqa: BLE001
        return []
    if count > limit:
        raise XBrowserPageChanged("X login controls are ambiguous")
    candidates = []
    for index in range(min(count, limit)):
        candidate = locator.nth(index)
        if await _is_actionable(candidate):
            candidates.append(candidate)
    return candidates


async def _single_actionable(locator, message):
    candidates = await _actionable_candidates(locator)
    if len(candidates) != 1:
        raise XBrowserPageChanged(message)
    return candidates[0]


async def _unique_actionable(locator, message):
    candidates = await _actionable_candidates(locator)
    if len(candidates) > 1:
        raise XBrowserPageChanged(message)
    return candidates[0] if candidates else None


async def _body_text(page):
    try:
        return (await page.locator("body").inner_text(timeout=2_000)).lower()
    except Exception:  # noqa: BLE001
        return ""


def _classify_text(text):
    text = str(text or "").lower()
    if any(term in text for term in (
        "captcha", "arkose", "security key", "passkey", "scan the qr",
        "approve this login", "check your other device",
    )):
        return "unsupported"
    if any(term in text for term in (
        "temporarily limited your login", "too many login attempts",
        "try again later",
    )):
        return "rate_limited"
    if any(term in text for term in (
        "wrong password", "incorrect password", "could not find your account",
        "we cannot find your account", "couldn't find an active x account",
        "account doesn’t exist", "account doesn't exist",
    )):
        return "credentials"
    if any(term in text for term in (
        "account is suspended", "account has been suspended", "account is locked",
    )):
        return "credentials"
    if any(term in text for term in (
        "authentication app", "authentication code", "two-factor authentication",
        "code generator",
    )):
        return "two_factor"
    if any(term in text for term in (
        "verification code", "confirmation code", "enter the code", "we sent a code",
    )):
        return "verification"
    if any(term in text for term in (
        "confirm your email", "confirm your phone", "phone number or email",
        "email address or phone", "enter your phone number", "enter your email",
    )):
        return "alternate_identifier"
    return None


def _explicit_challenge_rejection(text):
    return any(term in text for term in (
        "incorrect code", "wrong code", "invalid code", "code was incorrect",
        "code has expired", "code expired", "try again",
    ))


async def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise XBrowserCancelled("X browser login was cancelled")


async def _request_challenge(challenge_handler, kind, cancel_event):
    """Await Telegram input while allowing an explicit cancellation event to win."""
    response_task = asyncio.create_task(challenge_handler(kind, ""))
    cancel_task = None
    try:
        if cancel_event is None:
            return await response_task
        cancel_task = asyncio.create_task(cancel_event.wait())
        done, _pending = await asyncio.wait(
            {response_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done and cancel_event.is_set():
            response_task.cancel()
            await asyncio.gather(response_task, return_exceptions=True)
            raise XBrowserCancelled("X browser login was cancelled")
        return response_task.result()
    finally:
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
        if not response_task.done():
            response_task.cancel()


def _require_allowed_page(page):
    if getattr(page, "_tg2fb_untrusted_redirect", False) or not _top_level_allowed(page.url):
        raise XBrowserUnsupportedChallenge("X redirected login outside its reviewed hosts")


async def _wait_for_stage(
    page,
    context,
    *,
    cancel_event=None,
    timeout_ms=STEP_TIMEOUT_MS,
    allow_password=False,
    transition=None,
):
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        await _check_cancel(cancel_event)
        _require_allowed_page(page)
        cookies = await context.cookies(["https://x.com/"])
        names = {item.get("name") for item in cookies if isinstance(item, dict)}
        if {"auth_token", "ct0"}.issubset(names):
            return "success", cookies
        text = await _body_text(page)
        classification = _classify_text(text)
        if classification == "unsupported":
            raise XBrowserUnsupportedChallenge("X requested an unsupported challenge")
        if classification == "rate_limited":
            raise XBrowserRateLimited("X temporarily limited login attempts")
        if classification == "credentials":
            raise XBrowserCredentialsRejected("X rejected the account credentials")
        if transition is not None:
            if not await _transition_completed(page, transition, text):
                await page.wait_for_timeout(200)
                continue
            transition = None
        if allow_password:
            if await _unique_actionable(
                page.locator(PASSWORD_SELECTOR),
                "X password field is ambiguous",
            ) is not None:
                return "password", None
        if await _unique_actionable(
            page.locator(GENERIC_INPUT_SELECTOR),
            "X verification field is ambiguous",
        ) is not None:
            if classification in {
                "alternate_identifier", "verification", "two_factor",
            }:
                return classification, None
            raise XBrowserUnsupportedChallenge("X requested an unrecognized login challenge")
        await page.wait_for_timeout(200)
    raise XBrowserPageChanged("X login did not reach a supported step")


async def _capture_transition(page, field, selector):
    try:
        handle = await field.element_handle(timeout=STEP_TIMEOUT_MS)
    except Exception:  # noqa: BLE001
        handle = None
    if handle is None:
        raise XBrowserPageChanged("X login field identity is unavailable")
    body = await _body_text(page)
    return {
        "handle": handle,
        "url": str(page.url),
        "body": body,
        "classification": _classify_text(body),
        "selector": selector,
        "password_actionable": (
            await _unique_actionable(
                page.locator(PASSWORD_SELECTOR),
                "X password field is ambiguous",
            ) is not None
        ),
    }


async def _transition_completed(page, marker, current_text):
    """Prove X acknowledged a submission before interpreting the next DOM."""
    current_classification = _classify_text(current_text)
    reviewed_challenge = current_classification in {
        "alternate_identifier", "verification", "two_factor",
    }
    challenge_field = None
    if reviewed_challenge:
        challenge_field = await _unique_actionable(
            page.locator(GENERIC_INPUT_SELECTOR),
            "X verification field is ambiguous",
        )
    if (
        marker["selector"] == USERNAME_SELECTOR
        and not marker.get("password_actionable")
        and await _unique_actionable(
            page.locator(PASSWORD_SELECTOR),
            "X password field is ambiguous",
        ) is not None
    ):
        # The JF page keeps its password node mounted as an opacity-zero decoy
        # during the username step, then enables that same node after X accepts
        # the account identifier.
        return True
    if marker["selector"] in {USERNAME_SELECTOR, PASSWORD_SELECTOR}:
        if challenge_field is not None:
            return True
    if (
        marker["selector"] == GENERIC_INPUT_SELECTOR
        and challenge_field is not None
        and current_classification != marker["classification"]
    ):
        return True
    old = marker["handle"]
    node_changed = False
    try:
        if not await old.evaluate("node => node.isConnected"):
            node_changed = True
        current = await _unique_actionable(
            page.locator(marker["selector"]),
            "X login field is ambiguous",
        )
        if not node_changed and current is None:
            # A loading overlay may temporarily hide the same still-attached
            # input. Hidden is not proof that X accepted the submitted secret.
            return False
        if not node_changed:
            current_handle = await current.element_handle(timeout=1_000)
            if current_handle is None:
                return False
            node_changed = not await old.evaluate(
                "(old, other) => old === other", current_handle,
            )
    except Exception:  # noqa: BLE001 - protocol ambiguity must fail closed
        raise XBrowserPageChanged(
            "X login field transition could not be verified"
        ) from None
    # A same-node credentials error is an explicit server acknowledgement and
    # terminates the attempt; it never causes another secret to be filled.
    if current_text != marker["body"] and current_classification == "credentials":
        return True
    if node_changed:
        if marker["selector"] == USERNAME_SELECTOR:
            return await _unique_actionable(
                page.locator(PASSWORD_SELECTOR),
                "X password field is ambiguous",
            ) is not None or (
                current_classification in {
                    "alternate_identifier", "verification", "two_factor",
                }
                and await _unique_actionable(
                    page.locator(GENERIC_INPUT_SELECTOR),
                    "X verification field is ambiguous",
                ) is not None
            )
        if marker["selector"] == PASSWORD_SELECTOR:
            return (
                current_classification in {
                    "alternate_identifier", "verification", "two_factor",
                }
                and await _unique_actionable(
                    page.locator(GENERIC_INPUT_SELECTOR),
                    "X verification field is ambiguous",
                ) is not None
            )
        if marker["selector"] == GENERIC_INPUT_SELECTOR:
            if current_classification != marker["classification"]:
                return True
    if (
        current_text != marker["body"]
        and not _explicit_challenge_rejection(marker["body"])
        and _explicit_challenge_rejection(current_text)
    ):
        try:
            # X may reuse the same input for a rejected OTP.  Only treat that
            # as a new attempt once X itself has cleared the submitted value.
            current = await _unique_actionable(
                page.locator(marker["selector"]),
                "X login field is ambiguous",
            )
            if current is None:
                return False
            return not (await current.input_value()).strip()
        except Exception:
            raise XBrowserPageChanged(
                "X challenge rejection could not be verified"
            ) from None
    return False


async def _fill_and_next(page, selector, value, *, login=False):
    _require_allowed_page(page)
    field = await _single_actionable(
        page.locator(selector), "X login input is not actionable",
    )
    transition = await _capture_transition(page, field, selector)
    _require_allowed_page(page)
    await field.fill(value, timeout=STEP_TIMEOUT_MS)
    _require_allowed_page(page)
    button = None
    form = field.locator("xpath=ancestor::form[1]")
    if await form.count() == 1:
        scoped = await _actionable_candidates(form.locator(LOGIN_SELECTOR))
        if len(scoped) > 1:
            raise XBrowserPageChanged("X login submit control is ambiguous")
        if scoped:
            button = scoped[0]
    if button is None:
        button_selector = LOGIN_SELECTOR if login else NEXT_SELECTOR
        button = await _single_actionable(
            page.locator(button_selector),
            "X login submit control is unavailable",
        )
    _require_allowed_page(page)
    await button.click(timeout=STEP_TIMEOUT_MS)
    return transition


async def _fill(page, selector, value):
    _require_allowed_page(page)
    field = page.locator(selector).first
    await field.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
    _require_allowed_page(page)
    await field.fill(value, timeout=STEP_TIMEOUT_MS)


async def _click(page, selector):
    _require_allowed_page(page)
    button = page.locator(selector).first
    await button.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
    _require_allowed_page(page)
    await button.click(timeout=STEP_TIMEOUT_MS)


async def _fill_and_submit_static_form(page, username, password):
    """Fill and submit one reviewed full login form.

    X creates its ``Continue`` submit control only after both fields contain a
    value.  Resolve that control inside the *same* owning form as the two
    fields; the page also contains responsive duplicate forms and unrelated
    phone/Apple buttons, so a page-global click would be ambiguous.
    """
    _require_allowed_page(page)
    password_field = await _single_actionable(
        page.locator(PASSWORD_SELECTOR),
        "X full login password field is not actionable",
    )
    transition = await _capture_transition(page, password_field, PASSWORD_SELECTOR)
    form = password_field.locator("xpath=ancestor::form[1]")
    await form.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
    username_field = await _single_actionable(
        form.locator(USERNAME_SELECTOR), "X full login form is ambiguous",
    )
    password_field = await _single_actionable(
        form.locator(PASSWORD_SELECTOR), "X full login form is ambiguous",
    )
    _require_allowed_page(page)
    await username_field.fill(username, timeout=STEP_TIMEOUT_MS)
    _require_allowed_page(page)
    await password_field.fill(password, timeout=STEP_TIMEOUT_MS)
    _require_allowed_page(page)
    submit = await _single_actionable(
        form.locator(LOGIN_SELECTOR),
        "X full login submit control is ambiguous",
    )
    _require_allowed_page(page)
    await submit.click(timeout=STEP_TIMEOUT_MS)
    return transition


async def _dismiss_cookie_banner(page):
    for selector in COOKIE_REJECT_SELECTORS:
        locator = page.locator(selector)
        if await _is_visible(locator):
            try:
                await locator.first.click(timeout=2_000)
            except Exception:  # noqa: BLE001 - banner may disappear concurrently
                pass
            return


async def _safe_call(obj, method):
    if obj is None:
        return False
    callback = getattr(obj, method, None)
    if not callable(callback):
        return False
    try:
        await asyncio.wait_for(callback(), timeout=10)
        return True
    except Exception:  # cleanup is best-effort; cancellation must propagate
        return False


async def _cleanup_resources(page, context, browser, manager, starter):
    await _safe_call(page, "close")
    context_closed = await _safe_call(context, "close")
    browser_closed = await _safe_call(browser, "close")
    if manager is not None:
        manager_closed = await _safe_call(manager, "stop")
    else:
        # Playwright creates its driver connection before ``start()`` returns.
        # Keeping the context-manager object lets cancellation of that await
        # still stop the partially-created driver/process.
        manager_closed = await _safe_call(starter, "__aexit__")
    if context is not None and not (
        context_closed or browser_closed or manager_closed
    ):
        raise XBrowserCleanupError("Authenticated browser cleanup could not be confirmed")


async def _cleanup_preserving_cancellation(page, context, browser, manager, starter):
    """Finish bounded cleanup, then faithfully propagate task cancellation."""
    cleanup = asyncio.create_task(
        _cleanup_resources(page, context, browser, manager, starter)
    )
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    cleanup_error = None
    try:
        cleanup.result()
    except Exception as exc:  # noqa: BLE001 - fixed type/message below
        cleanup_error = exc
    if cleanup_error is not None:
        if isinstance(cleanup_error, XBrowserCleanupError):
            raise cleanup_error
        raise XBrowserCleanupError(
            "Authenticated browser cleanup could not be confirmed"
        ) from None
    if cancelled:
        raise asyncio.CancelledError


async def _install_navigation_guard(page):
    """Abort every untrusted top-level navigation before it can receive input."""
    route_method = getattr(page, "route", None)
    if not callable(route_method):
        raise XBrowserPageChanged("Browser navigation guard is unavailable")

    async def guard(route, request):
        try:
            top_level = request.is_navigation_request() and request.frame == page.main_frame
        except Exception:
            await route.abort()
            return
        if top_level and not _top_level_allowed(request.url):
            setattr(page, "_tg2fb_untrusted_redirect", True)
            await route.abort()
            return
        await route.continue_()

    await route_method("**/*", guard)


async def _obtain_cookies(
    credentials,
    challenge_handler,
    *,
    cancel_event=None,
    playwright_factory=None,
):
    if not callable(challenge_handler):
        raise TypeError("challenge_handler must be callable")
    if _unsafe_debug_environment():
        raise XBrowserUnavailable("Unsafe browser diagnostics are enabled")
    username, password, alternate = _validate_credentials(credentials)
    starter = manager = browser = context = page = None
    try:
        if playwright_factory is None:
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                raise XBrowserUnavailable("Playwright is not installed") from None
            playwright_factory = async_playwright
        try:
            starter = playwright_factory()
            manager = await starter.start()
            browser = await manager.chromium.launch(
                headless=True,
                chromium_sandbox=True,
            )
            context = await browser.new_context(
                locale="en-US",
                timezone_id="UTC",
                viewport={"width": 1280, "height": 800},
                accept_downloads=False,
            )
            page = await context.new_page()
        except Exception:  # noqa: BLE001
            raise XBrowserUnavailable("Chromium login is unavailable") from None

        await _install_navigation_guard(page)
        await _check_cancel(cancel_event)
        try:
            response = await page.goto(
                LOGIN_URL,
                wait_until="domcontentloaded",
                timeout=STEP_TIMEOUT_MS,
            )
        except Exception:  # noqa: BLE001
            raise XBrowserUnavailable("X login page is unavailable") from None
        if response is None or not 200 <= response.status < 400:
            raise XBrowserUnavailable("X login page is unavailable")
        if not _top_level_allowed(page.url):
            raise XBrowserUnsupportedChallenge("X redirected login outside its reviewed hosts")

        await _dismiss_cookie_banner(page)

        # X currently serves two reviewed variants: the older multi-step flow,
        # and a responsive full form with username and password visible together.
        # Detect the latter before clicking anything; never guess a button when
        # the password field is not actually present.
        static_form = await _unique_actionable(
            page.locator(PASSWORD_SELECTOR),
            "X password field is ambiguous",
        ) is not None
        if static_form:
            transition = await _fill_and_submit_static_form(page, username, password)
            username = None
            password = None
            stage, cookies = await _wait_for_stage(
                page, context, cancel_event=cancel_event,
                transition=transition,
            )
        else:
            transition = await _fill_and_next(page, USERNAME_SELECTOR, username)
            username = None
            stage, cookies = await _wait_for_stage(
                page, context, cancel_event=cancel_event,
                allow_password=True,
                transition=transition,
            )

        challenge_attempts = 0
        password_submitted = static_form
        while stage != "success":
            if stage == "password":
                if password_submitted:
                    raise XBrowserCredentialsRejected("X rejected the account credentials")
                transition = await _fill_and_next(
                    page, PASSWORD_SELECTOR, password, login=True,
                )
                password = None
                password_submitted = True
                stage, cookies = await _wait_for_stage(
                    page,
                    context,
                    cancel_event=cancel_event,
                    transition=transition,
                )
                continue
            if stage not in {"alternate_identifier", "verification", "two_factor"}:
                raise XBrowserUnsupportedChallenge("X requested an unsupported challenge")
            challenge_attempts += 1
            if challenge_attempts > MAX_CHALLENGE_ATTEMPTS:
                raise XBrowserChallengeRejected("X rejected too many verification responses")
            kind = stage
            value = None
            if kind == "alternate_identifier" and alternate:
                value = alternate
                alternate = None
            if value is None:
                value = await _request_challenge(
                    challenge_handler, kind, cancel_event,
                )
            await _check_cancel(cancel_event)
            _require_allowed_page(page)
            if not isinstance(value, str) or not value.strip():
                raise XBrowserChallengeRejected("X verification response was not provided")
            if any(ord(char) < 32 for char in value) or len(value) > 254:
                raise XBrowserChallengeRejected("X verification response was invalid")
            if kind == "two_factor" and (
                len(value.strip()) != 6 or not value.strip().isdigit()
            ):
                raise XBrowserChallengeRejected("X authenticator code was invalid")
            transition = await _fill_and_next(
                page, GENERIC_INPUT_SELECTOR, value.strip(),
            )
            value = None
            stage, cookies = await _wait_for_stage(
                page, context, cancel_event=cancel_event,
                allow_password=not password_submitted,
                transition=transition,
            )

        await _check_cancel(cancel_event)
        return playwright_to_twikit(cookies)
    except asyncio.CancelledError:
        raise
    except XBrowserError:
        raise
    except Exception:  # noqa: BLE001 - never expose Playwright/DOM details or secrets
        raise XBrowserPageChanged("X login page could not be completed") from None
    finally:
        username = password = alternate = None
        await _cleanup_preserving_cancellation(
            page, context, browser, manager, starter,
        )


async def obtain_cookies(
    credentials,
    challenge_handler,
    *,
    cancel_event=None,
    total_timeout=TOTAL_TIMEOUT,
    playwright_factory=None,
):
    """Return verified-shape X cookies while keeping every secret in memory only."""
    login_task = asyncio.create_task(
        _obtain_cookies(
            credentials,
            challenge_handler,
            cancel_event=cancel_event,
            playwright_factory=playwright_factory,
        )
    )
    cancel_task = None

    async def stop_login_task():
        """Cancel the flow, but never hide an unconfirmed browser cleanup."""
        if not login_task.done():
            login_task.cancel()
        drain = asyncio.gather(login_task, return_exceptions=True)
        while not drain.done():
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError:
                # Repeated /cancel + restart cancellations must not interrupt
                # inspection of the authenticated browser cleanup result.
                continue
        result = drain.result()[0]
        if isinstance(result, XBrowserCleanupError):
            raise result

    try:
        waiters = {login_task}
        if cancel_event is not None:
            cancel_task = asyncio.create_task(cancel_event.wait())
            waiters.add(cancel_task)
        done, _pending = await asyncio.wait(
            waiters,
            timeout=total_timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        # Cancellation wins even if Chromium completed in the same event-loop
        # turn.  This prevents verified cookies from being returned after the
        # Telegram user already pressed Cancel.
        if cancel_event is not None and cancel_event.is_set():
            await stop_login_task()
            raise XBrowserCancelled("X browser login was cancelled")
        if login_task in done:
            return login_task.result()
        await stop_login_task()
        raise XBrowserUnavailable("X browser login timed out") from None
    except asyncio.CancelledError:
        # An external task cancellation must also stop the current Playwright
        # operation (including goto/start/locator waits), while _obtain_cookies
        # completes its shielded browser cleanup before we propagate it.
        await stop_login_task()
        raise
    finally:
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
        if cancel_task is not None:
            await asyncio.gather(cancel_task, return_exceptions=True)
