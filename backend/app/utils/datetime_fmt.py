from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    """Naive UTC now — замена deprecated datetime.utcnow() без смены семантики.

    В БД храним naive UTC, поэтому возвращаем datetime без tzinfo.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
