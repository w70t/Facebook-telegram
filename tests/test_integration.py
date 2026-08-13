"""
اختبارات تكامل تشغّل خط المراجعة والنشر فعلياً.

ما يُختبر حقيقةً هنا (لا محاكاة):
- استيراد main.py مقابل telethon الحقيقي وتسجيل معالجاته.
- طلبات HTTP فعلية (بما فيها multipart) إلى خادم Graph API محلي.
- سلوك telethon الحقيقي في اشتقاق مسار التنزيل.

ما يبقى محاكىً: شبكة تلغرام و X — لا يمكن الاتصال بهما بلا حسابات.

يتخطّى الملف نفسه لو لم يكن telethon مثبّتاً:
    pip install "telethon>=1.42,<2" && python -m pytest tests/test_integration.py -v
"""
import asyncio
import itertools
import json
import os
import stat
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

telethon = pytest.importorskip("telethon", reason="اختبارات التكامل تحتاج telethon")

RELOADABLE = ("main", "settings", "store", "util", "twitter", "facebook", "jsonio")


# ════════════════════════ خادم Graph API محلي ════════════════════════
class GraphState:
    def __init__(self):
        self.requests = []
        self.photo_seq = itertools.count(1)
        self.fail_times = 0
        self.token_expired = False


class GraphHandler(BaseHTTPRequestHandler):
    state = None

    def log_message(self, *args):
        pass

    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        self.state.requests.append({"path": parsed.path, "fields": query, "raw": b""})
        if self.state.token_expired:
            return self._reply(401, {"error": {"code": 190, "message": "expired"}})
        self._reply(200, {"id": "PAGE", "name": "صفحتي"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        fields = {}
        if ctype.startswith("application/x-www-form-urlencoded"):
            fields = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
        path = urlparse(self.path).path
        self.state.requests.append({"path": path, "fields": fields, "raw": raw})

        if self.state.token_expired:
            return self._reply(401, {"error": {"code": 190, "message": "expired"}})
        if self.state.fail_times > 0:
            self.state.fail_times -= 1
            return self._reply(500, {"error": {"code": 2, "message": "temporary"}})
        if path.endswith("/photos"):
            return self._reply(200, {"id": f"media{next(self.state.photo_seq)}"})
        if path.endswith("/videos"):
            return self._reply(200, {"id": "video_1"})
        self._reply(200, {"id": "post_1"})


@pytest.fixture(scope="module")
def graph():
    state = GraphState()
    handler = type("H", (GraphHandler,), {"state": state})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()


# ════════════════════════ تحميل main ببيئة معزولة ════════════════════════
@pytest.fixture(scope="module")
def app(tmp_path_factory):
    home = tmp_path_factory.mktemp("bot")
    saved_env = {k: os.environ.get(k) for k in
                 ("SETTINGS_FILE", "API_ID", "API_HASH", "BOT_TOKEN", "NO_PROXY")}
    saved_mods = {m: sys.modules.get(m) for m in RELOADABLE}
    os.environ.update({
        "SETTINGS_FILE": str(home / "settings.json"),
        "API_ID": "1", "API_HASH": "hash", "BOT_TOKEN": "token",
        "NO_PROXY": "127.0.0.1,localhost",     # لا وسيط لخادم الاختبار المحلي
    })
    for name in RELOADABLE:
        sys.modules.pop(name, None)

    import main

    main.S.set("review_chat_id", -100999)
    main.S.set("fb_page_id", "123456789")
    main.S.set("fb_page_token", "tok")
    yield main

    for name in RELOADABLE:
        sys.modules.pop(name, None)
    sys.modules.update({k: v for k, v in saved_mods.items() if v is not None})
    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# ════════════════════════ بدائل شبكة تلغرام ════════════════════════
class Sent:
    _ids = itertools.count(1000)

    def __init__(self, kind, chat, payload, caption=None, buttons=None):
        self.id = next(Sent._ids)
        self.kind = kind
        self.chat = chat
        self.payload = payload
        self.caption = caption
        self.buttons = buttons

    @property
    def body(self):
        return self.caption if self.kind == "file" else self.payload

    def callback_data(self):
        return [b.data for row in (self.buttons or []) for b in row]


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []

    async def send_message(self, chat, text, buttons=None, **kwargs):
        msg = Sent("message", chat, text, buttons=buttons)
        msg.kwargs = kwargs
        self.sent.append(msg)
        return msg

    async def send_file(self, chat, file, caption=None, buttons=None):
        msg = Sent("file", chat, file, caption=caption, buttons=buttons)
        self.sent.append(msg)
        return msg

    async def edit_message(self, chat, message_id, text, buttons=None, **kwargs):
        self.edits.append({"chat": chat, "msg": message_id, "text": text,
                           "buttons": buttons, **kwargs})
        return Sent("edit", chat, text, buttons=buttons)


class FakeEvent:
    def __init__(self, sender_id=42):
        self.sender_id = sender_id
        self.answers = []
        self.responses = []
        self.edits = []

    async def answer(self, text=None, alert=False):
        self.answers.append(text)

    async def respond(self, text, buttons=None):
        self.responses.append(text)

    async def edit(self, text):
        self.edits.append(text)


@pytest.fixture
def bot(app, monkeypatch):
    fake = FakeBot()
    monkeypatch.setattr(app, "bot", fake)
    app.S.set("owner_id", 42)
    app.S.add_admin(42)
    yield fake
    for item_id in list(app.PENDING.items):
        app.PENDING.remove(item_id)


def photo_file(app, name):
    # PendingStore deliberately accepts only application-managed media names.
    # Keep the descriptive test name while giving it the same prefix as real
    # files produced by _safe_media_path().
    managed_name = name if name.startswith(("tg_", "x_")) else f"tg_{name}"
    path = os.path.join(app.DOWNLOAD_DIR, managed_name)
    with open(path, "wb") as f:
        f.write(b"\xff\xd8\xff" + b"0" * 64)
    return path


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ════════════════════════ ١) خط المراجعة ════════════════════════
def test_long_text_with_photo_respects_caption_limit(app, bot):
    """البق الأصلي: كابشن > 1024 يرمي استثناءً فيُحذف المنشور بلا أثر."""
    long_text = "خبر عاجل. " * 500
    item_id = run(app._queue_for_review(
        long_text, [{"path": photo_file(app, "a.jpg"), "type": "photo"}]
    ))

    assert item_id is not None, "ضاع المنشور"
    assert len(bot.sent) == 1
    msg = bot.sent[0]
    assert msg.kind == "file"
    assert len(msg.caption) <= 1024, f"الكابشن {len(msg.caption)} حرفاً"
    assert msg.caption.endswith("…")
    # النص الأصلي كامل محفوظ للنشر رغم اقتطاع المعاينة
    assert app.PENDING.get(item_id)["text"] == long_text


def test_long_text_without_media_uses_message_limit(app, bot):
    item_id = run(app._queue_for_review("ن" * 9000, []))
    msg = bot.sent[0]
    assert msg.kind == "message"
    assert 1024 < len(msg.payload) <= 4096
    assert app.PENDING.get(item_id)["text"] == "ن" * 9000


def test_album_sends_media_then_control_message(app, bot):
    """الألبوم لا يقبل أزراراً؛ لازم رسالة تحكّم منفصلة وإلا لا يمكن نشره."""
    media = [{"path": photo_file(app, f"al{i}.jpg"), "type": "photo"}
             for i in range(3)]
    item_id = run(app._queue_for_review("ألبوم", media))

    assert len(bot.sent) == 2
    album, control = bot.sent
    assert album.kind == "file" and isinstance(album.payload, list)
    assert len(album.payload) == 3
    assert album.buttons is None                       # تلغرام يرفضها هنا
    assert control.kind == "message"
    assert f"pub:{item_id}".encode() in control.callback_data()
    assert "3 صورة" in control.payload
    # رسالة التحكّم هي التي تُحدَّث لاحقاً لا الألبوم
    assert app.PENDING.get(item_id)["review"]["msg"] == control.id


def test_edit_updates_existing_message_not_a_new_one(app, bot):
    item_id = run(app._queue_for_review("النص الأول", []))
    original = bot.sent[0].id

    app.PENDING.update(item_id, text="النص المعدّل")
    run(app._send_for_review(item_id, refresh=True))

    assert len(bot.sent) == 1, "أُرسلت رسالة جديدة بدل تحديث القديمة"
    assert len(bot.edits) == 1
    assert bot.edits[0]["msg"] == original
    assert "النص المعدّل" in bot.edits[0]["text"]


def test_document_only_post_offers_no_media_publish(app, bot):
    path = photo_file(app, "doc.pdf")
    item_id = run(app._queue_for_review("تقرير", [{"path": path, "type": "document"}]))
    control = bot.sent[0]
    assert control.kind == "message"                   # لا يُرسل كوسيط
    assert b"pubtext:" not in b" ".join(control.callback_data())
    assert "لا تُنشر على فيسبوك" in control.payload
    assert f"pub:{item_id}".encode() in control.callback_data()


def test_filtered_words_block_before_download(app, bot):
    app.S.add_filter_word("اعلان")
    try:
        assert app.S.is_filtered("اعلان مدفوع")
        assert not app.S.is_filtered("خبر عادي")
    finally:
        app.S.remove_filter_word("اعلان")


# ════════════════════════ ٢) النشر على Graph API حقيقي ════════════════════════
@pytest.fixture
def graph_bound(app, graph, monkeypatch):
    url = graph.url
    # نوجّه العميل إلى خادم محلي بدل graph.facebook.com — بقية المسار حقيقي
    monkeypatch.setattr(app.FacebookPublisher, "graph", property(lambda self: url))
    monkeypatch.setattr(app.FacebookPublisher, "graph_video", property(lambda self: url))
    graph.requests.clear()
    graph.fail_times = 0
    graph.token_expired = False
    return graph


def test_publish_text_post(app, bot, graph_bound):
    item_id = run(app._queue_for_review("منشور نصي", []))
    event = FakeEvent()
    run(app._publish(event, item_id, include_media=False))

    feed = [r for r in graph_bound.requests if r["path"].endswith("/feed")]
    assert len(feed) == 1
    assert feed[0]["fields"]["message"] == "منشور نصي"
    assert feed[0]["fields"]["access_token"] == "tok"
    assert any("تم النشر" in e for e in event.edits)
    assert app.PENDING.get(item_id) is None            # نُظّف بعد النجاح


def test_publish_album_as_single_post(app, bot, graph_bound):
    """ثلاث صور = رفع غير منشور ×3 ثم منشور واحد يرفقها."""
    media = [{"path": photo_file(app, f"pub{i}.jpg"), "type": "photo"}
             for i in range(3)]
    item_id = run(app._queue_for_review("ألبوم للنشر", media))
    run(app._publish(FakeEvent(), item_id, include_media=True))

    photos = [r for r in graph_bound.requests if r["path"].endswith("/photos")]
    feed = [r for r in graph_bound.requests if r["path"].endswith("/feed")]
    assert len(photos) == 3, "لم تُرفع كل الصور"
    assert len(feed) == 1, "أُنشئ أكثر من منشور — البق الأصلي"
    for req in photos:
        assert b'name="published"' in req["raw"] and b"false" in req["raw"]
        assert b"\xff\xd8\xff" in req["raw"], "لم تُرفع بيانات الصورة فعلياً"
    attached = [json.loads(v) for k, v in feed[0]["fields"].items()
                if k.startswith("attached_media")]
    assert [a["media_fbid"] for a in attached] == ["media1", "media2", "media3"]


def test_publish_text_only_button_skips_media(app, bot, graph_bound):
    media = [{"path": photo_file(app, "skip.jpg"), "type": "photo"}]
    item_id = run(app._queue_for_review("نص فقط", media))
    run(app._publish(FakeEvent(), item_id, include_media=False))

    assert not [r for r in graph_bound.requests if r["path"].endswith("/photos")]
    assert len([r for r in graph_bound.requests if r["path"].endswith("/feed")]) == 1


def test_expired_token_keeps_post_and_warns(app, bot, graph_bound):
    """البق الأصلي: أي فشل يترك المستخدم بلا تفسير. الآن المنشور يبقى للمحاولة."""
    graph_bound.token_expired = True
    item_id = run(app._queue_for_review("منشور مهم", []))
    event = FakeEvent()
    run(app._publish(event, item_id, include_media=False))

    assert app.PENDING.get(item_id) is not None, "ضاع المنشور عند انتهاء التوكن"
    assert any("توكن فيسبوك" in r for r in event.responses)
    assert not event.edits                              # لم يُعلَن نجاح كاذب


def test_transient_post_is_not_retried_and_stays_uncertain(app, bot, graph_bound):
    graph_bound.fail_times = 2
    item_id = run(app._queue_for_review("مع إعادة محاولة", []))
    event = FakeEvent()
    run(app._publish(event, item_id, include_media=True))

    feed = [r for r in graph_bound.requests if r["path"].endswith("/feed")]
    assert len(feed) == 1       # POST غير idempotent: ممنوع تكراره تلقائياً
    assert app.PENDING.get(item_id)["publish_state"] == "publishing"
    assert any("غير محسومة" in response for response in event.responses)


def test_token_check_detects_expiry(app, graph_bound):
    fb = app.FacebookPublisher("123456789", "tok")
    assert fb.check_token()["name"] == "صفحتي"
    graph_bound.token_expired = True
    with pytest.raises(app.FacebookAuthError):
        fb.check_token()


# ════════════════════════ ٣) telethon الحقيقي: مسار التنزيل ════════════════════════
def test_real_telethon_ignores_hostile_filename(app):
    """
    نمرّر مساراً كاملاً نختاره نحن، فلا يستشير telethon اسم المُرسِل إطلاقاً.
    هذا هو الحاجز الذي يحمي حتى على الإصدارات المصابة (< 1.42).
    """
    from telethon.client.downloads import DownloadMethods

    class FakeFile:
        ext = ".jpg"
        size = 100

    target = app._safe_media_path(type("M", (), {"file": FakeFile()})(), "photo")
    hostile = ["../../../../etc/cron.d/pwn",
               "../../venv/lib/python3.11/site-packages/evil.pth"]
    resolved = DownloadMethods._get_proper_filename(
        target, "document", ".bin", possible_names=hostile
    )
    assert app._is_inside(resolved, app.DOWNLOAD_DIR)
    assert os.path.basename(resolved).startswith("tg_")
    for name in hostile:
        assert os.path.basename(name) not in resolved


def test_real_telethon_directory_mode_is_the_risky_one(app):
    """
    توثيق للفرق: تمرير *مجلد* يجعل telethon يستعمل اسم المُرسِل.
    على 1.44 يُقصّ بـ basename، وعلى < 1.42 كان يخرج من المجلد.
    """
    from telethon.client.downloads import DownloadMethods

    resolved = DownloadMethods._get_proper_filename(
        app.DOWNLOAD_DIR, "document", ".bin",
        possible_names=["../../../../etc/cron.d/pwn"],
    )
    contained = app._is_inside(resolved, app.DOWNLOAD_DIR)
    assert contained, (
        f"telethon {telethon.__version__} يسرّب المسار: {resolved} — "
        "ارفع الإصدار إلى 1.42+"
    )


def test_session_files_are_locked_down(app, tmp_path):
    probe = os.path.join(str(tmp_path), "_integration_probe.session")
    with open(probe, "w", encoding="utf-8") as f:
        f.write("fake session")
    os.chmod(probe, 0o644)
    app._harden_state_permissions(str(tmp_path))
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(probe).st_mode) == 0o600


