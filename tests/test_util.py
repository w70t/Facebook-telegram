from util import (
    CAPTION_LIMIT,
    TEXT_LIMIT,
    media_summary,
    normalize_phone,
    preview,
    review_body,
    truncate,
)

HEADER = "📥 منشور جديد للمراجعة:"


def test_truncate_never_exceeds_limit():
    assert len(truncate("ا" * 5000, 100)) == 100
    assert truncate("قصير", 100) == "قصير"


def test_preview_of_empty_text():
    assert preview("", 100) == "(بدون نص)"
    assert preview("   ", 100) == "(بدون نص)"


def test_review_body_fits_caption_limit():
    """الحالة التي كانت تُفقد المنشور: نص طويل + صورة = كابشن > 1024."""
    body = review_body("ن" * 4000, HEADER, CAPTION_LIMIT)
    assert len(body) <= CAPTION_LIMIT


def test_review_body_fits_text_limit():
    body = review_body("ن" * 9000, HEADER, TEXT_LIMIT)
    assert len(body) <= TEXT_LIMIT


def test_review_body_keeps_header_and_marks_truncation():
    body = review_body("ن" * 4000, HEADER, CAPTION_LIMIT)
    assert body.startswith(HEADER)
    assert body.endswith("…")


def test_review_body_short_text_untouched():
    assert review_body("مرحبا", HEADER, CAPTION_LIMIT) == f"{HEADER}\n\nمرحبا"


def test_review_body_survives_oversized_header():
    body = review_body("نص", "ه" * 2000, CAPTION_LIMIT)
    assert len(body) <= CAPTION_LIMIT


def test_normalize_phone_variants():
    assert normalize_phone("+966512345678") == "+966512345678"
    assert normalize_phone("+966 51 234 5678") == "+966512345678"
    assert normalize_phone("00966512345678") == "+966512345678"
    assert normalize_phone("0512345678", "966") == "+966512345678"
    assert normalize_phone("512345678", "966") == "+966512345678"
    assert normalize_phone("0512345678", "+966") == "+966512345678"


def test_normalize_phone_needs_country_code():
    assert normalize_phone("0512345678") is None
    assert normalize_phone("") is None
    assert normalize_phone("abc") is None


def test_media_summary():
    assert media_summary([]) == ""
    assert media_summary([{"type": "photo"}, {"type": "photo"}]) == "2 صورة"
    assert media_summary(
        [{"type": "photo"}, {"type": "video"}, {"type": "document"}]
    ) == "1 صورة + 1 فيديو + 1 ملف"
