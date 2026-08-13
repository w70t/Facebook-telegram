"""اختبارات regressions لمسارات النشر والاستعادة الحساسة في main.py."""
import asyncio
import os
import re
import sys
import types

import pytest


RELOADABLE = ("main", "settings", "store", "util", "twitter", "facebook", "jsonio")
_MISSING = object()


def _telethon_modules():
    """يلتقط مساحة أسماء Telethon كاملة كي لا تتسرّب النسخة الحقيقية للاختبار."""
    return {
        name: module
        for name, module in sys.modules.items()
        if name == "telethon" or name.startswith("telethon.")
    }


@pytest.fixture
def app(tmp_path):
    saved_env = {
        key: os.environ.get(key)
        for key in ("SETTINGS_FILE", "API_ID", "API_HASH", "BOT_TOKEN")
    }
    saved_mods = {name: sys.modules.get(name) for name in RELOADABLE}
    saved_telethon = _telethon_modules()
    saved_stub = sys.modules.get("stub_telethon", _MISSING)
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    added_tests_dir = tests_dir not in sys.path
    main = None
    try:
        os.environ.update({
            "SETTINGS_FILE": str(tmp_path / "settings.json"),
            "API_ID": "1",
            "API_HASH": "hash",
            "BOT_TOKEN": "token",
        })
        for name in RELOADABLE:
            sys.modules.pop(name, None)
        for name in saved_telethon:
            sys.modules.pop(name, None)
        if added_tests_dir:
            sys.path.insert(0, tests_dir)
        import stub_telethon
        stub_telethon.install()

        import main as loaded_main
        main = loaded_main
        main.S.set("owner_id", 42)
        main.S.add_admin(42)
        main.S.set("review_chat_id", -100999)
        main.S.set("fb_page_id", "123456789")
        main.S.set("fb_page_token", "token")
        yield main
    finally:
        if main is not None:
            main._publishing.clear()
            main._published.clear()
            for task in list(main._x_login_tasks.values()):
                if task is not None and not task.done():
                    task.cancel()
            main._x_login_tasks.clear()
            main._x_login_deleting.clear()
            main._x_login_cancelled.clear()
            main._x_secret_tombstones.clear()
            for task in list(main._x_secret_tasks):
                if not task.done():
                    task.cancel()
            main._x_secret_tasks.clear()
            main._x_browser_cleanup_unconfirmed = False
            main._x_secret_delete_unconfirmed = False
            main._restarting = False
            restart_task = main._restart_task
            if restart_task is not None and not restart_task.done():
                restart_task.cancel()
            main._restart_task = None
            for future in list(main._x_challenges.values()):
                if not future.done():
                    future.cancel()
            main._x_challenges.clear()
            for item_id in list(main.PENDING.items):
                main.PENDING.remove(item_id)
        for name in RELOADABLE:
            sys.modules.pop(name, None)
        sys.modules.update({name: mod for name, mod in saved_mods.items() if mod is not None})
        for name in _telethon_modules():
            sys.modules.pop(name, None)
        sys.modules.update(saved_telethon)
        if saved_stub is _MISSING:
            sys.modules.pop("stub_telethon", None)
        else:
            sys.modules["stub_telethon"] = saved_stub
        if added_tests_dir:
            try:
                sys.path.remove(tests_dir)
            except ValueError:
                pass
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class Event:
    def __init__(
        self, app=None, item_id=None, fail_edit=False, data=b"", text="",
        sender_id=42, chat_id=None, is_private=None, fail_delete=False,
        fail_respond_at=None,
    ):
        self.app = app
        self.item_id = item_id
        self.fail_edit = fail_edit
        self.data = data
        self.text = text
        self.sender_id = sender_id
        self.chat_id = sender_id if chat_id is None else chat_id
        self.is_private = (
            self.chat_id == sender_id if is_private is None else is_private
        )
        self.fail_delete = fail_delete
        self.fail_respond_at = set(fail_respond_at or ())
        self.respond_calls = 0
        self.answers = []
        self.responses = []
        self.response_buttons = []
        self.edits = []
        self.deleted = False

    async def answer(self, text=None, alert=False):
        self.answers.append((text, alert))

    async def respond(self, text, buttons=None):
        self.respond_calls += 1
        if self.respond_calls in self.fail_respond_at:
            raise RuntimeError("Telegram respond failed")
        self.responses.append(text)
        self.response_buttons.append(buttons)

    async def edit(self, text):
        if self.app and self.item_id:
            assert self.app.PENDING.get(self.item_id) is None, (
                "يجب إزالة pending قبل محاولة تعديل رسالة Telegram"
            )
        if self.fail_edit:
            raise RuntimeError("Telegram edit failed")
        self.edits.append(text)

    async def delete(self):
        if self.fail_delete:
            raise RuntimeError("Telegram delete failed")
        self.deleted = True


def test_app_fixture_always_uses_stub_telethon(app):
    stub = sys.modules["stub_telethon"]
    assert app.TelegramClient is stub.TelegramClient
    assert sys.modules["telethon"].TelegramClient is stub.TelegramClient


def test_concurrent_publish_claim_allows_only_one_facebook_call(app, monkeypatch):
    item_id = app.PENDING.add("خبر مهم")
    calls = []

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_to_thread(fn, *args):
            calls.append((fn, args))
            started.set()
            await release.wait()
            return {"id": "post_1"}

        monkeypatch.setattr(app.asyncio, "to_thread", fake_to_thread)
        first = Event()
        second = Event()
        task = asyncio.create_task(app._publish(first, item_id, include_media=False))
        await started.wait()
        await app._publish(second, item_id, include_media=False)
        assert len(calls) == 1
        assert any("قيد النشر" in (text or "") for text, _ in second.answers)
        release.set()
        await task

    asyncio.run(scenario())
    assert app.PENDING.get(item_id) is None
    assert item_id not in app._publishing


def test_success_removes_pending_even_if_telegram_edit_fails(app, monkeypatch):
    item_id = app.PENDING.add("تم نشره")

    async def fake_to_thread(fn, *args):
        return {"id": "post_1"}

    monkeypatch.setattr(app.asyncio, "to_thread", fake_to_thread)
    event = Event(app=app, item_id=item_id, fail_edit=True)
    asyncio.run(app._publish(event, item_id, include_media=False))

    assert app.PENDING.get(item_id) is None
    assert item_id not in app._publishing


def test_queue_add_failure_returns_none_and_warns_without_sending(app, monkeypatch):
    notices = []
    sends = []

    def fail_add(*args, **kwargs):
        raise OSError("disk full")

    async def fake_notify(text):
        notices.append(text)

    async def fake_send(item_id):
        sends.append(item_id)

    monkeypatch.setattr(app.PENDING, "add", fail_add)
    monkeypatch.setattr(app, "_notify_owner", fake_notify)
    monkeypatch.setattr(app, "_send_for_review", fake_send)

    assert asyncio.run(app._queue_for_review("لن يضيع بصمت", [])) is None
    assert sends == []
    assert notices and "تعذّر حفظه" in notices[0]


def test_queue_send_failure_keeps_durable_item_for_startup_replay(app, monkeypatch):
    notices = []

    async def fail_send(item_id):
        raise RuntimeError("Telegram unavailable")

    async def fake_notify(text):
        notices.append(text)

    monkeypatch.setattr(app, "_send_for_review", fail_send)
    monkeypatch.setattr(app, "_notify_owner", fake_notify)

    item_id = asyncio.run(app._queue_for_review("محفوظ للاستعادة", []))

    assert item_id is not None
    assert app.PENDING.get(item_id)["review"] is None
    assert notices and "وحُفظ" in notices[0]


def test_remove_failure_after_facebook_success_remains_duplicate_guarded(app, monkeypatch):
    item_id = app.PENDING.add("لا تنشرني مرتين")
    facebook_calls = []

    async def fake_to_thread(fn, *args):
        facebook_calls.append(args)
        return {"id": "post_1"}

    def fail_remove(item_id):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(app.asyncio, "to_thread", fake_to_thread)
    with monkeypatch.context() as scoped:
        scoped.setattr(app.PENDING, "remove", fail_remove)
        asyncio.run(app._publish(Event(), item_id, include_media=False))

    saved = app.PENDING.get(item_id)
    assert saved["publish_state"] == "published"
    assert item_id in app._published

    retry = Event()
    asyncio.run(app._publish(retry, item_id, include_media=False))
    assert len(facebook_calls) == 1
    assert any("نُشر" in (text or "") for text, _ in retry.answers)


def test_uncertain_old_button_offers_admin_resolution_under_64_bytes(app):
    item_id = app.PENDING.add("نتيجته غير معروفة")
    app.PENDING.update(item_id, publish_state="publishing")
    event = Event(data=f"pub:{item_id}".encode())

    asyncio.run(app.on_post_action(event))

    buttons = event.response_buttons[-1]
    labels = [button.text for row in buttons for button in row]
    data = [button.data for row in buttons for button in row]
    assert any("موجود على Facebook" in label for label in labels)
    assert any("غير موجود" in label for label in labels)
    assert all(len(value) <= 64 for value in data)


