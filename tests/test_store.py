import os
import time

import pytest

from store import PendingStore


@pytest.fixture
def store(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    return PendingStore(str(tmp_path / "pending.json"), str(downloads))


def _media_file(store, name="a.jpg"):
    path = os.path.join(store.download_dir, name)
    with open(path, "wb") as f:
        f.write(b"x")
    return path


def test_ids_are_unique_and_not_sequential(store):
    ids = {store.add(f"منشور {i}") for i in range(50)}
    assert len(ids) == 50
    assert "1" not in ids and "2" not in ids


def test_ids_are_not_reused_after_restart(tmp_path):
    """
    البق الأصلي: العدّاد يبدأ من 1 بعد كل تشغيل، فزر قديم في القروب كان ينشر
    منشوراً مختلفاً تماماً. المعرّفات العشوائية + الحفظ على القرص يمنعان ذلك.
    """
    path = str(tmp_path / "pending.json")
    downloads = str(tmp_path / "dl")
    os.makedirs(downloads)

    first = PendingStore(path, downloads)
    old_id = first.add("المنشور القديم")

    restarted = PendingStore(path, downloads)          # محاكاة إعادة التشغيل
    assert restarted.get(old_id)["text"] == "المنشور القديم"

    new_id = restarted.add("منشور جديد تماماً")
    assert new_id != old_id
    assert restarted.get(old_id)["text"] == "المنشور القديم"


def test_ids_fit_in_telegram_callback_data(store):
    item_id = store.add("نص")
    assert len(f"pubtext:{item_id}".encode()) <= 64


def test_remove_deletes_media_files(store):
    path = _media_file(store)
    item_id = store.add("نص", [{"path": path, "type": "photo"}])
    store.remove(item_id)
    assert not os.path.exists(path)
    assert store.get(item_id) is None


def test_media_paths_filters_by_kind_and_existence(store):
    photo = _media_file(store, "p.jpg")
    video = _media_file(store, "v.mp4")
    item_id = store.add("نص", [
        {"path": photo, "type": "photo"},
        {"path": video, "type": "video"},
        {"path": os.path.join(store.download_dir, "ghost.jpg"), "type": "photo"},
        {"path": _media_file(store, "d.pdf"), "type": "document"},
    ])
    assert store.media_paths(item_id, ("photo",)) == [photo]
    assert store.media_paths(item_id) == [photo, video]


def test_purge_expired_removes_old_items_and_files(tmp_path):
    downloads = tmp_path / "dl"
    downloads.mkdir()
    store = PendingStore(str(tmp_path / "p.json"), str(downloads), ttl_hours=1)
    path = _media_file(store)
    item_id = store.add("قديم", [{"path": path, "type": "photo"}])
    store.items[item_id]["created"] = time.time() - 7200      # عمره ساعتان

    fresh = store.add("جديد")
    assert store.purge_expired() == 1
    assert store.get(item_id) is None
    assert store.get(fresh) is not None
    assert not os.path.exists(path)


def test_sweep_orphans_removes_unreferenced_old_files(store):
    orphan = _media_file(store, "orphan.jpg")
    os.utime(orphan, (0, 0))                                  # ملف قديم بلا مرجع
    kept = _media_file(store, "kept.jpg")
    os.utime(kept, (0, 0))
    store.add("نص", [{"path": kept, "type": "photo"}])

    assert store.sweep_orphans() == 1
    assert not os.path.exists(orphan)
    assert os.path.exists(kept)


def test_sweep_orphans_spares_recent_files(store):
    """ملف نُزّل للتو (قد يكون قيد الاستخدام) لا يُحذف."""
    _media_file(store, "downloading.jpg")
    assert store.sweep_orphans() == 0


def test_corrupt_pending_file_does_not_crash(tmp_path):
    path = tmp_path / "pending.json"
    path.write_text("ليس JSON", encoding="utf-8")
    store = PendingStore(str(path), str(tmp_path))
    assert store.items == {}
    assert store.add("نص جديد")
