"""Трекинг файлов торрента: upsert torrent_files, BLAKE3, diff A/B/C, TG."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import timedelta
from app.utils.datetime_fmt import utcnow
from pathlib import Path
from typing import Any

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
    complete_path_for,
    extract_qb_content_path,
    extract_qb_file_priorities,
    extract_qb_save_path,
    is_incomplete_path,
    is_junk_file,
    is_partial_only,
    is_under_media_root,
    normalize_rel_path,
    parse_torrent_file_list,
    path_exists_including_incomplete,
    resolve_full_path,
    resolve_media_root,
)
from app.services.qbittorrent import ensure_announce_passkey
from app.services.runtime_settings import get_setting_value

logger = logging.getLogger(__name__)

# Догон TG только для свежих unnotified (не архивная история лет).
_UNNOTIFIED_EVENT_WINDOW = timedelta(days=30)

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
    # Есть ли предыдущая версия этого torrent_id (для сравнения new/ok/changed).
    has_prior_version: bool
    save_path: str | None
    content_path: str | None
    # Пути, которых не было в предыдущей версии → sticky «новый».
    first_seen_paths: set[str] = field(default_factory=set)
    # До sync в составе уже были ok/changed — mixed baseline (не partial backfill).
    baseline_had_known: bool = False


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
        """Только состав: upsert torrent_files (без BLAKE3).

        На master_added вызывается с notify=False: UI видит «новый», но
        file_change_events НЕ пишем — иначе hash_torrent не найдёт новых
        added и Telegram молчит. Events + TG — в track_torrent / hash_torrent.
        """
        prepared = self._prepare_track(info_hash=info_hash, torrent_id=torrent_id, torrent_bytes=torrent_bytes)
        if prepared.skipped_reason:
            return TrackTorrentResult(skipped_reason=prepared.skipped_reason)
        # notify=False (early sync) → только строки torrent_files, без events.
        synced = self._sync_composition(
            normalized_hash=prepared.normalized_hash,
            torrent_id=torrent_id,
            release_id=release_id,
            torrent_bytes=prepared.torrent_bytes,
            persist_events=notify,
        )
        if synced.events and notify:
            self._maybe_notify_telegram(
                release_id=release_id,
                torrent_id=torrent_id,
                events=synced.events,
                archive=prepared.archive,
                baseline=not synced.has_prior_version and not synced.baseline_had_known,
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
        first_seen_paths = set(synced.first_seen_paths)
        has_prior_version = synced.has_prior_version

        media_root = resolve_media_root()
        hash_phase_changes: list[FileChange] = []

        # Diff B/C: хеш и missing для выбранных
        rows = list(
            self._db.scalars(
                select(TorrentFile).where(TorrentFile.info_hash == prepared.normalized_hash)
            ).all()
        )
        rows_by_rel = {row.relative_path: row for row in rows}

        # Хеши предыдущей версии этого torrent_id (rel → content_hash) до перезаписи диска.
        prior_hash = self._prior_version_hash(
            torrent_id=torrent_id, current_hash=prepared.normalized_hash
        )
        prior_hash_by_rel = self._prior_version_hashes(prior_hash=prior_hash or "")
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
        is_baseline = not has_prior_version
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
                new_row = self._db.scalar(
                    select(DiskFileHash).where(DiskFileHash.full_path == full).limit(1)
                )
                new_content = (new_row.content_hash if new_row else None) or ""
                file_row = rows_by_rel.get(rel)
                if file_row is None:
                    continue
                # Сравниваем с хешем этого файла в предыдущей версии торрента.
                prior_content = prior_hash_by_rel.get(rel) or old_hashes.get(full) or ""
                first_seen = rel in first_seen_paths
                mismatch = bool(prior_content and new_content and new_content != prior_content)
                matched = bool(prior_content and new_content and new_content == prior_content)
                if mismatch:
                    hash_phase_changes.append(
                        FileChange(
                            kind=KIND_MODIFIED,
                            relative_path=rel,
                            full_path=full,
                            details={"old_hash": prior_content, "new_hash": new_content},
                        )
                    )
                self._settle_ui_status(
                    file_row,
                    first_seen=first_seen,
                    mismatch=mismatch,
                    matched=matched,
                    is_baseline=is_baseline,
                )
                settled_rels.add(rel)

        # Первый торрент: после hash-settle добавления остаются new (финальный статус версии).
        # Unselected / без пути / missing: settle без mismatch не трогает sticky new.
        # .!qB не трогаем: provisional уже выставлен.
        if is_baseline:
            for row in rows:
                if row.relative_path in settled_rels:
                    continue
                if row.full_path and is_incomplete_path(Path(row.full_path)):
                    continue
                self._settle_ui_status(
                    row,
                    first_seen=False,
                    mismatch=False,
                    matched=False,
                    is_baseline=True,
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
        # Одно уведомление на прогон:
        # - чистый baseline (нет prior и не mixed) → сводка «файлы в базе»
        # - prior или mixed (уже были ok/changed) → «изменения файлов» (➕ новый эпизод)
        all_events = list(synced.events) + list(hash_events)
        # Early sync мог уже записать added без TG — подтянем notified_at IS NULL.
        pending = self._unnotified_events(
            torrent_id=torrent_id,
            info_hash=prepared.normalized_hash,
            kinds={KIND_ADDED, KIND_REMOVED},
        )
        seen_ids = {ev.id for ev in all_events if getattr(ev, "id", None) is not None}
        for ev in pending:
            if ev.id not in seen_ids:
                all_events.append(ev)
                seen_ids.add(ev.id)
        if notify and all_events:
            self._maybe_notify_telegram(
                release_id=release_id,
                torrent_id=torrent_id,
                events=all_events,
                archive=prepared.archive,
                baseline=not has_prior_version and not synced.baseline_had_known,
            )
        return result

    @staticmethod
    def _settle_ui_status(
        row: TorrentFile,
        *,
        first_seen: bool,
        mismatch: bool,
        matched: bool,
        is_baseline: bool = False,
    ) -> None:
        """Sticky ui_status относительно ПРЕДЫДУЩЕЙ версии торрента.

        - new — файл добавлен ЭТОЙ версией (не было в prior) → финальный
        - changed — хеш разошёлся с прошлой версией → финальный
        - ok — файл был в прошлой версии и хеш совпал
        - первый торрент (нет prior): все — new; после hash остаются new
        """
        current = (row.ui_status or "").strip().lower()
        if is_baseline:
            if mismatch:
                row.ui_status = UI_STATUS_CHANGED
                return
            # Добавления первого торрента — финальный new, не сбрасываем в ok.
            if current == UI_STATUS_NEW:
                return
            if current != UI_STATUS_CHANGED:
                row.ui_status = UI_STATUS_OK
            return
        if current in _STICKY_FINAL:
            # new/changed относительно prior — финальные, не понижаем.
            return
        if first_seen:
            row.ui_status = UI_STATUS_NEW
            return
        if mismatch:
            row.ui_status = UI_STATUS_CHANGED
            return
        if matched:
            row.ui_status = UI_STATUS_OK
            return
        # Нет данных для сравнения → оставляем текущий статус как есть.

    def _prior_version_hash(self, *, torrent_id: int, current_hash: str) -> str | None:
        """info_hash самой свежей предыдущей версии этого torrent_id (или None).

        Предпочитаем prior, у которого уже есть состав в torrent_files — иначе
        first_seen_for_path при пустом составе считает всё «не новым», а UI
        теряет реальные added; пустой prior пропускаем в пользу более старого
        с файлами.
        """
        normalized = (current_hash or "").strip().lower()
        candidates = list(
            self._db.scalars(
                select(TorrentArchive.info_hash)
                .where(
                    TorrentArchive.torrent_id == torrent_id,
                    TorrentArchive.info_hash != normalized,
                )
                .order_by(TorrentArchive.id.desc())
            ).all()
        )
        if not candidates:
            return None
        normalized_candidates = [
            h.strip().lower() for h in candidates if isinstance(h, str) and h.strip()
        ]
        if not normalized_candidates:
            return None
        # Есть ли состав у кандидатов (один запрос).
        with_files = {
            (row or "").strip().lower()
            for row in self._db.scalars(
                select(TorrentFile.info_hash)
                .where(TorrentFile.info_hash.in_(normalized_candidates))
                .distinct()
            ).all()
            if isinstance(row, str) and row
        }
        for prior in normalized_candidates:
            if prior in with_files:
                return prior
        # Состава ни у кого нет — всё равно вернём самый свежий (has_prior=True, paths пустые).
        return normalized_candidates[0]

    def _prior_version_paths(self, *, prior_hash: str) -> set[str]:
        """relative_path состава предыдущей версии (для определения «новый»)."""
        return set(self._prior_version_files(prior_hash=prior_hash).keys())

    def _prior_version_files(self, *, prior_hash: str) -> dict[str, str | None]:
        """relative_path → full_path состава предыдущей версии."""
        if not prior_hash:
            return {}
        result: dict[str, str | None] = {}
        for row in self._db.scalars(
            select(TorrentFile).where(TorrentFile.info_hash == prior_hash)
        ).all():
            if not row.relative_path:
                continue
            result[normalize_rel_path(row.relative_path)] = row.full_path
        return result

    def prior_version_composition(
        self, *, torrent_id: int, info_hash: str
    ) -> tuple[bool, set[str]]:
        """(есть_прошлая_версия, состав_прошлой_версии) для расчёта first_seen вне track."""
        prior_hash = self._prior_version_hash(torrent_id=torrent_id, current_hash=info_hash)
        if prior_hash is None:
            return False, set()
        return True, self._prior_version_paths(prior_hash=prior_hash)

    @staticmethod
    def first_seen_for_path(
        *, has_prior_version: bool, prior_version_paths: set[str], relative_path: str
    ) -> bool:
        """«Новый» = достоверный состав прошлой версии есть и файла там не было.

        Нет прошлой версии вовсе → не first_seen по составу: provisional статус
        считает _baseline_provisional_status (диск + хэш в БД без .!qB).
        Прошлая версия есть, но состав неизвестен → считаем, что файл был (не «новый»).
        """
        if not has_prior_version:
            return False
        if not prior_version_paths:
            return False
        return normalize_rel_path(relative_path) not in prior_version_paths

    @staticmethod
    def _canonical_full_path(full_path: str) -> str:
        return str(complete_path_for(full_path))

    def _load_hashed_canonical_paths(self, full_paths: list[str]) -> set[str]:
        """full_path (без .!qB), для которых уже есть content_hash в disk_file_hashes."""
        canonicals = {self._canonical_full_path(p) for p in full_paths if p}
        if not canonicals:
            return set()
        return {
            row
            for row in self._db.scalars(
                select(DiskFileHash.full_path).where(
                    DiskFileHash.full_path.in_(list(canonicals)),
                    DiskFileHash.content_hash.is_not(None),
                )
            ).all()
            if isinstance(row, str) and row
        }

    @staticmethod
    def _ui_status_is_known(ui_status: str | None) -> bool:
        return (ui_status or "").strip().lower() in {UI_STATUS_OK, UI_STATUS_CHANGED}

    @classmethod
    def _baseline_has_known_among(
        cls,
        rows: object,
        *,
        exclude_rel: str | None = None,
        hashed_paths: set[str] | None = None,
    ) -> bool:
        """Есть ли среди строк ok/changed с реальным content_hash (не provisional early ok).

        hashed_paths — canonical full_path с хэшем в disk_file_hashes.
        Без хэша provisional ok с диска не считается mixed (иначе после early sync
        hash_torrent залипает в sticky new).
        """
        exclude = normalize_rel_path(exclude_rel) if exclude_rel else None
        for row in rows:
            if exclude is not None:
                rel = normalize_rel_path(getattr(row, "relative_path", "") or "")
                if rel == exclude:
                    continue
            if not cls._ui_status_is_known(getattr(row, "ui_status", None)):
                continue
            if hashed_paths is None:
                return True
            full = getattr(row, "full_path", None) or ""
            if not full:
                continue
            if cls._canonical_full_path(full) in hashed_paths:
                return True
        return False

    def _baseline_provisional_status(
        self,
        full_path: str | None,
        *,
        hashed_paths: set[str],
        mixed: bool = False,
    ) -> str:
        """Первый торрент torrent_id: provisional ui_status до hash-settle.

        Чистый baseline (нет prior / ещё не было ok|changed с хэшем):
        - все файлы → new (добавления ЭТОЙ версии; «ok» только vs prior)

        Mixed baseline (уже были известные ok/changed с хэшем):
        - есть хэш → ok
        - без хэша → new
        """
        if not mixed:
            return UI_STATUS_NEW
        if not full_path:
            return UI_STATUS_NEW
        canonical = self._canonical_full_path(full_path)
        if canonical in hashed_paths:
            return UI_STATUS_OK
        return UI_STATUS_NEW

    def _prior_version_hashes(self, *, prior_hash: str) -> dict[str, str]:
        """relative_path → content_hash файлов предыдущей версии торрента (один IN-запрос)."""
        if not prior_hash:
            return {}
        rel_by_path: dict[str, str] = {
            row.full_path: normalize_rel_path(row.relative_path)
            for row in self._db.scalars(
                select(TorrentFile).where(TorrentFile.info_hash == prior_hash)
            ).all()
            if row.full_path and row.relative_path
        }
        if not rel_by_path:
            return {}
        result: dict[str, str] = {}
        for dh in self._db.scalars(
            select(DiskFileHash).where(DiskFileHash.full_path.in_(list(rel_by_path.keys())))
        ).all():
            if dh.content_hash:
                result[rel_by_path[dh.full_path]] = dh.content_hash
        return result

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
        persist_events: bool = True,
    ) -> _CompositionSync:
        """Upsert torrent_files; опционально пишет added/removed (heal, если events потеряны)."""
        result = TrackTorrentResult()
        file_metas = parse_torrent_file_list(torrent_bytes)
        save_path, content_path, priorities = self._qb_paths_and_priorities(normalized_hash)

        previous = {
            normalize_rel_path(row.relative_path): row
            for row in self._db.scalars(
                select(TorrentFile).where(TorrentFile.info_hash == normalized_hash)
            ).all()
        }
        previous_paths = set(previous.keys())
        # Предыдущая версия этого torrent_id (факт наличия — по архиву, не по составу).
        prior_hash = self._prior_version_hash(torrent_id=torrent_id, current_hash=normalized_hash)
        has_prior_version = prior_hash is not None
        prior_files = self._prior_version_files(prior_hash=prior_hash) if prior_hash else {}
        prior_version_paths = set(prior_files.keys())
        current_paths: set[str] = set()
        now = utcnow()
        media_root = resolve_media_root()
        composition_changes: list[FileChange] = []
        first_seen_paths: set[str] = set()
        status_new = 0
        status_ok = 0

        # Сначала резолвим пути — для baseline нужен batched lookup хэшей без .!qB.
        prepared_metas: list[tuple[Any, str, bool, str | None, bool]] = []
        resolved_for_hash_lookup: list[str] = []
        for meta in file_metas:
            rel_norm = normalize_rel_path(meta.relative_path)
            current_paths.add(rel_norm)
            first_seen = self.first_seen_for_path(
                has_prior_version=has_prior_version,
                prior_version_paths=prior_version_paths,
                relative_path=rel_norm,
            )
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
                    resolved_for_hash_lookup.append(full_path)
            prepared_metas.append((meta, rel_norm, first_seen, full_path, selected))

        hashed_paths = (
            set()
            if has_prior_version
            else self._load_hashed_canonical_paths(resolved_for_hash_lookup)
        )
        # Mixed: до sync уже были ok/changed С хэшем. Provisional ok без hash
        # (early sync) не считается — иначе sticky new после первого hash_torrent.
        baseline_had_known = self._baseline_has_known_among(
            previous.values(), hashed_paths=hashed_paths
        )

        for meta, rel_norm, first_seen, full_path, selected in prepared_metas:
            if has_prior_version:
                initial_status = UI_STATUS_NEW if first_seen else UI_STATUS_OK
            else:
                initial_status = self._baseline_provisional_status(
                    full_path,
                    hashed_paths=hashed_paths,
                    mixed=baseline_had_known,
                )
                if baseline_had_known:
                    first_seen = initial_status == UI_STATUS_NEW
                else:
                    # Чистый baseline: все файлы — added этой версии; после hash остаются new.
                    first_seen = True

            if initial_status == UI_STATUS_NEW:
                status_new += 1
            else:
                status_ok += 1

            row = previous.get(meta.relative_path) or previous.get(rel_norm)
            if row is None:
                # Новый файл vs прошлая версия → «новый»; иначе provisional по диску/хэшу.
                row = TorrentFile(
                    torrent_id=torrent_id,
                    info_hash=normalized_hash,
                    release_id=release_id,
                    relative_path=rel_norm,
                    size=meta.size,
                    file_index=meta.file_index,
                    selected=selected,
                    full_path=full_path,
                    ui_status=initial_status,
                    created_at=now,
                    updated_at=now,
                )
                self._db.add(row)
                if first_seen:
                    first_seen_paths.add(rel_norm)
                    composition_changes.append(
                        FileChange(
                            kind=KIND_ADDED, relative_path=rel_norm, full_path=full_path
                        )
                    )
            else:
                row.torrent_id = torrent_id
                row.release_id = release_id
                row.size = meta.size
                row.file_index = meta.file_index
                row.selected = selected
                row.full_path = full_path
                row.relative_path = rel_norm
                row.updated_at = now
                if first_seen:
                    first_seen_paths.add(rel_norm)
                    cur = (row.ui_status or "").strip().lower()
                    if has_prior_version:
                        # Incremental: файла не было в prior → sticky new.
                        if cur not in _STICKY_FINAL:
                            row.ui_status = UI_STATUS_NEW
                    elif baseline_had_known:
                        # Mixed: sticky new среди уже известных (с хэшем).
                        if cur not in _STICKY_FINAL:
                            siblings_known = self._baseline_has_known_among(
                                previous.values(),
                                exclude_rel=rel_norm,
                                hashed_paths=hashed_paths,
                            )
                            if cur != UI_STATUS_OK or siblings_known:
                                row.ui_status = UI_STATUS_NEW
                    else:
                        # Чистый baseline: sticky new/changed не затираем.
                        if cur not in _STICKY_FINAL:
                            row.ui_status = UI_STATUS_NEW
                    # Heal: inventory/stop успели записать строку без события added.
                    if not self._has_prior_event(
                        torrent_id=torrent_id,
                        info_hash=normalized_hash,
                        kind=KIND_ADDED,
                        relative_path=rel_norm,
                        full_path=full_path,
                    ):
                        composition_changes.append(
                            FileChange(
                                kind=KIND_ADDED,
                                relative_path=rel_norm,
                                full_path=full_path,
                            )
                        )
                elif not has_prior_version:
                    # Re-sync baseline: sticky new/changed; provisional только если пусто.
                    cur = (row.ui_status or "").strip().lower()
                    if cur not in _STICKY_FINAL:
                        row.ui_status = initial_status
            result.files_upserted += 1

        self._log(
            f"sync_composition hash={normalized_hash[:12]}… torrent_id={torrent_id}: "
            f"prior={'yes:' + (prior_hash or '')[:12] + '…' if has_prior_version else 'no'} "
            f"prior_paths={len(prior_version_paths)} files={len(file_metas)} "
            f"ui_new={status_new} ui_ok={status_ok} first_seen={len(first_seen_paths)} "
            f"hashed_known={len(hashed_paths)} baseline_had_known={baseline_had_known} "
            f"sample_prior={sorted(prior_version_paths)[:3]!r} "
            f"sample_cur={sorted(current_paths)[:3]!r}",
            "info",
        )

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

        # Смена версии: путь был в immediate prior, в новом составе нет → removed
        # только у ЭТОЙ версии (info_hash). Следующая версия сравнивает уже с ней —
        # старое удаление не повторяется. Пример: file__1 ушёл на v2, на v3 его нет в UI.
        if has_prior_version:
            for rel in sorted(set(prior_files) - current_paths - removed_paths):
                if self._has_prior_event(
                    torrent_id=torrent_id,
                    info_hash=normalized_hash,
                    kind=KIND_REMOVED,
                    relative_path=rel,
                    full_path=prior_files.get(rel),
                ):
                    continue
                composition_changes.append(
                    FileChange(
                        kind=KIND_REMOVED,
                        relative_path=rel,
                        full_path=prior_files.get(rel),
                    )
                )

        self._db.commit()
        result.changes.extend(composition_changes)
        events: list[FileChangeEvent] = []
        if persist_events:
            events = self._persist_events(
                release_id=release_id,
                torrent_id=torrent_id,
                info_hash=normalized_hash,
                changes=composition_changes,
            )
        return _CompositionSync(
            result=result,
            events=events,
            has_prior_version=has_prior_version,
            save_path=save_path,
            content_path=content_path,
            first_seen_paths=first_seen_paths,
            baseline_had_known=baseline_had_known,
        )

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
            if is_junk_file(path):
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

    def _unnotified_events(
        self,
        *,
        torrent_id: int,
        info_hash: str,
        kinds: set[str],
    ) -> list[FileChangeEvent]:
        """Свежие события этой версии торрента, ещё не уходившие в Telegram."""
        normalized = (info_hash or "").strip().lower()
        if not normalized or not kinds:
            return []
        since = utcnow() - _UNNOTIFIED_EVENT_WINDOW
        return list(
            self._db.scalars(
                select(FileChangeEvent)
                .where(
                    FileChangeEvent.torrent_id == torrent_id,
                    FileChangeEvent.info_hash == normalized,
                    FileChangeEvent.kind.in_(sorted(kinds)),
                    FileChangeEvent.notified_at.is_(None),
                    FileChangeEvent.created_at >= since,
                )
                .order_by(FileChangeEvent.id.asc())
            ).all()
        )

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
        now = utcnow()
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
    incomplete: bool | None = None,
) -> str:
    """Бейдж для UI: sticky-статус торрента + временный «проверка».

    Финальные (не меняются после работы над торрентом):
    - new — файл добавлен в состав этого торрента
    - removed — файл убран из состава (событие removed)
    - changed — хеш разошёлся с предыдущей версией
    - ok — хеш совпал с предыдущей версией / baseline (первый торрент) после settle

    Временный:
    - checking — идёт hash_torrent, либо известный файл снова в .!qB
      (кусок не сошёлся / докачка) — после complete + hash снова ok/changed
    """
    del relative_path  # только для сигнатуры/логов вызывающего
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

    is_inc = incomplete
    if is_inc is None and full_path:
        try:
            is_inc = is_partial_only(full_path)
        except OSError:
            is_inc = False
    # Известный контент временно снова .!qB → «проверка», не ok (sticky в БД не трогаем).
    if is_inc and stored in {UI_STATUS_OK, UI_STATUS_CHANGED}:
        return UI_STATUS_CHECKING

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
