"""دوال مساعدة خالصة — بلا اعتماد على telethon، ليسهل اختبارها."""
import re

# حدود تلغرام: نص الرسالة 4096 حرفاً، لكن **الكابشن** المرافق للوسائط 1024 فقط.
# استخدام 4096 للاثنين كان يجعل أي منشور طويل مع صورة يفشل ويُحذف بصمت.
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024

# روابط Telegram العامة التي يضعها بعض أصحاب القنوات داخل نص المنشور. نطلب
# حداً واضحاً قبل المضيف حتى لا نمس رابطاً مثل evil.example/path/t.me/name.
_TELEGRAM_LINK_RE = re.compile(
    r"(?<![\w@./?=&%\-])(?:"
    r"(?:https?://)?(?:www\.)?"
    r"(?:t\.me|telegram\.me|telegram\.dog)/"
    r"(?:[A-Za-z0-9_+\-]{1,128})(?:/[A-Za-z0-9_+\-]{1,128})*/?"
    r"(?:\?[A-Za-z0-9_=&%+.,:\-]*)?(?:#[A-Za-z0-9_\-]*)?"
    r"|tg://(?:resolve|join|privatepost)\?[A-Za-z0-9_=&%+.,:\-]+"
    r")",
    re.IGNORECASE,
)


def _merge_spans(spans, text_length):
    """ينظف ويدمج مواضع الحذف قبل لمس النص الأصلي."""
    valid = []
    for span in spans:
        try:
            start, end = span
        except (TypeError, ValueError):
            continue
        if (
            isinstance(start, int) and isinstance(end, int)
            and 0 <= start < end <= text_length
        ):
            valid.append((start, end))
    merged = []
    for start, end in sorted(valid):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _expand_deletion_whitespace(text, spans):
    """يزيل فقط الفراغ الذي خلّفه الحذف، ويحفظ تنسيق بقية المنشور."""
    expanded = []
    for start, end in spans:
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        if line_end < 0:
            line_end = len(text)
        # لو كان الرابط/العبارة هو المحتوى الوحيد في السطر، احذف السطر نفسه.
        if not text[line_start:start].strip() and not text[end:line_end].strip():
            start = line_start
            end = line_end + (line_end < len(text))
        else:
            before = start > 0 and text[start - 1] in " \t\f\v"
            after = end < len(text) and text[end] in " \t\f\v"
            if before and after:
                while end < len(text) and text[end] in " \t\f\v":
                    end += 1
            elif before and end < len(text) and text[end] in ",.;!?،؛؟":
                while start > line_start and text[start - 1] in " \t\f\v":
                    start -= 1
            elif start == line_start:
                while end < len(text) and text[end] in " \t\f\v":
                    end += 1
            elif end == line_end:
                while start > line_start and text[start - 1] in " \t\f\v":
                    start -= 1
        expanded.append((start, end))
    return _merge_spans(expanded, len(text))


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


def clean_source_text(text, phrases=(), hidden_link_spans=()):
    """يحذف روابط Telegram والعبارات الحرفية دون إعادة تنسيق بقية النص."""
    original = text or ""
    spans = list(hidden_link_spans or ())
    spans.extend(match.span() for match in _TELEGRAM_LINK_RE.finditer(original))
    # نجمع التطابقات على النص الأصلي نفسه؛ بذلك لا يصنع حذف العبارة الأولى
    # تطابقاً جديداً للثانية لم يكن موجوداً في المنشور الأصلي.
    for phrase in phrases:
        if not isinstance(phrase, str) or not phrase:
            continue
        spans.extend(
            match.span()
            for match in re.finditer(re.escape(phrase), original, re.IGNORECASE)
        )
    merged = _merge_spans(spans, len(original))
    merged = _expand_deletion_whitespace(original, merged)
    cleaned = original
    for start, end in reversed(merged):
        cleaned = cleaned[:start] + cleaned[end:]
    return cleaned.strip(" \t\f\v\r\n")


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
