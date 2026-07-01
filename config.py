"""تحميل الإعدادات من ملف .env"""
import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"المتغير {name} غير موجود في ملف .env")
    return value


def _coerce_chat(value: str):
    """يحوّل المعرّف الرقمي إلى int ويترك @username كما هو."""
    value = value.strip()
    if value.lstrip("-").isdigit():
        return int(value)
    return value


API_ID = int(_require("API_ID"))
API_HASH = _require("API_HASH")

BOT_TOKEN = _require("BOT_TOKEN")

# اختياري: يمكن ترك القائمة فارغة وإضافة القنوات من داخل البوت بـ /addsource
SOURCE_CHANNELS = [
    _coerce_chat(c)
    for c in os.environ.get("SOURCE_CHANNELS", "").split(",")
    if c.strip() and not c.strip().startswith("<")
]

REVIEW_CHAT_ID = int(_require("REVIEW_CHAT_ID"))

ADMIN_IDS = {int(x) for x in _require("ADMIN_IDS").split(",") if x.strip()}

FB_PAGE_ID = _require("FB_PAGE_ID")
FB_PAGE_TOKEN = _require("FB_PAGE_TOKEN")

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "downloads")
