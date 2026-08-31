"""Группировка архива торрентов по релизам для UI."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from app.utils.datetime_fmt import utcnow
from pathlib import Path
from typing import Any, Literal, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    DiskFileHash,
    FileChangeEvent,
    Job,
    TorrentArchive,
    TorrentFile,
    TorrentPipeline,
    TrackedRelease,
)
from app.services.file_tracker import (
    KIND_ORPHAN,
    KIND_REMOVED,
    UI_STATUS_CHECKING,
    UI_STATUS_REMOVED,
    file_status_for_ui,
    resolve_orphan_scan_root,
)
from app.services.hevc_pairing import (
    HevcFilter,
    classify_archive_codec,
    file_keys_by_archive_id,
    load_file_keys_by_archive_id,
    max_overdue_hours_by_release_id,
    overdue_hours_past_sla,
    unpaired_by_archive_id,
    release_ids_matching_hevc_filter,
)
from app.services.job_runner import STATUS_PENDING, STATUS_RUNNING
from app.services.release_meta import load_release_meta_by_ids
from app.services.runtime_settings import get_setting_value
from app.services.torrent_files_meta import (
    QB_INCOMPLETE_SUFFIX,
    complete_path_for,
    is_junk_file,
    is_under_media_root,
    resolve_media_root,
)
from app.services.torrent_qb_meta import (
    block_flags_from_quality_json,
    build_release_admin_url,
    build_release_torrents_url,
    genres_from_quality_json,
    members_from_quality_json,
    resolve_anilibria_site_url,
)

# События новее этого окна влияют на бейдж в UI.
_EVENT_WINDOW = timedelta(days=30)
_REMOVED_KINDS = frozenset({KIND_REMOVED, KIND_ORPHAN})
_NAT_SPLIT = re.compile(r"(\d+)")


@dataclass
class ReleaseFileRow:
    relative_path: str
    size: int
    selected: bool
    full_path: str | None
    status: str  # new|changed|removed|checking|ok
    in_torrent: bool = True
    file_id: int | None = None
    downloadable: bool = False


_DOWNLOADABLE_STATUSES = frozenset({"ok", "new", "changed"})

# Порядок в сводке: новый → изменён → проверка → старый; удалён — отдельно.
_FILE_SUMMARY_ACTIVE_ORDER: tuple[tuple[str, tuple[str, str, str]], ...] = (
    ("new", ("новый", "новые", "новых")),
    ("changed", ("изменён", "изменённые", "изменённых")),
    ("checking", ("проверка", "проверки", "проверок")),
    ("ok", ("старый", "старые", "старых")),
)


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение для числительных: 1/2–4/5–20/21…"""
    n_abs = abs(n) % 100
    n1 = n_abs % 10
    if 11 <= n_abs <= 19:
        return many
    if n1 == 1:
        return one
    if 2 <= n1 <= 4:
        return few
    return many


def format_torrent_files_summary(files: Sequence[Any] | None) -> str:
    """Сводка без разворота: «4 файла (1 новый, 3 старые), 1 удалён.»"""
    counts = {"new": 0, "changed": 0, "checking": 0, "ok": 0, "removed": 0}
    for item in files or ():
        status = (getattr(item, "status", None) or "ok").strip().lower()
        if status not in counts:
            status = "ok"
        counts[status] += 1

    active = counts["new"] + counts["changed"] + counts["checking"] + counts["ok"]
    removed = counts["removed"]

    if active == 0:
        if removed == 0:
            return "0 файлов."
        return (
            f"{removed} {_ru_plural(removed, 'удалён', 'удалённых', 'удалённых')}."
        )

    head = f"{active} {_ru_plural(active, 'файл', 'файла', 'файлов')}"
    detail_parts = [
        f"{counts[key]} {_ru_plural(counts[key], one, few, many)}"
        for key, (one, few, many) in _FILE_SUMMARY_ACTIVE_ORDER
        if counts[key]
    ]
    # Детализация, если есть отличия от «все старые» или есть удалённые.
    interesting = bool(
        counts["new"] or counts["changed"] or counts["checking"] or removed
    )
    if interesting and detail_parts:
        head += f" ({', '.join(detail_parts)})"
    if removed:
        head += (
            f", {removed} {_ru_plural(removed, 'удалён', 'удалённых', 'удалённых')}"
        )
    return f"{head}."


def _natural_name_key(name: str) -> tuple:
    """Ключ natural sort: ep2 < ep10 (не лексикографически)."""
    parts = _NAT_SPLIT.split(name.casefold())
    key: list[tuple[int, int | str]] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part))
    return tuple(key)


