"""
يُشغَّل في عملية منفصلة من tests/test_main_smoke.py.
يستورد main.py فعلياً (مع بديل telethon) ويفحص دواله الخالصة.
"""
import os
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import stub_telethon  # noqa: E402

stub_telethon.install()

import main  # noqa: E402


def msg(**attrs):
    m = types.SimpleNamespace(photo=None, video=None, video_note=None, gif=None,
                              document=None, media=None, file=None, message="")
    for key, value in attrs.items():
        setattr(m, key, value)
    return m


def doc(mime):
    return types.SimpleNamespace(mime_type=mime)


# --- نوع الوسيط ---
assert main._media_kind(msg(photo=object())) == "photo"
assert main._media_kind(msg(video=object(), document=doc("video/mp4"))) == "video"
assert main._media_kind(msg(gif=object(), document=doc("video/mp4"))) == "video"
# ملف مرسل كمستند: كان يُسقَط بصمت عند النشر
assert main._media_kind(msg(document=doc("image/png"))) == "photo"
assert main._media_kind(msg(document=doc("video/quicktime"))) == "video"
assert main._media_kind(msg(document=doc("application/pdf"))) == "document"
assert main._media_kind(msg()) is None

# --- تنقية امتداد الملف من رابط خارجي ---
assert main._SAFE_EXT.match(".jpg")
assert main._SAFE_EXT.match(".mp4")
assert not main._SAFE_EXT.match("./../etc/passwd")
assert not main._SAFE_EXT.match(".verylongextension")
assert not main._SAFE_EXT.match("")

# --- ترويسة المراجعة ---
header = main._review_header({
    "media": [{"type": "photo"}, {"type": "document"}],
    "origin": "https://x.com/a/status/1",
})
assert "1 صورة + 1 ملف" in header
assert "لا تُنشر على فيسبوك" in header
assert "https://x.com/a/status/1" in header
assert main._review_header({"media": [], "origin": ""}) == "📥 منشور جديد للمراجعة:"

# --- الأزرار ---
buttons = main._build_buttons("abc123XY", {"media": [{"type": "photo"}]})
data = [b.data for row in buttons for b in row]
assert b"pub:abc123XY" in data
assert b"pubtext:abc123XY" in data          # يظهر فقط مع وسائط قابلة للنشر
assert all(len(d) <= 64 for d in data)      # حد callback_data في تلغرام

text_only = main._build_buttons("abc123XY", {"media": []})
assert b"pubtext:abc123XY" not in [b.data for row in text_only for b in row]
docs_only = main._build_buttons("abc123XY", {"media": [{"type": "document"}]})
assert b"pubtext:abc123XY" not in [b.data for row in docs_only for b in row]

# --- مهلة محادثات الإعداد ---
main._set_state(7, {"action": "add_source"})
assert main._get_state(7)["action"] == "add_source"
main.state[7]["ts"] = time.time() - main.STATE_TTL - 1
assert main._get_state(7) is None, "الحالة المنتهية يجب أن تُهمل"
assert 7 not in main.state

main._set_state(8, {"action": "x_add"})
main.state[8]["ts"] = time.time() - main.STATE_TTL - 1
main._set_state(9, {"action": "x_del"})
main._purge_states()
assert 8 not in main.state and 9 in main.state
main._clear_state(9)
assert main.state == {}

# --- حدود الحجم ---
assert main._max_media_bytes() == 200 * 1024 * 1024

# --- تسجيل المعالجات فعلاً ---
assert len(main.user.handlers) == 2, "NewMessage + Album على الحساب الشخصي"
assert len(main.bot.handlers) >= 8

print("SMOKE OK")
