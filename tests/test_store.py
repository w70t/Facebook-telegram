import os
import time
from copy import deepcopy

import pytest

import store as store_module
from store import PendingStore


@pytest.fixture
def store(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    return PendingStore(str(tmp_path / "pending.json"), str(downloads))


def _media_file(store, name="tg_a.jpg"):
    path = os.path.join(store.download_dir, name)
    with open(path, "wb") as f:
        f.write(b"x")
    return path


def _symlink_or_skip(link, target, *, target_is_directory=False):
    try:
        os.symlink(target, link, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"إنشاء symlink غير متاح على هذه المنصة: {exc}")


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
    photo = _media_file(store, "tg_p.jpg")
    video = _media_file(store, "tg_v.mp4")
    item_id = store.add("نص", [
        {"path": photo, "type": "photo"},
        {"path": video, "type": "video"},
        {"path": os.path.join(store.download_dir, "tg_ghost.jpg"), "type": "photo"},
        {"path": _media_file(store, "tg_d.pdf"), "type": "document"},
    ])
    assert store.media_paths(item_id, ("photo",)) == [os.path.realpath(photo)]
    assert store.media_paths(item_id) == [os.path.realpath(photo), os.path.realpath(video)]


def test_purge_expired_removes_old_items_and_files(tmp_path):
    downloads = tmp_path / "dl"
    downloads.mkdir()
    store = PendingStore(str(tmp_path / "p.json"), str(downloads), ttl_hours=1)
    path = _media_file(store)
    item_id = store.add("قديم", [{"path": path, "type": "photo"}])
    store.update(
        item_id,
        created=time.time() - 7200,                           # عمره ساعتان
        review={"chat": 1, "msg": 1},
    )

    fresh = store.add("جديد")
    assert store.purge_expired() == 1
    assert store.get(item_id) is None
    assert store.get(fresh) is not None
    assert not os.path.exists(path)


def test_purge_expired_preserves_unreviewed_outbox(tmp_path):
    downloads = tmp_path / "dl"
    downloads.mkdir()
    store = PendingStore(str(tmp_path / "p.json"), str(downloads), ttl_hours=1)
    media = _media_file(store, "tg_outbox.jpg")
    item_id = store.add("حُفظ قبل إرسال المراجعة", [{"path": media, "type": "photo"}])
    store.update(item_id, created=0)

    assert store.purge_expired() == 0
    assert store.get(item_id) is not None
    assert os.path.exists(media)
    assert PendingStore(store.path, store.download_dir).get(item_id) is not None


@pytest.mark.parametrize("publish_state", ["publishing", "published"])
def test_purge_expired_preserves_publish_guards(tmp_path, publish_state):
    downloads = tmp_path / "dl"
    downloads.mkdir()
    store = PendingStore(str(tmp_path / "p.json"), str(downloads), ttl_hours=1)
    media = _media_file(store, f"tg_{publish_state}.jpg")
    item_id = store.add("لا تكرر النشر", [{"path": media, "type": "photo"}])
    store.update(
        item_id,
        created=0,
        review={"chat": 1, "msg": 1},
        publish_state=publish_state,
    )

    assert store.purge_expired() == 0
    assert store.get(item_id)["publish_state"] == publish_state
    assert os.path.exists(media)
    assert PendingStore(store.path, store.download_dir).get(item_id) is not None


def test_sweep_orphans_removes_unreferenced_old_files(store):
    orphan = _media_file(store, "tg_orphan.jpg")
    os.utime(orphan, (0, 0))                                  # ملف قديم بلا مرجع
    kept = _media_file(store, "tg_kept.jpg")
    os.utime(kept, (0, 0))
    store.add("نص", [{"path": kept, "type": "photo"}])

    assert store.sweep_orphans() == 1
    assert not os.path.exists(orphan)
    assert os.path.exists(kept)


def test_sweep_orphans_spares_recent_files(store):
    """ملف نُزّل للتو (قد يكون قيد الاستخدام) لا يُحذف."""
    _media_file(store, "tg_downloading.jpg")
    assert store.sweep_orphans() == 0


def test_remove_refuses_path_outside_downloads(store, tmp_path):
    victim = tmp_path / "tg_victim.jpg"
    victim.write_bytes(b"do not delete")
    escaped = os.path.join(store.download_dir, "..", victim.name)
    item_id = store.add("نص", [{"path": escaped, "type": "photo"}])

    store.remove(item_id)

    assert victim.read_bytes() == b"do not delete"
    assert store.get(item_id) is None


def test_remove_keeps_media_still_referenced_by_another_item(store):
    path = _media_file(store, "tg_shared_remove.jpg")
    media = [{"path": path, "type": "photo"}]
    removed_id = store.add("الأول", media)
    remaining_id = store.add("الثاني", media)

    store.remove(removed_id)

    assert os.path.exists(path)
    assert store.media_paths(remaining_id) == [os.path.realpath(path)]

    store.remove(remaining_id)
    assert not os.path.exists(path)


def test_sweep_spares_unmanaged_files_inside_downloads(store):
    victim = _media_file(store, "important.txt")
    os.utime(victim, (0, 0))

    assert store.sweep_orphans() == 0
    assert os.path.exists(victim)


def test_sweep_does_not_follow_file_symlink(store, tmp_path):
    victim = tmp_path / "tg_victim.jpg"
    victim.write_bytes(b"do not delete")
    link = os.path.join(store.download_dir, "tg_link.jpg")
    _symlink_or_skip(link, str(victim))

    assert store.sweep_orphans() == 0
    assert os.path.islink(link)
    assert victim.read_bytes() == b"do not delete"


def test_sweep_refuses_download_root_symlinked_outside_state(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "tg_victim.jpg"
    victim.write_bytes(b"do not delete")
    os.utime(victim, (0, 0))
    link = state / "downloads"
    _symlink_or_skip(str(link), str(outside), target_is_directory=True)
    pending = state / "pending.json"
    store = PendingStore(str(pending), str(link))

    assert store.sweep_orphans() == 0
    assert victim.read_bytes() == b"do not delete"


def test_add_is_transactional_when_save_fails(tmp_path, monkeypatch):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    store = PendingStore(
        str(tmp_path / "pending.json"), str(downloads), max_items=1
    )
    old_media = _media_file(store, "tg_old.jpg")
    old_id = store.add("القديم", [{"path": old_media, "type": "photo"}])
    store.update(old_id, review={"chat": 1, "msg": 1})
    before = dict(store.items)

    def fail_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store_module, "atomic_write_json", fail_write)
    with pytest.raises(OSError, match="disk full"):
        store.add("الجديد")

    assert store.items == before
    assert store.get(old_id) is not None
    assert os.path.exists(old_media)       # لم تُحذف وسائط العنصر الذي كان سيُزاح
    reloaded = PendingStore(store.path, store.download_dir, max_items=1)
    assert set(reloaded.items) == {old_id}


@pytest.mark.parametrize("publish_state", ["publishing", "published"])
def test_cap_keeps_publish_guard_and_new_item(tmp_path, publish_state):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    store = PendingStore(str(tmp_path / "pending.json"), str(downloads), max_items=2)
    guarded_media = _media_file(store, f"tg_guarded_{publish_state}.jpg")
    guarded_id = store.add(
        "حارس قديم", [{"path": guarded_media, "type": "photo"}]
    )
    store.update(guarded_id, created=0, publish_state=publish_state)
    evictable_media = _media_file(store, f"tg_evictable_{publish_state}.jpg")
    evictable_id = store.add(
        "عنصر قابل للإزاحة", [{"path": evictable_media, "type": "photo"}]
    )
    store.update(evictable_id, review={"chat": 1, "msg": 2})

    new_id = store.add("العنصر الجديد")

    assert set(store.items) == {guarded_id, new_id}
    assert store.get(guarded_id)["publish_state"] == publish_state
    assert store.get(evictable_id) is None
    assert os.path.exists(guarded_media)
    assert not os.path.exists(evictable_media)


def test_cap_keeps_media_referenced_by_non_evicted_item(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    store = PendingStore(str(tmp_path / "pending.json"), str(downloads), max_items=2)
    path = _media_file(store, "tg_shared_cap.jpg")
    media = [{"path": path, "type": "photo"}]
    evicted_id = store.add("الأقدم", media)
    store.update(evicted_id, created=0, review={"chat": 1, "msg": 1})
    remaining_id = store.add("يبقى", media)
    store.update(remaining_id, review={"chat": 1, "msg": 2})

    new_id = store.add("الجديد")

    assert set(store.items) == {remaining_id, new_id}
    assert os.path.exists(path)
    assert store.media_paths(remaining_id) == [os.path.realpath(path)]

    store.remove(remaining_id)
    assert not os.path.exists(path)


def test_cap_rejects_atomically_when_all_existing_items_are_guarded(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    store = PendingStore(str(tmp_path / "pending.json"), str(downloads), max_items=2)
    media_paths = []
    for publish_state in ("publishing", "published"):
        media = _media_file(store, f"tg_only_{publish_state}.jpg")
        media_paths.append(media)
        item_id = store.add("محمي", [{"path": media, "type": "photo"}])
        store.update(item_id, created=0, publish_state=publish_state)
    before = deepcopy(store.items)

    with pytest.raises(OSError, match="ممتلئ بعناصر محمية"):
        store.add("لا يجوز أن يزيح الحراس")

    assert store.items == before
    assert all(os.path.exists(path) for path in media_paths)
    assert PendingStore(store.path, store.download_dir, max_items=2).items == before


def test_cap_does_not_evict_unreviewed_outbox(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    store = PendingStore(str(tmp_path / "pending.json"), str(downloads), max_items=1)
    outbox_id = store.add("لم يصل إلى Telegram بعد")
    before = deepcopy(store.items)

    with pytest.raises(OSError, match="ممتلئ بعناصر محمية أو لم تُرسل"):
        store.add("منشور تالٍ")

    assert store.items == before
    assert store.get(outbox_id)["review"] is None


def test_remove_is_transactional_when_save_fails(store, monkeypatch):
    media = _media_file(store, "tg_kept_on_failure.jpg")
    item_id = store.add("نص", [{"path": media, "type": "photo"}])

    def fail_write(*args, **kwargs):
        raise OSError("read only")

    monkeypatch.setattr(store_module, "atomic_write_json", fail_write)
    with pytest.raises(OSError, match="read only"):
        store.remove(item_id)

    assert store.get(item_id) is not None
    assert os.path.exists(media)
    reloaded = PendingStore(store.path, store.download_dir)
    assert reloaded.get(item_id) is not None


def test_purge_is_transactional_when_save_fails(store, monkeypatch):
    media = _media_file(store, "tg_stale.jpg")
    item_id = store.add("قديم", [{"path": media, "type": "photo"}])
    store.update(item_id, created=0, review={"chat": 1, "msg": 1})

    def fail_write(*args, **kwargs):
        raise OSError("device error")

    monkeypatch.setattr(store_module, "atomic_write_json", fail_write)
    with pytest.raises(OSError, match="device error"):
        store.purge_expired()

    assert store.get(item_id) is not None
    assert os.path.exists(media)
    reloaded = PendingStore(store.path, store.download_dir)
    assert reloaded.get(item_id) is not None


def test_malformed_media_record_cannot_break_orphan_sweep(store):
    orphan = _media_file(store, "tg_orphan_with_bad_record.jpg")
    os.utime(orphan, (0, 0))
    item_id = store.add("نص")
    store.items[item_id]["media"] = ["not-a-dict", None, 123]

    assert store.sweep_orphans() == 1
    assert not os.path.exists(orphan)


@pytest.mark.parametrize("bad_media", [123, {"path": "tg_x.jpg"}, "bad"])
def test_malformed_media_container_is_treated_as_empty(store, bad_media):
    item_id = store.add("سجل قديم")
    store.items[item_id]["media"] = bad_media

    assert store.media_paths(item_id) == []
    assert store.sweep_orphans() == 0


def test_corrupt_pending_file_does_not_crash(tmp_path):
    path = tmp_path / "pending.json"
    path.write_text("ليس JSON", encoding="utf-8")
    store = PendingStore(str(path), str(tmp_path))
    assert store.items == {}
    assert store.add("نص جديد")
