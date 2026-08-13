import asyncio

import pytest

import xbrowser


GOOD_COOKIES = [
    {"name": "auth_token", "value": "auth-value", "domain": ".x.com", "path": "/", "expires": -1},
    {"name": "ct0", "value": "csrf-value", "domain": ".x.com", "path": "/", "expires": -1},
    {"name": "twid", "value": "user-id", "domain": "x.com", "path": "/", "expires": -1},
]


class FakeResponse:
    status = 200


class FakeElementHandle:
    def __init__(self, locator):
        self.page = locator.page
        self.selector = locator.selector
        self.generation = locator.page.dom_generation

    async def is_visible(self):
        return (
            self.generation == self.page.dom_generation
            and FakeLocator(self.page, self.selector)._visible()
        )

    async def evaluate(self, expression, other=None):
        if self.page.fail_transition_evaluate:
            self.page.fail_transition_evaluate = False
            raise RuntimeError("transient protocol error")
        if "isConnected" in expression:
            return self.generation == self.page.dom_generation
        return (
            isinstance(other, FakeElementHandle)
            and self.selector == other.selector
            and self.generation == other.generation
        )

    async def input_value(self):
        return self.page.field_values.get((self.generation, self.selector), "")


class FakeLocator:
    def __init__(self, page, selector, *, kind=None):
        self.page = page
        self.selector = selector
        self.kind = kind
        self.first = self

    def _visible(self):
        state = self.page.state
        if self.kind == "form":
            return True
        if self.kind == "form_buttons":
            return self.page.form_submit_count > 0
        if self.selector == "body":
            return True
        if self.selector == xbrowser.USERNAME_SELECTOR:
            return state in {"username", "jf_username", "static"}
        if self.selector == xbrowser.PASSWORD_SELECTOR:
            return state in {"password", "jf_username", "static"}
        if self.selector == xbrowser.GENERIC_INPUT_SELECTOR:
            return (
                state in {
                    "alternate", "verification", "two_factor",
                    "two_factor_rejected", "verification_rejected",
                }
                and not self.page.hide_generic
            )
        if self.selector == xbrowser.NEXT_SELECTOR:
            return state in {
                "username", "alternate", "verification", "two_factor",
                "two_factor_rejected", "verification_rejected",
            }
        if self.selector == xbrowser.LOGIN_SELECTOR:
            return state in {"password", "static"}
        if self.selector in xbrowser.COOKIE_REJECT_SELECTORS:
            return False
        return False

    async def count(self):
        if self.kind == "form_buttons":
            return self.page.form_submit_count
        return int(self._visible())

    def nth(self, _index):
        return self

    async def evaluate(self, _expression):
        if self.selector == xbrowser.PASSWORD_SELECTOR and self.page.state == "jf_username":
            return False
        return self._visible()

    async def is_visible(self):
        return self._visible()

    async def wait_for(self, **_kwargs):
        if not self._visible():
            raise RuntimeError("not visible")

    async def fill(self, value, **_kwargs):
        self.page.fills.append((self.page.state, value))
        self.page.field_values[(self.page.dom_generation, self.selector)] = value
        if self.page.fail_fill:
            raise RuntimeError(value)

    async def element_handle(self, **_kwargs):
        if not self._visible():
            return None
        return FakeElementHandle(self)

    async def input_value(self):
        return self.page.field_values.get(
            (self.page.dom_generation, self.selector), "",
        )

    async def click(self, **_kwargs):
        if _kwargs.get("trial"):
            return
        self.page.clicks.append(self.page.state)
        self.page.clicked_locators.append((self.kind, self.selector))
        if not self.page.stages:
            raise RuntimeError("no next stage")
        self.page.advance(self.page.stages.pop(0))

    async def press(self, key, **_kwargs):
        assert key == "Enter"
        self.page.clicks.append((self.page.state, "Enter"))
        if not self.page.stages:
            raise RuntimeError("no next stage")
        self.page.advance(self.page.stages.pop(0))

    async def inner_text(self, **_kwargs):
        return {
            "alternate": "Confirm your email or phone number",
            "verification": "Enter the verification code sent to your email",
            "two_factor": "Enter the code from your authentication app",
            "two_factor_rejected": "Incorrect code. Try again with your authentication app",
            "verification_rejected": "Incorrect code. Try again with the verification code",
            "unsupported": "Complete this CAPTCHA with a security key",
            "credentials": "Wrong password",
        }.get(self.page.state, "Sign in to X")

    def locator(self, selector):
        if selector == "xpath=ancestor::form[1]":
            return FakeLocator(self.page, selector, kind="form")
        if self.kind == "form" and selector == xbrowser.LOGIN_SELECTOR:
            return FakeLocator(self.page, selector, kind="form_buttons")
        return FakeLocator(self.page, selector)


