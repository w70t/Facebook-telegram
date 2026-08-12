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
import stat
import time

from jsonio import atomic_write_json, read_json_resilient

log = logging.getLogger("tg2fb.store")

PLAYABLE = ("photo", "video")
MAX_ITEMS = 500          # سقف يمنع قناة مُغرِقة من ملء القرص
_MANAGED_PREFIXES = ("tg_", "x_")
_PROTECTED_PUBLISH_STATES = frozenset(("publishing", "published"))


class PendingStore:
    def __init__(self, path, download_dir, ttl_hours=48, max_items=MAX_ITEMS):
        self.path = path
        self.download_dir = download_dir
        self.ttl_hours = ttl_hours
        self.max_items = max_items
        self.items = {}
        self._load()

    def _load(self):
        data, status = read_json_resilient(self.path)
        if status == "corrupt":
            log.error("ملف المنشورات المعلّقة تالف — سأبدأ بقائمة فارغة.")
        if isinstance(data, dict):
            self.items = {k: v for k, v in data.items() if isinstance(v, dict)}
        log.info("منشورات معلّقة محمّلة: %d", len(self.items))

    def _commit(self, items):
        """يثبّت الحالة على القرص أولاً، ثم يبدّل نسخة الذاكرة."""
        atomic_write_json(self.path, items, mode=0o600)
        self.items = items

    @staticmethod
    def _created_at(item):
        """قيمة قابلة للفرز حتى إن كان سجل محلي قديم/تالفاً جزئياً."""
        try:
            return float(item.get("created", 0))
        except (AttributeError, TypeError, ValueError):
            return 0.0

    @staticmethod
    def _has_publish_guard(item):
        return item.get("publish_state") in _PROTECTED_PUBLISH_STATES

    @staticmethod
    def _media_records(item):
        """يعزل سجلات media القديمة/التالفة بدلاً من إسقاط دورة التنظيف."""
        media = item.get("media") if isinstance(item, dict) else None
        return media if isinstance(media, (list, tuple)) else ()

    # --- حدود الملفات التي يملكها التطبيق ---
    @staticmethod
    def _canonical(path):
        return os.path.normcase(os.path.realpath(os.path.abspath(path)))

    def _managed_download_root(self):
        """
        لا نسمح بالحذف إلا من مجلد فرعي صريح بجوار pending.json.

        سياسة السماح الإيجابية هذه تعمل على Linux وWindows معاً، بعكس قائمة حظر
        مثل /tmp التي تتحول إلى C:\\tmp على Windows. كما ترفض symlink يخرج من
        مجلد الحالة.
        """
        try:
            state_root = self._canonical(
                os.path.dirname(os.path.abspath(self.path)) or "."
            )
            download_root = self._canonical(self.download_dir)
        except (OSError, TypeError, ValueError):
            return None
        # لا تجعل ملف الحالة في جذر قرص تصريحاً ضمنياً بكنس أي مجلد على القرص.
        if os.path.dirname(state_root) == state_root:
            return None
        if download_root == state_root:
            return None
        try:
            if os.path.commonpath([state_root, download_root]) != state_root:
                return None
        except ValueError:  # أقراص مختلفة على Windows مثلاً
            return None
        return download_root

    def _managed_file(self, path, require_file=False):
        """يعيد مسار ملف يملكه التطبيق، أو None إن كان خارج حدوده."""
        if not path:
            return None
        download_root = self._managed_download_root()
        if not download_root:
            return None

        try:
            raw = os.path.abspath(path)
            resolved = os.path.realpath(raw)
            candidate = os.path.normcase(resolved)
        except (OSError, TypeError, ValueError):
            return None
        if not os.path.basename(resolved).startswith(_MANAGED_PREFIXES):
            return None
        # direct children فقط؛ realpath هنا يمنع الخروج عبر parent symlink أيضاً.
        if os.path.dirname(candidate) != download_root:
            return None
        if require_file:
            try:
                mode = os.stat(raw, follow_symlinks=False).st_mode
            except (OSError, NotImplementedError, TypeError, ValueError):
                return None
            if not stat.S_ISREG(mode):
                return None
        # استخدم normcase للمقارنة فقط. إعادة مسار lower-case للحذف قد تشير إلى
        # ملف مختلف على مجلد Windows حساس لحالة الأحرف.
        return resolved

    # --- عمليات ---
    def add(self, text, media=None, origin=None):
        """media: [{"path": str, "type": "photo"|"video"|"document"}]"""
        item_id = secrets.token_urlsafe(8)
        while item_id in self.items:                # احتياط نظري
            item_id = secrets.token_urlsafe(8)
        items = dict(self.items)
        items[item_id] = {
            "text": text or "",
            "media": list(media or []),
            "origin": origin or "",
            "created": time.time(),
            "review": None,                         # {"chat": .., "msg": ..}
        }
        evicted = []
        overflow = len(items) - self.max_items
        if overflow > 0:
            # لا نضحّي أبداً بالعنصر الجاري إضافته أو بحارس منع إعادة نشر
            # عنصر وصل/قد يكون وصل إلى Facebook. إذا امتلأت القائمة بالحراس،
            # فرفض العنصر الجديد أوضح وأأمن من فقد حالة دائمة بصمت.
            candidates = [
                key for key, item in self.items.items()
                if item.get("review") is not None
                if not self._has_publish_guard(item)
            ]
            if len(candidates) < overflow:
                raise OSError(
                    "سقف المنشورات المعلّقة ممتلئ بعناصر محمية أو لم تُرسل بعد؛ "
                    "تعذّرت إضافة منشور جديد بأمان"
                )
            oldest = sorted(candidates, key=lambda k: self._created_at(items[k]))
            for key in oldest[:overflow]:
                evicted.append(items.pop(key))
        self._commit(items)
        for item in evicted:
            self._delete_files(item)
        if evicted:
            log.warning(
                "تجاوز عدد المنشورات المعلّقة %d — حُذف %d من الأقدم",
                self.max_items, len(evicted),
            )
        return item_id

    def get(self, item_id):
        return self.items.get(item_id)

    def update(self, item_id, **fields):
        item = self.items.get(item_id)
        if not item:
            return None
        updated = dict(item)
        updated.update(fields)
        items = dict(self.items)
        items[item_id] = updated
        self._commit(items)
        return updated

    def remove(self, item_id):
        item = self.items.get(item_id)
        if item:
            items = dict(self.items)
            items.pop(item_id)
            self._commit(items)
            self._delete_files(item)
        return item

    def media_paths(self, item_id, kinds=PLAYABLE):
        item = self.items.get(item_id) or {}
        paths = []
        for media in self._media_records(item):
            if not isinstance(media, dict):
                continue
            if media.get("type") not in kinds:
                continue
            path = self._managed_file(media.get("path"), require_file=True)
            if path:
                paths.append(path)
        return paths

    # --- تنظيف دوري ---
    def _referenced_files(self):
        """مسارات الوسائط التي ما زالت مملوكة لأي عنصر باقٍ في المخزن."""
        return {
            safe
            for remaining in self.items.values()
            for media in self._media_records(remaining)
            if isinstance(media, dict)
            if (safe := self._managed_file(media.get("path")))
            if (safe := self._canonical(safe))
        }

    def _delete_files(self, item):
        # تُستدعى هذه الدالة بعد تثبيت إزالة العنصر من self.items؛ لذلك تمثل
        # هذه المجموعة كل المراجع الباقية فعلاً. قد يشترك منشوران في ملف واحد.
        referenced = self._referenced_files()
        for media in self._media_records(item):
            if not isinstance(media, dict):
                continue
            path = self._managed_file(media.get("path"), require_file=True)
            if not path or self._canonical(path) in referenced:
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
        stale = [
            key for key, item in self.items.items()
            # review=None هو outbox حُفظ قبل أن يتمكّن Telegram من إنشاء رسالة
            # المراجعة؛ حذفه بالـ TTL يحوّل عطلاً مؤقتاً إلى فقد دائم.
            if item.get("review") is not None
            # publishing/published حارس دائم ضد نشر Facebook المكرر بعد crash.
            if not self._has_publish_guard(item)
            if self._created_at(item) < cutoff
        ]
        items = dict(self.items)
        removed = []
        for key in stale:
            removed.append(items.pop(key))
        if stale:
            self._commit(items)
            for item in removed:
                self._delete_files(item)
            log.info("حُذف %d منشور معلّق منتهي الصلاحية", len(stale))
        return len(stale)

    def sweep_orphans(self):
        """
        يحذف ملفات مجلد التنزيلات التي لا يشير إليها أي منشور معلّق — بقايا
        تعطّل أو إيقاف مفاجئ. بدون هذا تمتلئ بطاقة الـ SD بصمت.
        """
        download_root = self._managed_download_root()
        if not download_root or not os.path.isdir(download_root):
            log.error("رُفض تنظيف مجلد غير مُدار: %s", self.download_dir)
            return 0
        referenced = self._referenced_files()
        removed = 0
        cutoff = time.time() - 3600          # مهلة أمان: لا نلمس ملفاً قيد التنزيل الآن
        with os.scandir(download_root) as entries:
            for entry in entries:
                if not entry.name.startswith(_MANAGED_PREFIXES):
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                full = self._managed_file(entry.path, require_file=True)
                if not full or self._canonical(full) in referenced:
                    continue
                try:
                    if entry.stat(follow_symlinks=False).st_mtime > cutoff:
                        continue
                    os.remove(full)
                    removed += 1
                except OSError:
                    pass
        if removed:
            log.info("حُذف %d ملف وسائط يتيم", removed)
        return removed
