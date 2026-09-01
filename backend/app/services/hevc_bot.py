"""Выборки и HTML-форматирование для второго Telegram-бота."""

from __future__ import annotations

import html
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Literal, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DiskFileHash, Job, Release, ReleaseMember, TorrentArchive, TorrentFile
from app.services.file_tracker import UI_STATUS_CHECKING, file_status_for_ui
from app.services.torrent_qb_meta import resolve_anilibria_site_url
from app.services.hevc_pairing import (
    HEVC_SLA_HOURS,
    UnpairedAvc,
    archive_is_newer_than_hevc,
    batch_start_key,
    classify_archive_codec,
    find_unpaired_avc,
    load_file_keys_by_archive_id,
    overdue_hours_past_sla,
    rip_family_key,
)
from app.services.job_runner import STATUS_PENDING, STATUS_RUNNING
from app.utils.datetime_fmt import utcnow

TELEGRAM_TEXT_LIMIT = 4096
HEVC_ROLE_LABEL = "Кодирование HEVC"


def _release_torrents_base_url() -> str:
    return f"{resolve_anilibria_site_url().rstrip('/')}/anime/releases/release"


HEVC_MISSING_STATUS_LIST_MAX_AGE_HOURS = 30 * 24

ListKind = Literal["overdue", "waiting", "error"]


@dataclass(frozen=True)
class StickyChange:
    relative_path: str
    basename: str
    status: Literal["new", "changed", "ok"]


@dataclass
class HevcReleaseStatus:
    release_id: int
    alias: str
    title: str
    original_title: str | None
    executors: list[str]
    items: list[UnpairedAvc] = field(default_factory=list)
    changes: list[StickyChange] = field(default_factory=list)
    changes_checking: bool = False

    @property
    def max_overdue_hours(self) -> float:
        values = [
            overdue_hours_past_sla(item.age_hours)
            for item in self.items
            if item.status == "overdue"
        ]
        return max((value for value in values if value is not None), default=0.0)


def telegram_text_length(value: str) -> int:
    """Длина в UTF-16 code units — именно так Telegram считает лимиты."""
    return len(value.encode("utf-16-le")) // 2


def truncate_telegram_text(value: str, max_len: int, *, suffix: str = "…") -> str:
    """Обрезает plain text, не разрывая surrogate pair."""
    if max_len <= 0:
        return ""
    if telegram_text_length(value) <= max_len:
        return value
    suffix_value = suffix if telegram_text_length(suffix) <= max_len else ""
    budget = max_len - telegram_text_length(suffix_value)
    result: list[str] = []
    used = 0
    for char in value:
        char_len = telegram_text_length(char)
        if used + char_len > budget:
            break
        result.append(char)
        used += char_len
    return "".join(result) + suffix_value


def rounded_hours(value: float | None) -> int:
    """Единое округление длительности вверх до полного часа."""
    return max(0, int(math.ceil(value or 0.0)))


def format_msk(value: datetime | None) -> str:
    if value is None:
        return "—"
    source = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    # MSK фиксированно UTC+3; без зависимости от TZ контейнера.
    shown = source.astimezone(timezone.utc) + timedelta(hours=3)
    return shown.strftime("%d.%m.%Y %H:%M MSK")


def aggregate_sticky_changes(files: Sequence[object]) -> list[StickyChange]:
    """Sticky-статус по exact relative_path с приоритетом new > changed > ok."""
    priorities = {"ok": 1, "changed": 2, "new": 3}
    strongest: dict[str, Literal["new", "changed", "ok"]] = {}
    for row in files:
        path = str(getattr(row, "relative_path", "") or "")
        status = str(
            getattr(row, "status", None) or getattr(row, "ui_status", "") or ""
        )
        if not path or status not in priorities:
            continue
        previous = strongest.get(path)
        if previous is None or priorities[status] > priorities[previous]:
            strongest[path] = status  # type: ignore[assignment]
    return [
        StickyChange(
            relative_path=path,
            basename=PurePosixPath(path.replace("\\", "/")).name,
            status=status,
        )
        for path, status in sorted(
            strongest.items(), key=lambda item: item[0].casefold()
        )
    ]