class FakePage:
    def __init__(
        self,
        stages,
        events,
        *,
        initial_state="username",
        fail_fill=False,
        evil_redirect=False,
        hang_goto=False,
        block_close=False,
        transition_delay=0,
        url_changes_before_dom=False,
        form_submit_count=1,
        initial_ready_polls=0,
    ):
        self.state = "new"
        self.stages = list(stages)
        self.events = events
        self.fail_fill = fail_fill
        self.evil_redirect = evil_redirect
        self.hang_goto = hang_goto
        self.initial_state = initial_state
        self.initial_ready_polls = initial_ready_polls
        self.url = xbrowser.LOGIN_URL
        self.fills = []
        self.clicks = []
        self.main_frame = object()
        self.route_handler = None
        self.block_close = block_close
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.goto_started = asyncio.Event()
        self.transition_delay = transition_delay
        self.url_changes_before_dom = url_changes_before_dom
        self.dom_generation = 0
        self.field_values = {}
        self.hide_generic = False
        self.fail_transition_evaluate = False
        self.form_submit_count = form_submit_count
        self.clicked_locators = []

    def advance(self, next_state):
        async def later():
            await asyncio.sleep(self.transition_delay)
            self.dom_generation += 1
            self.state = next_state

        if self.transition_delay:
            if self.url_changes_before_dom:
                self.url = xbrowser.LOGIN_URL + "?client-step=sent"
            asyncio.create_task(later())
        else:
            self.dom_generation += 1
            self.state = next_state

    async def goto(self, *_args, **_kwargs):
        self.goto_started.set()
        if self.hang_goto:
            await asyncio.Event().wait()
        self.state = "loading" if self.initial_ready_polls else self.initial_state
        if self.evil_redirect:
            self.url = "https://evil.example/login"
        return FakeResponse()

    def locator(self, selector):
        return FakeLocator(self, selector)

    async def route(self, pattern, handler):
        assert pattern == "**/*"
        self.route_handler = handler

    async def wait_for_timeout(self, _milliseconds):
        if self.initial_ready_polls:
            self.initial_ready_polls -= 1
            if not self.initial_ready_polls:
                self.dom_generation += 1
                self.state = self.initial_state
        await asyncio.sleep(min(_milliseconds / 1000, 0.01))

    async def close(self):
        self.close_started.set()
        if self.block_close:
            await self.release_close.wait()
        self.events.append("page.close")


class FakeContext:
    def __init__(self, page, events, cookies=None, fail_close=False):
        self.page = page
        self.events = events
        self.cookie_values = list(cookies or GOOD_COOKIES)
        self.fail_close = fail_close

    async def new_page(self):
        return self.page

    async def cookies(self, _urls):
        return self.cookie_values if self.page.state == "success" else []

    async def close(self):
        self.events.append("context.close")
        if self.fail_close:
            raise RuntimeError("close failed")


class FakeBrowser:
    def __init__(self, context, events, *, fail_close=False):
        self.context = context
        self.events = events
        self.launch_kwargs = None
        self.context_kwargs = None
        self.fail_close = fail_close

    async def new_context(self, **kwargs):
        self.context_kwargs = kwargs
        return self.context

    async def close(self):
        self.events.append("browser.close")
        if self.fail_close:
            raise RuntimeError("browser close failed")


class FakeChromium:
    def __init__(self, browser):
        self.browser = browser

    async def launch(self, **kwargs):
        self.browser.launch_kwargs = kwargs
        return self.browser


