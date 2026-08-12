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
    def __init__(self, app=None, item_id=None, fail_edit=False, data=b""):
        self.app = app
        self.item_id = item_id
        self.fail_edit = fail_edit
        self.data = data
        self.sender_id = 42
        self.answers = []
        self.responses = []
        self.response_buttons = []
        self.edits = []

    async def answer(self, text=None, alert=False):
        self.answers.append((text, alert))

    async def respond(self, text, buttons=None):
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
