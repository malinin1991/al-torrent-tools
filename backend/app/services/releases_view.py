"""Группировка архива торрентов по релизам для UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import TorrentArchive, TorrentPipeline, TrackedRelease
from app.services.torrent_qb_meta import (
    build_release_torrents_url,
    genres_from_quality_json,
    resolve_anilibria_site_url,
)


@dataclass
class ReleaseTorrentRow:
    archive_id: int
    torrent_id: int
    info_hash: str
    torrent_type: str | None
    torrent_description: str | None
    file_size: int | None
    file_size_label: str
    created_at: datetime | None
    pipeline_status: str | None
    pipeline_error: str | None


@dataclass
class ReleaseGroup:
    release_id: int
    release_alias: str | None
    anime_name: str | None
    category: str | None
    last_updated: datetime | None
    torrent_count: int
    release_url: str | None
    genres: list[str]
    torrents: list[ReleaseTorrentRow]
    tracked: bool = False
    track_source: str | None = None


def format_bytes(size: int | None) -> str:
    if size is None or size < 0:
        return "-"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def list_release_groups(
    db: Session,
    *,
    search: str | None = None,
    page: int = 1,
    per_page: int = 30,
) -> dict[str, Any]:
    """Релизы из архива, сортировка по последнему обновлению любого торрента."""
    page = max(1, page)
    per_page = max(1, min(per_page, 100))
    search_text = (search or "").strip()

    stats_query = (
        select(
            TorrentArchive.release_id,
            func.max(TorrentArchive.created_at).label("last_updated"),
            func.count(TorrentArchive.id).label("torrent_count"),
        )
        .group_by(TorrentArchive.release_id)
    )
    if search_text:
        matching_ids = (
            select(TorrentArchive.release_id)
            .where(
                or_(
                    TorrentArchive.anime_name.ilike(f"%{search_text}%"),
                    TorrentArchive.release_alias.ilike(f"%{search_text}%"),
                )
            )
            .distinct()
        )
        stats_query = stats_query.where(TorrentArchive.release_id.in_(matching_ids))

    total = db.scalar(select(func.count()).select_from(stats_query.subquery())) or 0
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)

    page_rows = db.execute(
        stats_query.order_by(func.max(TorrentArchive.created_at).desc().nullslast())
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).all()

    release_ids = [int(row.release_id) for row in page_rows]
    if not release_ids:
        return {
            "groups": [],
            "search": search_text,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        }

    archives = list(
        db.scalars(
            select(TorrentArchive)
            .where(TorrentArchive.release_id.in_(release_ids))
            .order_by(TorrentArchive.created_at.desc(), TorrentArchive.id.desc())
        ).all()
    )

    pipeline_by_hash = _latest_pipeline_by_hash(db, [a.info_hash for a in archives])
    tracked_by_id = _tracked_by_release_id(db, release_ids)
    site_url = resolve_anilibria_site_url()

    by_release: dict[int, list[TorrentArchive]] = {rid: [] for rid in release_ids}
    for archive in archives:
        by_release.setdefault(archive.release_id, []).append(archive)

    stats_by_id = {int(row.release_id): row for row in page_rows}
    groups: list[ReleaseGroup] = []
    for release_id in release_ids:
        items = by_release.get(release_id) or []
        head = items[0] if items else None
        stats = stats_by_id[release_id]
        genres: list[str] = []
        for item in items:
            genres = genres_from_quality_json(
                item.quality_json if isinstance(item.quality_json, dict) else None
            )
            if genres:
                break
        torrents = []
        for item in items:
            status, error = pipeline_by_hash.get(item.info_hash.lower(), (None, None))
            torrents.append(
                ReleaseTorrentRow(
                    archive_id=item.id,
                    torrent_id=item.torrent_id,
                    info_hash=item.info_hash,
                    torrent_type=item.torrent_type,
                    torrent_description=item.torrent_description,
                    file_size=item.file_size,
                    file_size_label=format_bytes(item.file_size),
                    created_at=item.created_at,
                    pipeline_status=status,
                    pipeline_error=error,
                )
            )
        tracked_row = tracked_by_id.get(release_id)
        groups.append(
            ReleaseGroup(
                release_id=release_id,
                release_alias=head.release_alias if head else None,
                anime_name=head.anime_name if head else None,
                category=head.category if head else None,
                last_updated=stats.last_updated,
                torrent_count=int(stats.torrent_count),
                release_url=build_release_torrents_url(
                    head.release_alias if head else None,
                    site_url=site_url,
                ),
                genres=genres,
                torrents=torrents,
                tracked=bool(tracked_row and tracked_row.enabled),
                track_source=tracked_row.source if tracked_row else None,
            )
        )

    return {
        "groups": groups,
        "search": search_text,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


def _latest_pipeline_by_hash(
    db: Session,
    hashes: list[str],
) -> dict[str, tuple[str | None, str | None]]:
    normalized = sorted({(h or "").strip().lower() for h in hashes if h})
    if not normalized:
        return {}
    rows = db.scalars(
        select(TorrentPipeline)
        .where(TorrentPipeline.info_hash.in_(normalized))
        .order_by(TorrentPipeline.id.desc())
    ).all()
    result: dict[str, tuple[str | None, str | None]] = {}
    for row in rows:
        key = row.info_hash.lower()
        if key not in result:
            result[key] = (row.status, row.error)
    return result


def _tracked_by_release_id(db: Session, release_ids: list[int]) -> dict[int, TrackedRelease]:
    if not release_ids:
        return {}
    rows = db.scalars(select(TrackedRelease).where(TrackedRelease.release_id.in_(release_ids))).all()
    return {row.release_id: row for row in rows}
