"""دوال مساعدة خالصة — بلا اعتماد على telethon، ليسهل اختبارها."""
import re

# حدود تلغرام: نص الرسالة 4096 حرفاً، لكن **الكابشن** المرافق للوسائط 1024 فقط.
# استخدام 4096 للاثنين كان يجعل أي منشور طويل مع صورة يفشل ويُحذف بصمت.
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024


def truncate(text, limit):
    """يقصّ النص إلى limit حرفاً شاملاً علامة القصّ."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


def preview(text, limit):
    """معاينة المنشور داخل رسالة المراجعة (النص الأصلي يبقى كاملاً للنشر)."""
    text = (text or "").strip()
    if not text:
        return "(بدون نص)"
    return truncate(text, limit)


def review_body(text, header, limit):
    """
    يبني نص رسالة المراجعة مضموناً ألا يتجاوز الحد.
    الترويسة ثابتة، فنحسب ما تبقّى للنص بدل افتراض مساحة كافية.
    """
    room = limit - len(header) - 2          # سطران فاصلان
    if room < 40:                           # ترويسة طويلة بشكل غير متوقع
        return truncate(header, limit)
    return f"{header}\n\n{preview(text, room)}"


def normalize_phone(raw, default_cc=None):
    """
    يحوّل رقم الهاتف إلى صيغة دولية.
    يُرجع None لو تعذّر تحديد رمز الدولة.
    """
    raw = (raw or "").strip().replace(" ", "").replace("-", "")
    if not raw:
        return None
    if raw.startswith("+"):
        digits = re.sub(r"\D", "", raw)
        return "+" + digits if digits else None
    if raw.startswith("00"):
        digits = re.sub(r"\D", "", raw[2:])
        return "+" + digits if digits else None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    cc = re.sub(r"\D", "", str(default_cc or ""))
    if cc:
        return "+" + cc + digits.lstrip("0")
    return None                              # يحتاج رمز الدولة


def human_size(num_bytes):
    step = 1024.0
    value = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < step:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} TB"


def media_summary(media):
    """وصف مختصر للوسائط المرفقة يظهر في رسالة المراجعة."""
    kinds = [m.get("type") for m in media or []]
    photos = kinds.count("photo")
    videos = kinds.count("video")
    docs = kinds.count("document")
    parts = []
    if photos:
        parts.append(f"{photos} صورة")
    if videos:
        parts.append(f"{videos} فيديو")
    if docs:
        parts.append(f"{docs} ملف")
    return " + ".join(parts)