def _sort_file_rows_desc(rows: list[ReleaseFileRow]) -> list[ReleaseFileRow]:
    """Имя файла по убыванию (natural: ep10 выше ep2)."""
    return sorted(
        rows,
        key=lambda r: _natural_name_key(Path(r.relative_path).name),
        reverse=True,
    )


def _existing_resolved_files(
    full_paths: list[str], *, media_root: Path
) -> set[str]:
    """Какие пути — обычные файлы под media root.

    Один scandir на уникальный parent вместо N×Path.is_file() на странице.
    """
    wanted_by_parent: dict[Path, set[str]] = {}
    for raw in full_paths:
        if not raw:
            continue
        path = Path(raw)
        if path.name.endswith(QB_INCOMPLETE_SUFFIX):
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not is_under_media_root(resolved, media_root=media_root):
            continue
        wanted_by_parent.setdefault(resolved.parent, set()).add(str(resolved))

    found: set[str] = set()
    for parent, wanted in wanted_by_parent.items():
        try:
            with os.scandir(parent) as entries:
                for entry in entries:
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        key = str(Path(entry.path).resolve())
                    except OSError:
                        continue
                    if key in wanted:
                        found.add(key)
        except OSError:
            for key in wanted:
                try:
                    if Path(key).is_file():
                        found.add(key)
                except OSError:
                    continue
    return found


def _file_is_downloadable(
    *,
    status: str,
    full_path: str | None,
    media_root: Path | None = None,
    existing_resolved: set[str] | None = None,
) -> bool:
    """Скачивание: финальный статус + complete-файл на диске под media root.

    existing_resolved — заранее посчитанный набор (батч на рендере страницы).
    Без него — одиночный is_file (endpoint скачивания).
    """
    if status not in _DOWNLOADABLE_STATUSES or not full_path:
        return False
    path = Path(full_path)
    # Только сам .!qB запрещён; соседний .!qB при уже complete-файле не блокирует.
    if path.name.endswith(QB_INCOMPLETE_SUFFIX):
        return False
    root = (media_root or resolve_media_root()).resolve()
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if not is_under_media_root(resolved, media_root=root):
        return False
    key = str(resolved)
    if existing_resolved is not None:
        return key in existing_resolved
    try:
        return resolved.is_file()
    except OSError:
        return False


def resolve_media_file_for_download(row: TorrentFile) -> Path | None:
    """Путь к media-файлу для отдачи, либо None если скачивать нельзя."""
    status = (getattr(row, "ui_status", None) or "").strip().lower() or "ok"
    full_path = row.full_path
    if not full_path:
        return None
    if not _file_is_downloadable(status=status, full_path=full_path):
        return None
    try:
        return Path(full_path).resolve()
    except OSError:
        return None


@dataclass
class _TorrentEvents:
    """latest kind по пути + кандидаты «удалён» (нет в торренте, есть на диске)."""

    latest_by_path: dict[str, str] = field(default_factory=dict)
    # (display_path, full_path, kind) — kind: removed|orphan
    removed_candidates: list[tuple[str, str | None, str]] = field(default_factory=list)


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
    pipeline_id: int | None = None
    # Дата загрузки версии на AniLibria (naive UTC = max created_at/updated_at API).
    api_created_at: datetime | None = None
    api_present: bool = True
    hevc_pair_status: Literal["missing", "overdue", "type_mismatch"] | None = None
    # Для бейджа overdue: часы сверх SLA (age − 24), не полный age AVC.
    hevc_pair_age_hours: float | None = None
    # True = age от api_created_at → красный бейдж; False = fallback system created_at → оранжевый.
    hevc_overdue_age_from_api: bool = False
    codec_family: str | None = None
    ignore_hevc: bool = False
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
    admin_url: str | None
    genres: list[str]
    torrents: list[ReleaseTorrentRow]
    archived_torrents: list[ReleaseTorrentRow] = field(default_factory=list)
    tracked: bool = False
    track_source: str | None = None
    members: list[dict[str, str]] = field(default_factory=list)
    is_blocked_by_geo: bool = False
    is_blocked_by_copyrights: bool = False


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


@dataclass
class ArchivePageRow:
    """Строка /archive с составом файлов и sticky-статусами."""

    id: int
    anime_name: str | None
    release_alias: str | None
    category: str | None
    torrent_type: str | None
    torrent_description: str | None
    release_id: int
    torrent_id: int
    info_hash: str
    file_size: int | None
    file_size_label: str
    created_at: datetime | None
    api_present: bool
    superseded: bool
    files: list[ReleaseFileRow] = field(default_factory=list)


