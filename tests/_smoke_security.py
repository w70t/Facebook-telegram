"""
يُشغَّل في عملية منفصلة من tests/test_security.py.
يفحص الحواجز الأمنية داخل main.py على بيانات هجومية حقيقية.
"""
import os
import stat
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import stub_telethon  # noqa: E402

stub_telethon.install()

import main  # noqa: E402


def tg_msg(filename_ext="", photo=True):
    file_obj = types.SimpleNamespace(ext=filename_ext, size=10)
    return types.SimpleNamespace(
        photo=object() if photo else None, video=None, video_note=None,
        gif=None, document=None, file=file_obj,
    )


# ── 1) اسم الملف من القناة المصدر لا يؤثر على مسار التنزيل إطلاقاً ──
# قبل الإصلاح كان يُمرَّر مجلد إلى download_media وTelethon<1.42 يدمج اسم
# المُرسِل كما هو، فاسم مثل ../../venv/.../x.pth يكتب خارج المجلد.
for hostile_ext in ["/../../evil.pth", "../../../etc/cron.d/x", ".jpg/../../y.pth",
                    ".pth", ".exe", "", ".jpg"]:
    path = main._safe_media_path(tg_msg(hostile_ext), "photo")
    assert main._is_inside(path, main.DOWNLOAD_DIR), f"تسرّب المسار عبر {hostile_ext!r}"
    assert os.path.basename(path).startswith("tg_")
    assert os.path.splitext(path)[1] in (".jpg", ".pth", ".exe"), path

# الامتداد الشاذ يسقط إلى الافتراضي الآمن
assert main._safe_media_path(tg_msg("/../../evil.pth"), "photo").endswith(".jpg")
assert main._safe_media_path(tg_msg(""), "video").endswith(".mp4")
assert main._safe_media_path(tg_msg(""), "document").endswith(".bin")

# ── 2) فحص الاحتواء نفسه ──
assert main._is_inside(os.path.join(main.DOWNLOAD_DIR, "a.jpg"), main.DOWNLOAD_DIR)
assert not main._is_inside("/etc/passwd", main.DOWNLOAD_DIR)
assert not main._is_inside(
    os.path.join(main.DOWNLOAD_DIR, "..", "..", "evil.py"), main.DOWNLOAD_DIR
)

# ── 3) SSRF: روابط وسائط X ──
for good in [
    "https://pbs.twimg.com/media/abc.jpg",
    "https://video.twimg.com/ext_tw_video/1/pu/vid/720x1280/x.mp4",
    "https://x.com/media/a.png",
]:
    assert main._trusted_media_url(good), good

for bad in [
    "http://pbs.twimg.com/media/abc.jpg",          # ليس https
    "https://169.254.169.254/latest/meta-data/",   # بيانات وصفية داخلية
    "https://127.0.0.1:8080/admin",
    "https://192.168.1.1/router",
    "https://evil.com/x.jpg",
    "https://twimg.com.evil.com/x.jpg",            # نطاق ملحق خادع
    "https://eviltwimg.com/x.jpg",
    "file:///etc/passwd",
    "",
]:
    assert not main._trusted_media_url(bad), bad

# ── 4) حجب بيانات الاعتماد في مخرجات git/pip ──
leaky = (
    "fatal: could not read from 'https://w70t:ghp_AbCdEf123456@github.com/w70t/x.git'\n"
    "hint: check https://user:pass@gitlab.internal/repo"
)
clean = main._redact(leaky)
assert "ghp_AbCdEf123456" not in clean
assert "pass@" not in clean
assert clean.count("***@") == 2
assert "github.com/w70t/x.git" in clean          # الرابط نفسه يبقى مفيداً
assert main._redact("لا شيء هنا") == "لا شيء هنا"
assert main._redact("") == ""

# ── 5) معرّف صفحة فيسبوك يُدمج في مسار URL ──
for ok in ["123456789012345", "1"]:
    assert main._FB_PAGE_ID.match(ok), ok
for bad in ["../../me", "me", "123/../../me", "123 456", "", "abc",
            "https://evil.com", "1" * 26]:
    assert not main._FB_PAGE_ID.match(bad), bad

# ── 6) تقييد صلاحيات ملفات الجلسة ──
session = os.path.join(main.BASE_DIR, "_pytest_probe.session")
try:
    with open(session, "w", encoding="utf-8") as f:
        f.write("fake session")
    os.chmod(session, 0o644)                     # كما ينشئها Telethon فعلياً
    assert stat.S_IMODE(os.stat(session).st_mode) == 0o644
    main._harden_state_permissions()
    assert stat.S_IMODE(os.stat(session).st_mode) == 0o600, "لم تُقيَّد صلاحيات الجلسة"
finally:
    os.remove(session)

# ── 7) امتداد رابط X الخارجي لا يُوثق به ──
assert main._SAFE_EXT.match(".mp4")
for bad_ext in ["/../../x", ".verylongext", ".", "", ".pth/../.."]:
    assert not main._SAFE_EXT.match(bad_ext), bad_ext

print("SECURITY OK")
