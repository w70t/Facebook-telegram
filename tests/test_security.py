"""اختبارات الحواجز الأمنية — كل واحدة تعيد إنتاج الثغرة الأصلية."""
import os
import stat
import subprocess
import sys

import pytest

from store import PendingStore

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "_smoke_security.py")


def test_main_security_helpers(tmp_path):
    """يفحص دوال main الأمنية في عملية منفصلة (main يحتاج بيئة إعدادات)."""
    env = dict(os.environ)
    env.update({
        "SETTINGS_FILE": str(tmp_path / "settings.json"),
        "API_ID": "1", "API_HASH": "h", "BOT_TOKEN": "t",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    result = subprocess.run(
        [sys.executable, SCRIPT], capture_output=True, text=True, env=env,
        cwd=str(tmp_path), timeout=120,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "SECURITY OK" in result.stdout


# --- سقف المنشورات المعلّقة (إغراق القرص) ---
def test_pending_queue_is_capped(tmp_path):
    downloads = tmp_path / "dl"
    downloads.mkdir()
    store = PendingStore(str(tmp_path / "p.json"), str(downloads), max_items=10)
    ids = [store.add(f"منشور {i}") for i in range(40)]
    assert len(store.items) <= 10
    assert store.get(ids[-1]) is not None      # الأحدث محفوظ
    assert store.get(ids[0]) is None           # الأقدم أُزيح


def test_cap_deletes_evicted_media_files(tmp_path):
    downloads = tmp_path / "dl"
    downloads.mkdir()
    store = PendingStore(str(tmp_path / "p.json"), str(downloads), max_items=2)
    paths = []
    for i in range(5):
        path = downloads / f"{i}.jpg"
        path.write_bytes(b"x")
        paths.append(str(path))
        store.add(f"منشور {i}", [{"path": str(path), "type": "photo"}])
    assert not os.path.exists(paths[0])        # ملف المُزاح حُذف معه
    assert os.path.exists(paths[-1])


# --- حماية التنظيف من مجلد خطير ---
@pytest.mark.parametrize("bad_dir", ["/", "/etc", "/tmp", "/home"])
def test_sweep_refuses_system_directories(tmp_path, bad_dir):
    store = PendingStore(str(tmp_path / "p.json"), bad_dir)
    assert store.sweep_orphans() == 0


def test_sweep_refuses_home_directory(tmp_path):
    store = PendingStore(str(tmp_path / "p.json"), os.path.expanduser("~"))
    assert store.sweep_orphans() == 0


# --- صلاحيات ملفات الأسرار ---
def test_pending_file_is_owner_only(tmp_path):
    downloads = tmp_path / "dl"
    downloads.mkdir()
    store = PendingStore(str(tmp_path / "p.json"), str(downloads))
    store.add("نص فيه محتوى القناة")
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


def test_touch_private_creates_restricted_file(tmp_path):
    from twitter import _touch_private

    path = str(tmp_path / "x_cookies_acct.json")
    _touch_private(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # الكتابة اللاحقة بـ open(...,"w") تقتطع الملف ولا تغيّر صلاحياته
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"cookie": "value"}')
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