def _load_release_rows(db: Session) -> tuple[list[TorrentArchive], dict[int, Release]]:
    archives = list(db.scalars(select(TorrentArchive)).all())
    release_ids = sorted({int(row.release_id) for row in archives})
    if not release_ids:
        return archives, {}
    releases = list(
        db.scalars(select(Release).where(Release.release_id.in_(release_ids))).all()
    )
    return archives, {int(row.release_id): row for row in releases}


def _executors_by_release(
    db: Session, release_ids: Sequence[int]
) -> dict[int, list[str]]:
    if not release_ids:
        return {}
    rows = list(
        db.scalars(
            select(ReleaseMember)
            .where(
                ReleaseMember.release_id.in_(release_ids),
                ReleaseMember.role_label == HEVC_ROLE_LABEL,
            )
            .order_by(
                ReleaseMember.release_id, ReleaseMember.sort_order, ReleaseMember.id
            )
        ).all()
    )
    result: dict[int, list[str]] = {}
    for row in rows:
        nickname = (row.nickname or "").strip()
        if nickname and nickname not in result.setdefault(int(row.release_id), []):
            result[int(row.release_id)].append(nickname)
    return result


def _archive_sequence_key(row: object) -> tuple[int, datetime, int]:
    """Порядок версий AniLibria: torrent_id, затем время и archive id."""
    # Как в pairing: системный created_at — tie-break для одинакового torrent_id;
    # api_created_at участвует только в grace парной загрузки HEVC→AVC.
    created = getattr(row, "created_at", None) or getattr(row, "api_created_at", None)
    if not isinstance(created, datetime):
        created = datetime.min
    elif created.tzinfo is not None:
        created = created.astimezone(timezone.utc).replace(tzinfo=None)
    return (
        int(getattr(row, "torrent_id", 0) or 0),
        created,
        int(getattr(row, "id", 0) or 0),
    )


def _archive_file_key(row: object) -> tuple[int, int, str]:
    return (
        int(getattr(row, "release_id", 0) or 0),
        int(getattr(row, "torrent_id", 0) or 0),
        str(getattr(row, "info_hash", "") or "").strip().lower(),
    )


def _changes_for_item(
    archives: Sequence[TorrentArchive],
    files_by_version: dict[tuple[int, int, str], list[TorrentFile]],
    item: UnpairedAvc,
) -> list[StickyChange]:
    """Текущий AVC относительно AVC-состояния, покрытого парным HEVC."""
    archives_by_id = {int(row.id): row for row in archives}
    current_avc = archives_by_id.get(item.archive_id)
    hevc = (
        archives_by_id.get(item.paired_hevc_archive_id)
        if item.paired_hevc_archive_id is not None
        else None
    )
    if current_avc is None or hevc is None:
        return []

    current_qj = (
        current_avc.quality_json if isinstance(current_avc.quality_json, dict) else None
    )
    current_family = rip_family_key(
        quality_json=current_qj,
        torrent_type=current_avc.torrent_type,
    )
    current_start = batch_start_key(current_avc.torrent_description)
    if not current_family or current_start is None:
        return []

    lineage: list[TorrentArchive] = []
    for row in archives:
        if int(row.release_id) != item.release_id:
            continue
        qj = row.quality_json if isinstance(row.quality_json, dict) else None
        if (
            classify_archive_codec(quality_json=qj, torrent_type=row.torrent_type)
            != "AVC"
        ):
            continue
        if (
            rip_family_key(quality_json=qj, torrent_type=row.torrent_type)
            != current_family
            or batch_start_key(row.torrent_description) != current_start
        ):
            continue
        lineage.append(row)

    # Grace из pairing важен здесь: AVC, загруженный сразу после HEVC как часть
    # одной публикации, считается покрытым этим HEVC и может стать baseline.
    baseline_candidates = [
        row for row in lineage if not archive_is_newer_than_hevc(row, hevc)
    ]
    if not baseline_candidates:
        return []
    baseline = max(baseline_candidates, key=_archive_sequence_key)

    baseline_key = _archive_sequence_key(baseline)
    current_key = _archive_sequence_key(current_avc)
    if baseline_key > current_key:
        return []
    chain = sorted(
        (
            row
            for row in lineage
            if baseline_key <= _archive_sequence_key(row) <= current_key
        ),
        key=_archive_sequence_key,
    )

    baseline_paths = {
        str(row.relative_path)
        for row in files_by_version.get(_archive_file_key(baseline), [])
        if str(row.relative_path or "")
    }
    current_files = files_by_version.get(_archive_file_key(current_avc), [])
    statuses_by_path: dict[str, set[str]] = {}
    for version in chain[1:]:
        for row in files_by_version.get(_archive_file_key(version), []):
            path = str(row.relative_path or "")
            status = str(row.ui_status or "")
            if path and status in {"new", "changed", "ok"}:
                statuses_by_path.setdefault(path, set()).add(status)

    result: list[StickyChange] = []
    seen: set[str] = set()
    for row in current_files:
        path = str(row.relative_path or "")
        if not path or path in seen:
            continue
        seen.add(path)
        chain_statuses = statuses_by_path.get(path, set())
        if path not in baseline_paths or "new" in chain_statuses:
            status: Literal["new", "changed", "ok"] = "new"
        elif "changed" in chain_statuses:
            status = "changed"
        else:
            status = "ok"
        result.append(
            StickyChange(
                relative_path=path,
                basename=PurePosixPath(path.replace("\\", "/")).name,
                status=status,
            )
        )
    return sorted(result, key=lambda row: row.relative_path.casefold())


