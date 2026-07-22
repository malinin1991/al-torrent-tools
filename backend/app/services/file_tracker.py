"""Трекинг файлов торрента: upsert torrent_files, BLAKE3, diff A/B/C, TG."""

from __future__ import annotations

import logging
import os
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
    is_incomplete_path,
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

# Sticky UI-статусы на torrent_files (привязаны к торренту, не к диску).
UI_STATUS_NEW = "new"
UI_STATUS_OK = "ok"
UI_STATUS_CHANGED = "changed"
UI_STATUS_REMOVED = "removed"
UI_STATUS_CHECKING = "checking"

_STICKY_FINAL = frozenset({UI_STATUS_NEW, UI_STATUS_CHANGED})


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


@dataclass
class _PreparedTrack:
    normalized_hash: str
    torrent_bytes: bytes
    archive: TorrentArchive | None
    skipped_reason: str | None = None


@dataclass
class _CompositionSync:
    result: TrackTorrentResult
    events: list[FileChangeEvent]
    is_baseline: bool
    save_path: str | None
    content_path: str | None
    newly_added_paths: set[str] = field(default_factory=set)


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

    def sync_torrent_composition(
        self,
        *,
        info_hash: str,
        torrent_id: int,
        release_id: int,
        torrent_bytes: bytes | None = None,
        notify: bool = True,
    ) -> TrackTorrentResult:
        """Только состав: upsert torrent_files + сразу persist added/removed (без BLAKE3).

        Нужен на master_added, чтобы UI успел показать «новый» пока идёт закачка.
        """
        prepared = self._prepare_track(info_hash=info_hash, torrent_id=torrent_id, torrent_bytes=torrent_bytes)
        if prepared.skipped_reason:
            return TrackTorrentResult(skipped_reason=prepared.skipped_reason)
        synced = self._sync_composition(
            normalized_hash=prepared.normalized_hash,
            torrent_id=torrent_id,
            release_id=release_id,
            torrent_bytes=prepared.torrent_bytes,
        )
        if synced.events and notify:
            self._maybe_notify_telegram(
                release_id=release_id,
                torrent_id=torrent_id,
                events=synced.events,
                archive=prepared.archive,
                baseline=synced.is_baseline,
            )
        return synced.result

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
        prepared = self._prepare_track(info_hash=info_hash, torrent_id=torrent_id, torrent_bytes=torrent_bytes)
        if prepared.skipped_reason:
            return TrackTorrentResult(skipped_reason=prepared.skipped_reason)

        synced = self._sync_composition(
            normalized_hash=prepared.normalized_hash,
            torrent_id=torrent_id,
            release_id=release_id,
            torrent_bytes=prepared.torrent_bytes,
        )
        result = synced.result
        save_path = synced.save_path
        content_path = synced.content_path
        newly_added_paths = set(synced.newly_added_paths)

        media_root = resolve_media_root()
        hash_phase_changes: list[FileChange] = []

        # Diff B/C: хеш и missing для выбранных
        rows = list(
            self._db.scalars(
                select(TorrentFile).where(TorrentFile.info_hash == prepared.normalized_hash)
            ).all()
        )
        # Settle-baseline: ещё нет ok/changed (в т.ч. после early sync на master_added).
        hash_baseline = self._is_hash_settle_baseline(rows)
        rows_by_rel = {row.relative_path: row for row in rows}

        # Incremental composition TG сразу; baseline-сводку — после hash-settle.
        if synced.events and notify and not hash_baseline:
            self._maybe_notify_telegram(
                release_id=release_id,
                torrent_id=torrent_id,
                events=synced.events,
                archive=prepared.archive,
                baseline=False,
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
                hash_phase_changes.append(
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
        settled_rels: set[str] = set()
        if to_hash:
            self._log(f"hash_torrent: хеширование files={len(to_hash)}, workers={workers}", "debug")
            stop_fn = None
            if self._job_id is not None:
                from app.services.job_runner import is_stop_requested

                job_id = self._job_id
                stop_fn = lambda: is_stop_requested(self._db, job_id)
            stats = hash_paths_parallel(
                self._db,
                to_hash,
                workers=workers,
                log_fn=lambda msg: self._log(msg, "info"),
                should_stop=stop_fn,
            )
            result.hashed = stats["hashed"]
            result.gated = stats["gated"]
            result.errors += stats.get("errors", 0)
            if stats.get("stopped"):
                from app.services.job_runner import JobStopRequested

                self._log("hash_torrent: остановка по запросу (прогресс хешей сохранён)", "warning")
                raise JobStopRequested()
            for full, rel in path_to_rel.items():
                old_content = old_hashes.get(full)
                new_row = self._db.scalar(
                    select(DiskFileHash).where(DiskFileHash.full_path == full).limit(1)
                )
                new_content = (new_row.content_hash if new_row else None) or ""
                file_row = rows_by_rel.get(rel)
                if file_row is None:
                    continue
                if old_content and new_content and new_content != old_content:
                    hash_phase_changes.append(
                        FileChange(
                            kind=KIND_MODIFIED,
                            relative_path=rel,
                            full_path=full,
                            details={"old_hash": old_content, "new_hash": new_content},
                        )
                    )
                    self._settle_ui_status(
                        file_row,
                        newly_added=rel in newly_added_paths,
                        is_baseline=hash_baseline,
                        mismatch=True,
                        compared=True,
                    )
                else:
                    # gate skip / hash match / первый хеш без old
                    self._settle_ui_status(
                        file_row,
                        newly_added=rel in newly_added_paths,
                        is_baseline=hash_baseline,
                        mismatch=False,
                        compared=bool(old_content),
                    )
                settled_rels.add(rel)

        # Unselected / без пути / missing на первом hash-settle → ok (кроме .!qB).
        if hash_baseline:
            for row in rows:
                if row.relative_path in settled_rels:
                    continue
                if row.full_path and is_incomplete_path(Path(row.full_path)):
                    continue
                self._settle_ui_status(
                    row,
                    newly_added=row.relative_path in newly_added_paths,
                    is_baseline=True,
                    mismatch=False,
                    compared=False,
                )
            self._db.commit()
        elif to_hash:
            self._db.commit()

        # Orphan только под корнем контента торрента (не общий save_path года).
        known_paths = {r.full_path for r in rows if r.full_path}
        orphan_root = resolve_orphan_scan_root(
            save_path=save_path,
            content_path=content_path,
            known_full_paths=known_paths,
            media_root=media_root,
        )
        if orphan_root is not None:
            hash_phase_changes.extend(
                self._find_orphans_under_root(
                    root=orphan_root,
                    known_full_paths=known_paths,
                    media_root=media_root,
                )
            )

        result.changes.extend(hash_phase_changes)
        filtered_changes = self._filter_duplicate_changes(
            torrent_id=torrent_id,
            info_hash=prepared.normalized_hash,
            changes=hash_phase_changes,
        )
        hash_events = self._persist_events(
            release_id=release_id,
            torrent_id=torrent_id,
            info_hash=prepared.normalized_hash,
            changes=filtered_changes,
        )
        if notify and hash_baseline:
            baseline_events = [e for e in synced.events if e.kind == KIND_ADDED]
            if not baseline_events:
                baseline_events = self._load_added_events_for_hash(
                    torrent_id=torrent_id,
                    info_hash=prepared.normalized_hash,
                )
            if baseline_events:
                self._maybe_notify_telegram(
                    release_id=release_id,
                    torrent_id=torrent_id,
                    events=baseline_events,
                    archive=prepared.archive,
                    baseline=True,
                )
        elif hash_events and notify:
            self._maybe_notify_telegram(
                release_id=release_id,
                torrent_id=torrent_id,
                events=hash_events,
                archive=prepared.archive,
                baseline=False,
            )
        return result

    @staticmethod
    def _is_hash_settle_baseline(rows: list[TorrentFile]) -> bool:
        """Первый hash-settle версии: ещё нет sticky ok/changed (early sync оставил new)."""
        if not rows:
            return True
        settled = {UI_STATUS_OK, UI_STATUS_CHANGED}
        return not any((row.ui_status or "").strip().lower() in settled for row in rows)

    @staticmethod
    def _settle_ui_status(
        row: TorrentFile,
        *,
        newly_added: bool,
        is_baseline: bool,
        mismatch: bool,
        compared: bool,
    ) -> None:
        """Sticky ui_status: new/changed не откатываем; ok — только сравнение с прошлой версией.

        - incremental added → new навсегда
        - baseline после первого hash → ok (снимок принят)
        - mismatch у не-new → changed навсегда
        - match у не-new/не-changed → ok
        """
        current = (row.ui_status or UI_STATUS_OK).strip().lower()
        if newly_added and not is_baseline:
            row.ui_status = UI_STATUS_NEW
            return
        if current == UI_STATUS_NEW and not is_baseline:
            # Incremental «новый» — финальный для этого торрента.
            return
        if mismatch and compared:
            row.ui_status = UI_STATUS_CHANGED
            return
        if current == UI_STATUS_CHANGED:
            return
        if is_baseline or compared:
            row.ui_status = UI_STATUS_OK
            return
        # Первый хеш без old_hash на уже существующей строке — фиксируем ok.
        if current not in _STICKY_FINAL:
            row.ui_status = UI_STATUS_OK

    def _prepare_track(
        self,
        *,
        info_hash: str,
        torrent_id: int,
        torrent_bytes: bytes | None,
    ) -> _PreparedTrack:
        normalized_hash = (info_hash or "").strip().lower()
        if not normalized_hash:
            return _PreparedTrack(
                normalized_hash="",
                torrent_bytes=b"",
                archive=None,
                skipped_reason="пустой info_hash",
            )

        # Строго эта версия по info_hash; fallback — активная запись torrent_id.
        archive = self._db.scalar(
            select(TorrentArchive)
            .where(TorrentArchive.info_hash == normalized_hash)
            .order_by(TorrentArchive.superseded.asc(), TorrentArchive.id.desc())
            .limit(1)
        )
        if archive is None:
            archive = self._db.scalar(
                select(TorrentArchive)
                .where(
                    TorrentArchive.torrent_id == torrent_id,
                    TorrentArchive.superseded.is_(False),
                )
                .order_by(TorrentArchive.id.desc())
                .limit(1)
            )
        if archive is not None and not archive.api_present:
            self._log(f"hash_torrent: пропуск {normalized_hash[:12]}… — api_present=false", "debug")
            return _PreparedTrack(
                normalized_hash=normalized_hash,
                torrent_bytes=b"",
                archive=archive,
                skipped_reason="торрент не api_present (архивный)",
            )

        if torrent_bytes is None:
            torrent_bytes = self._load_torrent_bytes(archive, torrent_id=torrent_id, info_hash=normalized_hash)
        if torrent_bytes is None:
            self._log(f"hash_torrent: нет файла для {normalized_hash[:12]}…", "warning")
            return _PreparedTrack(
                normalized_hash=normalized_hash,
                torrent_bytes=b"",
                archive=archive,
                skipped_reason="нет .torrent в архиве",
            )
        return _PreparedTrack(
            normalized_hash=normalized_hash,
            torrent_bytes=torrent_bytes,
            archive=archive,
        )

    def _sync_composition(
        self,
        *,
        normalized_hash: str,
        torrent_id: int,
        release_id: int,
        torrent_bytes: bytes,
    ) -> _CompositionSync:
        """Upsert torrent_files и сразу пишет added/removed (heal, если events потеряны)."""
        result = TrackTorrentResult()
        file_metas = parse_torrent_file_list(torrent_bytes)
        save_path, content_path, priorities = self._qb_paths_and_priorities(normalized_hash)

        previous = {
            row.relative_path: row
            for row in self._db.scalars(
                select(TorrentFile).where(TorrentFile.info_hash == normalized_hash)
            ).all()
        }
        previous_paths = set(previous.keys())
        # Baseline: нет строк ИЛИ строки есть (inventory/stop), но added ещё не писали.
        had_added_events = self._torrent_has_kind(
            torrent_id=torrent_id, info_hash=normalized_hash, kind=KIND_ADDED
        )
        is_baseline = not previous_paths or not had_added_events
        current_paths: set[str] = set()
        now = datetime.utcnow()
        media_root = resolve_media_root()
        composition_changes: list[FileChange] = []
        newly_added_paths: set[str] = set()

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
                    ui_status=UI_STATUS_NEW,
                    created_at=now,
                    updated_at=now,
                )
                self._db.add(row)
                composition_changes.append(
                    FileChange(kind=KIND_ADDED, relative_path=meta.relative_path, full_path=full_path)
                )
                newly_added_paths.add(meta.relative_path)
            else:
                row.torrent_id = torrent_id
                row.release_id = release_id
                row.size = meta.size
                row.file_index = meta.file_index
                row.selected = selected
                row.full_path = full_path
                row.updated_at = now
                # Heal: inventory/stop успели записать torrent_files без события added.
                if not self._has_prior_event(
                    torrent_id=torrent_id,
                    info_hash=normalized_hash,
                    kind=KIND_ADDED,
                    relative_path=meta.relative_path,
                    full_path=full_path,
                ):
                    row.ui_status = UI_STATUS_NEW
                    composition_changes.append(
                        FileChange(
                            kind=KIND_ADDED,
                            relative_path=meta.relative_path,
                            full_path=full_path,
                        )
                    )
                    newly_added_paths.add(meta.relative_path)
            result.files_upserted += 1

        removed_paths = previous_paths - current_paths
        for rel in removed_paths:
            old = previous[rel]
            composition_changes.append(
                FileChange(
                    kind=KIND_REMOVED,
                    relative_path=rel,
                    full_path=old.full_path,
                )
            )
            self._db.delete(old)

        self._db.commit()
        result.changes.extend(composition_changes)
        events = self._persist_events(
            release_id=release_id,
            torrent_id=torrent_id,
            info_hash=normalized_hash,
            changes=composition_changes,
        )
        return _CompositionSync(
            result=result,
            events=events,
            is_baseline=is_baseline,
            save_path=save_path,
            content_path=content_path,
            newly_added_paths=newly_added_paths,
        )

    def _torrent_has_kind(self, *, torrent_id: int, info_hash: str, kind: str) -> bool:
        """Есть ли событие kind для этой версии торрента (строго по info_hash)."""
        normalized = (info_hash or "").strip().lower()
        stmt = select(FileChangeEvent.id).where(
            FileChangeEvent.torrent_id == torrent_id,
            FileChangeEvent.kind == kind,
        )
        if normalized:
            # Не учитываем legacy NULL: они относятся к неизвестной/старой версии.
            stmt = stmt.where(FileChangeEvent.info_hash == normalized)
        else:
            stmt = stmt.where(FileChangeEvent.info_hash.is_(None))
        return self._db.scalar(stmt.limit(1)) is not None

    def _find_orphans_under_root(
        self,
        *,
        root: Path,
        known_full_paths: set[str | None],
        media_root: Path,
    ) -> list[FileChange]:
        try:
            base = root.resolve()
        except OSError:
            return []
        if not is_under_media_root(base, media_root=media_root):
            return []
        if not base.is_dir():
            return []
        known = {Path(p).resolve() for p in known_full_paths if p}
        changes: list[FileChange] = []
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if path.name.endswith(".!qB"):
                continue
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in known:
                continue
            if not is_under_media_root(resolved, media_root=media_root):
                continue
            try:
                resolved.relative_to(base)
            except ValueError:
                continue
            changes.append(
                FileChange(kind=KIND_ORPHAN, relative_path=None, full_path=str(resolved))
            )
        return changes

    def _filter_duplicate_changes(
        self,
        *,
        torrent_id: int,
        info_hash: str,
        changes: list[FileChange],
    ) -> list[FileChange]:
        """Не дублировать повторные missing/modified/removed/orphan для того же пути."""
        dedup_kinds = {KIND_MISSING, KIND_MODIFIED, KIND_REMOVED, KIND_ORPHAN}
        filtered: list[FileChange] = []
        for change in changes:
            if change.kind not in dedup_kinds:
                filtered.append(change)
                continue
            if not self._has_prior_event(
                torrent_id=torrent_id,
                info_hash=info_hash,
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
        info_hash: str | None = None,
        kind: str,
        relative_path: str | None,
        full_path: str | None,
    ) -> bool:
        stmt = select(FileChangeEvent).where(
            FileChangeEvent.torrent_id == torrent_id,
            FileChangeEvent.kind == kind,
        )
        normalized = (info_hash or "").strip().lower()
        if normalized:
            # Строго эта версия: legacy NULL не смешиваем с новым info_hash.
            stmt = stmt.where(FileChangeEvent.info_hash == normalized)
        else:
            stmt = stmt.where(FileChangeEvent.info_hash.is_(None))
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
        info_hash: str | None = None,
    ) -> list[FileChangeEvent]:
        if not changes:
            return []
        now = datetime.utcnow()
        normalized_hash = (info_hash or "").strip().lower() or None
        events: list[FileChangeEvent] = []
        for change in changes:
            if change.kind not in EVENT_KINDS:
                continue
            event = FileChangeEvent(
                release_id=release_id,
                torrent_id=torrent_id,
                info_hash=normalized_hash,
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

    def _load_added_events_for_hash(
        self,
        *,
        torrent_id: int,
        info_hash: str,
    ) -> list[FileChangeEvent]:
        """Уникальные added по relative_path для baseline-уведомления после early sync."""
        normalized = (info_hash or "").strip().lower()
        if not normalized:
            return []
        rows = list(
            self._db.scalars(
                select(FileChangeEvent)
                .where(
                    FileChangeEvent.torrent_id == torrent_id,
                    FileChangeEvent.info_hash == normalized,
                    FileChangeEvent.kind == KIND_ADDED,
                )
                .order_by(FileChangeEvent.id.asc())
            ).all()
        )
        seen: set[str] = set()
        unique: list[FileChangeEvent] = []
        for row in rows:
            key = row.relative_path or row.full_path or ""
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(row)
        return unique

    def _load_torrent_bytes(
        self,
        archive: TorrentArchive | None,
        *,
        torrent_id: int,
        info_hash: str,
    ) -> bytes | None:
        normalized = (info_hash or "").strip().lower()
        # Всегда предпочитаем .torrent именно этой версии (info_hash), не активный другой hash.
        by_hash = self._db.scalar(
            select(TorrentArchive)
            .where(TorrentArchive.info_hash == normalized)
            .order_by(TorrentArchive.superseded.asc(), TorrentArchive.id.desc())
            .limit(1)
        )
        if by_hash is not None:
            archive = by_hash
        elif archive is None:
            archive = self._db.scalar(
                select(TorrentArchive)
                .where(
                    TorrentArchive.torrent_id == torrent_id,
                    TorrentArchive.superseded.is_(False),
                )
                .order_by(TorrentArchive.id.desc())
                .limit(1)
            )
        if archive is None:
            return None
        # Если archive от другого hash — не подставляем чужой файл.
        if (archive.info_hash or "").strip().lower() != normalized:
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


def resolve_orphan_scan_root(
    *,
    save_path: str | None,
    content_path: str | None,
    known_full_paths: set[str | None] | set[str],
    media_root: Path,
) -> Path | None:
    """Корень для orphan-скана: папка контента торрента, не общий save_path категории.

    qB часто ставит save_path=/anilibria/2012 на все раздачи года — обход этого каталога
    помечал бы чужие тайтлы как orphan текущего торрента.
    """
    try:
        media = media_root.resolve()
    except OSError:
        media = media_root

    save_resolved: Path | None = None
    if save_path and save_path.strip():
        try:
            save_resolved = Path(save_path).resolve()
        except OSError:
            save_resolved = Path(save_path)

    known_files: list[Path] = []
    for raw in known_full_paths:
        if not raw:
            continue
        try:
            known_files.append(Path(raw).resolve())
        except OSError:
            known_files.append(Path(raw))
    known_strs = {str(p) for p in known_files}

    if content_path and content_path.strip():
        try:
            content = Path(content_path).resolve()
        except OSError:
            content = Path(content_path)
        content_is_file = False
        try:
            if content.exists():
                content_is_file = content.is_file()
            else:
                # Медиа может быть не смонтировано (UI): файл = путь из known.
                content_is_file = str(content) in known_strs
        except OSError:
            content_is_file = str(content) in known_strs
        if content_is_file:
            return None
        if is_under_media_root(content, media_root=media) and content != media:
            return content

    if not known_files:
        return None

    try:
        common = Path(os.path.commonpath([str(p) for p in known_files]))
    except ValueError:
        return None

    # commonpath одного файла = сам файл; нескольких под Show/ = каталог Show
    # (без exists(): на UI диск может быть недоступен).
    root = common.parent if str(common) in known_strs else common
    try:
        root = root.resolve()
    except OSError:
        pass
    if not is_under_media_root(root, media_root=media) or root == media:
        return None

    # Корень совпал с общим save_path категории (год) — слишком широко, кроме случая
    # когда qB save_path уже = папка этого торрента (content_path совпадает).
    if save_resolved is not None and root == save_resolved:
        content_ok = False
        if content_path and content_path.strip():
            try:
                content_ok = Path(content_path).resolve() == root
            except OSError:
                content_ok = Path(content_path) == root
        if not content_ok:
            return None
    return root


def update_api_present_for_release(
    db: Session,
    release_id: int,
    present_torrent_ids: set[int],
) -> dict[str, int]:
    """Актуальные torrent_id → api_present=True; остальные по релизу → False.

    superseded-версии (заменённый info_hash) всегда остаются api_present=False.
    """
    present = {int(x) for x in present_torrent_ids if x is not None}
    rows = list(db.scalars(select(TorrentArchive).where(TorrentArchive.release_id == release_id)).all())
    marked_true = 0
    marked_false = 0
    for row in rows:
        if bool(getattr(row, "superseded", False)):
            if row.api_present:
                row.api_present = False
                marked_false += 1
            continue
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
            TorrentArchive.superseded.is_(False),
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
    latest_kind: str | None = None,
    disk_hash: Any | None = None,
    hash_job_active: bool = False,
    in_torrent: bool = True,
    ui_status: str | None = None,
) -> str:
    """Бейдж для UI: sticky-статус торрента + временный «проверка».

    Финальные (не меняются после работы над торрентом):
    - new — файл добавлен в состав этого торрента
    - removed — файл убран из состава (событие removed)
    - changed — хеш разошёлся с предыдущей версией
    - ok — хеш совпал с предыдущей версией / baseline принят

    Временный:
    - checking — идёт hash_torrent (оверлей поверх ok/changed/new с известным hash)
    """
    del relative_path, full_path  # статус не от живого FS
    if not in_torrent or latest_kind == KIND_REMOVED:
        return UI_STATUS_REMOVED

    stored = (ui_status or "").strip().lower()
    if stored not in {UI_STATUS_NEW, UI_STATUS_OK, UI_STATUS_CHANGED}:
        if latest_kind == KIND_ADDED:
            stored = UI_STATUS_NEW
        elif latest_kind == KIND_MODIFIED:
            stored = UI_STATUS_CHANGED
        else:
            stored = UI_STATUS_OK

    if hash_job_active and stored != UI_STATUS_NEW:
        return UI_STATUS_CHECKING
    if hash_job_active and stored == UI_STATUS_NEW and _has_stored_content_hash(disk_hash):
        # Перекачка уже учтённого «нового» — кратко «проверка».
        return UI_STATUS_CHECKING
    return stored


def _has_stored_content_hash(disk_hash: Any | None) -> bool:
    if disk_hash is None:
        return False
    return bool(getattr(disk_hash, "content_hash", None) or "")