class FakeManager:
    def __init__(self, browser, events, *, fail_stop=False):
        self.chromium = FakeChromium(browser)
        self.events = events
        self.fail_stop = fail_stop

    async def stop(self):
        self.events.append("manager.stop")
        if self.fail_stop:
            raise RuntimeError("manager stop failed")


class FakeStarter:
    def __init__(self, manager, events, *, block_start=False):
        self.manager = manager
        self.events = events
        self.block_start = block_start
        self.start_started = asyncio.Event()
        self.release_start = asyncio.Event()

    async def start(self):
        self.start_started.set()
        if self.block_start:
            await self.release_start.wait()
        return self.manager

    async def __aexit__(self, *_args):
        self.events.append("starter.stop")


class FakeFactory:
    def __init__(self, starter):
        self.starter = starter

    def __call__(self):
        return self.starter


def fake_runtime(
    stages,
    *,
    cookies=None,
    initial_state="username",
    fail_fill=False,
    evil_redirect=False,
    hang_goto=False,
    block_page_close=False,
    block_start=False,
    transition_delay=0,
    url_changes_before_dom=False,
    fail_context_close=False,
    fail_browser_close=False,
    fail_manager_stop=False,
    form_submit_count=1,
    initial_ready_polls=0,
):
    events = []
    page = FakePage(
        stages, events,
        initial_state=initial_state,
        fail_fill=fail_fill,
        evil_redirect=evil_redirect,
        hang_goto=hang_goto,
        block_close=block_page_close,
        transition_delay=transition_delay,
        url_changes_before_dom=url_changes_before_dom,
        form_submit_count=form_submit_count,
        initial_ready_polls=initial_ready_polls,
    )
    context = FakeContext(page, events, cookies=cookies, fail_close=fail_context_close)
    browser = FakeBrowser(context, events, fail_close=fail_browser_close)
    manager = FakeManager(browser, events, fail_stop=fail_manager_stop)
    starter = FakeStarter(manager, events, block_start=block_start)
    return FakeFactory(starter), page, browser, events


def run_login(factory, handler, credentials=None, **kwargs):
    credentials = credentials or {
        "username": "reader",
        "email": None,
        "password": "account-password",
    }
    return asyncio.run(xbrowser.obtain_cookies(
        credentials,
        handler,
        playwright_factory=factory,
        **kwargs,
    ))


def test_jf_password_decoy_is_visible_but_not_actionable():
    _factory, page, _browser, _events = fake_runtime(
        [], initial_state="jf_username",
    )
    page.state = "jf_username"
    password = page.locator(xbrowser.PASSWORD_SELECTOR).first

    assert asyncio.run(password.is_visible()) is True
    assert asyncio.run(xbrowser._is_actionable(password)) is False


def test_actionability_defers_ancestor_pointer_targeting_to_trial_click():
    class AncestorPointerLocator:
        async def evaluate(self, expression):
            # The regression contract is visible in the reviewed JS: an
            # ancestor may disable its own hit target while a child restores
            # pointer-events:auto. Playwright's trial click is authoritative.
            assert "current === node && style.pointerEvents === 'none'" in expression
            return True

        async def click(self, *, trial, timeout):
            assert trial is True
            assert timeout == 1_000

    assert asyncio.run(
        xbrowser._is_actionable(AncestorPointerLocator())
    ) is True


def test_current_jf_failure_messages_are_classified_without_remote_details():
    assert xbrowser._classify_text(
        "We couldn't find an active X account with that username."
    ) == "credentials"
    assert xbrowser._classify_text(
        "We’ve temporarily limited your login. Please try again later."
    ) == "rate_limited"


def test_browser_login_without_challenge_returns_memory_cookies_and_closes_all():
    factory, page, browser, events = fake_runtime(["password", "success"])

    result = run_login(factory, lambda *_args: None)

    assert result["auth_token"] == "auth-value"
    assert result["ct0"] == "csrf-value"
    assert page.fills == [("username", "reader"), ("password", "account-password")]
    assert browser.launch_kwargs == {"headless": True, "chromium_sandbox": True}
    assert browser.context_kwargs["accept_downloads"] is False
    assert events == ["page.close", "context.close", "browser.close", "manager.stop"]


