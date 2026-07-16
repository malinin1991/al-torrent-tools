from datetime import datetime
from typing import Any


def as_utc_iso(value: Any) -> str:
    """Naive datetime из БД трактуем как UTC → ISO-8601 с суффиксом Z для браузера."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        if text.endswith("Z") or "+" in text[10:] or text.endswith("z"):
            return text
        # "2026-07-16 10:43:10.689657" или iso без TZ
        return text.replace(" ", "T") + ("Z" if "Z" not in text.upper() else "")
    if isinstance(value, datetime):
        # Храним naive UTC
        return value.isoformat(sep="T") + ("Z" if value.tzinfo is None else "")
    return str(value)