def test_uncertain_resolution_close_removes_without_post(app, monkeypatch):
    item_id = app.PENDING.add("موجود فعلاً")
    app.PENDING.update(item_id, publish_state="publishing")

    def forbidden_publisher(*args, **kwargs):
        raise AssertionError("manual close must not contact Facebook")

    monkeypatch.setattr(app, "FacebookPublisher", forbidden_publisher)
    event = Event(data=f"pubfix:close:{item_id}".encode())
    asyncio.run(app.on_publish_resolution(event))

    assert app.PENDING.get(item_id) is None
    assert any("إغلاق الطلب" in edit for edit in event.edits)


def test_uncertain_resolution_retry_only_clears_state_without_post(app, monkeypatch):
    item_id = app.PENDING.add("غير موجود فعلاً")
    app.PENDING.update(
        item_id, publish_state="publishing", publishing_at=123, published_at=456
    )

    def forbidden_publisher(*args, **kwargs):
        raise AssertionError("manual reopen must not contact Facebook")

    monkeypatch.setattr(app, "FacebookPublisher", forbidden_publisher)
    event = Event(data=f"pubfix:retry:{item_id}".encode())
    asyncio.run(app.on_publish_resolution(event))

    item = app.PENDING.get(item_id)
    assert item is not None
    assert item["publish_state"] is None
    assert item["publishing_at"] is None
    assert item["published_at"] is None


def test_confirmed_published_old_button_only_retries_cleanup(app, monkeypatch):
    item_id = app.PENDING.add("منشور مؤكد")
    app.PENDING.update(item_id, publish_state="published")

    def forbidden_publisher(*args, **kwargs):
        raise AssertionError("published cleanup must not contact Facebook")

    monkeypatch.setattr(app, "FacebookPublisher", forbidden_publisher)
    event = Event(data=f"pub:{item_id}".encode())
    asyncio.run(app.on_post_action(event))

    assert app.PENDING.get(item_id) is None
    assert any("إغلاق الطلب" in edit for edit in event.edits)


def test_replay_only_items_without_review_message(app, monkeypatch):
    stranded = app.PENDING.add("انقطع التشغيل قبل الإرسال")
    already_sent = app.PENDING.add("له رسالة مراجعة")
    app.PENDING.update(already_sent, review={"chat": -100999, "msg": 10})
    replayed = []

    async def fake_send(item_id):
        replayed.append(item_id)
        app.PENDING.update(item_id, review={"chat": -100999, "msg": 11})

    monkeypatch.setattr(app, "_send_for_review", fake_send)
    count = asyncio.run(app._replay_unreviewed())

    assert count == 1
    assert replayed == [stranded]


def test_x_replay_waits_for_checkpoint_then_sends_same_item(app, monkeypatch):
    item_id = app.PENDING.add(
        "تغريدة محفوظة",
        [],
        "https://x.com/source/status/101",
    )
    writes = []
    sends = []

    def flaky_set_last_id(screen_name, last_id):
        writes.append((screen_name, last_id))
        if len(writes) == 1:
            raise OSError("settings unavailable")

    async def fake_send(replayed_id):
        sends.append(replayed_id)
        app.PENDING.update(
            replayed_id,
            review={"chat": -100999, "msg": 11, "has_media": False},
        )

    monkeypatch.setattr(app.S, "set_x_last_id", flaky_set_last_id)
    monkeypatch.setattr(app, "_send_for_review", fake_send)

    assert asyncio.run(app._replay_unreviewed()) == 0
    assert sends == []
    assert app.PENDING.get(item_id)["review"] is None

    assert asyncio.run(app._replay_unreviewed()) == 1
    assert sends == [item_id]
    assert writes == [("source", "101"), ("source", "101")]


def test_x_replay_of_old_outbox_never_moves_cursor_backwards(app, monkeypatch):
    app.S.add_x_account("source", "7", last_id=105)
    item_id = app.PENDING.add(
        "تغريدة قديمة محفوظة",
        [],
        "https://x.com/source/status/101",
    )

    async def fake_send(replayed_id):
        app.PENDING.update(
            replayed_id,
            review={"chat": -100999, "msg": 12, "has_media": False},
        )

    monkeypatch.setattr(app, "_send_for_review", fake_send)

    assert asyncio.run(app._replay_unreviewed()) == 1
    assert app.PENDING.get(item_id)["review"]["msg"] == 12
    account = next(a for a in app.S.x_accounts() if a["screen_name"] == "source")
    assert account["last_id"] == "105"


def test_failed_x_queue_does_not_advance_last_id(app, monkeypatch):
    account = {"screen_name": "source", "user_id": "7", "last_id": "100"}
    tweet = types.SimpleNamespace(id="101", full_text="خبر", media=[])
    writes = []

    async def fail_queue(*args, **kwargs):
        return None

    monkeypatch.setattr(app, "_queue_for_review", fail_queue)
    monkeypatch.setattr(app.XReader, "extract_media_urls", staticmethod(lambda tweet: []))
    monkeypatch.setattr(app.S, "set_x_last_id", lambda *args: writes.append(args))

    assert asyncio.run(app.handle_x_tweet(account, tweet)) is False
    assert writes == []


def test_x_last_id_failure_resumes_same_pending_without_duplicate_send(app, monkeypatch):
    account = {"screen_name": "source", "user_id": "7", "last_id": "100"}
    tweet = types.SimpleNamespace(id="101", full_text="خبر", media=[])
    writes = []
    sends = []
    notices = []

    def flaky_set_last_id(*args):
        writes.append(args)
        if len(writes) == 1:
            raise OSError("settings disk unavailable")

    async def fake_send(item_id):
        sends.append(item_id)
        app.PENDING.update(
            item_id, review={"chat": -100999, "msg": 77, "has_media": False}
        )

    async def fake_notify(text):
        notices.append(text)

    monkeypatch.setattr(app.S, "set_x_last_id", flaky_set_last_id)
    monkeypatch.setattr(app, "_send_for_review", fake_send)
    monkeypatch.setattr(app, "_notify_owner", fake_notify)
    monkeypatch.setattr(app.XReader, "extract_media_urls", staticmethod(lambda tweet: []))

    assert asyncio.run(app.handle_x_tweet(account, tweet)) is False
    item_ids = list(app.PENDING.items)
    assert len(item_ids) == 1
    assert app.PENDING.get(item_ids[0])["review"] is None
    assert sends == []

    assert asyncio.run(app.handle_x_tweet(account, tweet)) is True
    assert list(app.PENDING.items) == item_ids
    assert sends == item_ids

    # حتى لو عُرضت التغريدة نفسها مرة ثالثة، وجود review يمنع رسالة مكررة.
    assert asyncio.run(app.handle_x_tweet(account, tweet)) is True
    assert list(app.PENDING.items) == item_ids
    assert sends == item_ids
    assert len(writes) == 3
    assert notices


def test_external_path_returned_by_telethon_is_rejected_not_deleted(app, tmp_path):
    outside = tmp_path / "belongs-to-someone-else.txt"
    outside.write_text("keep", encoding="utf-8")

    class Message:
        photo = object()
        video = video_note = gif = document = None
        file = types.SimpleNamespace(size=4, ext=".jpg")

        async def download_media(self, file):
            return str(outside)

    assert asyncio.run(app._download_tg_media(Message())) is None
    assert outside.read_text(encoding="utf-8") == "keep"


def test_download_dir_must_be_strictly_inside_state(app, tmp_path):
    state = tmp_path / "state"
    outside = tmp_path / "outside"
    state.mkdir()
    outside.mkdir()

    assert app._resolve_download_dir("media", str(state)) == os.path.normcase(
        os.path.realpath(state / "media")
    )
    assert app._resolve_download_dir(str(outside), str(state)) == os.path.join(
        os.path.normcase(os.path.realpath(state)), "downloads"
    )
    assert app._resolve_download_dir(str(state), str(state)) == os.path.join(
        os.path.normcase(os.path.realpath(state)), "downloads"
    )


def test_skip_is_blocked_while_publish_is_in_progress(app):
    item_id = app.PENDING.add("لا تحذفني أثناء الرفع")
    app._publishing.add(item_id)
    event = Event(data=f"skip:{item_id}".encode())

    asyncio.run(app.on_post_action(event))

    assert app.PENDING.get(item_id) is not None
    assert any("قيد النشر" in (text or "") for text, _ in event.answers)


async def _wait_for_x_code_state(app):
    for _ in range(100):
        current = app._get_state(42)
        if current and current.get("action") == "x_auth_code":
            return current
        await asyncio.sleep(0)
    raise AssertionError("لم يصل تسجيل X إلى خطوة الرمز")