def test_telethon_sessions_live_beside_settings_not_checkout(app):
    expected = os.path.normcase(os.path.realpath(app.STATE_DIR))
    for client in (app.user, app.bot):
        filename = os.path.realpath(client.session.filename)
        assert os.path.normcase(os.path.dirname(filename)) == expected


def test_x_cookie_state_lives_beside_settings(app):
    import twitter

    expected = os.path.normcase(os.path.realpath(app.STATE_DIR))
    cookie_dir = os.path.dirname(os.path.realpath(twitter._cookies_path("acct")))
    assert os.path.normcase(cookie_dir) == expected


# ════════════════════════ ٤) قارئ X ببديل twikit ════════════════════════
class FakeTweet:
    def __init__(self, tid, text="تغريدة"):
        self.id = tid
        self.full_text = text
        self.in_reply_to = None
        self.retweeted_tweet = None
        self.media = []


class FakeXClient:
    def __init__(self, cookies_valid=True, tweets=None):
        self.cookies_valid = cookies_valid
        self.tweets = tweets or []
        self.logged_in = False
        self.saved_to = None
        self.cookies = {}

    def set_cookies(self, cookies, clear_cookies=False):
        if clear_cookies:
            self.cookies.clear()
        self.cookies.update(cookies)

    def get_cookies(self):
        return dict(self.cookies)

    def load_cookies(self, path):
        self.loaded_from = path

    def save_cookies(self, path):
        self.saved_to = path
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"auth_token": "secret"}, f)

    async def user(self):
        if not self.cookies_valid and not self.logged_in:
            raise Exception("401 Unauthorized")
        return type("U", (), {"id": 1, "name": "me", "screen_name": "acct"})()

    async def login(self, **kwargs):
        self.logged_in = True

    async def get_user_tweets(self, uid, kind, count=20):
        return self.tweets


