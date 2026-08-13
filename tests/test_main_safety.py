"""اختبارات regressions لمسارات النشر والاستعادة الحساسة في main.py."""
import asyncio
import os
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
    assert app.S.x_logins()[0]["password"] == "account-password"
    with open(app.S.path, encoding="utf-8") as settings_file:
        settings_text = settings_file.read()
    assert "739184" not in settings_text
    assert "739 184" not in settings_text
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
    assert app.S.x_logins()[0]["password"] == "/ exact password "
    assert event.deleted is True


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
    delete_started = asyncio.Event()
    release_delete = asyncio.Event()

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
    app.S.add_x_login("reader", None, "password")
    discarded = []

    def discard(username):
        discarded.append(username)
        return True

    monkeypatch.setattr(app.xreader, "discard_session", discard)
    app._set_state(42, {"action": "x_login_del"})
    event = Event(text="@reader")

    asyncio.run(app.on_text(event))

    assert discarded == ["reader"]
    assert app.S.x_logins() == []
