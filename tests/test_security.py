"""اختبارات الحواجز الأمنية — كل واحدة تعيد إنتاج الثغرة الأصلية."""
import os
import stat
import subprocess
import sys

import pytest

from store import PendingStore

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
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


def test_crash_leftover_x_cookie_temp_is_gitignored():
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", ".x-cookies-deadbeef.tmp"],
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0


def test_x_cooldown_hmac_key_is_gitignored():
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "x_cooldown_key.json"],
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0


# --- سقف المنشورات المعلّقة (إغراق القرص) ---
def test_pending_queue_is_capped(tmp_path):
    downloads = tmp_path / "dl"
    downloads.mkdir()
    store = PendingStore(str(tmp_path / "p.json"), str(downloads), max_items=10)
    ids = []
    for i in range(40):
        item_id = store.add(f"منشور {i}")
        store.update(item_id, review={"chat": 1, "msg": i + 1})
        ids.append(item_id)
    assert len(store.items) <= 10
    assert store.get(ids[-1]) is not None      # الأحدث محفوظ
    assert store.get(ids[0]) is None           # الأقدم أُزيح


def test_cap_deletes_evicted_media_files(tmp_path):
    downloads = tmp_path / "dl"
    downloads.mkdir()
    store = PendingStore(str(tmp_path / "p.json"), str(downloads), max_items=2)
    paths = []
    for i in range(5):
        path = downloads / f"tg_{i}.jpg"
        path.write_bytes(b"x")
        paths.append(str(path))
        item_id = store.add(f"منشور {i}", [{"path": str(path), "type": "photo"}])
        store.update(item_id, review={"chat": 1, "msg": i + 1})
    assert not os.path.exists(paths[0])        # ملف المُزاح حُذف معه
    assert os.path.exists(paths[-1])


# --- حماية التنظيف من مجلد غير مُدار (الاختبار كله داخل tmp_path) ---
def test_sweep_refuses_directory_outside_state_root(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "tg_victim.jpg"
    victim.write_bytes(b"do not delete")
    os.utime(victim, (0, 0))
    store = PendingStore(str(state / "pending.json"), str(outside))

    assert store.sweep_orphans() == 0
    assert victim.read_bytes() == b"do not delete"


def test_sweep_refuses_state_root_itself(tmp_path):
    victim = tmp_path / "tg_victim.jpg"
    victim.write_bytes(b"do not delete")
    os.utime(victim, (0, 0))
    store = PendingStore(str(tmp_path / "pending.json"), str(tmp_path))

    assert store.sweep_orphans() == 0
    assert victim.read_bytes() == b"do not delete"


# --- صلاحيات ملفات الأسرار ---
@pytest.mark.skipif(os.name != "posix", reason="أوضاع Unix 0600 لا تنطبق على Windows")
def test_pending_file_is_owner_only(tmp_path):
    downloads = tmp_path / "dl"
    downloads.mkdir()
    store = PendingStore(str(tmp_path / "p.json"), str(downloads))
    store.add("نص فيه محتوى القناة")
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="أوضاع Unix 0600 لا تنطبق على Windows")
def test_atomic_cookie_save_creates_restricted_file(tmp_path):
    from twitter import _save_cookies_atomic

    class Client:
        @staticmethod
        def save_cookies(path):
            with open(path, "w", encoding="utf-8") as cookie_file:
                cookie_file.write('{"cookie": "value"}')

    path = str(tmp_path / "x_cookies_acct.json")
    _save_cookies_atomic(Client(), path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