def test_browser_waits_for_lazy_initial_form_before_filling_any_value():
    factory, page, _browser, events = fake_runtime(
        ["password", "success"], initial_ready_polls=4,
    )

    result = run_login(factory, lambda *_args: None)

    assert result["auth_token"] == "auth-value"
    assert page.fills == [
        ("username", "reader"),
        ("password", "account-password"),
    ]
    assert events[-1] == "manager.stop"


def test_initial_form_readiness_timeout_fails_without_filling_or_clicking():
    factory, page, _browser, events = fake_runtime(
        [], initial_ready_polls=100,
    )
    context = factory.starter.manager.chromium.browser.context

    with pytest.raises(xbrowser.XBrowserPageChanged) as caught:
        asyncio.run(xbrowser._wait_for_initial_login_stage(
            page, context, timeout_ms=5,
        ))

    assert caught.value.reason == "initial_controls_timeout"
    assert page.fills == []
    assert page.clicks == []
    assert events == []


def test_initial_form_readiness_honors_cancellation_before_any_secret_fill():
    factory, page, _browser, _events = fake_runtime(
        [], initial_ready_polls=100,
    )
    context = factory.starter.manager.chromium.browser.context
    cancel = asyncio.Event()
    cancel.set()

    with pytest.raises(xbrowser.XBrowserCancelled):
        asyncio.run(xbrowser._wait_for_initial_login_stage(
            page, context, cancel_event=cancel,
        ))

    assert page.fills == []
    assert page.clicks == []


def test_current_static_full_form_fills_both_fields_then_submits_once():
    factory, page, _browser, events = fake_runtime(["success"], initial_state="static")

    result = run_login(factory, lambda *_args: None)

    assert result["auth_token"] == "auth-value"
    assert page.fills == [("static", "reader"), ("static", "account-password")]
    assert page.clicks == ["static"]
    assert page.clicked_locators == [("form_buttons", xbrowser.LOGIN_SELECTOR)]
    assert events[-1] == "manager.stop"


def test_current_jf_form_ignores_password_decoy_then_submits_each_real_step():
    factory, page, _browser, events = fake_runtime(
        ["password", "success"], initial_state="jf_username",
    )

    result = run_login(factory, lambda *_args: None)

    assert result["auth_token"] == "auth-value"
    assert page.fills == [
        ("jf_username", "reader"),
        ("password", "account-password"),
    ]
    assert page.clicks == ["jf_username", "password"]
    assert events[-1] == "manager.stop"


def test_static_full_form_rejects_ambiguous_submit_controls():
    factory, page, _browser, events = fake_runtime(
        ["success"], initial_state="static", form_submit_count=2,
    )

    with pytest.raises(xbrowser.XBrowserPageChanged, match="submit control"):
        run_login(factory, lambda *_args: None)

    assert page.clicks == []
    assert events[-1] == "manager.stop"


def test_browser_login_requests_alternate_then_authenticator_without_persisting_values():
    factory, page, _browser, events = fake_runtime(
        ["alternate", "password", "two_factor", "success"]
    )
    requested = []

    async def handler(kind, prompt):
        requested.append((kind, prompt))
        return {
            "alternate_identifier": "reader@example.test",
            "two_factor": "739184",
        }[kind]

    result = run_login(factory, handler)

    assert result["auth_token"] == "auth-value"
    assert requested == [("alternate_identifier", ""), ("two_factor", "")]
    assert page.fills == [
        ("username", "reader"),
        ("alternate", "reader@example.test"),
        ("password", "account-password"),
        ("two_factor", "739184"),
    ]
    assert events[-1] == "manager.stop"


def test_supplied_email_is_used_only_if_x_requests_alternate():
    factory, page, _browser, _events = fake_runtime(
        ["alternate", "password", "success"]
    )

    async def handler(*_args):
        raise AssertionError("stored alternate should be used")

    run_login(factory, handler, {
        "username": "reader",
        "email": "stored@example.test",
        "password": "password",
    })

    assert ("alternate", "stored@example.test") in page.fills