def test_x_authenticator_code_round_trip_is_deleted_and_not_stored(app, monkeypatch):
    captured = {}

    async def fake_login(credentials, challenge_handler):
        captured["credentials"] = dict(credentials)
        captured["code"] = await challenge_handler("two_factor", "hidden prompt")
        app.xreader.active = credentials["username"]
        return True

    monkeypatch.setattr(app.xreader, "login_interactive", fake_login)
    password_event = Event(text="account-password")

    async def scenario():
        task = asyncio.create_task(app._save_x_login(
            password_event,
            {"x_username": "reader", "x_email": "reader@example.test"},
            password_event.text,
        ))
        current = await _wait_for_x_code_state(app)
        assert current["x_challenge_kind"] == "two_factor"
        assert "account-password" not in repr(current)

        invalid = Event(text="12ab")
        await app.on_text(invalid)
        assert invalid.deleted is True
        assert not task.done()
        assert app._get_state(42)["action"] == "x_auth_code"

        code_event = Event(text="739 184")
        await app.on_text(code_event)
        await task
        return code_event, invalid

    code_event, invalid = asyncio.run(scenario())
    assert password_event.deleted is True
    assert code_event.deleted is True
    assert invalid.deleted is True
    assert captured["code"] == "739184"
    assert captured["credentials"]["password"] == "account-password"
    assert app.S.x_logins()[0]["username"] == "reader"
    assert app.S.x_logins()[0]["password"] is None
    with open(app.S.path, encoding="utf-8") as settings_file:
        settings_text = settings_file.read()
    assert "739184" not in settings_text
    assert "739 184" not in settings_text
    assert "account-password" not in settings_text
    all_replies = "\n".join(password_event.responses + code_event.responses)
    assert "739184" not in all_replies


def test_x_authenticator_code_is_bound_to_original_private_chat(app, monkeypatch):
    captured = []

    async def fake_login(_credentials, challenge_handler):
        captured.append(await challenge_handler("two_factor", "hidden prompt"))
        app.xreader.active = "reader"
        return True

    monkeypatch.setattr(app.xreader, "login_interactive", fake_login)
    password_event = Event(text="account-password")

    async def scenario():
        task = asyncio.create_task(app._save_x_login(
            password_event,
            {
                "x_username": "reader",
                "x_email": None,
                "x_chat_id": 42,
            },
            password_event.text,
        ))
        await _wait_for_x_code_state(app)
        wrong_chat = Event(
            text="111111", chat_id=-100777, is_private=False,
        )
        await app.on_text(wrong_chat)
        assert wrong_chat.deleted is True
        assert not task.done()
        assert captured == []

        right_chat = Event(text="222222")
        await app.on_text(right_chat)
        await task
        return wrong_chat

    wrong_chat = asyncio.run(scenario())
    assert captured == ["222222"]
    assert any("المحادثة الخاصة" in reply for reply in wrong_chat.responses)


def test_x_additional_verification_accepts_phone_format_requested_by_x(
    app, monkeypatch,
):
    captured = []

    async def fake_login(_credentials, challenge_handler):
        captured.append(await challenge_handler("verification", "hidden prompt"))
        app.xreader.active = "reader"
        return True

    monkeypatch.setattr(app.xreader, "login_interactive", fake_login)
    password_event = Event(text="account-password")

    async def scenario():
        task = asyncio.create_task(app._save_x_login(
            password_event,
            {"x_username": "reader", "x_email": None, "x_chat_id": 42},
            password_event.text,
        ))
        await _wait_for_x_code_state(app)
        value_event = Event(text="+49 (170) 123 45")
        await app.on_text(value_event)
        await task
        return value_event

    value_event = asyncio.run(scenario())
    assert captured == ["+49 (170) 123 45"]
    assert value_event.deleted is True


def test_x_login_cannot_start_in_group(app):
    event = Event(data=b"xlog:add", chat_id=-100777, is_private=False)

    asyncio.run(app.on_xlogin(event))

    assert app._get_state(42) is None
    assert any(alert for _text, alert in event.answers)


def test_x_email_step_shows_visible_skip_and_cancel_buttons(app):
    async def scenario():
        await app.on_xlogin(Event(data=b"xlog:add"))
        username = Event(text="reader")
        await app.on_text(username)
        return username

    username = asyncio.run(scenario())
    assert app._get_state(42)["action"] == "x_email"
    buttons = username.response_buttons[-1]
    assert [button.text for row in buttons for button in row] == [
        "⏭️ تخطي البريد", "🛑 إلغاء",
    ]
    assert [button.data for row in buttons for button in row] == [
        f"xsetup:{app._get_state(42)['x_setup_id']}:skip_email".encode(),
        f"xsetup:{app._get_state(42)['x_setup_id']}:cancel".encode(),
    ]
    assert all(len(button.data) <= 64 for row in buttons for button in row)
    assert "`-`" not in username.responses[-1]


def test_x_email_skip_button_advances_without_typed_dash(app):
    setup_id = "flow-a"
    app._set_state(42, {
        "action": "x_email",
        "x_username": "reader",
        "x_chat_id": 42,
        "x_setup_id": setup_id,
    })
    event = Event(data=f"xsetup:{setup_id}:skip_email".encode())
    asyncio.run(app.on_xsetup(event))

    st = app._get_state(42)
    assert st["action"] == "x_pass"
    assert st["x_username"] == "reader"
    assert st["x_email"] is None
    assert any(text == "تم تخطي البريد" and not alert for text, alert in event.answers)
    assert "كلمة مرور" in event.responses[-1]
    assert event.response_buttons[-1][0][0].data == b"xsetup:flow-a:cancel"


def test_x_setup_cancel_button_clears_private_flow(app):
    setup_id = "flow-a"
    app._set_state(42, {
        "action": "x_email",
        "x_username": "reader",
        "x_chat_id": 42,
        "x_setup_id": setup_id,
    })
    event = Event(data=f"xsetup:{setup_id}:cancel".encode())
    asyncio.run(app.on_xsetup(event))

    assert app._get_state(42) is None
    assert (42, 42) in app._x_secret_tombstones
    assert any("أُلغيت" in response for response in event.responses)


def test_x_email_skip_button_is_rejected_outside_original_private_chat(app):
    setup_id = "flow-a"
    app._set_state(42, {
        "action": "x_email",
        "x_username": "reader",
        "x_chat_id": 42,
        "x_setup_id": setup_id,
    })
    event = Event(
        data=f"xsetup:{setup_id}:skip_email".encode(),
        chat_id=-100777, is_private=False,
    )
    asyncio.run(app.on_xsetup(event))

    assert app._get_state(42)["action"] == "x_email"
    assert any(alert for _text, alert in event.answers)


def test_stale_x_email_skip_button_cannot_change_password_step(app):
    setup_id = "flow-a"
    app._set_state(42, {
        "action": "x_pass",
        "x_username": "reader",
        "x_email": "reader@example.test",
        "x_chat_id": 42,
        "x_setup_id": setup_id,
    })
    event = Event(data=f"xsetup:{setup_id}:skip_email".encode())
    asyncio.run(app.on_xsetup(event))

    st = app._get_state(42)
    assert st["action"] == "x_pass"
    assert st["x_email"] == "reader@example.test"
    assert any(alert for _text, alert in event.answers)


@pytest.mark.parametrize("action", ["skip_email", "cancel"])
def test_old_x_setup_button_cannot_change_new_flow(app, action):
    async def scenario():
        await app.on_xlogin(Event(data=b"xlog:add"))
        first_username = Event(text="first-reader")
        await app.on_text(first_username)
        old_id = app._get_state(42)["x_setup_id"]

        cancel = Event(data=f"xsetup:{old_id}:cancel".encode())
        await app.on_xsetup(cancel)
        # يمثّل السر القديم الذي كان في الطريق؛ يستهلكه tombstone بأمان قبل
        # بدء المحاولة الجديدة.
        await app.on_text(Event(text="late-old-secret"))
        await app.on_xlogin(Event(data=b"xlog:add"))
        second_username = Event(text="second-reader")
        await app.on_text(second_username)
        new_state = app._get_state(42)
        assert new_state["x_setup_id"] != old_id

        stale = Event(data=f"xsetup:{old_id}:{action}".encode())
        await app.on_xsetup(stale)
        return new_state, stale

    new_state, stale = asyncio.run(scenario())
    assert app._get_state(42) is new_state
    assert new_state["action"] == "x_email"
    assert new_state["x_username"] == "second-reader"
    assert any(alert for _text, alert in stale.answers)


def test_email_arriving_while_skip_is_in_flight_is_never_a_password(app, monkeypatch):
    setup_id = "flow-a"
    app._set_state(42, {
        "action": "x_email",
        "x_username": "reader",
        "x_chat_id": 42,
        "x_setup_id": setup_id,
    })
    submitted = []

    async def forbidden_save(*args):
        submitted.append(args)

    monkeypatch.setattr(app, "_save_x_login", forbidden_save)

    async def scenario():
        answer_started = asyncio.Event()
        release_answer = asyncio.Event()
        skip = Event(data=f"xsetup:{setup_id}:skip_email".encode())

        async def blocked_answer(text=None, alert=False):
            answer_started.set()
            await release_answer.wait()
            skip.answers.append((text, alert))

        skip.answer = blocked_answer
        skip_task = asyncio.create_task(app.on_xsetup(skip))
        await answer_started.wait()
        assert app._get_state(42)["action"] == "x_pass_pending"

        email = Event(text="reader@example.test")
        await app.on_text(email)
        assert email.deleted is True
        assert submitted == []

        release_answer.set()
        await skip_task
        return email

    email = asyncio.run(scenario())
    assert app._get_state(42)["action"] == "x_pass"
    assert submitted == []
    assert any("انتظر" in response for response in email.responses)