def test_stale_cookies_require_explicit_interactive_login(app, monkeypatch, tmp_path):
    """
    poller لا يجوز أن يحاول كلمة المرور أو input عند انتهاء الكوكيز؛ يحذف جلسة
    401 ويترك تسجيل الدخول التفاعلي لواجهة Telegram التي تستطيع طلب 2FA.
    """
    monkeypatch.setattr(app.xreader.S.__class__, "x_logins",
                        lambda self: [{"username": "acct", "email": None,
                                       "password": "pw", "failed": False}])
    clients = []

    def factory(self):
        client = FakeXClient(cookies_valid=False)
        clients.append(client)
        return client

    monkeypatch.setattr(type(app.xreader), "_new_client", factory)

    import twitter

    monkeypatch.setattr(twitter, "BASE_DIR", str(tmp_path))
    cookie_file = twitter._cookies_path("acct")
    with open(cookie_file, "w", encoding="utf-8") as f:
        json.dump({"auth_token": "expired"}, f)
    try:
        app.xreader.invalidate()
        assert run(app.xreader.ensure_login()) is False
        assert not clients[-1].logged_in, "poller حاول تسجيل دخول تفاعلياً"
        assert clients[-1].saved_to is None
        assert not os.path.exists(cookie_file), "لم تُحذف جلسة 401 المنتهية"
    finally:
        app.xreader.invalidate()
        if os.path.exists(cookie_file):
            os.remove(cookie_file)