def _current_avc_has_checking(
    archives: Sequence[TorrentArchive],
    files_by_version: dict[tuple[int, int, str], list[TorrentFile]],
    item: UnpairedAvc,
    *,
    active_hashes: set[str],
    disk_hashes_by_path: dict[str, DiskFileHash],
) -> bool:
    """Overlay checking только для файлов текущего сравниваемого AVC.

    UI «проверка» на части состава (при ok на остальных) — is_checking в БД /
    stored-hash overlay, а не только torrent-wide hash_torrent: активный hash
    job пометил бы все ok/changed файлы как checking.
    """
    current_avc = next(
        (row for row in archives if int(row.id) == item.archive_id),
        None,
    )
    if current_avc is None:
        return False
    info_hash = str(current_avc.info_hash or "").strip().lower()
    current_files = files_by_version.get(_archive_file_key(current_avc), [])
    for row in current_files:
        full_path = str(getattr(row, "full_path", None) or "")
        disk_hash = disk_hashes_by_path.get(full_path)
        status = file_status_for_ui(
            relative_path=str(row.relative_path or ""),
            full_path=row.full_path,
            disk_hash=disk_hash,
            hash_job_active=info_hash in active_hashes,
            in_torrent=True,
            ui_status=row.ui_status,
            incomplete=False,
            is_checking=bool(getattr(row, "is_checking", False)),
        )
        if status == UI_STATUS_CHECKING:
            return True
    return False


def _aggregate_file_state(
    archives: Sequence[TorrentArchive],
    files: Sequence[TorrentFile],
    items: Sequence[UnpairedAvc],
    *,
    active_hashes: set[str],
    disk_hashes_by_path: dict[str, DiskFileHash],
) -> tuple[dict[int, list[StickyChange]], set[int]]:
    files_by_version: dict[tuple[int, int, str], list[TorrentFile]] = {}
    for row in files:
        files_by_version.setdefault(_archive_file_key(row), []).append(row)

    grouped: dict[int, list[StickyChange]] = {}
    checking_release_ids: set[int] = set()
    for item in items:
        grouped.setdefault(item.release_id, []).extend(
            _changes_for_item(archives, files_by_version, item)
        )
        if _current_avc_has_checking(
            archives,
            files_by_version,
            item,
            active_hashes=active_hashes,
            disk_hashes_by_path=disk_hashes_by_path,
        ):
            checking_release_ids.add(item.release_id)
    return (
        {
            release_id: aggregate_sticky_changes(rows)
            for release_id, rows in grouped.items()
        },
        checking_release_ids,
    )