def test_cancel_during_skip_answer_prevents_password_prompt(app):
    setup_id = "flow-a"
    app._set_state(42, {
        "action": "x_email",
        "x_username": "reader",
        "x_chat_id": 42,
        "x_setup_id": setup_id,
    })

    async def scenario():
        answer_started = asyncio.Event()
        release_answer = asyncio.Event()
        skip = Event(data=f"xsetup:{setup_id}:skip_email".encode())

        async def blocked_answer(text=None, alert=False):
            answer_started.set()
            await release_answer.wait()
            skip.answers.append((text, alert))

        skip.answer = blocked_answer
        skip_task = asyncio.create_task(app.on_xsetup(skip))
        await answer_started.wait()
        cancel = Event(data=f"xsetup:{setup_id}:cancel".encode())
        await app.on_xsetup(cancel)
        release_answer.set()
        await skip_task
        return skip, cancel

    skip, cancel = asyncio.run(scenario())
    assert app._get_state(42) is None
    assert skip.responses == []
    assert any("أُلغيت" in response for response in cancel.responses)


def test_admin_revocation_during_skip_answer_prevents_password_prompt(app, monkeypatch):
    setup_id = "flow-a"
    app._set_state(42, {
        "action": "x_email",
        "x_username": "reader",
        "x_chat_id": 42,
        "x_setup_id": setup_id,
    })
    authorized = {"value": True}
    monkeypatch.setattr(app.S, "is_admin", lambda _uid: authorized["value"])

    async def scenario():
        answer_started = asyncio.Event()
        release_answer = asyncio.Event()
        skip = Event(data=f"xsetup:{setup_id}:skip_email".encode())

        async def blocked_answer(text=None, alert=False):
            answer_started.set()
            await release_answer.wait()
            skip.answers.append((text, alert))

        skip.answer = blocked_answer
        task = asyncio.create_task(app.on_xsetup(skip))
        await answer_started.wait()
        authorized["value"] = False
        release_answer.set()
        await task
        return skip

    skip = asyncio.run(scenario())
    assert app._get_state(42) is None
    assert skip.responses == []


def test_same_flow_cancel_button_stops_running_x_login(app, monkeypatch):
    setup_id = "flow-a"
    login_started = None
    release_login = None
    saved = []

    async def fake_login(_credentials, _challenge_handler):
        login_started.set()
        await release_login.wait()
        return True

    monkeypatch.setattr(app.xreader, "login_interactive", fake_login)
    monkeypatch.setattr(app.S, "add_x_login", lambda *args: saved.append(args))

    async def scenario():
        nonlocal login_started, release_login
        login_started = asyncio.Event()
        release_login = asyncio.Event()
        password = Event(text="secret-password")
        task = asyncio.create_task(app._save_x_login(
            password,
            {
                "x_username": "reader",
                "x_email": None,
                "x_chat_id": 42,
                "x_setup_id": setup_id,
            },
            password.text,
        ))
        await login_started.wait()
        assert app._get_state(42)["action"] == "x_login_running"

        cancel = Event(data=f"xsetup:{setup_id}:cancel".encode())
        await app.on_xsetup(cancel)
        await task
        return password, cancel

    password, cancel = asyncio.run(scenario())
    assert password.deleted is True
    assert app._get_state(42) is None
    assert saved == []
    assert any("أُلغيت" in response for response in cancel.responses)


def test_typed_dash_remains_a_backward_compatible_email_skip(app):
    setup_id = "flow-a"
    app._set_state(42, {
        "action": "x_email",
        "x_username": "reader",
        "x_chat_id": 42,
        "x_setup_id": setup_id,
    })
    asyncio.run(app.on_text(Event(text="-")))

    st = app._get_state(42)
    assert st["action"] == "x_pass"
    assert st["x_email"] is None
    assert st["x_setup_id"] == setup_id


@pytest.mark.parametrize(
    ("handler", "data"),
    [
        ("on_menu", b"m:fb"),
        ("on_menu", b"m:login"),
        ("on_src", b"src:add"),
        ("on_xlogin", b"xlog:add"),
        ("on_xlogin", b"xlog:switch"),
        ("on_xlogin", b"xlog:del"),
        ("on_xacc", b"xacc:add"),
        ("on_flt", b"flt:add"),
        ("on_adm", b"adm:add"),
        ("on_post_action", b"edit:missing-item"),
    ],
)
def test_other_inline_actions_cannot_overwrite_x_password_state(app, handler, data):
    setup_id = "flow-a"
    app._set_state(42, {
        "action": "x_pass",
        "x_username": "reader",
        "x_email": None,
        "x_chat_id": 42,
        "x_setup_id": setup_id,
    })
    event = Event(data=data)
    asyncio.run(getattr(app, handler)(event))

    st = app._get_state(42)
    assert st["action"] == "x_pass"
    assert st["x_setup_id"] == setup_id
    assert any(alert for _text, alert in event.answers)


def test_old_challenge_code_cannot_satisfy_new_challenge(app):
    async def scenario():
        delete_started = asyncio.Event()
        release_delete = asyncio.Event()
        loop = asyncio.get_running_loop()
        old_future = loop.create_future()
        app._x_challenges[(42, 42)] = old_future
        app._set_state(42, {
            "action": "x_auth_code",
            "x_challenge_kind": "two_factor",
            "x_chat_id": 42,
            "x_setup_id": "old-flow",
        })
        old_state = app._get_state(42)
        code = Event(text="123456")

        async def blocked_delete():
            delete_started.set()
            await release_delete.wait()
            code.deleted = True

        code.delete = blocked_delete
        old_submit = asyncio.create_task(
            app._submit_x_challenge_code(code, old_state, code.text)
        )
        await delete_started.wait()

        new_future = loop.create_future()
        app._x_challenges[(42, 42)] = new_future
        app._set_state(42, {
            "action": "x_auth_code",
            "x_challenge_kind": "two_factor",
            "x_chat_id": 42,
            "x_setup_id": "new-flow",
        })
        new_state = app._get_state(42)
        release_delete.set()
        await old_submit
        return old_future, new_future, new_state

    old_future, new_future, new_state = asyncio.run(scenario())
    assert old_future.done() is False
    assert new_future.done() is False
    assert app._get_state(42) is new_state


def test_old_challenge_delete_failure_warns_without_touching_new_flow(app):
    async def scenario():
        delete_started = asyncio.Event()
        release_delete = asyncio.Event()
        loop = asyncio.get_running_loop()
        old_future = loop.create_future()
        app._x_challenges[(42, 42)] = old_future
        app._set_state(42, {
            "action": "x_auth_code",
            "x_challenge_kind": "two_factor",
            "x_chat_id": 42,
            "x_setup_id": "old-flow",
        })
        old_state = app._get_state(42)
        code = Event(text="123456")

        async def blocked_failed_delete():
            delete_started.set()
            await release_delete.wait()
            raise RuntimeError("delete denied")

        code.delete = blocked_failed_delete
        old_submit = asyncio.create_task(
            app._submit_x_challenge_code(code, old_state, code.text)
        )
        await delete_started.wait()
        new_future = loop.create_future()
        app._x_challenges[(42, 42)] = new_future
        app._set_state(42, {
            "action": "x_auth_code",
            "x_challenge_kind": "two_factor",
            "x_chat_id": 42,
            "x_setup_id": "new-flow",
        })
        new_state = app._get_state(42)
        release_delete.set()
        await old_submit
        return code, new_future, new_state

    code, new_future, new_state = asyncio.run(scenario())
    assert new_future.done() is False
    assert app._get_state(42) is new_state
    assert any("احذف الرسالة" in response for response in code.responses)


@pytest.mark.parametrize("kind", ["menu_login", "source_add"])
def test_callback_rechecks_x_setup_after_await(app, monkeypatch, kind):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        if kind == "menu_login":
            app.S.set("user_phone", "+491234")
            event = Event(data=b"m:login")

            async def blocked_respond(text, buttons=None):
                event.responses.append(text)
                event.response_buttons.append(buttons)
                started.set()
                await release.wait()

            event.respond = blocked_respond
            task = asyncio.create_task(app.on_menu(event))
        else:
            event = Event(data=b"src:add")

            async def blocked_authorized():
                started.set()
                await release.wait()
                return True

            monkeypatch.setattr(app.user, "is_user_authorized", blocked_authorized)
            task = asyncio.create_task(app.on_src(event))

        await started.wait()
        app._set_state(42, {
            "action": "x_pass",
            "x_username": "reader",
            "x_email": None,
            "x_chat_id": 42,
            "x_setup_id": "flow-a",
        })
        release.set()
        await task
        return event

    event = asyncio.run(scenario())
    assert app._get_state(42)["action"] == "x_pass"
    assert any(alert for _text, alert in event.answers)