def test_ambiguous_forbidden_keeps_session_for_later_retry(app, monkeypatch, tmp_path):
    import twitter

    monkeypatch.setattr(twitter, "BASE_DIR", str(tmp_path))
    cookie_file = twitter._cookies_path("acct")
    with open(cookie_file, "w", encoding="utf-8"):
        pass
    try:
        app.xreader.active = "acct"
        app.xreader.client = object()
        app.xreader.ready = True
        session = app.xreader.capture_session()
        assert app.xreader.report_failure(
            Exception("403 Forbidden"), session=session
        ) is False
        assert os.path.exists(cookie_file), "403 غامض حذف جلسة صالحة محتملة"
    finally:
        app.xreader.active = None
        if os.path.exists(cookie_file):
            os.remove(cookie_file)


def test_network_error_does_not_burn_account(app):
    app.xreader.active = "acct"
    try:
        app.xreader.client = object()
        app.xreader.ready = True
        session = app.xreader.capture_session()
        assert app.xreader.report_failure(
            Exception("bandwidth limit exceeded"), session=session
        ) is False
        assert app.xreader.active == "acct"
    finally:
        app.xreader.active = None


# ════════════════════════ ٥) التحديث الذاتي على مستودع git حقيقي ════════════
class Restarted(Exception):
    """بديل os.execv — يخبرنا أن البوت كان سيعيد التشغيل."""