def _changes_by_release(
    db: Session,
    release_ids: Sequence[int],
    archives: Sequence[TorrentArchive],
    items: Sequence[UnpairedAvc],
) -> dict[int, list[StickyChange]]:
    if not release_ids:
        return {}
    files = list(
        db.scalars(
            select(TorrentFile).where(TorrentFile.release_id.in_(release_ids))
        ).all()
    )
    files_by_version: dict[tuple[int, int, str], list[TorrentFile]] = {}
    for row in files:
        files_by_version.setdefault(_archive_file_key(row), []).append(row)

    grouped: dict[int, list[StickyChange]] = {}
    for item in items:
        grouped.setdefault(item.release_id, []).extend(
            _changes_for_item(archives, files_by_version, item)
        )
    return {
        release_id: aggregate_sticky_changes(rows)
        for release_id, rows in grouped.items()
    }


def _file_state_by_release(
    db: Session,
    release_ids: Sequence[int],
    archives: Sequence[TorrentArchive],
    items: Sequence[UnpairedAvc],
) -> tuple[dict[int, list[StickyChange]], set[int]]:
    if not release_ids:
        return {}, set()
    files = list(
        db.scalars(
            select(TorrentFile).where(TorrentFile.release_id.in_(release_ids))
        ).all()
    )
    current_archive_ids = {item.archive_id for item in items}
    current_hashes = {
        str(row.info_hash or "").strip().lower()
        for row in archives
        if int(row.id) in current_archive_ids and row.info_hash
    }
    active_hashes: set[str] = set()
    if current_hashes:
        jobs = db.scalars(
            select(Job).where(
                Job.type == "hash_torrent",
                Job.status.in_((STATUS_PENDING, STATUS_RUNNING)),
            )
        ).all()
        for job in jobs:
            params = job.params_json if isinstance(getattr(job, "params_json", None), dict) else {}
            info_hash = str(params.get("info_hash") or "").strip().lower()
            if info_hash in current_hashes:
                active_hashes.add(info_hash)

    current_file_paths = {
        str(row.full_path)
        for row in files
        if str(row.info_hash or "").strip().lower() in current_hashes and row.full_path
    }
    disk_hashes_by_path: dict[str, DiskFileHash] = {}
    if current_file_paths:
        disk_hashes = db.scalars(
            select(DiskFileHash).where(DiskFileHash.full_path.in_(current_file_paths))
        ).all()
        disk_hashes_by_path = {str(row.full_path): row for row in disk_hashes}
    return _aggregate_file_state(
        archives,
        files,
        items,
        active_hashes=active_hashes,
        disk_hashes_by_path=disk_hashes_by_path,
    )


def query_hevc_statuses(
    db: Session,
    *,
    kind: ListKind,
    nickname: str | None = None,
    release_id: int | None = None,
    now: datetime | None = None,
) -> list[HevcReleaseStatus]:
    """Состояния поверх pairing: overdue, ожидание либо расхождение типов."""
    current = now or utcnow()
    archives, releases = _load_release_rows(db)
    file_keys = load_file_keys_by_archive_id(db, archives)
    actual = find_unpaired_avc(
        archives, now=current, file_keys_by_archive_id=file_keys
    )
    # Отрицательный SLA раскрывает catch-up, который ещё не просрочен.
    all_pending = find_unpaired_avc(
        archives, now=current, sla_hours=-1, file_keys_by_archive_id=file_keys
    )
    actual_by_archive = {item.archive_id: item for item in actual}

    selected: list[UnpairedAvc] = []
    if kind == "overdue":
        selected = [item for item in actual if item.status == "overdue"]
    elif kind == "error":
        selected = [item for item in all_pending if item.type_mismatch]
    else:
        for item in all_pending:
            current_item = actual_by_archive.get(item.archive_id)
            if current_item is not None and current_item.status == "overdue":
                continue
            pending_item = current_item or replace(item, overdue=False)
            if pending_item.type_mismatch:
                continue
            selected.append(pending_item)
        # Pure missing уже присутствует и в actual, но all_pending сохраняет его.
        if release_id is None:
            # Старый pure missing скрываем только из общего /status. Возраст уже
            # рассчитан pairing от api_created_at с fallback на created_at.
            selected = [
                item
                for item in selected
                if not (
                    item.missing
                    and item.age_hours is not None
                    and item.age_hours >= HEVC_MISSING_STATUS_LIST_MAX_AGE_HOURS
                )
            ]

    if release_id is not None:
        selected = [item for item in selected if item.release_id == int(release_id)]

    release_ids = sorted({item.release_id for item in selected})
    executors = _executors_by_release(db, release_ids)
    nickname_folded = (nickname or "").strip().casefold()
    if nickname_folded:
        release_ids = [
            rid
            for rid in release_ids
            if any(
                name.casefold() == nickname_folded for name in executors.get(rid, [])
            )
        ]

    grouped: dict[int, list[UnpairedAvc]] = {}
    for item in selected:
        if item.release_id in release_ids:
            grouped.setdefault(item.release_id, []).append(item)
    changes, checking_release_ids = _file_state_by_release(
        db,
        release_ids,
        archives,
        [item for rows in grouped.values() for item in rows],
    )

    result: list[HevcReleaseStatus] = []
    for rid in release_ids:
        release = releases.get(rid)
        archive = next((row for row in archives if int(row.release_id) == rid), None)
        alias = (
            (release.release_alias if release is not None else None)
            or (archive.release_alias if archive is not None else None)
            or str(rid)
        )
        title = (release.title if release is not None else None) or ""
        result.append(
            HevcReleaseStatus(
                release_id=rid,
                alias=str(alias),
                title=str(title),
                original_title=release.original_title if release is not None else None,
                executors=executors.get(rid, []),
                items=grouped.get(rid, []),
                changes=changes.get(rid, []),
                changes_checking=rid in checking_release_ids,
            )
        )
    if kind == "overdue":
        result.sort(
            key=lambda row: (-row.max_overdue_hours, (row.title or row.alias).casefold())
        )
    else:
        result.sort(key=lambda row: (row.title or row.alias).casefold())
    return result