def test_browser_login_retries_verification_with_fresh_values():
    factory, page, _browser, _events = fake_runtime(
        ["password", "two_factor", "two_factor_rejected", "success"]
    )
    values = iter(["111111", "222222"])

    async def handler(kind, _prompt):
        assert kind == "two_factor"
        return next(values)

    run_login(factory, handler)

    assert page.fills[-2:] == [
        ("two_factor", "111111"), ("two_factor_rejected", "222222"),
    ]


def test_browser_waits_for_generic_dom_transition_before_requesting_next_code():
    factory, page, _browser, _events = fake_runtime(
        ["password", "two_factor", "two_factor_rejected", "success"],
        transition_delay=0.02,
    )
    asked = []

    async def handler(kind, _prompt):
        asked.append(kind)
        return "111111" if len(asked) == 1 else "222222"

    run_login(factory, handler)

    assert asked == ["two_factor", "two_factor"]
    assert page.fills[-2:] == [
        ("two_factor", "111111"), ("two_factor_rejected", "222222"),
    ]


def test_allowed_url_change_alone_does_not_acknowledge_old_challenge_dom():
    factory, page, _browser, _events = fake_runtime(
        ["password", "two_factor", "two_factor_rejected", "success"],
        transition_delay=0.02,
        url_changes_before_dom=True,
    )
    asked = []

    async def handler(kind, _prompt):
        asked.append(kind)
        return "111111" if len(asked) == 1 else "222222"

    run_login(factory, handler)

    assert asked == ["two_factor", "two_factor"]
    assert page.fills[-2:] == [
        ("two_factor", "111111"), ("two_factor_rejected", "222222"),
    ]


def test_temporarily_hidden_but_connected_challenge_is_not_a_transition():
    factory, page, _browser, _events = fake_runtime([])
    page.state = "two_factor"
    old = page.locator(xbrowser.GENERIC_INPUT_SELECTOR).first

    async def scenario():
        marker = await xbrowser._capture_transition(
            page, old, xbrowser.GENERIC_INPUT_SELECTOR,
        )
        page.hide_generic = True
        assert not await xbrowser._transition_completed(
            page, marker, "Enter the code from your authentication app",
        )
        page.hide_generic = False
        assert not await xbrowser._transition_completed(
            page, marker, "Enter the code from your authentication app",
        )

    asyncio.run(scenario())
    assert factory.starter.start_started.is_set() is False


def test_transition_protocol_error_fails_closed_instead_of_reusing_old_dom():
    factory, page, _browser, _events = fake_runtime(
        ["password", "two_factor", "two_factor"]
    )
    asked = []

    async def handler(kind, _prompt):
        asked.append(kind)
        page.fail_transition_evaluate = True
        return "111111"

    with pytest.raises(xbrowser.XBrowserPageChanged):
        run_login(factory, handler)

    assert asked == ["two_factor"]
    assert page.fills[-1:] == [("two_factor", "111111")]


def test_unknown_captcha_fails_closed_and_cleanup_continues_after_close_error():
    factory, _page, _browser, events = fake_runtime(
        ["unsupported"], fail_context_close=True,
    )

    with pytest.raises(xbrowser.XBrowserUnsupportedChallenge):
        run_login(factory, lambda *_args: None)

    assert events == ["page.close", "context.close", "browser.close", "manager.stop"]


def test_success_is_rejected_if_authenticated_context_cannot_be_confirmed_closed():
    factory, _page, _browser, events = fake_runtime(
        ["success"],
        initial_state="static",
        fail_context_close=True,
        fail_browser_close=True,
        fail_manager_stop=True,
    )

    with pytest.raises(xbrowser.XBrowserCleanupError):
        run_login(factory, lambda *_args: None)

    assert events == ["page.close", "context.close", "browser.close", "manager.stop"]


def test_redirect_outside_x_is_rejected_before_typing_credentials():
    factory, page, _browser, _events = fake_runtime([], evil_redirect=True)

    with pytest.raises(xbrowser.XBrowserUnsupportedChallenge):
        run_login(factory, lambda *_args: None)

    assert page.fills == []