@pytest.fixture
def repo(app, tmp_path, monkeypatch):
    import subprocess

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    run_git = lambda *a, cwd: subprocess.run(  # noqa: E731
        ["git", *a], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    run_git("init", "-q", "-b", "main", cwd=upstream)
    run_git("config", "user.email", "t@t", cwd=upstream)
    run_git("config", "user.name", "t", cwd=upstream)
    (upstream / "requirements.txt").write_text("requests>=2.31\n", encoding="utf-8")
    (upstream / "main.py").write_text("# bot\n", encoding="utf-8")
    run_git("add", "-A", cwd=upstream)
    run_git("commit", "-qm", "init", cwd=upstream)

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(upstream), str(clone)], check=True)
    run_git("config", "user.email", "t@t", cwd=clone)
    run_git("config", "user.name", "t", cwd=clone)

    monkeypatch.setattr(app, "BASE_DIR", str(clone))
    monkeypatch.setattr(app.os, "execv",
                        lambda *a: (_ for _ in ()).throw(Restarted()))

    async def noop():
        return None

    monkeypatch.setattr(app.bot, "disconnect", noop, raising=False)
    monkeypatch.setattr(app.user, "disconnect", noop, raising=False)
    # SimpleNamespace لا يربط الدوال كتوابع — لولاه لمُرِّر self إلى git
    return types.SimpleNamespace(upstream=upstream, clone=clone, git=run_git)


