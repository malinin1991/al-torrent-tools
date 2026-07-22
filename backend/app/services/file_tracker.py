"""Трекинг файлов торрента: upsert torrent_files, BLAKE3, diff A/B/C, TG."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import qbittorrentapi
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    DiskFileHash,
    FileChangeEvent,
    JobLog,
    QbClient,
    TorrentArchive,
    TorrentFile,
    TrackedRelease,
)
from app.services.file_hasher import clamp_hash_workers, hash_paths_parallel
from app.services.torrent_archive import TorrentArchiveService
from app.services.torrent_files_meta import (
    extract_qb_content_path,
    extract_qb_file_priorities,
    extract_qb_save_path,
    is_under_media_root,
    parse_torrent_file_list,
    path_exists_including_incomplete,
    resolve_full_path,
    resolve_media_root,
)
from app.services.qbittorrent import ensure_announce_passkey
from app.services.runtime_settings import get_setting_value

logger = logging.getLogger(__name__)

KIND_ADDED = "added"
KIND_REMOVED = "removed"
KIND_MODIFIED = "modified"
KIND_MISSING = "missing"
KIND_ORPHAN = "orphan"

EVENT_KINDS = frozenset({KIND_ADDED, KIND_REMOVED, KIND_MODIFIED, KIND_MISSING, KIND_ORPHAN})


@dataclass
class FileChange:
    kind: str
    relative_path: str | None = None
    full_path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class TrackTorrentResult:
    files_upserted: int = 0
    hashed: int = 0
    gated: int = 0
    errors: int = 0
    changes: list[FileChange] = field(default_factory=list)
    skipped_reason: str | None = None


class FileTrackerService:
    def __init__(self, db: Session, job_id: int | None = None) -> None:
        self._db = db
        self._job_id = job_id

    def _log(self, message: str, level: str = "info") -> None:
        if self._job_id is None:
            logger.log(logging.INFO if level == "info" else logging.WARNING, message)
            return
        self._db.add(JobLog(job_id=self._job_id, level=level, message=message))
        self._db.commit()

    def track_torrent(
        self,
        *,
        info_hash: str,
        torrent_id: int,
        release_id: int,
        torrent_bytes: bytes | None = None,
        hash_selected_only: bool = True,
        notify: bool = True,
    ) -> TrackTorrentResult:
        """Upsert состава, хеш выбранных файлов, запись событий изменений."""
        result = TrackTorrentResult()
        normalized_hash = (info_hash or "").strip().lower()
        if not normalized_hash:
            result.skipped_reason = "пустой info_hash"
            return result

        archive = self._db.scalar(
            select(TorrentArchive)
            .where(
                (TorrentArchive.info_hash == normalized_hash)
                | (TorrentArchive.torrent_id == torrent_id)
            )
            .order_by(TorrentArchive.id.desc())
            .limit(1)
        )
        if archive is not None and not archive.api_present:
            result.skipped_reason = "торрент не api_present (архивный)"
            self._log(f"hash_torrent: пропуск {normalized_hash[:12]}… — api_present=false", "debug")
            return result

        if torrent_bytes is None:
            torrent_bytes = self._load_torrent_bytes(archive, torrent_id=torrent_id, info_hash=normalized_hash)
        if torrent_bytes is None:
            result.skipped_reason = "нет .torrent в архиве"
            self._log(f"hash_torrent: нет файла для {normalized_hash[:12]}…", "warning")
            return result

        file_metas = parse_torrent_file_list(torrent_bytes)
        save_path, content_path, priorities = self._qb_paths_and_priorities(normalized_hash)

        previous = {
            row.relative_path: row
            for row in self._db.scalars(
                select(TorrentFile).where(TorrentFile.info_hash == normalized_hash)
            ).all()
        }
        previous_paths = set(previous.keys())
        is_baseline = not previous_paths
        current_paths: set[str] = set()
        now = datetime.utcnow()
        media_root = resolve_media_root()

        for meta in file_metas:
            current_paths.add(meta.relative_path)
            selected = True
            if priorities:
                selected = priorities.get(meta.file_index, 0) > 0
            full_path: str | None = None
            if save_path or content_path:
                resolved = resolve_full_path(
                    save_path or content_path or "",
                    meta.relative_path,
                    content_path=content_path,
                    media_root=media_root,
                )
                if resolved is not None:
                    full_path = str(resolved)

            row = previous.get(meta.relative_path)
            if row is None:
                row = TorrentFile(
                    torrent_id=torrent_id,
                    info_hash=normalized_hash,
                    release_id=release_id,
                    relative_path=meta.relative_path,
                    size=meta.size,
                    file_index=meta.file_index,
                    selected=selected,
                    full_path=full_path,
                    created_at=now,
                    updated_at=now,
                )
                self._db.add(row)
                # В т.ч. первый снимок (раньше пропускали baseline → в UI всё было «ok»).
                result.changes.append(
                    FileChange(kind=KIND_ADDED, relative_path=meta.relative_path, full_path=full_path)
                )
            else:
                row.torrent_id = torrent_id
                row.release_id = release_id
                row.size = meta.size
                row.file_index = meta.file_index
                row.selected = selected
                row.full_path = full_path
                row.updated_at = now
            result.files_upserted += 1

        removed_paths = previous_paths - current_paths
        for rel in removed_paths:
            old = previous[rel]
            result.changes.append(
                FileChange(
                    kind=KIND_REMOVED,
                    relative_path=rel,
                    full_path=old.full_path,
                )
            )
            self._db.delete(old)

        self._db.commit()

        # Diff B/C: хеш и missing для выбранных
        rows = list(
            self._db.scalars(select(TorrentFile).where(TorrentFile.info_hash == normalized_hash)).all()
        )
        to_hash: list[Path] = []
        path_to_rel: dict[str, str] = {}
        old_hashes: dict[str, str] = {}
        for row in rows:
            if hash_selected_only and not row.selected:
                continue
            if not row.full_path:
                continue
            path = Path(row.full_path)
            if not path_exists_including_incomplete(path):
                result.changes.append(
                    FileChange(
                        kind=KIND_MISSING,
                        relative_path=row.relative_path,
                        full_path=row.full_path,
                    )
                )
                continue
            if not path.is_file():
                # Качается (.!qB) — не missing, не хешируем
                continue
            if not is_under_media_root(path, media_root=media_root):
                self._log(f"Путь вне media root, пропуск хеша: {path}", "warning")
                continue

            try:
                full = str(path.resolve())
            except OSError as exc:
                self._log(f"хеш пропуск `{path}`: {exc}", "warning")
                result.errors += 1
                continue
            prev_hash = self._db.scalar(
                select(DiskFileHash).where(DiskFileHash.full_path == full).limit(1)
            )
            if prev_hash and prev_hash.content_hash:
                old_hashes[full] = prev_hash.content_hash
            to_hash.append(path)
            path_to_rel[full] = row.relative_path

        workers = clamp_hash_workers(
            get_setting_value(self._db, "file_hash_workers", str(settings.file_hash_workers))
        )
        if to_hash:
            self._log(f"hash_torrent: хеширование files={len(to_hash)}, workers={workers}", "debug")
            stats = hash_paths_parallel(
                self._db,
                to_hash,
                workers=workers,
                log_fn=lambda msg: self._log(msg, "info"),
            )
            result.hashed = stats["hashed"]
            result.gated = stats["gated"]
            result.errors += stats.get("errors", 0)
            for full, rel in path_to_rel.items():
                old_content = old_hashes.get(full)
                if not old_content:
                    continue
                new_row = self._db.scalar(
                    select(DiskFileHash).where(DiskFileHash.full_path == full).limit(1)
                )
                if new_row and new_row.content_hash and new_row.content_hash != old_content:
                    result.changes.append(
                        FileChange(
                            kind=KIND_MODIFIED,
                            relative_path=rel,
                            full_path=full,
                            details={"old_hash": old_content, "new_hash": new_row.content_hash},
                        )
                    )

        # Orphan под корнем торрента относительно активных torrent_files
        orphan_root = save_path or content_path
        if orphan_root:
            result.changes.extend(
                self._find_orphans_under_save_path(
                    save_path=orphan_root,
                    known_full_paths={r.full_path for r in rows if r.full_path},
                    media_root=media_root,
                )
            )

        filtered_changes = self._filter_duplicate_changes(torrent_id=torrent_id, changes=result.changes)
        events = self._persist_events(
            release_id=release_id,
            torrent_id=torrent_id,
            changes=filtered_changes,
        )
        if events and notify:
            self._maybe_notify_telegram(
                release_id=release_id,
                torrent_id=torrent_id,
                events=events,
                archive=archive,
                baseline=is_baseline,
            )
        return result

    def _find_orphans_under_save_path(
        self,
        *,
        save_path: str,
        known_full_paths: set[str | None],
        media_root: Path,
    ) -> list[FileChange]:
        base = Path(save_path).resolve()
        if not is_under_media_root(base, media_root=media_root):
            return []
        if not base.exists():
            return []
        known = {Path(p).resolve() for p in known_full_paths if p}
        changes: list[FileChange] = []
        walk_root = base if base.is_dir() else base.parent
        if not walk_root.is_dir():
            return []
        for path in walk_root.rglob("*"):
            if not path.is_file():
                continue
            if path.name.endswith(".!qB"):
                continue
            if path.resolve() in known:
                continue
            if not is_under_media_root(path, media_root=media_root):
                continue
            # Только файлы под save_path этого торрента
            try:
                path.resolve().relative_to(base if base.is_dir() else base.parent)
            except ValueError:
                continue
            changes.append(
                FileChange(kind=KIND_ORPHAN, relative_path=None, full_path=str(path.resolve()))
            )
        return changes

    def _filter_duplicate_changes(
        self,
        *,
        torrent_id: int,
        changes: list[FileChange],
    ) -> list[FileChange]:
        """Не дублировать повторные missing/modified/removed для того же пути."""
        dedup_kinds = {KIND_MISSING, KIND_MODIFIED, KIND_REMOVED}
        filtered: list[FileChange] = []
        for change in changes:
            if change.kind not in dedup_kinds:
                filtered.append(change)
                continue
            if not self._has_prior_event(
                torrent_id=torrent_id,
                kind=change.kind,
                relative_path=change.relative_path,
                full_path=change.full_path,
            ):
                filtered.append(change)
        return filtered

    def _has_prior_event(
        self,
        *,
        torrent_id: int,
        kind: str,
        relative_path: str | None,
        full_path: str | None,
    ) -> bool:
        stmt = select(FileChangeEvent).where(
            FileChangeEvent.torrent_id == torrent_id,
            FileChangeEvent.kind == kind,
        )
        if relative_path:
            stmt = stmt.where(FileChangeEvent.relative_path == relative_path)
        elif full_path:
            stmt = stmt.where(FileChangeEvent.full_path == full_path)
        else:
            return False
        return self._db.scalar(stmt.order_by(FileChangeEvent.id.desc()).limit(1)) is not None

    def _persist_events(
        self,
        *,
        release_id: int,
        torrent_id: int,
        changes: list[FileChange],
    ) -> list[FileChangeEvent]:
        if not changes:
            return []
        now = datetime.utcnow()
        events: list[FileChangeEvent] = []
        for change in changes:
            if change.kind not in EVENT_KINDS:
                continue
            event = FileChangeEvent(
                release_id=release_id,
                torrent_id=torrent_id,
                kind=change.kind,
                relative_path=change.relative_path,
                full_path=change.full_path,
                details_json=change.details or {},
                created_at=now,
            )
            self._db.add(event)
            events.append(event)
        self._db.commit()
        for event in events:
            self._db.refresh(event)
        return events

    def _maybe_notify_telegram(
        self,
        *,
        release_id: int,
        torrent_id: int,
        events: list[FileChangeEvent],
        archive: TorrentArchive | None,
        baseline: bool = False,
    ) -> None:
        from app.services.telegram_notify import enqueue_file_changes_notification

        tracked = self._db.get(TrackedRelease, release_id)
        if tracked is None or not tracked.enabled:
            return
        if baseline:
            # Первый проход отслеживаемого торрента: сводка «файлы в базе», без missing/orphan шума.
            relevant = [e for e in events if e.kind == KIND_ADDED]
        else:
            notify_kinds = {KIND_ADDED, KIND_REMOVED, KIND_MODIFIED, KIND_MISSING}
            relevant = [e for e in events if e.kind in notify_kinds]
        if not relevant:
            return
        enqueue_file_changes_notification(
            self._db,
            release_id=release_id,
            torrent_id=torrent_id,
            events=relevant,
            archive=archive,
            baseline=baseline,
        )

    def _load_torrent_bytes(
        self,
        archive: TorrentArchive | None,
        *,
        torrent_id: int,
        info_hash: str,
    ) -> bytes | None:
        if archive is None:
            archive = self._db.scalar(
                select(TorrentArchive)
                .where(
                    (TorrentArchive.info_hash == info_hash) | (TorrentArchive.torrent_id == torrent_id)
                )
                .order_by(TorrentArchive.id.desc())
                .limit(1)
            )
        if archive is None:
            return None
        path = TorrentArchiveService(self._db).resolve_file_path(archive)
        if not path.exists():
            return None
        passkey = get_setting_value(self._db, "anilibria_passkey", "")
        return ensure_announce_passkey(path.read_bytes(), passkey)

    def _qb_paths_and_priorities(
        self, info_hash: str
    ) -> tuple[str | None, str | None, dict[int, int]]:
        master = self._db.scalar(
            select(QbClient).where(QbClient.role == "master", QbClient.enabled.is_(True)).limit(1)
        )
        if master is None:
            self._log("hash_torrent: master qB не настроен — пути без priority", "warning")
            return None, None, {}
        try:
            qb = qbittorrentapi.Client(
                host=master.host,
                port=master.port,
                username=master.username,
                password=master.password_encrypted,
            )
            qb.auth_log_in()
            torrents = qb.torrents_info(hashes=info_hash)
            if not torrents:
                self._log(f"hash_torrent: торрент {info_hash[:12]}… нет на master", "warning")
                return None, None, {}
            save_path = extract_qb_save_path(torrents[0])
            content_path = extract_qb_content_path(torrents[0])
            qb_files = qb.torrents_files(torrent_hash=info_hash)
            priorities = extract_qb_file_priorities(qb_files)
            return save_path, content_path, priorities
        except Exception as exc:
            self._log(f"hash_torrent: ошибка qB master: {exc}", "warning")
            return None, None, {}


def update_api_present_for_release(
    db: Session,
    release_id: int,
    present_torrent_ids: set[int],
) -> dict[str, int]:
    """Актуальные torrent_id → api_present=True; остальные по релизу → False."""
    present = {int(x) for x in present_torrent_ids if x is not None}
    rows = list(db.scalars(select(TorrentArchive).where(TorrentArchive.release_id == release_id)).all())
    marked_true = 0
    marked_false = 0
    for row in rows:
        should_present = row.torrent_id in present
        if bool(row.api_present) == should_present:
            continue
        row.api_present = should_present
        if should_present:
            marked_true += 1
        else:
            marked_false += 1
    db.commit()
    return {"true": marked_true, "false": marked_false, "total": len(rows)}


def mark_missing_api_present_false(db: Session, seen_torrent_ids: set[int]) -> int:
    """full_sync: торренты, ни разу не встретившиеся в API за проход → api_present=False."""
    if not seen_torrent_ids:
        return 0
    result = db.execute(
        update(TorrentArchive)
        .where(
            TorrentArchive.api_present.is_(True),
            TorrentArchive.torrent_id.notin_(seen_torrent_ids),
        )
        .values(api_present=False)
    )
    db.commit()
    return int(result.rowcount or 0)


def file_status_for_ui(
    *,
    relative_path: str,
    full_path: str | None,
    recent_kinds: set[str],
) -> str:
    """Бейдж для UI: new / changed / removed / missing / ok."""
    if KIND_REMOVED in recent_kinds:
        return "removed"
    if KIND_MISSING in recent_kinds:
        return "missing"
    if KIND_ADDED in recent_kinds:
        return "new"
    if KIND_MODIFIED in recent_kinds:
        return "changed"
    if full_path and not path_exists_including_incomplete(Path(full_path)):
        return "missing"
    return "ok"
