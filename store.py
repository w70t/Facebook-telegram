"""
مخزن المنشورات المعلّقة (بانتظار المراجعة).

لماذا على القرص وليس في الذاكرة؟
عدّاد تسلسلي في الذاكرة (1, 2, 3…) يبدأ من الصفر بعد كل إعادة تشغيل، بينما تبقى
رسائل المراجعة القديمة في القروب بأزرارها. ضغطة على زر قديم كانت تنشر **منشوراً
مختلفاً تماماً** على فيسبوك. الحل: معرّف عشوائي غير قابل لإعادة الاستخدام + حفظ
الحالة على القرص لتنجو من إعادة التشغيل.
"""
import logging
import os
import secrets
import time

from jsonio import atomic_write_json, read_json_resilient

log = logging.getLogger("tg2fb.store")

PLAYABLE = ("photo", "video")


class PendingStore:
    def __init__(self, path, download_dir, ttl_hours=48):
        self.path = path
        self.download_dir = download_dir
        self.ttl_hours = ttl_hours
        self.items = {}
        self._load()

    def _load(self):
        data, status = read_json_resilient(self.path)
        if status == "corrupt":
            log.error("ملف المنشورات المعلّقة تالف — سأبدأ بقائمة فارغة.")
        if isinstance(data, dict):
            self.items = {k: v for k, v in data.items() if isinstance(v, dict)}
        log.info("منشورات معلّقة محمّلة: %d", len(self.items))

    def _save(self):
        try:
            atomic_write_json(self.path, self.items, mode=0o600)
        except OSError as e:
            log.error("تعذّر حفظ المنشورات المعلّقة: %s", e)

    # --- عمليات ---
    def add(self, text, media=None, origin=None):
        """media: [{"path": str, "type": "photo"|"video"|"document"}]"""
        item_id = secrets.token_urlsafe(8)
        while item_id in self.items:                # احتياط نظري
            item_id = secrets.token_urlsafe(8)
        self.items[item_id] = {
            "text": text or "",
            "media": list(media or []),
            "origin": origin or "",
            "created": time.time(),
            "review": None,                         # {"chat": .., "msg": ..}
        }
        self._save()
        return item_id

    def get(self, item_id):
        return self.items.get(item_id)

    def update(self, item_id, **fields):
        item = self.items.get(item_id)
        if not item:
            return None
        item.update(fields)
        self._save()
        return item

    def remove(self, item_id):
        item = self.items.pop(item_id, None)
        if item:
            self._delete_files(item)
            self._save()
        return item

    def media_paths(self, item_id, kinds=PLAYABLE):
        item = self.items.get(item_id) or {}
        return [
            m["path"] for m in (item.get("media") or [])
            if m.get("type") in kinds and m.get("path") and os.path.exists(m["path"])
        ]

    # --- تنظيف دوري ---
    @staticmethod
    def _delete_files(item):
        for media in item.get("media") or []:
            path = media.get("path")
            if not path:
                continue
            try:
                os.remove(path)
            except OSError:
                pass

    def purge_expired(self):
        """يحذف المنشورات التي لم يتخذ فيها أحد قراراً خلال المهلة."""
        if not self.ttl_hours:
            return 0
        cutoff = time.time() - self.ttl_hours * 3600
        stale = [k for k, v in self.items.items() if v.get("created", 0) < cutoff]
        for key in stale:
            self._delete_files(self.items.pop(key))
        if stale:
            self._save()
            log.info("حُذف %d منشور معلّق منتهي الصلاحية", len(stale))
        return len(stale)

    def sweep_orphans(self):
        """
        يحذف ملفات مجلد التنزيلات التي لا يشير إليها أي منشور معلّق — بقايا
        تعطّل أو إيقاف مفاجئ. بدون هذا تمتلئ بطاقة الـ SD بصمت.
        """
        if not os.path.isdir(self.download_dir):
            return 0
        referenced = {
            os.path.abspath(m["path"])
            for item in self.items.values()
            for m in (item.get("media") or [])
            if m.get("path")
        }
        removed = 0
        cutoff = time.time() - 3600          # مهلة أمان: لا نلمس ملفاً قيد التنزيل الآن
        for name in os.listdir(self.download_dir):
            full = os.path.abspath(os.path.join(self.download_dir, name))
            if full in referenced or not os.path.isfile(full):
                continue
            try:
                if os.path.getmtime(full) > cutoff:
                    continue
                os.remove(full)
                removed += 1
            except OSError:
                pass
        if removed:
            log.info("حُذف %d ملف وسائط يتيم", removed)
        return removed
