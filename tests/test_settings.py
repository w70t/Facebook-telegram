import json
import os
import stat

import pytest

from settings import DEFAULTS, Settings


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


def test_saved_file_is_owner_only(settings):
    settings.set("fb_page_token", "secret")
    mode = stat.S_IMODE(os.stat(settings.path).st_mode)
    assert mode == 0o600


def test_corrupt_file_is_recovered_from_backup(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings(path=str(path))
    s.set("fb_page_id", "123")
    s.set("fb_page_token", "tok")          # هذه الكتابة تنشئ .bak من النسخة السابقة

    path.write_text('{"fb_page_id": "12', encoding="utf-8")   # محاكاة انقطاع كهرباء

    recovered = Settings(path=str(path))
    assert recovered.recovery == "recovered"
    assert recovered.get("fb_page_id") == "123"
    # الملف التالف يُحفظ للفحص ولا يُحذف
    assert list(tmp_path.glob("settings.json.corrupt-*"))


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
