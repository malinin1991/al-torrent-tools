"""Пропуск неизменённых релизов: markers API + fingerprint торрентов."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from app.utils.datetime_fmt import utcnow
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import ReleaseCheckpoint


@dataclass(frozen=True, slots=True)
class ReleaseRef:
    release_id: int
    alias: str | None = None
    updated_at: str | None = None
    fresh_at: str | None = None


def normalize_api_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def extract_release_markers(payload: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    return normalize_api_datetime(payload.get("updated_at")), normalize_api_datetime(payload.get("fresh_at"))


def torrents_fingerprint(torrents: list[dict[str, Any]]) -> str:
    """Стабильный отпечаток списка торрентов релиза (id/hash/updated_at)."""
    parts: list[str] = []
    for item in torrents:
        torrent_id = item.get("id") if item.get("id") is not None else item.get("torrent_id")
        raw_hash = item.get("hash") or item.get("info_hash") or ""
        updated = normalize_api_datetime(item.get("updated_at")) or ""
        parts.append(f"{torrent_id}:{str(raw_hash).strip().lower()}:{updated}")
    return "|".join(sorted(parts))


def should_skip_unchanged(
    db: Session,
    release_id: int,
    *,
    updated_at: str | None,
    fresh_at: str | None,
) -> bool:
    """True если markers совпадают и релиз уже успешно обработан с торрентами.

    Пустой torrents_fingerprint не пропускаем: иначе релиз без торрентов
    (или после частичного сбоя) навсегда скрывает появление новых серий
    без смены updated_at/fresh_at.
    """
    if not updated_at and not fresh_at:
        return False
    row = db.get(ReleaseCheckpoint, release_id)
    if row is None:
        return False
    if not (row.torrents_fingerprint or "").strip():
        return False
    return (row.api_updated_at or "") == (updated_at or "") and (row.api_fresh_at or "") == (fresh_at or "")


def should_skip_by_torrents_fingerprint(
    db: Session,
    release_id: int,
    fingerprint: str,
) -> bool:
    if not fingerprint:
        return False
    row = db.get(ReleaseCheckpoint, release_id)
    if row is None:
        return False
    return (row.torrents_fingerprint or "") == fingerprint


def invalidate_release_checkpoint(db: Session, release_id: int) -> None:
    """Сбросить fingerprint, чтобы early-skip не блокировал ретрай после сбоя."""
    row = db.get(ReleaseCheckpoint, release_id)
    if row is None:
        return
    row.torrents_fingerprint = ""
    db.commit()


def mark_release_processed(
    db: Session,
    release_id: int,
    *,
    updated_at: str | None = None,
    fresh_at: str | None = None,
    torrents_fingerprint_value: str | None = None,
) -> None:
    row = db.get(ReleaseCheckpoint, release_id)
    now = utcnow()
    if row is None:
        db.add(
            ReleaseCheckpoint(
                release_id=release_id,
                api_updated_at=updated_at or "",
                api_fresh_at=fresh_at or "",
                torrents_fingerprint=torrents_fingerprint_value or "",
                processed_at=now,
            )
        )
    else:
        if updated_at is not None:
            row.api_updated_at = updated_at
        if fresh_at is not None:
            row.api_fresh_at = fresh_at
        if torrents_fingerprint_value is not None:
            row.torrents_fingerprint = torrents_fingerprint_value
        row.processed_at = now
    db.commit()
