"""
بديل خفيف لمكتبة telethon لأغراض الاختبار فقط.

telethon تحتاج امتدادات تُبنى أثناء التثبيت وقد لا تتوفر في بيئة CI، لكننا نريد
التأكد أن main.py يُستورد فعلاً (تنفيذ الديكوريتورات وكل الكود العلوي) لا مجرد
فحصه بالـ lint.
"""
import sys
import struct
import types


class Button:
    def __init__(
        self, text, data, *, resize=None, single_use=None, selective=None,
        persistent=None, placeholder=None,
    ):
        self.text = text
        self.data = data
        self.resize = resize
        self.single_use = single_use
        self.selective = selective
        self.persistent = persistent
        self.placeholder = placeholder

    @staticmethod
    def inline(text, data=None):
        return Button(text, data)

    @staticmethod
    def text(
        text, *, resize=None, single_use=None, selective=None,
        persistent=None, placeholder=None,
    ):
        return Button(
            text, None, resize=resize, single_use=single_use,
            selective=selective, persistent=persistent,
            placeholder=placeholder,
        )

    @staticmethod
    def clear(selective=None):
        return types.SimpleNamespace(clear=True, selective=selective)


class _Event:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class NewMessage(_Event):
    pass


class Album(_Event):
    pass


class CallbackQuery(_Event):
    pass


class StopPropagation(Exception):
    pass


class TelegramClient:
    def __init__(self, session, api_id=None, api_hash=None, **kwargs):
        self.session = session
        self.handlers = []

    def on(self, event):
        def decorator(fn):
            self.handlers.append((event, fn))
            return fn
        return decorator

    def list_event_handlers(self):
        return [(callback, event) for event, callback in self.handlers]

    def remove_event_handler(self, callback, event=None):
        before = len(self.handlers)
        self.handlers = [
            (builder, handler)
            for builder, handler in self.handlers
            if not (
                handler is callback
                and (event is None or builder is event)
            )
        ]
        return before - len(self.handlers)

    def add_event_handler(self, callback, event=None):
        self.handlers.append((event, callback))

    def is_connected(self):
        return False

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def is_user_authorized(self):
        return False


class FloodWaitError(Exception):
    def __init__(self, seconds=5):
        super().__init__(f"flood wait {seconds}")
        self.seconds = seconds


class MessageNotModifiedError(Exception):
    pass


class SessionPasswordNeededError(Exception):
    pass


class MessageEntityTextUrl:
    def __init__(self, offset, length, url):
        self.offset = offset
        self.length = length
        self.url = url


class MessageEntityUrl:
    def __init__(self, offset, length):
        self.offset = offset
        self.length = length


def add_surrogate(text):
    """Test equivalent of Telethon's UTF-16 surrogate expansion helper."""
    return "".join(
        "".join(chr(unit) for unit in struct.unpack("<HH", char.encode("utf-16le")))
        if "\U00010000" <= char <= "\U0010ffff"
        else char
        for char in text
    )


def del_surrogate(text):
    return text.encode("utf-16le", "surrogatepass").decode("utf-16le")


def get_peer_id(entity):
    return getattr(entity, "id", 0)


def install():
    """يسجّل الوحدات البديلة في sys.modules قبل استيراد main."""
    telethon = types.ModuleType("telethon")
    events = types.ModuleType("telethon.events")
    errors = types.ModuleType("telethon.errors")
    helpers = types.ModuleType("telethon.helpers")
    tl = types.ModuleType("telethon.tl")
    tl_types = types.ModuleType("telethon.tl.types")
    utils = types.ModuleType("telethon.utils")

    events.NewMessage = NewMessage
    events.Album = Album
    events.CallbackQuery = CallbackQuery
    events.StopPropagation = StopPropagation

    errors.FloodWaitError = FloodWaitError
    errors.MessageNotModifiedError = MessageNotModifiedError
    errors.SessionPasswordNeededError = SessionPasswordNeededError

    helpers.add_surrogate = add_surrogate
    helpers.del_surrogate = del_surrogate

    tl_types.MessageEntityTextUrl = MessageEntityTextUrl
    tl_types.MessageEntityUrl = MessageEntityUrl
    tl.types = tl_types

    utils.get_peer_id = get_peer_id

    telethon.Button = Button
    telethon.TelegramClient = TelegramClient
    telethon.events = events
    telethon.errors = errors
    telethon.helpers = helpers
    telethon.types = tl_types
    telethon.tl = tl
    telethon.utils = utils

    sys.modules["telethon"] = telethon
    sys.modules["telethon.events"] = events
    sys.modules["telethon.errors"] = errors
    sys.modules["telethon.helpers"] = helpers
    sys.modules["telethon.tl"] = tl
    sys.modules["telethon.tl.types"] = tl_types
    sys.modules["telethon.utils"] = utils