def build_archive_page_rows(db: Session, archives: list[TorrentArchive]) -> list[ArchivePageRow]:
    """Обогащает записи архива составом файлов (как на /releases)."""
    if not archives:
        return []
    hashes = [a.info_hash for a in archives]
    release_ids = sorted({int(a.release_id) for a in archives})
    files_by_hash = _files_by_hash(db, hashes)
    events_by_hash = _recent_events_by_info_hash(db, release_ids)
    all_full_paths = [
        f.full_path
        for files in files_by_hash.values()
        for f in files
        if f.full_path
    ]
    for events in events_by_hash.values():
        for _rel, full, _kind in events.removed_candidates:
            if full:
                all_full_paths.append(full)
    hashes_by_path = _disk_hashes_by_path(db, all_full_paths)
    active_hash_jobs = (
        _info_hashes_with_active_hash_job(db, list(files_by_hash.keys())) if files_by_hash else set()
    )
    paths_by_release = _active_file_paths_by_release(archives, files_by_hash)

    result: list[ArchivePageRow] = []
    for item in archives:
        info_hash_key = (item.info_hash or "").strip().lower()
        torrent_events = events_by_hash.get(info_hash_key) or _TorrentEvents()
        torrent_files = files_by_hash.get(info_hash_key, [])
        sib_rels, sib_fulls = _sibling_owned_paths(
            paths_by_release.get(int(item.release_id), {}),
            exclude_hash=info_hash_key,
        )
        file_rows = _build_file_rows(
            torrent_files,
            torrent_events.latest_by_path,
            hashes_by_path,
            hash_job_active=info_hash_key in active_hash_jobs,
            removed_candidates=_filter_removed_candidates(
                torrent_events.removed_candidates,
                torrent_files,
                sibling_rel_paths=sib_rels,
                sibling_full_paths=sib_fulls,
            ),
        )
        result.append(
            ArchivePageRow(
                id=item.id,
                anime_name=item.anime_name,
                release_alias=item.release_alias,
                category=item.category,
                torrent_type=item.torrent_type,
                torrent_description=item.torrent_description,
                release_id=item.release_id,
                torrent_id=item.torrent_id,
                info_hash=item.info_hash,
                file_size=item.file_size,
                file_size_label=format_bytes(item.file_size),
                created_at=item.created_at,
                api_present=bool(getattr(item, "api_present", True)),
                superseded=bool(getattr(item, "superseded", False)),
                files=file_rows,
            )
        )
    return result


def _normalize_hevc_filter(value: str | None) -> HevcFilter:
    text = (value or "").strip().lower()
    if text in ("missing", "overdue", "type_mismatch"):
        return text  # type: ignore[return-value]
    return ""


def _active_archives_for_hevc_pairing(db: Session) -> list[Any]:
    """SELECT архивов для фильтров HEVC (до пагинации).

    Включает superseded/api_absent — find_unpaired_avc использует их только
    как якорь overdue для преемников AVC; бейджи строит по active.
    """
    return list(
        db.execute(
            select(
                TorrentArchive.id,
                TorrentArchive.release_id,
                TorrentArchive.torrent_id,
                TorrentArchive.torrent_type,
                TorrentArchive.torrent_description,
                TorrentArchive.quality_json,
                TorrentArchive.created_at,
                TorrentArchive.api_created_at,
                TorrentArchive.info_hash,
                TorrentArchive.api_present,
                TorrentArchive.superseded,
                TorrentArchive.ignore_hevc,
            )
        ).all()
    )