def test_x_password_delete_failure_aborts_before_login(app, monkeypatch):
    called = []

    async def fake_login(*args):
        called.append(args)

    monkeypatch.setattr(app.xreader, "login_interactive", fake_login)
    password_event = Event(text="do-not-use", fail_delete=True)

    asyncio.run(app._save_x_login(
        password_event,
        {"x_username": "reader", "x_email": None, "x_chat_id": 42},
        password_event.text,
    ))

    assert called == []
    assert app.S.x_logins() == []
    assert app._x_login_tasks == {}
    assert any("لم أستطع حذف" in reply for reply in password_event.responses)


def test_x_password_is_not_trimmed_and_may_start_with_slash(app, monkeypatch):
    captured = []

    async def fake_login(credentials, _challenge_handler):
        captured.append(dict(credentials))
        app.xreader.active = credentials["username"]
        return True

    monkeypatch.setattr(app.xreader, "login_interactive", fake_login)
    app._set_state(42, {
        "action": "x_pass",
        "x_username": "reader",
        "x_email": None,
        "x_chat_id": 42,
    })
    event = Event(text="/ exact password ")

    asyncio.run(app.on_text(event))

    assert captured[0]["password"] == "/ exact password "
    assert app.S.x_logins()[0]["password"] is None
    assert event.deleted is True


@pytest.mark.parametrize("command", [
    "/panel", "/start", "/id", "/claim old-code", "/panel@my_bot",
])
def test_known_slash_command_is_never_submitted_as_x_password(
    app, monkeypatch, command,
):
    async def must_not_submit(*_args, **_kwargs):
        raise AssertionError("أمر البوت وصل ككلمة مرور X")

    monkeypatch.setattr(app, "_save_x_login", must_not_submit)
    app._set_state(42, {
        "action": "x_pass",
        "x_username": "reader",
        "x_email": None,
        "x_chat_id": 42,
    })
    asyncio.run(app.on_text(Event(text=command)))
    # generic handler لا يستهلك الأمر ولا tombstone؛ المعالج الخاص يتولى
    # التنقل ويرفع StopPropagation في dispatch الحقيقي.
    assert app._get_state(42)["action"] == "x_pass"


@pytest.mark.parametrize("handler", [
    "on_panel_command", "on_id_command", "on_cancel_command",
])
def test_legacy_command_handler_stops_generic_dispatch(app, handler):
    app._set_state(42, {
        "action": "x_pass",
        "x_username": "reader",
        "x_email": None,
        "x_chat_id": 42,
    })

    async def scenario():
        with pytest.raises(app.events.StopPropagation):
            await getattr(app, handler)(Event(text="/panel"))

    asyncio.run(scenario())
    assert app._get_state(42) is None
    assert (42, 42) in app._x_secret_tombstones


def test_first_telegram_response_failure_releases_x_login_lock(app, monkeypatch):
    called = []

    async def fake_login(*args):
        called.append(args)

    monkeypatch.setattr(app.xreader, "login_interactive", fake_login)
    password_event = Event(text="do-not-retain", fail_respond_at={1})

    asyncio.run(app._save_x_login(
        password_event,
        {"x_username": "reader", "x_email": None, "x_chat_id": 42},
        password_event.text,
    ))

    assert called == []
    assert app._x_login_tasks == {}
    assert app._x_challenges == {}
    assert app._get_state(42) is None
    assert password_event.deleted is True


def test_cancel_during_password_deletion_prevents_x_login(app, monkeypatch):
    login_calls = []
    delete_started = None
    release_delete = None

    async def fake_login(*args):
        login_calls.append(args)
        return True

    monkeypatch.setattr(app.xreader, "login_interactive", fake_login)
    app._set_state(42, {
        "action": "x_pass",
        "x_username": "reader",
        "x_email": None,
        "x_chat_id": 42,
    })
    password_event = Event(text="do-not-use")

    async def blocked_delete():
        delete_started.set()
        await release_delete.wait()
        password_event.deleted = True

    password_event.delete = blocked_delete
    cancel_event = Event(text="/cancel")

    async def scenario():
        nonlocal delete_started, release_delete
        delete_started = asyncio.Event()
        release_delete = asyncio.Event()
        task = asyncio.create_task(app.on_text(password_event))
        await delete_started.wait()
        assert app._x_login_tasks[42] is task
        await app.cmd_cancel(cancel_event)
        assert not task.done(), "الإلغاء يجب ألا يقطع حذف رسالة السر"
        release_delete.set()
        await task

    asyncio.run(scenario())
    assert password_event.deleted is True
    assert login_calls == []
    assert app.S.x_logins() == []
    assert app._x_login_tasks == {}
    assert app._x_login_deleting == set()
    assert app._x_login_cancelled == set()


def test_hung_password_delete_times_out_and_releases_secret_lock(app, monkeypatch):
    login_calls = []
    monkeypatch.setattr(app, "X_SECRET_DELETE_TIMEOUT", 0.01)

    async def fake_login(*args):
        login_calls.append(args)
        return True

    async def never_delete():
        await asyncio.Event().wait()

    monkeypatch.setattr(app.xreader, "login_interactive", fake_login)
    app._set_state(42, {
        "action": "x_pass",
        "x_username": "reader",
        "x_email": None,
        "x_chat_id": 42,
    })
    password_event = Event(text="do-not-retain")
    password_event.delete = never_delete

    asyncio.run(app.on_text(password_event))

    assert login_calls == []
    assert app.S.x_logins() == []
    assert app._x_login_tasks == {}
    assert app._x_login_deleting == set()
    assert app._x_login_cancelled == set()
    assert any("احذفها يدوياً" in reply for reply in password_event.responses)


def test_session_switch_during_login_notice_prevents_old_x_attempt(app, monkeypatch):
    notice_started = None
    release_notice = None
    login_calls = []

    async def fake_login(*args):
        login_calls.append(args)
        return True

    monkeypatch.setattr(app.xreader, "login_interactive", fake_login)
    app._set_state(42, {
        "action": "x_pass",
        "x_username": "reader",
        "x_email": None,
        "x_chat_id": 42,
    })
    password_event = Event(text="account-password")
    original_respond = password_event.respond

    async def blocked_respond(text, buttons=None):
        notice_started.set()
        await release_notice.wait()
        await original_respond(text, buttons=buttons)

    password_event.respond = blocked_respond

    async def scenario():
        nonlocal notice_started, release_notice
        notice_started = asyncio.Event()
        release_notice = asyncio.Event()
        task = asyncio.create_task(app.on_text(password_event))
        await notice_started.wait()
        app.xreader.invalidate()
        app.xreader._set_active(object(), "second")
        release_notice.set()
        await task

    asyncio.run(scenario())
    assert login_calls == []
    assert app.xreader.active == "second"
    assert app.S.x_logins() == []


def test_cancel_during_failed_password_delete_still_warns_manual_cleanup(
    app, monkeypatch,
):
    delete_started = None
    release_delete = None
    app._set_state(42, {
        "action": "x_pass",
        "x_username": "reader",
        "x_email": None,
        "x_chat_id": 42,
    })
    password_event = Event(text="visible-secret")

    async def blocked_failed_delete():
        delete_started.set()
        await release_delete.wait()
        raise RuntimeError("delete denied")

    password_event.delete = blocked_failed_delete

    async def scenario():
        nonlocal delete_started, release_delete
        delete_started = asyncio.Event()
        release_delete = asyncio.Event()
        task = asyncio.create_task(app.on_text(password_event))
        await delete_started.wait()
        await app.cmd_cancel(Event(text="/cancel"))
        release_delete.set()
        await task

    asyncio.run(scenario())
    assert any("احذفها يدوياً" in reply for reply in password_event.responses)
    assert app._x_login_tasks == {}
    assert app.S.x_logins() == []


def test_revocation_tombstone_deletes_late_secret_only_in_original_chat(app):
    app.S.add_admin(84)
    app._set_state(84, {
        "action": "x_pass",
        "x_username": "reader",
        "x_email": None,
        "x_chat_id": 84,
    })
    app.S.remove_admin(84)
    app._cancel_x_login(84)

    wrong_chat = Event(text="not-the-secret", sender_id=84, chat_id=-100)
    asyncio.run(app.on_text(wrong_chat))
    assert wrong_chat.deleted is False

    late_secret = Event(text="/late-password", sender_id=84, chat_id=84)
    asyncio.run(app.on_text(late_secret))
    assert late_secret.deleted is True
    assert app._x_secret_tombstones == {}
    assert any("أُهملت" in reply for reply in late_secret.responses)


def test_expired_x_password_state_deletes_same_late_message(app):
    app._set_state(42, {
        "action": "x_pass",
        "x_username": "reader",
        "x_email": None,
        "x_chat_id": 42,
    })
    app.state[42]["ts"] -= app.STATE_TTL + 1
    late_password = Event(text="/password-that-arrived-late")

    asyncio.run(app.on_text(late_password))

    assert late_password.deleted is True
    assert app._get_state(42) is None
    assert app._x_secret_tombstones == {}
    assert any("أُهملت" in reply for reply in late_password.responses)


