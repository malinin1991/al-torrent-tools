"""Группировка архива торрентов по релизам для UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    DiskFileHash,
    FileChangeEvent,
    Job,
    TorrentArchive,
    TorrentFile,
    TorrentPipeline,
    TrackedRelease,
)
from app.services.file_tracker import KIND_ORPHAN, KIND_REMOVED, file_status_for_ui
from app.services.job_runner import STATUS_PENDING, STATUS_RUNNING
from app.services.torrent_files_meta import path_exists_including_incomplete
from app.services.torrent_qb_meta import (
    build_release_torrents_url,
    genres_from_quality_json,
    resolve_anilibria_site_url,
)

# События новее этого окна влияют на бейдж в UI.
_EVENT_WINDOW = timedelta(days=30)
_REMOVED_KINDS = frozenset({KIND_REMOVED, KIND_ORPHAN})


@dataclass
class ReleaseFileRow:
    relative_path: str
    size: int
    selected: bool
    full_path: str | None
    status: str  # new|changed|removed|checking|ok
    in_torrent: bool = True


@dataclass
class _TorrentEvents:
    """latest kind по пути + кандидаты «удалён» (нет в торренте, есть на диске)."""

    latest_by_path: dict[str, str] = field(default_factory=dict)
    removed_candidates: list[tuple[str, str | None]] = field(default_factory=list)


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
    api_present: bool = True
    files: list[ReleaseFileRow] = field(default_factory=list)


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
    archived_torrents: list[ReleaseTorrentRow] = field(default_factory=list)
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
    tracked_only: bool = False,
    page: int = 1,
    per_page: int = 30,
) -> dict[str, Any]:
    """Релизы из архива, сортировка по последнему обновлению любого торрента."""
    page = max(1, page)
    per_page = max(1, min(per_page, 100))
    search_text = (search or "").strip()
    only_tracked = bool(tracked_only)

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
    if only_tracked:
        tracked_ids = select(TrackedRelease.release_id).where(TrackedRelease.enabled.is_(True))
        stats_query = stats_query.where(TorrentArchive.release_id.in_(tracked_ids))

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
            "tracked_only": only_tracked,
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
    files_by_hash = _files_by_hash(db, [a.info_hash for a in archives])
    events_by_torrent = _recent_events_by_torrent(db, release_ids)
    all_full_paths = [
        f.full_path
        for files in files_by_hash.values()
        for f in files
        if f.full_path
    ]
    for events in events_by_torrent.values():
        for _rel, full in events.removed_candidates:
            if full:
                all_full_paths.append(full)
    hashes_by_path = _disk_hashes_by_path(db, all_full_paths)
    active_hash_jobs = (
        _info_hashes_with_active_hash_job(db, list(files_by_hash.keys()))
        if files_by_hash
        else set()
    )
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
        active: list[ReleaseTorrentRow] = []
        archived: list[ReleaseTorrentRow] = []
        for item in items:
            status, error = pipeline_by_hash.get(item.info_hash.lower(), (None, None))
            info_hash_key = item.info_hash.lower()
            torrent_events = events_by_torrent.get(item.torrent_id) or _TorrentEvents()
            file_rows = _build_file_rows(
                files_by_hash.get(info_hash_key, []),
                torrent_events.latest_by_path,
                hashes_by_path,
                hash_job_active=info_hash_key in active_hash_jobs,
                removed_candidates=torrent_events.removed_candidates,
            )
            row = ReleaseTorrentRow(
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
                api_present=bool(getattr(item, "api_present", True)),
                files=file_rows,
            )
            if row.api_present:
                active.append(row)
            else:
                archived.append(row)
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
                torrents=active,
                archived_torrents=archived,
                tracked=bool(tracked_row and tracked_row.enabled),
                track_source=tracked_row.source if tracked_row else None,
            )
        )

    return {
        "groups": groups,
        "search": search_text,
        "tracked_only": only_tracked,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


def split_active_archived(
    torrents: list[ReleaseTorrentRow],
) -> tuple[list[ReleaseTorrentRow], list[ReleaseTorrentRow]]:
    """Хелпер для тестов: разделение по api_present."""
    active = [t for t in torrents if t.api_present]
    archived = [t for t in torrents if not t.api_present]
    return active, archived


def _build_file_rows(
    files: list[TorrentFile],
    events_by_path: dict[str, str],
    hashes_by_path: dict[str, DiskFileHash] | None = None,
    *,
    hash_job_active: bool = False,
    removed_candidates: list[tuple[str, str | None]] | None = None,
) -> list[ReleaseFileRow]:
    hash_map = hashes_by_path or {}
    rows: list[ReleaseFileRow] = []
    seen_keys: set[str] = set()
    for item in files:
        latest_kind = events_by_path.get(item.relative_path)
        disk_hash = hash_map.get(item.full_path) if item.full_path else None
        rows.append(
            ReleaseFileRow(
                relative_path=item.relative_path,
                size=int(item.size or 0),
                selected=bool(item.selected),
                full_path=item.full_path,
                in_torrent=True,
                status=file_status_for_ui(
                    relative_path=item.relative_path,
                    full_path=item.full_path,
                    latest_kind=latest_kind,
                    disk_hash=disk_hash,
                    hash_job_active=hash_job_active,
                    in_torrent=True,
                ),
            )
        )
        seen_keys.add(item.relative_path)
        if item.full_path:
            seen_keys.add(item.full_path)

    for display_path, full_path in removed_candidates or []:
        key = display_path or full_path or ""
        if not key or key in seen_keys:
            continue
        if full_path and full_path in seen_keys:
            continue
        if not full_path or not path_exists_including_incomplete(Path(full_path)):
            continue
        disk_hash = hash_map.get(full_path)
        rows.append(
            ReleaseFileRow(
                relative_path=display_path or Path(full_path).name,
                size=0,
                selected=False,
                full_path=full_path,
                in_torrent=False,
                status=file_status_for_ui(
                    relative_path=display_path or Path(full_path).name,
                    full_path=full_path,
                    latest_kind=KIND_REMOVED,
                    disk_hash=disk_hash,
                    hash_job_active=False,
                    in_torrent=False,
                ),
            )
        )
        seen_keys.add(key)
        seen_keys.add(full_path)
    return rows


def _disk_hashes_by_path(db: Session, paths: list[str]) -> dict[str, DiskFileHash]:
    normalized = sorted({(p or "").strip() for p in paths if p})
    if not normalized:
        return {}
    rows = list(
        db.scalars(select(DiskFileHash).where(DiskFileHash.full_path.in_(normalized))).all()
    )
    return {row.full_path: row for row in rows}


def _files_by_hash(db: Session, hashes: list[str]) -> dict[str, list[TorrentFile]]:
    normalized = sorted({(h or "").strip().lower() for h in hashes if h})
    if not normalized:
        return {}
    rows = list(
        db.scalars(
            select(TorrentFile)
            .where(TorrentFile.info_hash.in_(normalized))
            .order_by(TorrentFile.file_index.asc(), TorrentFile.id.asc())
        ).all()
    )
    result: dict[str, list[TorrentFile]] = {}
    for row in rows:
        result.setdefault(row.info_hash.lower(), []).append(row)
    return result


def _recent_events_by_torrent(
    db: Session,
    release_ids: list[int],
) -> dict[int, _TorrentEvents]:
    """torrent_id → latest kind по пути + кандидаты removed/orphan для UI."""
    if not release_ids:
        return {}
    since = datetime.utcnow() - _EVENT_WINDOW
    rows = db.scalars(
        select(FileChangeEvent)
        .where(
            FileChangeEvent.release_id.in_(release_ids),
            FileChangeEvent.created_at >= since,
        )
        .order_by(FileChangeEvent.id.desc())
    ).all()
    result: dict[int, _TorrentEvents] = {}
    removed_seen: dict[int, set[str]] = {}
    for row in rows:
        if row.torrent_id is None:
            continue
        bucket = result.setdefault(row.torrent_id, _TorrentEvents())
        path_key = row.relative_path or row.full_path or ""
        if path_key and path_key not in bucket.latest_by_path:
            bucket.latest_by_path[path_key] = row.kind
        if row.kind not in _REMOVED_KINDS:
            continue
        # id DESC: если уже видели более новое non-removed событие — путь снова в торренте.
        if path_key and bucket.latest_by_path.get(path_key) not in _REMOVED_KINDS:
            continue
        display = row.relative_path or row.full_path or ""
        if not display:
            continue
        seen = removed_seen.setdefault(row.torrent_id, set())
        dedupe_key = row.full_path or display
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        bucket.removed_candidates.append((display, row.full_path))
    return result


def _info_hashes_with_active_hash_job(db: Session, info_hashes: list[str]) -> set[str]:
    """info_hash с pending/running job hash_torrent (для бейджа «проверка»)."""
    wanted = {(h or "").strip().lower() for h in info_hashes if h}
    if not wanted:
        return set()
    rows = db.scalars(
        select(Job).where(
            Job.type == "hash_torrent",
            Job.status.in_((STATUS_PENDING, STATUS_RUNNING)),
        )
    ).all()
    active: set[str] = set()
    for job in rows:
        params = job.params_json or {}
        key = str(params.get("info_hash") or "").strip().lower()
        if key and key in wanted:
            active.add(key)
    return active


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
