import json
import os
import stat

from jsonio import atomic_write_json, read_json_resilient


def test_written_file_is_valid_and_owner_only(tmp_path):
    path = tmp_path / "d.json"
    atomic_write_json(str(path), {"مفتاح": "قيمة"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"مفتاح": "قيمة"}
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_no_temp_files_left_behind(tmp_path):
    for i in range(10):
        atomic_write_json(str(tmp_path / "d.json"), {"i": i})
    assert [f for f in os.listdir(tmp_path) if f.startswith(".tmp-")] == []


def test_failed_write_keeps_previous_content(tmp_path):
    """قيمة غير قابلة للتسلسل تفشل — الملف القديم يبقى سليماً لا نصف مكتوب."""
    path = tmp_path / "d.json"
    atomic_write_json(str(path), {"ok": 1})
    try:
        atomic_write_json(str(path), {"bad": object()})
    except TypeError:
        pass
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": 1}
    assert [f for f in os.listdir(tmp_path) if f.startswith(".tmp-")] == []


def test_missing_file_returns_none(tmp_path):
    assert read_json_resilient(str(tmp_path / "nope.json")) == (None, None)


def test_valid_file_read_directly(tmp_path):
    path = tmp_path / "d.json"
    atomic_write_json(str(path), {"a": 1})
    assert read_json_resilient(str(path)) == ({"a": 1}, None)


def test_corrupt_file_quarantined_and_recovered(tmp_path):
    path = tmp_path / "d.json"
    (tmp_path / "d.json.bak").write_text('{"a": 1}', encoding="utf-8")
    path.write_text('{"a": ', encoding="utf-8")

    data, status = read_json_resilient(str(path))
    assert (data, status) == ({"a": 1}, "recovered")
    assert list(tmp_path.glob("d.json.corrupt-*"))
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    assert json.loads((tmp_path / "d.json.bak").read_text(encoding="utf-8")) == {"a": 1}
    assert read_json_resilient(str(path)) == ({"a": 1}, None)


def test_missing_primary_is_recovered_from_backup(tmp_path):
    path = tmp_path / "d.json"
    backup = tmp_path / "d.json.bak"
    backup.write_text('{"a": 2}', encoding="utf-8")

    assert read_json_resilient(str(path)) == ({"a": 2}, "recovered")
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2}
    assert json.loads(backup.read_text(encoding="utf-8")) == {"a": 2}
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_missing_primary_and_corrupt_backup(tmp_path):
    (tmp_path / "d.json.bak").write_text("}}}", encoding="utf-8")
    assert read_json_resilient(str(tmp_path / "d.json")) == (None, "corrupt")


def test_corrupt_file_and_corrupt_backup(tmp_path):
    path = tmp_path / "d.json"
    path.write_text("{{{", encoding="utf-8")
    (tmp_path / "d.json.bak").write_text("}}}", encoding="utf-8")
    assert read_json_resilient(str(path)) == (None, "corrupt")
