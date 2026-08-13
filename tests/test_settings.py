import json
import os
import stat

import pytest

import settings as settings_module
from settings import DEFAULTS, Settings

TXID_KEY = "__settings_txid"


def read_document(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def split_primary(document):
    data = dict(document)
    return data.pop(TXID_KEY), data


@pytest.fixture
def settings(tmp_path):
    return Settings(path=str(tmp_path / "settings.json"))


def test_defaults_are_not_shared_between_instances(tmp_path):
    """deepcopy: تعديل نسخة يجب ألا يلوّث DEFAULTS ولا النسخ الأخرى."""
    a = Settings(path=str(tmp_path / "a.json"))
    a.add_filter_word("اعلان")
    b = Settings(path=str(tmp_path / "b.json"))
    assert b.filter_words() == []
    assert DEFAULTS["filter_words"] == []


def test_saved_files_are_flat_owner_only_and_hide_transaction_id(settings):
    settings.set("fb_page_token", "secret")
    primary = read_document(settings.path)
    assert primary["fb_page_token"] == "secret"
    assert TXID_KEY in primary
    assert TXID_KEY not in settings.data
    assert "format" not in primary
    assert "state" not in primary

    if os.name == "posix":
        for path in (settings.path, settings.path + ".bak"):
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_corrupt_file_is_recovered_from_backup(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings(path=str(path))
    s.set("fb_page_id", "123")
    s.set("fb_page_token", "tok")

    path.write_text('{"fb_page_id": "12', encoding="utf-8")   # محاكاة انقطاع كهرباء

    recovered = Settings(path=str(path))
    assert recovered.recovery == "recovered"
    assert recovered.get("fb_page_id") == "123"
    assert recovered.get("fb_page_token") == "tok"
    # الملف التالف يُحفظ للفحص ولا يُحذف
    assert list(tmp_path.glob("settings.json.corrupt-*"))
    restored_primary = read_document(path)
    assert restored_primary["fb_page_id"] == "123"
    assert TXID_KEY in restored_primary
    assert Settings(path=str(path)).recovery is None


def test_recovery_never_resurrects_revoked_admin_or_deleted_x_secret(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings(path=str(path))
    s.add_admin(84)
    s.add_x_login("Reader", "reader@example.test", "secret-password")
    s.remove_admin(84)
    s.remove_x_login("reader")

    primary = read_document(path)
    backup = read_document(tmp_path / "settings.json.bak")
    primary_txid, primary_data = split_primary(primary)
    assert backup["format"] == 2
    assert backup["state"] == "committed"
    assert backup["txid"] == primary_txid
    assert primary_data == backup["data"]
    assert 84 not in backup["data"]["admin_ids"]
    assert backup["data"]["x_logins"] == []

    path.write_text("{broken", encoding="utf-8")
    recovered = Settings(path=str(path))
    assert recovered.recovery == "recovered"
    assert recovered.is_admin(84) is False
    assert recovered.x_logins() == []


def test_missing_primary_is_recovered_from_backup(tmp_path):
    path = tmp_path / "settings.json"
    original = Settings(path=str(path))
    original.set("fb_page_id", "456")
    path.unlink()

    recovered = Settings(path=str(path))
    assert recovered.recovery == "recovered"
    assert recovered.get("fb_page_id") == "456"
    primary = read_document(path)
    backup = read_document(tmp_path / "settings.json.bak")
    assert primary["fb_page_id"] == "456"
    assert primary[TXID_KEY] == backup["txid"]


def test_legacy_unmarked_backup_is_rejected_without_primary(tmp_path):
    path = tmp_path / "settings.json"
    backup = tmp_path / "settings.json.bak"
    backup.write_text(json.dumps({"admin_ids": [84]}), encoding="utf-8")

    recovered = Settings(path=str(path))
    assert recovered.recovery == "corrupt"
    assert recovered.is_admin(84) is False
    assert not path.exists()


def test_corrupt_file_without_backup_does_not_crash(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("}{ليس JSON", encoding="utf-8")
    s = Settings(path=str(path))
    assert s.recovery == "corrupt"
    assert s.get("sources") == []


def test_write_is_atomic_no_partial_file(settings):
    """لا يوجد ملف مؤقت متبقٍّ، والملف الناتج دائماً JSON صالح."""
    for i in range(20):
        settings.set("default_cc", str(i))
        json.loads(open(settings.path, encoding="utf-8").read())
    leftovers = [f for f in os.listdir(os.path.dirname(settings.path))
                 if f.startswith(".tmp-")]
    assert leftovers == []


def test_get_int_survives_corrupt_values(settings):
    settings.data["x_poll_seconds"] = "غير رقم"
    assert settings.get_int("x_poll_seconds", 120) == 120
    settings.data["x_poll_seconds"] = -5
    assert settings.get_int("x_poll_seconds", 120) == 120
    settings.data["x_poll_seconds"] = "300"
    assert settings.get_int("x_poll_seconds", 120) == 300


def test_add_x_account_stores_starting_point(settings):
    """بدون last_id كانت أول دورة ترسل آخر 20 تغريدة دفعة واحدة."""
    assert settings.add_x_account("someone", "42", last_id=999) is True
    assert settings.x_accounts()[0]["last_id"] == "999"
    assert settings.add_x_account("SOMEONE", "42") is False   # لا تكرار


def test_filter_words_are_case_insensitive(settings):
    settings.add_filter_word("Sale")
    assert settings.is_filtered("BIG SALE TODAY")
    assert not settings.is_filtered("خبر عادي")
    assert settings.add_filter_word("sale") is False          # موجودة مسبقاً
    assert settings.remove_filter_word("SALE") == 1


def test_x_login_rotation(settings):
    settings.add_x_login("first", None, "pw1")
    settings.add_x_login("second", None, "pw2")
    assert settings.active_x_login()["username"] == "second"   # الأحدث يتصدّر
    settings.mark_x_login_failed("second")
    assert settings.active_x_login()["username"] == "first"
    settings.reset_x_failures()
    assert settings.x_login_ready()


def test_settings_persist_across_reload(tmp_path):
    path = str(tmp_path / "settings.json")
    a = Settings(path=path)
    a.add_source(-100123, "قناة", "@channel")
    assert Settings(path=path).source_ids() == {-100123}


def test_prepared_write_failure_does_not_mutate_memory_or_disk(settings, monkeypatch):
    settings.set("default_cc", "49")
    before = json.loads(json.dumps(settings.data))
    primary_before = read_document(settings.path)
    backup_before = read_document(settings.path + ".bak")
    real_write = settings_module.atomic_write_json

    def fail_prepared(target, data, mode=0o600):
        assert os.path.abspath(target) == os.path.abspath(settings.path + ".bak")
        assert data["state"] == "prepared"
        raise OSError("disk full")

    monkeypatch.setattr(settings_module, "atomic_write_json", fail_prepared)
    with pytest.raises(OSError, match="disk full"):
        settings.set("default_cc", "966")

    assert settings.data == before
    assert read_document(settings.path) == primary_before
    assert read_document(settings.path + ".bak") == backup_before
    monkeypatch.setattr(settings_module, "atomic_write_json", real_write)
    assert Settings(path=settings.path).get("default_cc") == "49"


def test_primary_failure_leaves_prepared_backup_uncommitted(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    s = Settings(path=str(path))
    real_write = settings_module.atomic_write_json

    def fail_primary(target, data, mode=0o600):
        if os.path.abspath(target) == os.path.abspath(str(path)):
            raise OSError("primary unavailable")
        return real_write(target, data, mode=mode)

    monkeypatch.setattr(settings_module, "atomic_write_json", fail_primary)
    with pytest.raises(OSError, match="primary unavailable"):
        s.add_admin(84)
    assert s.is_admin(84) is False
    assert not path.exists()
    prepared = read_document(tmp_path / "settings.json.bak")
    assert prepared["state"] == "prepared"
    assert prepared["data"]["admin_ids"] == [84]
    assert TXID_KEY not in prepared["data"]

    monkeypatch.setattr(settings_module, "atomic_write_json", real_write)
    recovered = Settings(path=str(path))
    assert recovered.recovery == "corrupt"
    assert recovered.is_admin(84) is False


def test_failed_change_cannot_replace_existing_primary_on_restart(
    tmp_path, monkeypatch,
):
    path = tmp_path / "settings.json"
    s = Settings(path=str(path))
    s.set("default_cc", "49")
    real_write = settings_module.atomic_write_json

    def fail_primary(target, data, mode=0o600):
        if os.path.abspath(target) == os.path.abspath(str(path)):
            raise OSError("primary unavailable")
        return real_write(target, data, mode=mode)

    monkeypatch.setattr(settings_module, "atomic_write_json", fail_primary)
    with pytest.raises(OSError, match="primary unavailable"):
        s.add_admin(84)
    monkeypatch.setattr(settings_module, "atomic_write_json", real_write)

    recovered = Settings(path=str(path))
    assert recovered.recovery is None
    assert recovered.get("default_cc") == "49"
    assert recovered.is_admin(84) is False
    primary = read_document(path)
    backup = read_document(tmp_path / "settings.json.bak")
    assert backup["state"] == "committed"
    assert backup["txid"] == primary[TXID_KEY]
    assert backup["data"]["admin_ids"] == []


def test_crash_after_primary_commit_is_finalized_on_restart(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    s = Settings(path=str(path))
    real_write = settings_module.atomic_write_json

    class SimulatedCrash(BaseException):
        pass

    def crash_after_primary_write(target, data, mode=0o600):
        real_write(target, data, mode=mode)
        if os.path.abspath(target) == os.path.abspath(str(path)):
            raise SimulatedCrash

    monkeypatch.setattr(
        settings_module, "atomic_write_json", crash_after_primary_write
    )
    with pytest.raises(SimulatedCrash):
        s.add_admin(84)

    # العملية الحالية لم تصل لتحديث الذاكرة، لكن primary هي نقطة التثبيت على القرص.
    assert s.is_admin(84) is False
    primary = read_document(path)
    prepared = read_document(tmp_path / "settings.json.bak")
    assert primary["admin_ids"] == [84]
    assert primary[TXID_KEY] == prepared["txid"]
    assert prepared["state"] == "prepared"

    monkeypatch.setattr(settings_module, "atomic_write_json", real_write)
    recovered = Settings(path=str(path))
    assert recovered.recovery is None
    assert recovered.is_admin(84) is True
    committed = read_document(tmp_path / "settings.json.bak")
    assert committed["state"] == "committed"
    assert committed["txid"] == primary[TXID_KEY]


def test_backup_finalization_failure_still_commits_primary_and_memory(
    tmp_path, monkeypatch,
):
    path = tmp_path / "settings.json"
    s = Settings(path=str(path))
    real_write = settings_module.atomic_write_json

    def fail_finalization(target, data, mode=0o600):
        if (
            os.path.abspath(target) == os.path.abspath(str(path) + ".bak")
            and data.get("state") == "committed"
        ):
            raise OSError("backup finalize unavailable")
        return real_write(target, data, mode=mode)

    monkeypatch.setattr(settings_module, "atomic_write_json", fail_finalization)
    s.add_admin(84)
    assert s.is_admin(84) is True
    primary = read_document(path)
    prepared = read_document(tmp_path / "settings.json.bak")
    assert primary["admin_ids"] == [84]
    assert primary[TXID_KEY] == prepared["txid"]
    assert prepared["state"] == "prepared"

    monkeypatch.setattr(settings_module, "atomic_write_json", real_write)
    recovered = Settings(path=str(path))
    assert recovered.is_admin(84) is True
    backup = read_document(tmp_path / "settings.json.bak")
    assert backup["state"] == "committed"
    assert backup["txid"] == primary[TXID_KEY]


def test_modern_primary_repairs_backup_with_matching_data_but_wrong_txid(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings(path=str(path))
    s.set("default_cc", "49")
    primary = read_document(path)
    primary_txid, primary_data = split_primary(primary)
    backup_path = tmp_path / "settings.json.bak"
    backup = read_document(backup_path)
    backup["txid"] = "different-transaction"
    backup_path.write_text(json.dumps(backup), encoding="utf-8")

    loaded = Settings(path=str(path))
    assert loaded.get("default_cc") == "49"
    repaired = read_document(backup_path)
    assert repaired["state"] == "committed"
    assert repaired["txid"] == primary_txid
    assert repaired["data"] == primary_data


def test_modern_primary_repairs_backup_with_matching_txid_but_wrong_data(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings(path=str(path))
    s.set("default_cc", "49")
    primary = read_document(path)
    primary_txid, primary_data = split_primary(primary)
    backup_path = tmp_path / "settings.json.bak"
    backup = read_document(backup_path)
    backup["data"]["default_cc"] = "966"
    backup_path.write_text(json.dumps(backup), encoding="utf-8")

    loaded = Settings(path=str(path))
    assert loaded.get("default_cc") == "49"
    repaired = read_document(backup_path)
    assert repaired["txid"] == primary_txid
    assert repaired["data"] == primary_data


def test_legacy_primary_is_migrated_before_legacy_backup_is_trusted(tmp_path):
    path = tmp_path / "settings.json"
    backup_path = tmp_path / "settings.json.bak"
    legacy_primary = {"default_cc": "49", "admin_ids": []}
    stale_legacy_backup = {"default_cc": "966", "admin_ids": [84]}
    path.write_text(json.dumps(legacy_primary), encoding="utf-8")
    backup_path.write_text(json.dumps(stale_legacy_backup), encoding="utf-8")

    loaded = Settings(path=str(path))
    assert loaded.get("default_cc") == "49"
    assert loaded.is_admin(84) is False
    assert TXID_KEY not in loaded.data

    migrated_primary = read_document(path)
    txid, migrated_data = split_primary(migrated_primary)
    committed = read_document(backup_path)
    assert migrated_data == legacy_primary
    assert committed == {
        "format": 2,
        "state": "committed",
        "txid": txid,
        "data": legacy_primary,
    }


def test_failed_legacy_migration_keeps_primary_authoritative(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    backup_path = tmp_path / "settings.json.bak"
    legacy_primary = {"default_cc": "49", "admin_ids": []}
    stale_legacy_backup = {"default_cc": "966", "admin_ids": [84]}
    path.write_text(json.dumps(legacy_primary), encoding="utf-8")
    backup_path.write_text(json.dumps(stale_legacy_backup), encoding="utf-8")

    def fail_migration(*args, **kwargs):
        raise OSError("read-only disk")

    monkeypatch.setattr(settings_module, "atomic_write_json", fail_migration)
    loaded = Settings(path=str(path))
    assert loaded.get("default_cc") == "49"
    assert loaded.is_admin(84) is False
    assert read_document(path) == legacy_primary
    assert read_document(backup_path) == stale_legacy_backup


def test_successful_grant_recovers_only_from_committed_backup(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings(path=str(path))
    s.add_admin(84)
    path.write_text("{broken", encoding="utf-8")

    recovered = Settings(path=str(path))
    assert recovered.recovery == "recovered"
    assert recovered.is_admin(84) is True


def test_nested_accessors_return_copies(settings):
    settings.add_x_login("acct", None, "secret")
    settings.add_x_account("source", "7", last_id=1)

    logins = settings.x_logins()
    accounts = settings.x_accounts()
    logins[0]["password"] = "changed-outside"
    accounts[0]["last_id"] = "999"

    assert settings.x_logins()[0]["password"] == "secret"
    assert settings.x_accounts()[0]["last_id"] == "1"


def test_set_many_is_atomic(settings, monkeypatch):
    before = json.loads(json.dumps(settings.data))

    def fail_write(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(settings_module, "atomic_write_json", fail_write)
    with pytest.raises(OSError, match="disk unavailable"):
        settings.set_many({"api_id": 123, "api_hash": "hash", "bot_token": "token"})

    assert settings.data == before


def test_x_last_id_never_moves_backwards(settings):
    settings.add_x_account("source", "7", last_id=105)
    settings.set_x_last_id("source", 101)
    assert settings.x_accounts()[0]["last_id"] == "105"
    settings.set_x_last_id("source", 106)
    assert settings.x_accounts()[0]["last_id"] == "106"