def query_release_detail(
    db: Session,
    release_id: int,
    *,
    now: datetime | None = None,
) -> HevcReleaseStatus | None:
    """Полное актуальное состояние релиза, включая mismatch и overdue."""
    current = now or utcnow()
    archives, releases = _load_release_rows(db)
    release_archives = [
        row for row in archives if int(row.release_id) == int(release_id)
    ]
    if not release_archives:
        return None
    file_keys = load_file_keys_by_archive_id(db, archives)
    actual = find_unpaired_avc(
        archives, now=current, file_keys_by_archive_id=file_keys
    )
    all_pending = find_unpaired_avc(
        archives, now=current, sla_hours=-1, file_keys_by_archive_id=file_keys
    )
    actual_by_id = {item.archive_id: item for item in actual}
    items = [
        actual_by_id.get(item.archive_id) or replace(item, overdue=False)
        for item in all_pending
        if item.release_id == int(release_id)
    ]
    release = releases.get(int(release_id))
    archive = release_archives[0]
    alias = (
        (release.release_alias if release is not None else None)
        or archive.release_alias
        or str(release_id)
    )
    title = (release.title if release is not None else None) or ""
    executors = _executors_by_release(db, [int(release_id)])
    changes, checking_release_ids = _file_state_by_release(
        db, [int(release_id)], archives, items
    )
    return HevcReleaseStatus(
        release_id=int(release_id),
        alias=str(alias),
        title=str(title),
        original_title=release.original_title if release is not None else None,
        executors=executors.get(int(release_id), []),
        items=items,
        changes=changes.get(int(release_id), []),
        changes_checking=int(release_id) in checking_release_ids,
    )


def _html_or_dash(value: str | None) -> str:
    text = (value or "").strip()
    return html.escape(text) if text else "—"


def _release_link(row: HevcReleaseStatus) -> str:
    title = html.escape((row.title or "").strip() or row.alias)
    alias = html.escape(row.alias, quote=True)
    return f'<a href="{_release_torrents_base_url()}/{alias}/torrents">{title}</a>'


