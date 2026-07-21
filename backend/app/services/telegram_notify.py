"""Отслеживание релизов и очередь Telegram-уведомлений."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import TelegramOutbox, TorrentPipeline, TrackedRelease
from app.services.runtime_settings import get_setting_value

TG_STATUS_SKIPPED = "skipped"
TG_STATUS_PENDING = "pending"
TG_STATUS_QUEUED = "queued"
TG_STATUS_SENT = "sent"

OUTBOX_PENDING = "pending"
OUTBOX_SENT = "sent"
OUTBOX_FAILED = "failed"

SOURCE_BOT = "bot"
SOURCE_UI = "ui"

DEFAULT_BOT_API_BASE = "https://api.telegram.org"


def escape_markdown_v2(text: str) -> str:
    escape_chars = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{ch}" if ch in escape_chars else ch for ch in text)


def resolve_telegram_bot_api_base(db: Session | None) -> str:
    raw = get_setting_value(db, "telegram_bot_api_base_url", "").strip()
    if not raw:
        return DEFAULT_BOT_API_BASE
    return raw.rstrip("/")


def is_telegram_enabled(db: Session | None) -> bool:
    value = get_setting_value(db, "telegram_enabled", "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def get_telegram_chat_id(db: Session | None) -> str:
    return get_setting_value(db, "telegram_chat_id", "").strip()


def get_telegram_bot_token(db: Session | None) -> str:
    return get_setting_value(db, "telegram_bot_token", "").strip()


def is_release_tracked(db: Session, release_id: int) -> bool:
    row = db.get(TrackedRelease, release_id)
    return row is not None and bool(row.enabled)


def list_enabled_tracked_releases(db: Session) -> list[TrackedRelease]:
    return list(
        db.scalars(
            select(TrackedRelease)
            .where(TrackedRelease.enabled.is_(True))
            .order_by(TrackedRelease.title.asc(), TrackedRelease.release_id.asc())
        ).all()
    )


def upsert_tracked_release(
    db: Session,
    *,
    release_id: int,
    release_alias: str,
    title: str = "",
    source: str = SOURCE_UI,
    enabled: bool = True,
) -> TrackedRelease:
    alias = (release_alias or "").strip().strip("/")
    title_value = (title or "").strip() or alias or str(release_id)
    source_value = source if source in {SOURCE_BOT, SOURCE_UI} else SOURCE_UI
    row = db.get(TrackedRelease, release_id)
    if row is None:
        row = TrackedRelease(
            release_id=release_id,
            release_alias=alias,
            title=title_value,
            enabled=enabled,
            source=source_value,
            created_at=datetime.utcnow(),
        )
        db.add(row)
    else:
        row.release_alias = alias or row.release_alias
        if title_value:
            row.title = title_value
        row.enabled = enabled
        # bot имеет приоритет бейджа; ui не затирает bot
        if source_value == SOURCE_BOT or row.source != SOURCE_BOT:
            row.source = source_value
    db.commit()
    db.refresh(row)
    return row


def set_tracked_enabled(db: Session, release_id: int, enabled: bool, *, source: str = SOURCE_UI) -> TrackedRelease | None:
    row = db.get(TrackedRelease, release_id)
    if row is None:
        return None
    row.enabled = enabled
    if source == SOURCE_BOT or row.source != SOURCE_BOT:
        row.source = source if enabled else row.source
    db.commit()
    db.refresh(row)
    return row


def disable_tracked_by_alias(db: Session, alias: str) -> TrackedRelease | None:
    cleaned = (alias or "").strip().strip("/").lower()
    if not cleaned:
        return None
    row = db.scalar(
        select(TrackedRelease).where(TrackedRelease.release_alias.ilike(cleaned)).limit(1)
    )
    if row is None:
        return None
    row.enabled = False
    db.commit()
    db.refresh(row)
    return row


def build_torrent_notification_text(
    *,
    title: str,
    alias: str,
    torrents: list[dict[str, Any]],
) -> str:
    """MarkdownV2-шаблон обновления торрентов (из Anilibria Tracker Bot)."""
    message = (
        f"🔔 **Обновление для [{escape_markdown_v2(title)}]"
        f"(https://anilibria\\.top/anime/releases/release/{escape_markdown_v2(alias)})**\n\n"
    )
    for torrent in torrents:
        label = str(torrent.get("label") or torrent.get("type") or "торрент")
        codec_obj = torrent.get("codec")
        if isinstance(codec_obj, dict):
            codec = str(codec_obj.get("description") or "N/A")
        else:
            codec = str(codec_obj or "N/A")
        description = str(torrent.get("description") or "серии не указаны")
        message += (
            f"▫️ *{escape_markdown_v2(label)}*\n"
            f"    Серии: `{escape_markdown_v2(description)}`\n"
            f"    Кодек: `{escape_markdown_v2(codec)}`\n\n"
        )
    return message


def enqueue_pipeline_telegram_notification(
    db: Session,
    pipeline: TorrentPipeline,
    *,
    release_payload: dict[str, Any] | None = None,
    torrent_payload: dict[str, Any] | None = None,
) -> TorrentPipeline:
    """Если релиз отслеживается и TG включён — пишем в outbox; иначе tg_status=skipped."""
    if not is_release_tracked(db, pipeline.release_id):
        pipeline.tg_status = TG_STATUS_SKIPPED
        db.commit()
        db.refresh(pipeline)
        return pipeline

    if not is_telegram_enabled(db):
        pipeline.tg_status = TG_STATUS_SKIPPED
        db.commit()
        db.refresh(pipeline)
        return pipeline

    chat_id = get_telegram_chat_id(db)
    if not chat_id:
        pipeline.tg_status = TG_STATUS_SKIPPED
        db.commit()
        db.refresh(pipeline)
        return pipeline

    tracked = db.get(TrackedRelease, pipeline.release_id)
    alias = ""
    title = ""
    if tracked is not None:
        alias = tracked.release_alias or ""
        title = tracked.title or alias
    if release_payload:
        raw_alias = release_payload.get("alias")
        if isinstance(raw_alias, str) and raw_alias.strip():
            alias = raw_alias.strip()
        name = release_payload.get("name")
        if isinstance(name, dict):
            main = name.get("main")
            if isinstance(main, str) and main.strip():
                title = main.strip()
        elif isinstance(name, str) and name.strip():
            title = name.strip()
    if not title:
        title = alias or str(pipeline.release_id)
    if not alias:
        alias = str(pipeline.release_id)

    torrents = [torrent_payload] if isinstance(torrent_payload, dict) else []
    text = build_torrent_notification_text(title=title, alias=alias, torrents=torrents)
    payload = {
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
        "text": text,
        "release_id": pipeline.release_id,
        "torrent_id": pipeline.torrent_id,
        "title": title,
        "alias": alias,
    }
    db.add(
        TelegramOutbox(
            pipeline_id=pipeline.id,
            chat_id=chat_id,
            payload_json=payload,
            status=OUTBOX_PENDING,
            attempts=0,
            created_at=datetime.utcnow(),
        )
    )
    pipeline.tg_status = TG_STATUS_QUEUED
    db.commit()
    db.refresh(pipeline)
    return pipeline


def mark_outbox_sent(db: Session, outbox: TelegramOutbox) -> None:
    outbox.status = OUTBOX_SENT
    outbox.sent_at = datetime.utcnow()
    outbox.last_error = None
    if outbox.pipeline_id is not None:
        pipeline = db.get(TorrentPipeline, outbox.pipeline_id)
        if pipeline is not None:
            pipeline.tg_status = TG_STATUS_SENT
    db.commit()


def mark_outbox_attempt_failed(db: Session, outbox: TelegramOutbox, error: str) -> None:
    outbox.attempts = int(outbox.attempts or 0) + 1
    outbox.last_error = error[:2000]
    outbox.status = OUTBOX_PENDING
    if outbox.pipeline_id is not None:
        pipeline = db.get(TorrentPipeline, outbox.pipeline_id)
        if pipeline is not None and pipeline.tg_status != TG_STATUS_SENT:
            pipeline.tg_status = TG_STATUS_PENDING
    db.commit()


async def test_telegram_get_me(
    *,
    token: str,
    base_url: str = DEFAULT_BOT_API_BASE,
) -> dict[str, Any]:
    """Проверка токена через Bot API getMe."""
    cleaned_token = (token or "").strip()
    if not cleaned_token:
        raise ValueError("Не задан токен Telegram-бота")
    api_base = (base_url or DEFAULT_BOT_API_BASE).rstrip("/")
    url = urljoin(f"{api_base}/", f"bot{cleaned_token}/getMe")
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url)
    data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if response.status_code != 200 or not data.get("ok"):
        description = data.get("description") if isinstance(data, dict) else None
        raise RuntimeError(description or f"HTTP {response.status_code}")
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError("Некорректный ответ getMe")
    return result