def test_playwright_error_never_exposes_password_in_public_exception():
    factory, _page, _browser, events = fake_runtime(
        ["password", "success"], fail_fill=True,
    )

    with pytest.raises(xbrowser.XBrowserPageChanged) as caught:
        run_login(factory, lambda *_args: None)

    assert "account-password" not in str(caught.value)
    assert events[-1] == "manager.stop"


def test_pre_set_cancel_event_stops_before_navigation_and_closes_browser():
    factory, page, _browser, events = fake_runtime(["password", "success"])

    async def scenario():
        cancelled = asyncio.Event()
        cancelled.set()
        with pytest.raises(xbrowser.XBrowserCancelled):
            await xbrowser.obtain_cookies(
                {"username": "reader", "password": "secret"},
                lambda *_args: None,
                cancel_event=cancelled,
                playwright_factory=factory,
            )

    asyncio.run(scenario())
    assert page.state == "new"
    assert events == ["page.close", "context.close", "browser.close", "manager.stop"]


def test_cancel_event_interrupts_navigation_and_closes_browser():
    factory, page, _browser, events = fake_runtime([], hang_goto=True)

    async def scenario():
        cancel_event = asyncio.Event()
        task = asyncio.create_task(xbrowser.obtain_cookies(
            {"username": "reader", "password": "secret"},
            lambda *_args: None,
            cancel_event=cancel_event,
            playwright_factory=factory,
        ))
        await page.goto_started.wait()
        cancel_event.set()
        with pytest.raises(xbrowser.XBrowserCancelled):
            await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())
    assert events == ["page.close", "context.close", "browser.close", "manager.stop"]


def test_cancel_event_interrupts_pending_telegram_challenge_and_closes_browser():
    factory, _page, _browser, events = fake_runtime(
        ["password", "two_factor", "success"]
    )

    async def scenario():
        cancel_event = asyncio.Event()
        handler_started = asyncio.Event()
        handler_cancelled = asyncio.Event()

        async def handler(_kind, _prompt):
            handler_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                handler_cancelled.set()

        task = asyncio.create_task(xbrowser.obtain_cookies(
            {"username": "reader", "password": "secret"},
            handler,
            cancel_event=cancel_event,
            playwright_factory=factory,
        ))
        await handler_started.wait()
        cancel_event.set()
        with pytest.raises(xbrowser.XBrowserCancelled):
            await task
        assert handler_cancelled.is_set()

    asyncio.run(scenario())
    assert events == ["page.close", "context.close", "browser.close", "manager.stop"]


def test_total_timeout_cancels_navigation_and_still_closes_everything():
    factory, _page, _browser, events = fake_runtime([], hang_goto=True)

    with pytest.raises(xbrowser.XBrowserUnavailable, match="timed out"):
        run_login(factory, lambda *_args: None, total_timeout=0.01)

    assert events == ["page.close", "context.close", "browser.close", "manager.stop"]


@pytest.mark.parametrize("mode", ["cancel_event", "timeout", "task_cancel"])
def test_cancellation_never_hides_unconfirmed_authenticated_cleanup(mode):
    factory, page, _browser, _events = fake_runtime(
        [],
        hang_goto=True,
        fail_context_close=True,
        fail_browser_close=True,
        fail_manager_stop=True,
    )

    async def scenario():
        cancel_event = asyncio.Event()
        task = asyncio.create_task(xbrowser.obtain_cookies(
            {"username": "reader", "password": "secret"},
            lambda *_args: None,
            cancel_event=cancel_event,
            total_timeout=0.02 if mode == "timeout" else 10,
            playwright_factory=factory,
        ))
        await page.goto_started.wait()
        if mode == "cancel_event":
            cancel_event.set()
        elif mode == "task_cancel":
            task.cancel()
        with pytest.raises(xbrowser.XBrowserCleanupError):
            await task

    asyncio.run(scenario())