def test_purged_x_code_state_leaves_full_length_tombstone(app):
    app._set_state(42, {
        "action": "x_auth_code",
        "x_challenge_kind": "two_factor",
        "x_chat_id": 42,
    })
    app.state[42]["ts"] -= app.STATE_TTL + 1
    before = app.time.monotonic()

    app._purge_states()

    expires = app._x_secret_tombstones[(42, 42)]
    assert expires - before >= app.X_CHALLENGE_TIMEOUT
    assert expires - before >= app.STATE_TTL - 1


def test_x_code_delete_failure_cancels_login_without_saving(app, monkeypatch):
    async def fake_login(_credentials, challenge_handler):
        await challenge_handler("two_factor", "hidden prompt")
        return True

    monkeypatch.setattr(app.xreader, "login_interactive", fake_login)
    password_event = Event(text="account-password")

    async def scenario():
        task = asyncio.create_task(app._save_x_login(
            password_event,
            {"x_username": "reader", "x_email": None, "x_chat_id": 42},
            password_event.text,
        ))
        await _wait_for_x_code_state(app)
        code_event = Event(text="123456", fail_delete=True)
        await app.on_text(code_event)
        await task
        return code_event

    code_event = asyncio.run(scenario())
    assert app.S.x_logins() == []
    assert app._x_login_tasks == {}
    assert app._x_challenges == {}
    assert any("ألغيت محاولة" in reply for reply in code_event.responses)


def test_x_authenticator_timeout_does_not_save_credentials(app, monkeypatch):
    async def fake_login(_credentials, challenge_handler):
        await challenge_handler("two_factor", "hidden prompt")
        return True

    monkeypatch.setattr(app.xreader, "login_interactive", fake_login)
    monkeypatch.setattr(app, "X_CHALLENGE_TIMEOUT", 0.01)
    password_event = Event(text="do-not-save")

    asyncio.run(app._save_x_login(
        password_event,
        {"x_username": "reader", "x_email": None},
        password_event.text,
    ))

    assert password_event.deleted is True
    assert app.S.x_logins() == []
    assert app._get_state(42) is None
    assert app._x_challenges == {}
    assert any("انتهت مهلة" in reply for reply in password_event.responses)
    assert (42, 42) in app._x_secret_tombstones

    late_code = Event(text="/123456")
    asyncio.run(app.on_text(late_code))
    assert late_code.deleted is True
    assert app._x_secret_tombstones == {}


def test_cancel_stops_x_authenticator_attempt_without_saving(app, monkeypatch):
    async def fake_login(_credentials, challenge_handler):
        await challenge_handler("two_factor", "hidden prompt")
        return True

    monkeypatch.setattr(app.xreader, "login_interactive", fake_login)
    password_event = Event(text="do-not-save")
    cancel_event = Event(text="/cancel")

    async def scenario():
        task = asyncio.create_task(app._save_x_login(
            password_event,
            {"x_username": "reader", "x_email": None},
            password_event.text,
        ))
        await _wait_for_x_code_state(app)
        await app.cmd_cancel(cancel_event)
        await task

    asyncio.run(scenario())
    assert app.S.x_logins() == []
    assert app._get_state(42) is None
    assert app._x_login_tasks == {}
    assert app._x_challenges == {}
    assert any("أُلغيت محاولة" in reply for reply in cancel_event.responses)


def test_x_login_failure_is_sanitized_and_does_not_persist(app, monkeypatch):
    async def fake_login(_credentials, _challenge_handler):
        raise RuntimeError("secret server detail")

    monkeypatch.setattr(app.xreader, "login_interactive", fake_login)
    password_event = Event(text="do-not-save")
    asyncio.run(app._save_x_login(
        password_event,
        {"x_username": "reader", "x_email": "reader@example.test"},
        password_event.text,
    ))

    assert app.S.x_logins() == []
    assert password_event.deleted is True
    assert all("secret server detail" not in reply for reply in password_event.responses)


def test_x_login_rate_limit_has_a_specific_retry_later_message(app, monkeypatch):
    async def fake_login(_credentials, _challenge_handler):
        raise app.XBrowserRateLimited("fixed internal detail")

    monkeypatch.setattr(app.xreader, "login_interactive", fake_login)
    password_event = Event(text="do-not-save")

    asyncio.run(app._save_x_login(
        password_event,
        {"x_username": "reader", "x_email": None},
        password_event.text,
    ))

    assert app.S.x_logins() == []
    assert password_event.deleted is True
    assert any("مؤقتاً" in reply and "انتظر" in reply for reply in password_event.responses)
    assert all("fixed internal detail" not in reply for reply in password_event.responses)


def test_second_admin_cannot_overlap_x_login_attempt(app):
    app.S.add_admin(84)
    second = Event(text="second-password", sender_id=84)

    async def scenario():
        app._x_login_tasks[42] = asyncio.current_task()
        try:
            await app._save_x_login(
                second,
                {"x_username": "second", "x_email": None},
                second.text,
            )
        finally:
            app._x_login_tasks.clear()

    asyncio.run(scenario())
    assert second.deleted is True
    assert app.S.x_logins() == []
    assert any("محاولة تسجيل X أخرى" in reply for reply in second.responses)


def test_x_settings_save_failure_discards_verified_session(app, monkeypatch):
    discarded = []

    def discard(username):
        discarded.append(username)
        return True

    async def fake_login(credentials, _challenge_handler):
        app.xreader.active = credentials["username"]
        app.xreader.ready = True
        return True

    def fail_save(*_args):
        raise OSError("disk full secret path")

    monkeypatch.setattr(app.xreader, "login_interactive", fake_login)
    monkeypatch.setattr(app.xreader, "discard_session", discard)
    monkeypatch.setattr(app.S, "add_x_login", fail_save)
    password_event = Event(text="do-not-save")

    asyncio.run(app._save_x_login(
        password_event,
        {"x_username": "reader", "x_email": None},
        password_event.text,
    ))

    assert discarded == ["reader"]
    assert app.S.x_logins() == []
    assert password_event.deleted is True
    assert all("disk full secret path" not in reply for reply in password_event.responses)
    assert any("تعذّر حفظ الإعدادات" in reply for reply in password_event.responses)


def test_removing_x_login_also_deletes_its_session(app, monkeypatch):
    app.S.add_x_login("Reader", None, "password")
    discarded = []

    def discard(username):
        discarded.append(username)
        return True

    monkeypatch.setattr(app.xreader, "discard_session", discard)
    app._set_state(42, {"action": "x_login_del"})
    event = Event(text="@reader")

    asyncio.run(app.on_text(event))

    assert discarded == ["Reader"]
    assert app.S.x_logins() == []


def test_revoked_admin_cannot_finish_x_login_or_keep_session(app, monkeypatch):
    login_returned = None
    allow_return = None
    discarded = []

    async def fake_login(credentials, _challenge_handler):
        app.xreader.active = credentials["username"]
        app.xreader.ready = True
        login_returned.set()
        await allow_return.wait()
        return True

    def discard(username):
        discarded.append(username)
        app.xreader.invalidate()
        return True

    monkeypatch.setattr(app.xreader, "login_interactive", fake_login)
    monkeypatch.setattr(app.xreader, "discard_session", discard)
    password_event = Event(text="account-password", sender_id=84)

    async def scenario():
        nonlocal login_returned, allow_return
        login_returned = asyncio.Event()
        allow_return = asyncio.Event()
        app.S.add_admin(84)
        task = asyncio.create_task(app._save_x_login(
            password_event,
            {"x_username": "reader", "x_email": None, "x_chat_id": 84},
            password_event.text,
        ))
        await login_returned.wait()
        app.S.remove_admin(84)
        app._cancel_x_login(84, cancel_task=False)
        allow_return.set()
        await task

    asyncio.run(scenario())
    assert app.S.x_logins() == []
    assert discarded == ["reader"]
    assert app.xreader.ready is False
    assert app._x_login_tasks == {}
    assert app._x_challenges == {}


def test_revocation_warns_owner_when_new_x_session_cannot_be_deleted(
    app, monkeypatch,
):
    login_returned = None
    allow_return = None
    warnings = []

    async def fake_login(credentials, _challenge_handler):
        app.xreader.active = credentials["username"]
        app.xreader.ready = True
        login_returned.set()
        await allow_return.wait()
        return True

    async def notify(text):
        warnings.append(text)

    monkeypatch.setattr(app.xreader, "login_interactive", fake_login)
    monkeypatch.setattr(app.xreader, "discard_session", lambda _username: False)
    monkeypatch.setattr(app, "_notify_owner", notify)
    password_event = Event(text="account-password", sender_id=84)

    async def scenario():
        nonlocal login_returned, allow_return
        login_returned = asyncio.Event()
        allow_return = asyncio.Event()
        app.S.add_admin(84)
        task = asyncio.create_task(app._save_x_login(
            password_event,
            {"x_username": "reader", "x_email": None, "x_chat_id": 84},
            password_event.text,
        ))
        await login_returned.wait()
        app.S.remove_admin(84)
        app._cancel_x_login(84, cancel_task=False)
        allow_return.set()
        await task

    asyncio.run(scenario())
    assert app.S.x_logins() == []
    assert len(warnings) == 1
    assert "تعذّر حذف" in warnings[0]