def test_update_restarts_after_successful_pull(app, repo, monkeypatch):
    (repo.upstream / "main.py").write_text("# bot v2\n", encoding="utf-8")
    repo.git("commit", "-qam", "v2", cwd=repo.upstream)

    event = FakeEvent()
    with pytest.raises(Restarted):
        run(app._self_update(event))
    assert "# bot v2" in (repo.clone / "main.py").read_text(encoding="utf-8")
    assert any("إعادة تشغيل" in r for r in event.responses)


def test_failed_pull_does_not_restart(app, repo):
    """
    البق الأصلي: كان يعيد التشغيل حتى لو فشل السحب. تعديل محلي متعارض يجعل
    --ff-only يفشل؛ إعادة التشغيل هنا تعني حلقة أعطال على الـ Pi.
    """
    (repo.clone / "main.py").write_text("# تعديل محلي\n", encoding="utf-8")
    (repo.upstream / "main.py").write_text("# upstream مختلف\n", encoding="utf-8")
    repo.git("commit", "-qam", "upstream", cwd=repo.upstream)

    event = FakeEvent()
    run(app._self_update(event))                     # لا Restarted = لم يُعد التشغيل
    assert any("فشل السحب" in r for r in event.responses)
    assert (repo.clone / "main.py").read_text(encoding="utf-8") == "# تعديل محلي\n"


def test_changed_requirements_trigger_pip(app, repo, monkeypatch):
    (repo.upstream / "requirements.txt").write_text(
        "requests>=2.31\nnewdep>=1.0\n", encoding="utf-8"
    )
    repo.git("commit", "-qam", "deps", cwd=repo.upstream)

    calls = []
    real_run = app._run

    def spy(cmd, timeout):
        calls.append(cmd)
        if "pip" in cmd:
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        return real_run(cmd, timeout)

    monkeypatch.setattr(app, "_run", spy)
    with pytest.raises(Restarted):
        run(app._self_update(FakeEvent()))
    assert any("pip" in c for c in calls), "لم تُثبَّت الاعتماديات الجديدة"