def test_cancel_during_cleanup_is_propagated_after_every_resource_closes():
    factory, page, _browser, events = fake_runtime(
        ["success"], initial_state="static", block_page_close=True,
    )

    async def scenario():
        task = asyncio.create_task(xbrowser.obtain_cookies(
            {"username": "reader", "password": "secret"},
            lambda *_args: None,
            playwright_factory=factory,
        ))
        await page.close_started.wait()
        task.cancel()
        page.release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()

    asyncio.run(scenario())
    assert events == ["page.close", "context.close", "browser.close", "manager.stop"]


def test_repeated_cancel_during_cleanup_cannot_hide_cleanup_failure():
    factory, page, _browser, events = fake_runtime(
        ["success"],
        initial_state="static",
        block_page_close=True,
        fail_context_close=True,
        fail_browser_close=True,
        fail_manager_stop=True,
    )

    async def scenario():
        task = asyncio.create_task(xbrowser.obtain_cookies(
            {"username": "reader", "password": "secret"},
            lambda *_args: None,
            playwright_factory=factory,
        ))
        await page.close_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        page.release_close.set()
        with pytest.raises(xbrowser.XBrowserCleanupError):
            await task

    asyncio.run(scenario())
    assert events == ["page.close", "context.close", "browser.close", "manager.stop"]


def test_timeout_during_playwright_start_stops_partial_driver():
    factory, _page, _browser, events = fake_runtime([], block_start=True)

    with pytest.raises(xbrowser.XBrowserUnavailable, match="timed out"):
        run_login(factory, lambda *_args: None, total_timeout=0.01)

    assert factory.starter.start_started.is_set()
    assert events == ["starter.stop"]


def test_redirect_during_challenge_callback_is_rejected_before_secret_fill():
    factory, page, _browser, _events = fake_runtime(
        ["password", "two_factor", "success"]
    )

    async def handler(kind, _prompt):
        assert kind == "two_factor"
        page.url = "https://evil.example/phish"
        return "739184"

    with pytest.raises(xbrowser.XBrowserUnsupportedChallenge):
        run_login(factory, handler)

    assert page.fills == [
        ("username", "reader"),
        ("password", "account-password"),
    ]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("DEBUGP", ""),
        ("PWDEBUG", "1"),
        ("DEBUG", "pw:protocol"),
        ("DEBUG", "playwright:*"),
        ("DEBUG", "*"),
    ],
)
def test_secret_printing_debug_environment_fails_before_browser_start(
    monkeypatch, key, value,
):
    factory, _page, _browser, events = fake_runtime([])
    monkeypatch.setenv(key, value)

    with pytest.raises(xbrowser.XBrowserUnavailable, match="diagnostics"):
        run_login(factory, lambda *_args: None)

    assert not factory.starter.start_started.is_set()
    assert events == []


@pytest.mark.parametrize(
    "cookies",
    [
        [],
        [GOOD_COOKIES[0]],
        [GOOD_COOKIES[1]],
        [
            GOOD_COOKIES[0], GOOD_COOKIES[1],
            {"name": "ct0", "value": "different", "domain": "x.com", "path": "/", "expires": -1},
        ],
        [
            {"name": "auth_token", "value": "secret", "domain": "evilx.com", "path": "/", "expires": -1},
            GOOD_COOKIES[1],
        ],
        [
            {"name": "auth_token", "value": "secret", "domain": ".x.com", "path": "/", "expires": 10},
            GOOD_COOKIES[1],
        ],
    ],
)
def test_cookie_conversion_rejects_incomplete_conflicting_or_untrusted_sessions(cookies):
    with pytest.raises(xbrowser.XBrowserSessionError):
        xbrowser.playwright_to_twikit(cookies, now=100)


def test_cookie_conversion_filters_unrelated_domains_and_expired_optional_values():
    cookies = GOOD_COOKIES + [
        {"name": "evil", "value": "value", "domain": "example.com", "path": "/", "expires": -1},
        {"name": "old", "value": "value", "domain": ".x.com", "path": "/", "expires": 10},
    ]

    result = xbrowser.playwright_to_twikit(cookies, now=100)

    assert result == {"auth_token": "auth-value", "ct0": "csrf-value", "twid": "user-id"}
