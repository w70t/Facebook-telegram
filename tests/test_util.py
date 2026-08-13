import pytest

from util import (
    CAPTION_LIMIT,
    TEXT_LIMIT,
    clean_source_text,
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


@pytest.mark.parametrize(
    "link",
    [
        "https://t.me/MillionStore_1",
        "HTTP://TELEGRAM.ME/+Invite-Code",
        "www.t.me/c/123456/789?single#post",
        "t.me/channel_name/42",
    ],
)
def test_clean_source_text_removes_real_public_telegram_links(link):
    assert clean_source_text(f"قبل {link} بعد") == "قبل بعد"


@pytest.mark.parametrize(
    "text",
    [
        "راجع https://not.t.me/channel الآن",
        "راجع https://t.me.evil.example/channel الآن",
        "راجع https://evil.example/path/t.me/channel الآن",
        "راجع https://example.com/?next=https://t.me/channel الآن",
        "راجع https://example.com/?next=https%3A%2F%2Ft.me%2Fchannel الآن",
        "راسل name@t.me/channel الآن",
        "هذه الكلمةt.me/channel ليست رابطاً",
    ],
)
def test_clean_source_text_does_not_touch_lookalike_hosts(text):
    assert clean_source_text(text) == text


def test_clean_source_text_removes_trailing_slash_and_keeps_surrounding_text():
    assert clean_source_text("ابدأ https://t.me/channel/ ثم أكمل") == "ابدأ ثم أكمل"


def test_clean_source_text_preserves_punctuation_without_leaving_a_gap():
    assert clean_source_text("خبر https://t.me/channel، مهم") == "خبر، مهم"


def test_clean_source_text_literal_phrases_are_case_insensitive_and_overlap_once():
    assert clean_source_text(
        "خبر Million Store Store مفيد",
        ["million store", "Store"],
    ) == "خبر مفيد"


def test_clean_source_text_does_not_create_second_order_phrase_match():
    # حذف X يصنع النص "ab"، لكن "ab" لم يكن متصلاً في النص الأصلي.
    assert clean_source_text("aXb", ["X", "ab"]) == "ab"


def test_clean_source_text_merges_overlapping_hidden_spans():
    assert clean_source_text("abcdef", hidden_link_spans=[(1, 4), (2, 5)]) == "af"


def test_clean_source_text_is_idempotent_and_preserves_unmatched_rtl_unicode():
    original = "🟢 خبرٌ عربي — مهم\n\nتابع t.me/example"
    once = clean_source_text(original, ["غير موجودة"])
    assert clean_source_text(once, ["غير موجودة"]) == once
    assert once == "🟢 خبرٌ عربي — مهم\n\nتابع"