def test_revoked_admin_challenge_is_cancelled_without_saving(app, monkeypatch):
    async def fake_login(_credentials, challenge_handler):
        await challenge_handler("two_factor", "hidden prompt")
        return True

    monkeypatch.setattr(app.xreader, "login_interactive", fake_login)
    password_event = Event(text="account-password", sender_id=84)

    async def scenario():
        app.S.add_admin(84)
        task = asyncio.create_task(app._save_x_login(
            password_event,
            {"x_username": "reader", "x_email": None, "x_chat_id": 84},
            password_event.text,
        ))
        for _ in range(100):
            current = app._get_state(84)
            if current and current.get("action") == "x_auth_code":
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("لم يصل تسجيل X إلى خطوة الرمز")
        app.S.remove_admin(84)
        app._cancel_x_login(84)
        await task

    asyncio.run(scenario())
    assert app.S.x_logins() == []
    assert app._x_login_tasks == {}
    assert app._x_challenges == {}
    assert app._get_state(84) is None


def _button_labels(markup):
    return [button.text for row in markup for button in row]


def test_command_reply_keyboard_is_persistent_arabic_and_not_inline(app):
    markup = app._command_keyboard()
    assert _button_labels(markup) == [
        app.BTN_PANEL, app.BTN_ID, app.BTN_CANCEL,
    ]
    for row in markup:
        for button in row:
            assert button.data is None
            assert button.resize is True
            assert button.persistent is True
            assert button.placeholder == "اختر أمراً"

    claim_markup = app._command_keyboard(claim=True)
    assert _button_labels(claim_markup) == [
        app.BTN_CLAIM, app.BTN_ID, app.BTN_CANCEL,
    ]


def test_start_sends_reply_keyboard_separately_from_inline_panel(app):
    event = Event(text="/start")
    asyncio.run(app.cmd_panel(event))

    assert len(event.response_buttons) == 2
    reply_markup, inline_markup = event.response_buttons
    assert all(button.data is None for row in reply_markup for button in row)
    assert all(button.data is not None for row in inline_markup for button in row)


@pytest.mark.parametrize("label, expected", [
    ("BTN_PANEL", "لوحة التحكم"),
    ("BTN_ID", "معرّفك"),
    ("BTN_CANCEL", "لا توجد عملية معلّقة"),
])
def test_arabic_reply_buttons_route_as_normal_messages(app, label, expected):
    event = Event(text=getattr(app, label))
    asyncio.run(app.on_text(event))
    assert any(expected in response for response in event.responses)


def test_reply_keyboard_is_not_installed_in_a_group(app):
    event = Event(text="/panel", chat_id=-100123, is_private=False)
    asyncio.run(app.cmd_panel(event))
    assert len(event.response_buttons) == 1
    assert all(
        button.data is not None
        for row in event.response_buttons[0]
        for button in row
    )


def test_stale_keyboard_is_cleared_for_unauthorized_user(app):
    event = Event(text=app.BTN_PANEL, sender_id=84)
    asyncio.run(app.on_text(event))
    assert "لست ضمن الأدمنين" in event.responses[-1]
    assert event.response_buttons[-1].clear is True


def test_panel_button_during_x_password_cancels_without_submitting(
    app, monkeypatch,
):
    async def must_not_submit(*_args, **_kwargs):
        raise AssertionError("عنوان Reply Keyboard وصل ككلمة مرور X")

    monkeypatch.setattr(app, "_save_x_login", must_not_submit)
    app._set_state(42, {
        "action": "x_pass",
        "x_username": "reader",
        "x_email": None,
        "x_chat_id": 42,
    })
    event = Event(text=app.BTN_PANEL)
    asyncio.run(app.on_text(event))

    assert event.deleted is True
    assert app._get_state(42) is None
    assert (42, 42) in app._x_secret_tombstones
    assert any("لوحة التحكم" in response for response in event.responses)


def test_cancel_button_during_x_password_uses_secure_cancel_path(app):
    app._set_state(42, {
        "action": "x_pass",
        "x_username": "reader",
        "x_email": None,
        "x_chat_id": 42,
    })
    event = Event(text=app.BTN_CANCEL)
    asyncio.run(app.on_text(event))

    assert event.deleted is True
    assert app._get_state(42) is None
    assert app._x_login_tasks == {}
    assert app._x_challenges == {}
    assert (42, 42) in app._x_secret_tombstones
    assert any("أُلغيت عملية الإعداد" in response for response in event.responses)


def test_reply_button_preserves_x_tombstone_and_still_routes(app):
    app._x_secret_tombstones[(42, 42)] = (
        app.time.monotonic() + app.X_SECRET_TOMBSTONE_TTL
    )
    event = Event(text=app.BTN_PANEL)
    asyncio.run(app.on_text(event))

    assert event.deleted is True
    assert (42, 42) in app._x_secret_tombstones
    assert any("لوحة التحكم" in response for response in event.responses)

    late_secret = Event(text="actual-late-password")
    asyncio.run(app.on_text(late_secret))
    assert late_secret.deleted is True
    assert (42, 42) not in app._x_secret_tombstones
    assert any("أُهملت هذه الرسالة" in response for response in late_secret.responses)


def test_expired_x_password_reply_button_is_deleted_and_still_routes(app):
    app._set_state(42, {
        "action": "x_pass",
        "x_username": "reader",
        "x_email": None,
        "x_chat_id": 42,
    })
    app.state[42]["ts"] = app.time.time() - app.STATE_TTL - 1
    event = Event(text=app.BTN_PANEL)
    asyncio.run(app.on_text(event))

    assert event.deleted is True
    assert app._get_state(42) is None
    assert (42, 42) in app._x_secret_tombstones
    assert any("لوحة التحكم" in response for response in event.responses)

    late_secret = Event(text="actual-late-password")
    asyncio.run(app.on_text(late_secret))
    assert late_secret.deleted is True
    assert any("أُهملت هذه الرسالة" in response for response in late_secret.responses)


def test_near_match_is_not_a_reply_command(app):
    app._set_state(42, {"action": "add_filter"})
    text = app.BTN_PANEL + " الآن"
    event = Event(text=text)
    asyncio.run(app.on_text(event))
    assert text in app.S.filter_words()
    assert not any(response == "⚙️ لوحة التحكم:" for response in event.responses)


def test_claim_button_accepts_code_without_typing_slash_command(app):
    app.S.set_many({"owner_id": None, "admin_ids": []})
    app._claim_code = "safe-claim-code"
    app._claim_attempts = 0

    start = Event(text=app.BTN_CLAIM)
    asyncio.run(app.on_text(start))
    assert app._get_state(42)["action"] == "claim_code"

    code = Event(text="safe-claim-code")
    asyncio.run(app.on_text(code))
    assert code.deleted is True
    assert app.S.get("owner_id") == 42
    assert app.S.is_admin(42)
    assert app._get_state(42) is None
    assert _button_labels(code.response_buttons[0]) == [
        app.BTN_PANEL, app.BTN_ID, app.BTN_CANCEL,
    ]
    assert all(
        button.data is not None
        for row in code.response_buttons[1]
        for button in row
    )


def test_expired_claim_code_is_deleted_and_never_claims_owner(app):
    app.S.set_many({"owner_id": None, "admin_ids": []})
    app._claim_code = "late-claim-code"
    app._set_state(42, {"action": "claim_code", "claim_chat_id": 42})
    app.state[42]["ts"] = app.time.time() - app.STATE_TTL - 1

    event = Event(text="late-claim-code")
    asyncio.run(app.on_text(event))
    assert event.deleted is True
    assert app.S.get("owner_id") is None
    assert app._get_state(42) is None
    assert any("أُهملت هذه الرسالة" in response for response in event.responses)


def test_command_keyboard_is_offered_only_once_per_version(app, monkeypatch):
    sent = []

    async def send_message(chat, text, buttons=None):
        sent.append((chat, text, buttons))

    monkeypatch.setattr(app.bot, "send_message", send_message, raising=False)

    async def scenario():
        assert await app._offer_command_keyboard(42) is True
        assert await app._offer_command_keyboard(42) is False

    asyncio.run(scenario())
    assert len(sent) == 1
    assert app.S.get("reply_keyboard_versions") == {
        "42": app.REPLY_KEYBOARD_VERSION,
    }


@pytest.mark.parametrize("handler_name, valid, invalid", [
    ("on_panel_command", "/panel", "/panelXYZ"),
    ("on_panel_command", "/start@my_bot", "/starter"),
    ("on_id_command", "/id", "/identify"),
    ("on_cancel_command", "/cancel@my_bot", "/cancelXYZ"),
    ("on_claim_command", "/claim secret", "/claimXYZ"),
])
def test_legacy_command_patterns_have_strict_boundaries(
    app, handler_name, valid, invalid,
):
    event_builder = next(
        builder
        for builder, handler in app.bot.handlers
        if handler is getattr(app, handler_name)
    )
    pattern = event_builder.kwargs["pattern"]
    assert re.match(pattern, valid)
    assert not re.match(pattern, invalid)


