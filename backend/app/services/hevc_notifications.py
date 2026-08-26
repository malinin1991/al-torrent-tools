"""Адресные уведомления HEVC-бота о новых переходах AVC в overdue."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import PipelineEvent, TelegramOutbox
from app.services.hevc_bot import (
    TELEGRAM_TEXT_LIMIT,
    format_release_detail,
    query_release_detail,
    telegram_text_length,
)
from app.services.telegram_access import HEVC_BOT_KEY, list_approved_group_ids
from app.services.telegram_notify import OUTBOX_PENDING
from app.utils.datetime_fmt import utcnow


def overdue_transition_dedupe_key(
    *,
    release_id: int,
    info_hash: str,
    prev_event_id: int,
    chat_id: int,
) -> str:
    """Устойчивый ключ входа в overdue: один переход, даже при параллельных event.id."""
    return (
        f"hevc_overdue:{int(release_id)}:{info_hash.strip().lower()}"
        f":after:{int(prev_event_id)}:chat:{int(chat_id)}"
    )


def enqueue_overdue_event_notifications(
    db: Session,
    event: PipelineEvent,
    *,
    commit: bool = True,
) -> int:
    """По одной записи на переход overdue+approved chat; повторный sync безопасен."""
    if event.event_type != "hevc_status" or event.to_status != "overdue":
        return 0
    if (event.from_status or "") == "overdue":
        return 0
    details = event.details_json if isinstance(event.details_json, dict) else {}
    release_id = details.get("release_id")
    info_hash = details.get("info_hash")
    if not isinstance(release_id, int):
        return 0
    if not isinstance(info_hash, str) or not info_hash.strip():
        return 0
    prev_event_id = details.get("prev_hevc_status_event_id")
    if not isinstance(prev_event_id, int):
        prev_event_id = 0
    row = query_release_detail(db, release_id)
    if row is None:
        return 0
    heading = "🔔 <b>Новая просрочка HEVC</b>\n\n"
    text = heading + format_release_detail(
        row,
        max_len=TELEGRAM_TEXT_LIMIT - telegram_text_length(heading),
    )
    queued = 0
    for chat_id in list_approved_group_ids(db, bot_key=HEVC_BOT_KEY):
        dedupe_key = overdue_transition_dedupe_key(
            release_id=release_id,
            info_hash=info_hash,
            prev_event_id=prev_event_id,
            chat_id=chat_id,
        )
        exists = db.scalar(
            select(TelegramOutbox.id)
            .where(
                TelegramOutbox.bot_key == HEVC_BOT_KEY,
                TelegramOutbox.dedupe_key == dedupe_key,
            )
            .limit(1)
        )
        if exists is not None:
            continue
        try:
            with db.begin_nested():
                db.add(
                    TelegramOutbox(
                        pipeline_id=event.pipeline_id,
                        bot_key=HEVC_BOT_KEY,
                        dedupe_key=dedupe_key,
                        chat_id=str(chat_id),
                        payload_json={
                            "text": text[:4096],
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True,
                            "kind": "hevc_overdue",
                            "release_id": release_id,
                            "event_id": event.id,
                            "info_hash": info_hash.strip().lower(),
                            "reply_markup": {
                                "inline_keyboard": [
                                    [
                                        {
                                            "text": "Детали",
                                            "callback_data": f"status:{release_id}",
                                        }
                                    ]
                                ]
                            },
                        },
                        status=OUTBOX_PENDING,
                        attempts=0,
                        created_at=utcnow(),
                    )
                )
                db.flush()
        except IntegrityError:
            continue
        queued += 1
    if queued:
        if commit:
            db.commit()
        else:
            db.flush()
    return queued
