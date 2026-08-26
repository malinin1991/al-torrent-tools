"""Исходящая очередь Telegram: drain pending → Bot API."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import TelegramOutbox
from app.services.telegram_access import HEVC_BOT_KEY
from app.services.telegram_notify import (
    OUTBOX_PENDING,
    build_telegram_api_url,
    is_terminal_user_dm_error,
    mark_outbox_attempt_failed,
    mark_outbox_cancelled,
    mark_outbox_failed,
    mark_outbox_sent,
    normalize_telegram_bot_api_base,
)
from app.services.runtime_settings import resolve_telegram_bot_settings

logger = logging.getLogger(__name__)


def fetch_pending_outbox(
    db: Session,
    *,
    bot_key: str,
    limit: int = 20,
) -> list[TelegramOutbox]:
    return list(
        db.scalars(
            select(TelegramOutbox)
            .where(
                TelegramOutbox.status == OUTBOX_PENDING,
                TelegramOutbox.bot_key == bot_key,
            )
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
    body: dict[str, Any] = {
        "chat_id": chat_id,
        "text": payload.get("text") or "",
        "disable_web_page_preview": bool(payload.get("disable_web_page_preview", True)),
    }
    parse_mode = payload.get("parse_mode")
    if isinstance(parse_mode, str) and parse_mode.strip():
        body["parse_mode"] = parse_mode.strip()
    reply_markup = payload.get("reply_markup")
    if isinstance(reply_markup, dict):
        body["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(url, json=body)
    data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if response.status_code != 200 or not (isinstance(data, dict) and data.get("ok")):
        description = data.get("description") if isinstance(data, dict) else None
        raise RuntimeError(description or f"HTTP {response.status_code}")


async def drain_outbox(
    db: Session,
    *,
    bot_key: str = "primary",
    limit: int = 20,
) -> dict[str, int]:
    """Отправляет pending-сообщения. При ошибке оставляет pending и увеличивает attempts."""
    config = resolve_telegram_bot_settings(db, bot_key)
    token = config.token
    if not token:
        return {"sent": 0, "failed": 0, "skipped": 0}

    base_url = normalize_telegram_bot_api_base(config.api_base_url)
    pending = fetch_pending_outbox(db, bot_key=config.bot_key, limit=limit)
    sent = 0
    failed = 0
    skipped = 0
    for item in pending:
        payload = item.payload_json if isinstance(item.payload_json, dict) else {}
        if config.bot_key == HEVC_BOT_KEY:
            from app.services.telegram_access import hevc_recipient_is_approved

            if not hevc_recipient_is_approved(db, item.chat_id):
                mark_outbox_cancelled(db, item, "acl_rejected")
                skipped += 1
                continue
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
            error = str(exc)
            if config.bot_key == HEVC_BOT_KEY and is_terminal_user_dm_error(
                error, item.chat_id
            ):
                logger.warning("Outbox %s: постоянная ошибка ЛС: %s", item.id, exc)
                mark_outbox_failed(db, item, error)
            else:
                logger.warning("Outbox %s: ошибка отправки: %s", item.id, exc)
                mark_outbox_attempt_failed(db, item, error)
            failed += 1
    return {"sent": sent, "failed": failed, "skipped": skipped if pending else 1}