def list_release_groups(
    db: Session,
    *,
    search: str | None = None,
    tracked_only: bool = False,
    hevc_filter: str | None = None,
    show_hidden: bool = False,
    page: int = 1,
    per_page: int = 30,
) -> dict[str, Any]:
    """Релизы из архива, сортировка по последнему обновлению любого торрента.

    При hevc_filter=overdue — сортировка по длительности просрочки ASC, затем last_updated DESC.
    show_hidden влияет только при активном HEVC-фильтре (include ignore_hevc AVC).
    """
    page = max(1, page)
    per_page = max(1, min(per_page, 100))
    search_text = (search or "").strip()
    only_tracked = bool(tracked_only)
    hevc = _normalize_hevc_filter(hevc_filter)
    # Чекбокс «Отображать скрытое» — только для HEVC-фильтров.
    include_ignored = bool(show_hidden) and bool(hevc)
    empty = {
        "groups": [],
        "search": search_text,
        "tracked_only": only_tracked,
        "hevc_filter": hevc,
        "show_hidden": bool(show_hidden),
        "page": page,
        "per_page": per_page,
        "total": 0,
        "total_pages": 1,
    }

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

    hevc_archives: list[Any] | None = None
    overdue_hours_map: dict[int, float] = {}
    if hevc:
        hevc_archives = _active_archives_for_hevc_pairing(db)
        hevc_file_keys = load_file_keys_by_archive_id(db, hevc_archives)
        hevc_release_ids = release_ids_matching_hevc_filter(
            hevc_archives,
            hevc_filter=hevc,
            include_ignored=include_ignored,
            file_keys_by_archive_id=hevc_file_keys,
        )
        if not hevc_release_ids:
            return empty
        stats_query = stats_query.where(TorrentArchive.release_id.in_(hevc_release_ids))
        if hevc == "overdue":
            overdue_hours_map = max_overdue_hours_by_release_id(
                hevc_archives,
                include_ignored=include_ignored,
                file_keys_by_archive_id=hevc_file_keys,
            )

    if hevc == "overdue":
        # Полный набор stats → сортировка по overdue hours ASC → пагинация.
        all_stats = db.execute(stats_query).all()
        total = len(all_stats)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)

        def _overdue_sort_key(row: Any) -> tuple:
            rid = int(row.release_id)
            hours = overdue_hours_map.get(rid)
            # Нет часов — в конец; иначе ASC по длительности просрочки.
            hours_key = float("inf") if hours is None else float(hours)
            updated = row.last_updated
            if updated is None:
                updated_ts = float("-inf")
            else:
                # tie-break last_updated DESC → отрицательный timestamp.
                try:
                    updated_ts = -float(updated.timestamp())
                except (OSError, OverflowError, ValueError):
                    updated_ts = float("-inf")
            return (hours_key, updated_ts)

        sorted_stats = sorted(all_stats, key=_overdue_sort_key)
        offset = (page - 1) * per_page
        page_rows = sorted_stats[offset : offset + per_page]
    else:
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
            "hevc_filter": hevc,
            "show_hidden": bool(show_hidden),
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
    # Сразу после архивов — стабильный слот для моков в тестах.
    release_meta = load_release_meta_by_ids(db, release_ids)

    pipeline_by_hash = _latest_pipeline_by_hash(db, [a.info_hash for a in archives])
    tracked_by_id = _tracked_by_release_id(db, release_ids)
    files_by_hash = _files_by_hash(db, [a.info_hash for a in archives])
    events_by_hash = _recent_events_by_info_hash(db, release_ids)
    all_full_paths = [
        f.full_path
        for files in files_by_hash.values()
        for f in files
        if f.full_path
    ]
    for events in events_by_hash.values():
        for _rel, full, _kind in events.removed_candidates:
            if full:
                all_full_paths.append(full)
    hashes_by_path = _disk_hashes_by_path(db, all_full_paths)
    active_hash_jobs = (
        _info_hashes_with_active_hash_job(db, list(files_by_hash.keys()))
        if files_by_hash
        else set()
    )
    paths_by_release = _active_file_paths_by_release(archives, files_by_hash)
    page_file_keys = file_keys_by_archive_id(
        archives,
        [row for files in files_by_hash.values() for row in files],
    )
    site_url = resolve_anilibria_site_url()
    admin_url_template = get_setting_value(
        db,
        "anilibria_admin_url_template",
        settings.anilibria_admin_url_template,
        allow_empty=True,
    )

    by_release: dict[int, list[TorrentArchive]] = {rid: [] for rid in release_ids}
    for archive in archives:
        by_release.setdefault(archive.release_id, []).append(archive)

    stats_by_id = {int(row.release_id): row for row in page_rows}
    groups: list[ReleaseGroup] = []
    for release_id in release_ids:
        items = by_release.get(release_id) or []
        head = items[0] if items else None
        stats = stats_by_id[release_id]
        meta = release_meta.get(release_id)
        genres: list[str] = list(meta.genres) if meta and meta.genres else []
        members: list[dict[str, str]] = list(meta.members) if meta and meta.members else []
        blocked_geo = bool(meta.is_blocked_by_geo) if meta and meta.is_blocked_by_geo is not None else False
        blocked_copy = (
            bool(meta.is_blocked_by_copyrights)
            if meta and meta.is_blocked_by_copyrights is not None
            else False
        )
        need_block_fallback = meta is None or meta.is_blocked_by_geo is None or (
            meta.is_blocked_by_copyrights is None
        )
        # Fallback: старые строки / sparse upsert ещё только в quality_json.
        if not genres or not members or need_block_fallback:
            for item in items:
                qj = item.quality_json if isinstance(item.quality_json, dict) else None
                if not genres:
                    genres = genres_from_quality_json(qj)
                if not members:
                    members = members_from_quality_json(qj)
                if need_block_fallback:
                    geo, copy = block_flags_from_quality_json(qj)
                    if meta is None or meta.is_blocked_by_geo is None:
                        blocked_geo = blocked_geo or geo
                    if meta is None or meta.is_blocked_by_copyrights is None:
                        blocked_copy = blocked_copy or copy
        # Бейджи HEVC в UI: ignore_hevc по-прежнему скрывает (без include_ignored).
        hevc_unpaired = unpaired_by_archive_id(
            items, file_keys_by_archive_id=page_file_keys
        )
        release_paths = paths_by_release.get(release_id, {})
        active: list[ReleaseTorrentRow] = []
        archived: list[ReleaseTorrentRow] = []
        for item in items:
            status, error, pipeline_id = pipeline_by_hash.get(
                item.info_hash.lower(), (None, None, None)
            )
            info_hash_key = item.info_hash.lower()
            torrent_events = events_by_hash.get(info_hash_key) or _TorrentEvents()
            torrent_files = files_by_hash.get(info_hash_key, [])
            sib_rels, sib_fulls = _sibling_owned_paths(
                release_paths, exclude_hash=info_hash_key
            )
            file_rows = _build_file_rows(
                torrent_files,
                torrent_events.latest_by_path,
                hashes_by_path,
                hash_job_active=info_hash_key in active_hash_jobs,
                removed_candidates=_filter_removed_candidates(
                    torrent_events.removed_candidates,
                    torrent_files,
                    sibling_rel_paths=sib_rels,
                    sibling_full_paths=sib_fulls,
                ),
            )
            unpaired = hevc_unpaired.get(item.id)
            qj = item.quality_json if isinstance(item.quality_json, dict) else None
            codec = classify_archive_codec(quality_json=qj, torrent_type=item.torrent_type)
            row = ReleaseTorrentRow(
                archive_id=item.id,
                torrent_id=item.torrent_id,
                info_hash=item.info_hash,
                torrent_type=item.torrent_type,
                torrent_description=item.torrent_description,
                file_size=item.file_size,
                file_size_label=format_bytes(item.file_size),
                created_at=item.created_at,
                api_created_at=getattr(item, "api_created_at", None),
                pipeline_status=status,
                pipeline_error=error,
                pipeline_id=pipeline_id,
                api_present=bool(getattr(item, "api_present", True)),
                hevc_pair_status=unpaired.status if unpaired else None,
                hevc_pair_age_hours=(
                    overdue_hours_past_sla(unpaired.age_hours) if unpaired else None
                ),
                hevc_overdue_age_from_api=bool(unpaired.age_from_api) if unpaired else False,
                codec_family=codec,
                ignore_hevc=bool(getattr(item, "ignore_hevc", False)),
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
                admin_url=build_release_admin_url(release_id, admin_url_template),
                genres=genres,
                members=members,
                is_blocked_by_geo=blocked_geo,
                is_blocked_by_copyrights=blocked_copy,
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
        "hevc_filter": hevc,
        "show_hidden": bool(show_hidden),
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


def _filter_removed_candidates(
    candidates: list[tuple[str, str | None, str]] | list[tuple[str, str | None]],
    files: list[TorrentFile],
    *,
    sibling_rel_paths: set[str] | None = None,
    sibling_full_paths: set[str] | None = None,
) -> list[tuple[str, str | None, str]]:
    """Отбрасывает «удалён/orphan» вне корня этого торрента и служебный мусор.

    Orphan-пути, принадлежащие другим активным торрентам того же release_id
    (siblings), не показываем как «удалён». Sticky ``removed`` (история vs prior
    ЭТОГО торрента) оставляем.
    """
    if not candidates:
        return []
    sib_rels = sibling_rel_paths or set()
    sib_fulls = sibling_full_paths or set()
    known = {f.full_path for f in files if f.full_path}
    root = resolve_orphan_scan_root(
        save_path=None,
        content_path=None,
        known_full_paths=known,
        media_root=resolve_media_root(),
    )

    normalized: list[tuple[str, str | None, str]] = []
    for item in candidates:
        if len(item) == 3:
            display, full, kind = item  # type: ignore[misc]
        else:
            display, full = item  # type: ignore[misc]
            kind = KIND_REMOVED
        normalized.append((display or "", full, kind or KIND_REMOVED))

    def _is_sibling_owned(display: str, full: str | None) -> bool:
        if display and (display in sib_rels or display in sib_fulls):
            return True
        if full and (full in sib_fulls or full in sib_rels):
            return True
        return False

    if root is None:
        # Нет якоря по файлам торрента — показываем только removed с relative_path
        # (состав .torrent), без абсолютных orphan-путей чужих тайтлов.
        filtered: list[tuple[str, str | None, str]] = []
        for display, full, kind in normalized:
            if not display or display.startswith("/") or is_junk_file(display):
                continue
            if kind == KIND_ORPHAN and _is_sibling_owned(display, full):
                continue
            filtered.append((display, full, kind))
        return filtered

    filtered = []
    for display, full, kind in normalized:
        path_raw = full or display
        if not path_raw:
            continue
        if is_junk_file(path_raw):
            continue
        try:
            Path(path_raw).resolve().relative_to(root)
        except (ValueError, OSError):
            continue
        # Sibling composition: только orphan-оверлей, sticky removed — история.
        if kind == KIND_ORPHAN and _is_sibling_owned(display, full):
            continue
        filtered.append((display, full, kind))
    return filtered


def _active_file_paths_by_release(
    archives: Sequence[Any] | list[TorrentArchive],
    files_by_hash: dict[str, list[TorrentFile]],
) -> dict[int, dict[str, tuple[set[str], set[str]]]]:
    """release_id → info_hash → (relative_paths, full_paths) для api_present не-superseded."""
    result: dict[int, dict[str, tuple[set[str], set[str]]]] = {}
    for archive in archives:
        if not bool(getattr(archive, "api_present", True)):
            continue
        if bool(getattr(archive, "superseded", False)):
            continue
        key = (getattr(archive, "info_hash", None) or "").strip().lower()
        if not key:
            continue
        release_id = int(getattr(archive, "release_id"))
        rels: set[str] = set()
        fulls: set[str] = set()
        for f in files_by_hash.get(key, []):
            if f.relative_path:
                rels.add(f.relative_path)
            if f.full_path:
                fulls.add(f.full_path)
        result.setdefault(release_id, {})[key] = (rels, fulls)
    return result


def _sibling_owned_paths(
    by_hash: dict[str, tuple[set[str], set[str]]],
    *,
    exclude_hash: str,
) -> tuple[set[str], set[str]]:
    """Объединение путей всех активных торрентов релиза, кроме текущего."""
    current = (exclude_hash or "").strip().lower()
    rels: set[str] = set()
    fulls: set[str] = set()
    for key, (r, f) in by_hash.items():
        if key == current:
            continue
        rels |= r
        fulls |= f
    return rels, fulls


def _build_file_rows(
    files: list[TorrentFile],
    events_by_path: dict[str, str],
    hashes_by_path: dict[str, DiskFileHash] | None = None,
    *,
    hash_job_active: bool = False,
    removed_candidates: list[tuple[str, str | None, str]] | list[tuple[str, str | None]] | None = None,
) -> list[ReleaseFileRow]:
    hash_map = hashes_by_path or {}
    rows: list[ReleaseFileRow] = []
    seen_keys: set[str] = set()
    for item in files:
        latest_kind = events_by_path.get(item.relative_path)
        disk_hash = hash_map.get(item.full_path) if item.full_path else None
        # Диск не трогаем на SSR: .!qB→проверка и кнопки — фоновый /downloadable-files.
        status = file_status_for_ui(
            relative_path=item.relative_path,
            full_path=item.full_path,
            latest_kind=latest_kind,
            disk_hash=disk_hash,
            hash_job_active=hash_job_active,
            in_torrent=True,
            ui_status=getattr(item, "ui_status", None),
            incomplete=False,
            is_checking=bool(getattr(item, "is_checking", False)),
        )
        full_path = item.full_path
        rows.append(
            ReleaseFileRow(
                relative_path=item.relative_path,
                size=int(item.size or 0),
                selected=bool(item.selected),
                full_path=full_path,
                in_torrent=True,
                status=status,
                file_id=getattr(item, "id", None),
                downloadable=False,
            )
        )
        seen_keys.add(item.relative_path)
        if item.full_path:
            seen_keys.add(item.full_path)

    for cand in removed_candidates or []:
        if len(cand) == 3:
            display_path, full_path, _kind = cand  # type: ignore[misc]
        else:
            display_path, full_path = cand  # type: ignore[misc]
        key = display_path or full_path or ""
        if not key or key in seen_keys:
            continue
        if full_path and full_path in seen_keys:
            continue
        # Sticky «удалён»: показываем по событию, даже если файла уже нет на диске.
        disk_hash = hash_map.get(full_path) if full_path else None
        status = file_status_for_ui(
            relative_path=display_path or (Path(full_path).name if full_path else "?"),
            full_path=full_path,
            latest_kind=KIND_REMOVED,
            disk_hash=disk_hash,
            hash_job_active=False,
            in_torrent=False,
            ui_status=UI_STATUS_REMOVED,
            incomplete=False,
        )
        rows.append(
            ReleaseFileRow(
                relative_path=display_path or (Path(full_path).name if full_path else "?"),
                size=0,
                selected=False,
                full_path=full_path,
                in_torrent=False,
                status=status,
                file_id=None,
                downloadable=False,
            )
        )
        seen_keys.add(key)
        if full_path:
            seen_keys.add(full_path)
    return _sort_file_rows_desc(rows)


def torrent_allows_media_download(db: Session, info_hash: str) -> bool:
    """Актуальный торрент (api_present, не superseded) — можно отдавать media."""
    archive = _active_archive_for_hash(db, info_hash)
    return archive is not None


def _active_archive_for_hash(db: Session, info_hash: str) -> TorrentArchive | None:
    normalized = (info_hash or "").strip().lower()
    if not normalized or len(normalized) < 16:
        return None
    archive = db.scalar(
        select(TorrentArchive)
        .where(TorrentArchive.info_hash == normalized)
        .order_by(TorrentArchive.superseded.asc(), TorrentArchive.id.desc())
        .limit(1)
    )
    if archive is None:
        return None
    if bool(getattr(archive, "superseded", False)):
        return None
    if not bool(getattr(archive, "api_present", True)):
        return None
    return archive


@dataclass
class TorrentMediaProbe:
    """Результат фонового опроса диска для одного торрента."""

    downloadable_ids: list[int] = field(default_factory=list)
    checking_ids: list[int] = field(default_factory=list)


def probe_torrent_media_files(db: Session, info_hash: str) -> TorrentMediaProbe:
    """Кнопки скачивания с диска + overlay «проверка» (is_checking / master progress / hash_torrent).

    Только api_present и не superseded. «проверка» не сканирует .!qB на диске:
    live-флаг берём из qB master (progress < 1) и колонки is_checking.
    """
    if _active_archive_for_hash(db, info_hash) is None:
        return TorrentMediaProbe()

    normalized = (info_hash or "").strip().lower()
    from app.services.qb_inventory import refresh_checking_flags_from_master

    refresh_checking_flags_from_master(db, normalized)
    rows = list(
        db.scalars(select(TorrentFile).where(TorrentFile.info_hash == normalized)).all()
    )
    if not rows:
        return TorrentMediaProbe()

    hash_job_active = normalized in _info_hashes_with_active_hash_job(db, [normalized])
    checking: list[int] = []
    download_candidates: list[tuple[TorrentFile, str]] = []
    for row in rows:
        status = (row.ui_status or "").strip().lower() or "ok"
        ui = file_status_for_ui(
            relative_path=str(row.relative_path or ""),
            full_path=row.full_path,
            hash_job_active=hash_job_active,
            in_torrent=True,
            ui_status=status,
            incomplete=False,
            is_checking=bool(getattr(row, "is_checking", False)),
        )
        if ui == UI_STATUS_CHECKING and row.id is not None:
            checking.append(int(row.id))
        if status not in _DOWNLOADABLE_STATUSES:
            continue
        if not row.full_path or row.id is None:
            continue
        download_candidates.append((row, status))

    downloadable: list[int] = []
    if download_candidates:
        paths = [row.full_path for row, _ in download_candidates if row.full_path]
        media_root = resolve_media_root().resolve()
        existing, partial = _scan_media_presence(paths, media_root=media_root)
        for row, status in download_candidates:
            full = row.full_path or ""
            canon = str(complete_path_for(full))
            if canon in partial:
                continue
            if _file_is_downloadable(
                status=status,
                full_path=full,
                media_root=media_root,
                existing_resolved=existing,
            ):
                downloadable.append(int(row.id))
    return TorrentMediaProbe(downloadable_ids=downloadable, checking_ids=checking)


def list_downloadable_file_ids(db: Session, info_hash: str) -> list[int]:
    """file_id актуального торрента, которые можно скачать с диска."""
    return probe_torrent_media_files(db, info_hash).downloadable_ids


def _scan_media_presence(
    full_paths: list[str], *, media_root: Path
) -> tuple[set[str], set[str]]:
    """Один scandir на parent → (complete resolved under root, partial-only canonical)."""
    # parent → список (canonical_str, basename, resolved_or_none)
    by_parent: dict[str, list[tuple[str, str, str | None]]] = {}
    for raw in full_paths:
        if not raw:
            continue
        path = Path(raw)
        if path.name.endswith(QB_INCOMPLETE_SUFFIX):
            continue
        canon = complete_path_for(path)
        resolved_key: str | None = None
        try:
            resolved = canon.resolve()
            if is_under_media_root(resolved, media_root=media_root):
                resolved_key = str(resolved)
        except OSError:
            resolved_key = None
        by_parent.setdefault(str(canon.parent), []).append(
            (str(canon), canon.name, resolved_key)
        )

    existing: set[str] = set()
    partial: set[str] = set()
    for parent, items in by_parent.items():
        try:
            names: set[str] = set()
            with os.scandir(parent) as entries:
                for entry in entries:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            names.add(entry.name)
                    except OSError:
                        continue
        except OSError:
            for canon_str, base, resolved_key in items:
                try:
                    if Path(canon_str).is_file():
                        if resolved_key:
                            existing.add(resolved_key)
                        continue
                    if Path(canon_str + QB_INCOMPLETE_SUFFIX).is_file():
                        partial.add(canon_str)
                except OSError:
                    continue
            continue
        for canon_str, base, resolved_key in items:
            if base in names:
                if resolved_key:
                    existing.add(resolved_key)
                continue
            if (base + QB_INCOMPLETE_SUFFIX) in names:
                partial.add(canon_str)
    return existing, partial


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


def _recent_events_by_info_hash(
    db: Session,
    release_ids: list[int],
) -> dict[str, _TorrentEvents]:
    """info_hash → latest kind по пути + кандидаты removed/orphan для UI.

    История привязана к конкретной версии торрента (info_hash), а не только к torrent_id.
    """
    if not release_ids:
        return {}
    since = utcnow() - _EVENT_WINDOW
    rows = db.scalars(
        select(FileChangeEvent)
        .where(
            FileChangeEvent.release_id.in_(release_ids),
            FileChangeEvent.created_at >= since,
        )
        .order_by(FileChangeEvent.id.desc())
    ).all()
    # torrent_id → info_hash для legacy-событий без info_hash
    archive_hash_by_torrent: dict[int, str] = {}
    archives = db.scalars(
        select(TorrentArchive).where(TorrentArchive.release_id.in_(release_ids))
    ).all()
    for archive in archives:
        tid = int(archive.torrent_id)
        # Актуальная (не superseded) версия предпочтительнее для legacy fallback
        key = (archive.info_hash or "").strip().lower()
        if not key:
            continue
        if bool(getattr(archive, "superseded", False)):
            archive_hash_by_torrent.setdefault(tid, key)
        else:
            archive_hash_by_torrent[tid] = key

    result: dict[str, _TorrentEvents] = {}
    removed_seen: dict[str, set[str]] = {}
    for row in rows:
        hash_key = (getattr(row, "info_hash", None) or "").strip().lower()
        if not hash_key and row.torrent_id is not None:
            hash_key = archive_hash_by_torrent.get(int(row.torrent_id), "")
        if not hash_key:
            continue
        bucket = result.setdefault(hash_key, _TorrentEvents())
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
        seen = removed_seen.setdefault(hash_key, set())
        dedupe_key = row.full_path or display
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        bucket.removed_candidates.append((display, row.full_path, row.kind))
    return result


# Совместимость для тестов, которые импортировали старое имя.
_recent_events_by_torrent = _recent_events_by_info_hash

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
        params = job.params_json if isinstance(getattr(job, "params_json", None), dict) else {}
        key = str(params.get("info_hash") or "").strip().lower()
        if key and key in wanted:
            active.add(key)
    return active


def _latest_pipeline_by_hash(
    db: Session,
    hashes: list[str],
) -> dict[str, tuple[str | None, str | None, int | None]]:
    """Как get_latest_by_hash: предпочитаем не failed/cancelled."""
    from app.services.pipeline import TorrentPipelineService

    excluded = TorrentPipelineService._EXCLUDED_FROM_LATEST
    normalized = sorted({(h or "").strip().lower() for h in hashes if h})
    if not normalized:
        return {}
    rows = db.scalars(
        select(TorrentPipeline)
        .where(TorrentPipeline.info_hash.in_(normalized))
        .order_by(TorrentPipeline.id.desc())
    ).all()
    result: dict[str, tuple[str | None, str | None, int | None]] = {}
    fallback: dict[str, tuple[str | None, str | None, int | None]] = {}
    for row in rows:
        key = row.info_hash.lower()
        payload = (row.status, row.error, row.id)
        if row.status in excluded:
            if key not in fallback:
                fallback[key] = payload
            continue
        if key not in result:
            result[key] = payload
    for key, payload in fallback.items():
        if key not in result:
            result[key] = payload
    return result


def _tracked_by_release_id(db: Session, release_ids: list[int]) -> dict[int, TrackedRelease]:
    if not release_ids:
        return {}
    rows = db.scalars(select(TrackedRelease).where(TrackedRelease.release_id.in_(release_ids))).all()
    return {row.release_id: row for row in rows}
