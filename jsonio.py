"""
كتابة/قراءة JSON بشكل ذرّي وآمن ضد انقطاع الكهرباء.

الكتابة المباشرة على الملف (open(path, "w")) تترك نافذة زمنية يكون فيها الملف
نصف مكتوب؛ على Raspberry Pi مع بطاقة SD وانقطاع كهرباء متكرر هذا يعني ضياع
كل الإعدادات. هنا نكتب في ملف مؤقت ثم os.replace (ذرّي على نفس القسم).
"""
import json
import logging
import os
import tempfile
import time

log = logging.getLogger("tg2fb.jsonio")

SECRET_MODE = 0o600


def _fsync_dir(directory):
    """يضمن أن إعادة التسمية نفسها وصلت للقرص وليس فقط محتوى الملف."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_json(path, data, mode=SECRET_MODE):
    """يكتب data كـ JSON إلى path ذرّياً. إمّا النسخة القديمة كاملة أو الجديدة كاملة."""
    path = os.path.abspath(path)
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        _fsync_dir(directory)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path):
    """يقرأ JSON. يرمي الاستثناء للمتصل ليقرر (لا ابتلاع صامت)."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_json_resilient(path, backup_suffix=".bak"):
    """
    يقرأ JSON مع استرجاع تلقائي:
    - الملف سليم            -> (data, None)
    - الملف تالف ونسخة .bak سليمة -> (data_from_bak, "recovered")
    - كلاهما تالف/مفقود      -> (None, سبب)

    الملف التالف لا يُحذف أبداً؛ يُنقل إلى path.corrupt-<وقت> للفحص لاحقاً.
    """
    if not os.path.exists(path):
        return None, None

    try:
        return read_json(path), None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        log.error("ملف %s تالف (%s)", path, reason)

    quarantine = f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        os.replace(path, quarantine)
        log.error("نُقل الملف التالف إلى %s", quarantine)
    except OSError:
        quarantine = None

    backup = path + backup_suffix
    if os.path.exists(backup):
        try:
            data = read_json(backup)
            log.warning("تم الاسترجاع من النسخة الاحتياطية %s", backup)
            return data, "recovered"
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            log.error("النسخة الاحتياطية %s تالفة أيضاً", backup)

    return None, "corrupt"
