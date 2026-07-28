import pytest

from twitter import XReader, is_auth_error, is_newer


class FakeTweet:
    def __init__(self, tid, text="نص", reply=False, retweet=False):
        self.id = tid
        self.full_text = text
        self.in_reply_to = tid if reply else None
        self.retweeted_tweet = object() if retweet else None


# --- تمييز أخطاء المصادقة عن أخطاء الشبكة ---
@pytest.mark.parametrize("message", [
    "401 Unauthorized",
    "Your account has been suspended",
    "Account is locked",
    "banned from this endpoint",
    "Could not authenticate you",
    "403 Forbidden",
    "session expired",
])
def test_real_auth_errors_detected(message):
    assert is_auth_error(Exception(message))


@pytest.mark.parametrize("message", [
    "bandwidth limit exceeded",       # كان يطابق "ban" ويحرق حساباً سليماً
    "banner image failed to load",
    "Connection aborted, timeout",
    "Rate limit exceeded (429)",
    "Temporary failure in name resolution",
])
def test_transient_errors_are_not_auth_errors(message):
    assert not is_auth_error(Exception(message))


def test_auth_error_detected_by_exception_class():
    class Unauthorized(Exception):
        pass

    assert is_auth_error(Unauthorized("مشكلة"))


# --- مقارنة معرّفات التغريدات ---
def test_is_newer_numeric():
    assert is_newer(200, 100)
    assert not is_newer(100, 200)
    assert not is_newer(100, 100)


def test_is_newer_when_no_last_id():
    assert is_newer(100, None)
    assert is_newer(100, "")


def test_is_newer_handles_non_numeric_ids():
    assert is_newer("abc", "xyz")
    assert not is_newer("abc", "abc")


# --- اختيار التغريدات الجديدة ---
def test_deleted_reference_tweet_does_not_resend_everything():
    """
    البق الأصلي: المقارنة بالتساوي + break. لو حُذفت التغريدة رقم 105 لم يتحقق
    الشرط أبداً فتُعاد كل التغريدات. المقارنة الرقمية تحلّها.
    """
    tweets = [FakeTweet(i) for i in (110, 109, 108, 107, 106)]
    account = {"last_id": "105"}          # 105 لم تعد موجودة في القائمة
    fresh = XReader.select_new(tweets, account, limit=100)
    assert [t.id for t in fresh] == [106, 107, 108, 109, 110]

    account = {"last_id": "108"}
    fresh = XReader.select_new(tweets, account, limit=100)
    assert [t.id for t in fresh] == [109, 110]


def test_results_are_oldest_first():
    tweets = [FakeTweet(i) for i in (5, 4, 3)]
    fresh = XReader.select_new(tweets, {"last_id": "2"}, limit=100)
    assert [t.id for t in fresh] == [3, 4, 5]


def test_per_cycle_limit_takes_oldest_first():
    """الحد يمنع الإغراق؛ الباقي يصل في الدورة التالية لأننا نبدأ بالأقدم."""
    tweets = [FakeTweet(i) for i in range(20, 0, -1)]
    fresh = XReader.select_new(tweets, {"last_id": "0"}, limit=5)
    assert [t.id for t in fresh] == [1, 2, 3, 4, 5]


def test_replies_and_retweets_skipped():
    tweets = [
        FakeTweet(10),
        FakeTweet(11, reply=True),
        FakeTweet(12, retweet=True),
        FakeTweet(13, text="@someone رد"),
    ]
    fresh = XReader.select_new(tweets, {"last_id": "9"}, skip_replies=True, limit=100)
    assert [t.id for t in fresh] == [10]


def test_replies_included_when_enabled():
    tweets = [FakeTweet(10), FakeTweet(11, reply=True)]
    fresh = XReader.select_new(tweets, {"last_id": "9"}, skip_replies=False, limit=100)
    assert [t.id for t in fresh] == [10, 11]


# --- استخراج الوسائط ---
class FakePhoto:
    type = "photo"

    def __init__(self, url):
        self.media_url = url


class FakeStream:
    def __init__(self, url, bitrate):
        self.url = url
        self.bitrate = bitrate


class FakeVideo:
    type = "video"

    def __init__(self, streams):
        self.streams = streams


def test_extract_all_photos_not_just_first():
    tweet = FakeTweet(1)
    tweet.media = [FakePhoto("a.jpg"), FakePhoto("b.jpg"), FakePhoto("c.jpg")]
    assert XReader.extract_media_urls(tweet) == [
        ("a.jpg", "photo"), ("b.jpg", "photo"), ("c.jpg", "photo"),
    ]


def test_extract_video_picks_highest_bitrate():
    tweet = FakeTweet(1)
    tweet.media = [FakeVideo([FakeStream("low.mp4", 100), FakeStream("hi.mp4", 900)])]
    assert XReader.extract_media_urls(tweet) == [("hi.mp4", "video")]


def test_extract_media_handles_missing_media():
    assert XReader.extract_media_urls(FakeTweet(1)) == []
