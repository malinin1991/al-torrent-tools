"""Исходящая очередь Telegram: drain pending → Bot API."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import TelegramOutbox
from app.services.telegram_notify import (
    OUTBOX_PENDING,
    build_telegram_api_url,
    get_telegram_bot_token,
    mark_outbox_attempt_failed,
    mark_outbox_sent,
    resolve_telegram_bot_api_base,
)

logger = logging.getLogger(__name__)


def fetch_pending_outbox(db: Session, *, limit: int = 20) -> list[TelegramOutbox]:
    return list(
        db.scalars(
            select(TelegramOutbox)
            .where(TelegramOutbox.status == OUTBOX_PENDING)
            .order_by(TelegramOutbox.id.asc())
            .limit(limit)
        ).all()
    )


async def send_outbox_message(
    *,
    token: str,
    base_url: str,
    chat_id: str,
    payload: dict[str, Any],
) -> None:
    url = build_telegram_api_url(base_url, token, "sendMessage")
    body = {
        "chat_id": chat_id,
        "text": payload.get("text") or "",
        "parse_mode": payload.get("parse_mode") or "MarkdownV2",
        "disable_web_page_preview": bool(payload.get("disable_web_page_preview", True)),
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(url, json=body)
    data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if response.status_code != 200 or not (isinstance(data, dict) and data.get("ok")):
        description = data.get("description") if isinstance(data, dict) else None
        raise RuntimeError(description or f"HTTP {response.status_code}")


async def drain_outbox(db: Session, *, limit: int = 20) -> dict[str, int]:
    """Отправляет pending-сообщения. При ошибке оставляет pending и увеличивает attempts."""
    token = get_telegram_bot_token(db)
    if not token:
        return {"sent": 0, "failed": 0, "skipped": 0}

    base_url = resolve_telegram_bot_api_base(db)
    pending = fetch_pending_outbox(db, limit=limit)
    sent = 0
    failed = 0
    for item in pending:
        payload = item.payload_json if isinstance(item.payload_json, dict) else {}
        try:
            await send_outbox_message(
                token=token,
                base_url=base_url,
                chat_id=item.chat_id,
                payload=payload,
            )
            mark_outbox_sent(db, item)
            sent += 1
        except Exception as exc:
            logger.warning("Outbox %s: ошибка отправки: %s", item.id, exc)
            mark_outbox_attempt_failed(db, item, str(exc))
            failed += 1
    return {"sent": sent, "failed": failed, "skipped": 0 if pending else 1}