def test_legacy_claim_code_in_group_is_deleted_and_never_used(app):
    app.S.set_many({"owner_id": None, "admin_ids": []})
    app._claim_code = "private-only-code"
    event = Event(
        text="/claim private-only-code", chat_id=-100123, is_private=False,
    )
    event.pattern_match = re.match(
        r"^/claim(?:@\w+)?(?:\s+(\S+))?$", event.text
    )
    asyncio.run(app.cmd_claim(event))
    assert event.deleted is True
    assert app.S.get("owner_id") is None
    assert app._claim_attempts == 0
    assert any("محادثة" in response for response in event.responses)


@pytest.mark.parametrize("text", [
    "/claim private-only-code ",
    "/claim private-only-code extra",
    "/claim\tprivate-only-code\textra",
])
def test_malformed_legacy_claim_is_always_deleted_without_attempt(app, text):
    app.S.set_many({"owner_id": None, "admin_ids": []})
    app._claim_code = "private-only-code"
    event = Event(text=text, chat_id=-100123, is_private=False)
    asyncio.run(app.cmd_claim(event))
    assert event.deleted is True
    assert app.S.get("owner_id") is None
    assert app._claim_attempts == 0


def test_claim_code_from_wrong_chat_is_deleted_without_counting_attempt(app):
    app.S.set_many({"owner_id": None, "admin_ids": []})
    app._claim_code = "private-only-code"
    app._set_state(42, {"action": "claim_code", "claim_chat_id": 42})
    event = Event(
        text="private-only-code", chat_id=-100123, is_private=False,
    )
    asyncio.run(app.on_text(event))
    assert event.deleted is True
    assert app.S.get("owner_id") is None
    assert app._claim_attempts == 0
    assert app._get_state(42)["action"] == "claim_code"


@pytest.mark.parametrize("label", ["BTN_PANEL", "BTN_ID", "BTN_CANCEL"])
def test_group_reply_button_cannot_cancel_private_x_state(app, label):
    app._set_state(42, {
        "action": "x_pass",
        "x_username": "reader",
        "x_email": None,
        "x_chat_id": 42,
    })
    event = Event(
        text=getattr(app, label), chat_id=-100123, is_private=False,
    )
    asyncio.run(app.on_text(event))
    assert event.deleted is True
    assert app._get_state(42)["action"] == "x_pass"
    assert (42, 42) not in app._x_secret_tombstones
    assert any("الخاصة" in response for response in event.responses)


def test_claim_navigation_cancels_synchronously_and_deletes_late_code(app):
    app.S.set_many({"owner_id": None, "admin_ids": []})
    app._claim_code = "late-claim-code"
    app._set_state(42, {"action": "claim_code", "claim_chat_id": 42})
    delete_started = None
    release_delete = None
    panel = Event(text=app.BTN_PANEL)

    async def blocked_delete():
        delete_started.set()
        await release_delete.wait()
        panel.deleted = True

    panel.delete = blocked_delete

    async def scenario():
        nonlocal delete_started, release_delete
        delete_started = asyncio.Event()
        release_delete = asyncio.Event()
        task = asyncio.create_task(app.on_text(panel))
        await delete_started.wait()
        # الإلغاء ثُبّت قبل اكتمال Telegram RPC.
        assert app._get_state(42) is None
        assert (42, 42) in app._x_secret_tombstones
        late = Event(text="late-claim-code")
        await app.on_text(late)
        assert late.deleted is True
        assert app.S.get("owner_id") is None
        release_delete.set()
        await task

    asyncio.run(scenario())


def test_cancel_while_claim_code_delete_is_in_flight_prevents_ownership(app):
    app.S.set_many({"owner_id": None, "admin_ids": []})
    app._claim_code = "in-flight-code"
    app._set_state(42, {"action": "claim_code", "claim_chat_id": 42})
    delete_started = None
    release_delete = None
    code = Event(text="in-flight-code")

    async def blocked_delete():
        delete_started.set()
        await release_delete.wait()
        code.deleted = True

    code.delete = blocked_delete

    async def scenario():
        nonlocal delete_started, release_delete
        delete_started = asyncio.Event()
        release_delete = asyncio.Event()
        code_task = asyncio.create_task(app.on_text(code))
        await delete_started.wait()
        await app.on_text(Event(text=app.BTN_PANEL))
        assert app._get_state(42) is None
        release_delete.set()
        await code_task

    asyncio.run(scenario())
    assert code.deleted is True
    assert app.S.get("owner_id") is None


def test_cancel_while_legacy_claim_delete_is_in_flight_prevents_ownership(app):
    app.S.set_many({"owner_id": None, "admin_ids": []})
    app._claim_code = "legacy-in-flight"
    delete_started = None
    release_delete = None
    command = Event(text="/claim legacy-in-flight")
    command.pattern_match = re.match(
        r"^/claim(?:@\w+)?(?:\s+(\S+))?$", command.text
    )

    async def blocked_delete():
        delete_started.set()
        await release_delete.wait()
        command.deleted = True

    command.delete = blocked_delete

    async def scenario():
        nonlocal delete_started, release_delete
        delete_started = asyncio.Event()
        release_delete = asyncio.Event()
        claim_task = asyncio.create_task(app.cmd_claim(command))
        await delete_started.wait()
        assert app._get_state(42)["action"] == "claim_code"
        await app.on_text(Event(text=app.BTN_CANCEL))
        release_delete.set()
        await claim_task

    asyncio.run(scenario())
    assert command.deleted is True
    assert app.S.get("owner_id") is None


def test_cancel_command_does_not_consume_its_own_tombstone(app):
    app._set_state(42, {
        "action": "x_pass",
        "x_username": "reader",
        "x_email": None,
        "x_chat_id": 42,
    })
    command = Event(text="/cancel")

    async def scenario():
        with pytest.raises(app.events.StopPropagation):
            await app.on_cancel_command(command)
        # حتى لو استُدعي generic يدوياً، الأمر المحجوز لا يستهلك الحارس.
        await app.on_text(command)

    asyncio.run(scenario())
    assert (42, 42) in app._x_secret_tombstones
    late = Event(text="actual-late-password")
    asyncio.run(app.on_text(late))
    assert late.deleted is True


def test_new_setup_does_not_clear_late_secret_tombstone(app):
    app._x_secret_tombstones[(42, 42)] = (
        app.time.monotonic() + app.X_SECRET_TOMBSTONE_TTL
    )
    app._set_state(42, {"action": "add_filter"})
    late = Event(text="actual-late-password")
    asyncio.run(app.on_text(late))
    assert late.deleted is True
    assert "actual-late-password" not in app.S.filter_words()
    assert app._get_state(42)["action"] == "add_filter"

    intended = Event(text="intended-filter")
    asyncio.run(app.on_text(intended))
    assert "intended-filter" in app.S.filter_words()


def test_shutdown_waits_for_inflight_secret_message_deletion(app):
    event = Event(text="secret")

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_delete():
            started.set()
            await release.wait()
            event.deleted = True

        event.delete = blocked_delete
        deleting = asyncio.create_task(app._delete_secret_message(event))
        await started.wait()
        shutdown = asyncio.create_task(app._shutdown_x_logins(timeout=1))
        await asyncio.sleep(0)
        assert not shutdown.done()
        assert app._x_secret_tasks
        release.set()
        assert await deleting is True
        assert await shutdown is True

    asyncio.run(scenario())
    assert event.deleted is True
    assert app._x_secret_tasks == set()


def test_shutdown_refuses_unconfirmed_browser_cleanup(app):
    app._x_browser_cleanup_unconfirmed = True
    assert asyncio.run(app._shutdown_x_logins(timeout=0.1)) is False


def test_shutdown_refuses_orphan_secret_deletion_failure(app):
    event = Event(text="one-time-code")

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def failing_delete():
            started.set()
            await release.wait()
            raise RuntimeError("Telegram delete denied")

        event.delete = failing_delete
        handler = asyncio.create_task(app._delete_secret_message(event))
        await started.wait()
        handler.cancel()
        with pytest.raises(asyncio.CancelledError):
            await handler
        assert app._x_secret_tasks
        shutdown = asyncio.create_task(app._shutdown_x_logins(timeout=1))
        await asyncio.sleep(0)
        assert not shutdown.done()
        release.set()
        assert await shutdown is False

    asyncio.run(scenario())
    assert app._x_secret_tasks == set()


def test_completed_secret_deletion_failure_persists_until_restart(app):
    event = Event(text="visible-secret", fail_delete=True)

    async def scenario():
        assert await app._delete_secret_message(event) is False
        assert app._x_secret_tasks == set()
        # Even though the failed child finished before the shutdown snapshot,
        # the process must not forget that a secret may still be visible.
        assert await app._shutdown_x_logins(timeout=0.1) is False

    asyncio.run(scenario())
    assert app._x_secret_delete_unconfirmed is True