def test_credentials_in_git_output_are_redacted(app, repo, monkeypatch):
    """رابط origin قد يحمل توكناً؛ مخرجات git تُرسل إلى محادثة تلغرام."""
    def leaky(cmd, timeout):
        return type("P", (), {
            "returncode": 1, "stdout": "",
            "stderr": "fatal: https://w70t:ghp_SECRET123@github.com/w70t/x.git denied",
        })()

    monkeypatch.setattr(app, "_run", leaky)
    event = FakeEvent()
    run(app._self_update(event))
    blob = " ".join(event.responses)
    assert "ghp_SECRET123" not in blob
    assert "***@github.com" in blob


# ════════════════════════ ٦) تنزيل وسائط X عبر HTTP حقيقي ═══════════════════
class MediaHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
            return
        size = int(parse_qs(urlparse(self.path).query).get("size", ["64"])[0])
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        if "nolen" not in self.path:
            self.send_header("Content-Length", str(size))
        self.end_headers()
        self.wfile.write(b"\xff" * size)


@pytest.fixture(scope="module")
def media_server():
    server = HTTPServer(("127.0.0.1", 0), MediaHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def open_hosts(app, monkeypatch):
    """نسمح مؤقتاً بالمضيف المحلي لاختبار بقية المنطق عبر HTTP حقيقي."""
    monkeypatch.setattr(app, "_trusted_media_url",
                        lambda url: urlparse(url).hostname == "127.0.0.1")


def test_download_respects_size_cap_by_header(app, media_server, open_hosts):
    with pytest.raises(ValueError, match="يتجاوز الحد"):
        app._download_url(f"{media_server}/a.jpg?size=5000", app.DOWNLOAD_DIR,
                          max_bytes=1000)
    leftovers = [f for f in os.listdir(app.DOWNLOAD_DIR) if f.startswith("x_")]
    assert leftovers == [], "بقي ملف جزئي على القرص"


def test_download_caps_stream_without_content_length(app, media_server, open_hosts):
    """خادم يخفي الحجم — لازم القطع أثناء التدفق لا الاعتماد على الترويسة."""
    with pytest.raises(ValueError, match="أثناء التنزيل"):
        app._download_url(f"{media_server}/a.jpg?size=9000&nolen=1", app.DOWNLOAD_DIR,
                          max_bytes=1000)
    assert [f for f in os.listdir(app.DOWNLOAD_DIR) if f.startswith("x_")] == []


def test_download_succeeds_within_cap(app, media_server, open_hosts):
    path = app._download_url(f"{media_server}/a.jpg?size=500", app.DOWNLOAD_DIR,
                             max_bytes=10000)
    try:
        assert os.path.getsize(path) == 500
        assert app._is_inside(path, app.DOWNLOAD_DIR)
        assert path.endswith(".jpg")
    finally:
        os.remove(path)


def test_redirect_to_internal_address_is_blocked(app, media_server, monkeypatch):
    """SSRF: التحويلة تُفحص أيضاً، لا الرابط الأول فقط."""
    monkeypatch.setattr(
        app, "_trusted_media_url",
        lambda url: urlparse(url).hostname == "127.0.0.1",   # 169.254.x مرفوض
    )
    with pytest.raises(ValueError, match="غير موثوق"):
        app._download_url(f"{media_server}/redirect", app.DOWNLOAD_DIR)
    assert [f for f in os.listdir(app.DOWNLOAD_DIR) if f.startswith("x_")] == []


def test_untrusted_host_never_contacted(app):
    with pytest.raises(ValueError, match="غير موثوق"):
        app._download_url("https://evil.example/x.jpg", app.DOWNLOAD_DIR)
