"""Фильтры/форматтеры логов: скрываем секреты (токен Telegram и т.п.)."""

from __future__ import annotations

import logging
import re

# Типичный BotFather-токен: 123456:AAH... (в URL .../botTOKEN/... и в тексте ошибок).
_BOT_TOKEN_RE = re.compile(r"\b(\d{6,}:[A-Za-z0-9_-]{20,})\b")
# URL вида https://api.telegram.org/bot<token>/method
_BOT_URL_TOKEN_RE = re.compile(r"(/(?:bot))(\d{6,}:[A-Za-z0-9_-]{20,})(/?)", re.IGNORECASE)


def redact_secrets(text: str) -> str:
    if not text:
        return text
    text = _BOT_URL_TOKEN_RE.sub(r"\1***\3", text)
    text = _BOT_TOKEN_RE.sub("***", text)
    return text


class RedactSecretsFilter(logging.Filter):
    """Редактирует msg/args до форматирования."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: redact_secrets(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact_secrets(arg) if isinstance(arg, str) else arg for arg in record.args
                )
        return True


class RedactingFormatter(logging.Formatter):
    """Правка всего formatted-вывода, включая traceback."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


def setup_redacted_logging(
    *,
    level: int = logging.INFO,
    fmt: str = "%(asctime)s %(levelname)s [%(name)s] %(message)s",
) -> None:
    """Ставит redacting formatter на root и приглушает болтливые логгеры с URL."""
    root = logging.getLogger()
    root.setLevel(level)

    formatter = RedactingFormatter(fmt)
    secret_filter = RedactSecretsFilter()

    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        handler.addFilter(secret_filter)
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            handler.setFormatter(formatter)
            handler.addFilter(secret_filter)

    # httpx логирует полный URL (с токеном) на INFO — оставляем WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
