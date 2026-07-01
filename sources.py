"""تخزين قائمة القنوات المصدر في ملف JSON لتبقى بعد إعادة التشغيل."""
import json
import os

SOURCES_FILE = os.environ.get("SOURCES_FILE", "sources.json")


def load() -> list[dict]:
    if not os.path.exists(SOURCES_FILE):
        return []
    try:
        with open(SOURCES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save(entries: list[dict]) -> None:
    with open(SOURCES_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