def format_release_list_item(row: HevcReleaseStatus, *, kind: ListKind) -> str:
    executors_plain = ", ".join(row.executors) or "неизвестно"
    executors = html.escape(truncate_telegram_text(executors_plain, 1024))
    if kind == "overdue":
        state = f"просрочка {rounded_hours(row.max_overdue_hours)} ч"
    elif kind == "error":
        state = "расхождение типов AVC/HEVC"
    else:
        missing = any(item.missing for item in row.items)
        max_age = max((item.age_hours or 0.0 for item in row.items), default=0.0)
        if missing and max_age > HEVC_SLA_HOURS:
            state = f"HEVC отсутствует уже {rounded_hours(max_age)} ч"
        elif missing:
            state = f"HEVC отсутствует {rounded_hours(max_age)} ч"
        else:
            remaining = max(0.0, HEVC_SLA_HOURS - max_age)
            state = f"ожидание, до SLA {rounded_hours(remaining)} ч"
    return f"• {_release_link(row)}\n  {html.escape(state)} · {executors}"


def split_release_list(
    rows: Sequence[HevcReleaseStatus],
    *,
    kind: ListKind,
    max_len: int = TELEGRAM_TEXT_LIMIT,
) -> list[tuple[str, list[HevcReleaseStatus]]]:
    heading = {
        "overdue": "⏰ <b>Просроченные AVC</b>",
        "waiting": "🕓 <b>Ожидают HEVC</b>",
        "error": "<b>Ошибки:</b>",
    }[kind]
    if not rows:
        empty_text = (
            "Расхождений типов AVC/HEVC нет."
            if kind == "error"
            else "Список пуст."
        )
        return [(heading + f"\n\n{empty_text}", [])]
    chunks: list[tuple[str, list[HevcReleaseStatus]]] = []
    current = heading
    current_rows: list[HevcReleaseStatus] = []
    for row in rows:
        block = "\n\n" + format_release_list_item(row, kind=kind)
        if current_rows and telegram_text_length(current + block) > max_len:
            chunks.append((current, current_rows))
            current = heading + block
            current_rows = [row]
        else:
            current += block
            current_rows.append(row)
    chunks.append((current, current_rows))
    return chunks


def format_release_detail(
    row: HevcReleaseStatus,
    *,
    max_len: int = TELEGRAM_TEXT_LIMIT,
) -> str:
    lines = [
        f"🎬 <b>{_release_link(row)}</b>",
        f"Оригинальное название: {_html_or_dash(row.original_title)}",
        f"За HEVC отвечает: {html.escape(', '.join(row.executors) or '—')}",
    ]
    if not row.items:
        lines.extend(["", "Нет AVC, требующих HEVC"])
    for item in sorted(row.items, key=lambda value: value.torrent_id):
        if item.ignore_hevc:
            state = "игнор HEVC"
        elif item.type_mismatch:
            state = "⚠️ расхождение типов"
        elif item.status == "overdue":
            state = f"⏰ просрочка {rounded_hours(overdue_hours_past_sla(item.age_hours))} ч"
        elif item.missing:
            state = f"HEVC отсутствует уже {rounded_hours(item.age_hours)} ч"
        else:
            state = f"ожидание HEVC {rounded_hours(item.age_hours)} ч"
        lines.extend(
            [
                "",
                f"<b>AVC #{item.torrent_id}</b> · {html.escape(item.rip_family or '—')}",
                f"Серии: {html.escape(item.episodes or '—')}",
                state,
                f"Загружен: {format_msk(item.upload_created_at or item.created_at)}",
            ]
        )
    visible_changes = [item for item in row.changes if item.status in {"new", "changed"}]
    if row.changes_checking:
        lines.extend(
            [
                "",
                "Изменения файлов после HEVC (sticky) появятся позже, так как новые файлы ещё не проверены.",
            ]
        )
    elif visible_changes:
        lines.extend(["", "<b>Изменения файлов после HEVC (sticky):</b>"])
        icon = {"new": "➕", "changed": "✏️"}
        lines.extend(
            f"{icon[item.status]} {html.escape(item.basename)} — {item.status}"
            for item in visible_changes
        )
    else:
        lines.extend(["", "Изменений файлов после HEVC нет."])
    result: list[str] = []
    used = 0
    for line in lines:
        separator_len = 1 if result else 0
        extra = telegram_text_length(line) + separator_len
        if used + extra > max_len:
            ellipsis_extra = telegram_text_length("…") + separator_len
            if used + ellipsis_extra <= max_len:
                result.append("…")
            break
        result.append(line)
        used += extra
    return "\n".join(result)
