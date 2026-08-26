"""Инвентаризация файлов из qB master под /anilibria (без хеширования)."""

from __future__ import annotations

from dataclasses import dataclass, field
from app.utils.datetime_fmt import utcnow
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import qbittorrentapi
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CleanupRule, DiskFileHash, QbClient, TorrentArchive, TorrentFile
from app.services.torrent_cleanup import match_cleanup_rule
from app.services.torrent_files_meta import (
    extract_qb_content_path,
    extract_qb_file_name,
    extract_qb_file_priorities,
    extract_qb_save_path,
    extract_qb_torrent_hash,
    is_under_media_root,
    normalize_rel_path,
    resolve_full_path,
    resolve_media_root,
)


@dataclass
class InventoryFile:
    info_hash: str
    torrent_id: int
    release_id: int
    relative_path: str
    size: int
    file_index: int
    selected: bool
    full_path: str
    folder_key: str


@dataclass
class InventoryResult:
    files: list[InventoryFile] = field(default_factory=list)
    valid_hashes: set[str] = field(default_factory=set)
    # torrents_files упал — prune не должен сносить уже сохранённые torrent_files/hashes.
    failed_hashes: set[str] = field(default_factory=set)
    skipped_invalid: int = 0
    skipped_path: int = 0
    torrents_under_root: int = 0


def connect_master(db: Session) -> qbittorrentapi.Client | None:
    master = db.scalar(
        select(QbClient).where(QbClient.role == "master", QbClient.enabled.is_(True)).limit(1)
    )
    if master is None:
        return None
    qb = qbittorrentapi.Client(
        host=master.host,
        port=master.port,
        username=master.username,
        password=master.password_encrypted,
    )
    qb.auth_log_in()
    return qb


def load_cleanup_rules(db: Session) -> list[CleanupRule]:
    """Правила для master (и both) — slave-only не влияют на inventory."""
    rows = db.scalars(select(CleanupRule).where(CleanupRule.enabled.is_(True))).all()
    return [r for r in rows if (r.target_client or "master") in {"master", "both"}]


def enrich_with_trackers(qb: qbittorrentapi.Client, torrents: list[Any]) -> list[Any]:
    enriched: list[Any] = []
    for torrent in torrents:
        info_hash = extract_qb_torrent_hash(torrent) or ""
        try:
            trackers = list(qb.torrents_trackers(torrent_hash=info_hash)) if info_hash else []
        except Exception:
            trackers = []
        enriched.append(
            SimpleNamespace(
                hash=info_hash,
                name=getattr(torrent, "name", info_hash),
                state_enum=getattr(torrent, "state_enum", None),
                trackers=trackers,
                save_path=extract_qb_save_path(torrent),
                content_path=extract_qb_content_path(torrent),
                progress=getattr(torrent, "progress", None),
                state=getattr(torrent, "state", None),
                _raw=torrent,
            )
        )
    return enriched


def archive_meta_by_hash(db: Session, info_hashes: set[str]) -> dict[str, tuple[int, int]]:
    """info_hash → (torrent_id, release_id) из torrent_archive."""
    if not info_hashes:
        return {}
    rows = db.scalars(
        select(TorrentArchive)
        .where(TorrentArchive.info_hash.in_(sorted(info_hashes)))
        .order_by(TorrentArchive.id.desc())
    ).all()
    result: dict[str, tuple[int, int]] = {}
    for row in rows:
        key = (row.info_hash or "").strip().lower()
        if key and key not in result:
            result[key] = (int(row.torrent_id), int(row.release_id))
    return result


def build_inventory(
    db: Session,
    qb: qbittorrentapi.Client,
    *,
    log_fn: Callable[[str], None] | None = None,
) -> InventoryResult:
    """Все файлы валидных раздач master под ANILIBRIA_MEDIA_ROOT."""
    media_root = resolve_media_root()
    rules = load_cleanup_rules(db)
    torrents = list(qb.torrents_info() or [])
    enriched = enrich_with_trackers(qb, torrents)
    result = InventoryResult()

    candidate_hashes: set[str] = set()
    candidates: list[Any] = []
    for torrent in enriched:
        save_path = torrent.save_path
        content_path = getattr(torrent, "content_path", None)
        if not save_path and not content_path:
            result.skipped_path += 1
            continue
        anchor_raw = save_path or content_path
        base = Path(anchor_raw).resolve()
        anchor = base if base.is_dir() else base.parent
        if not is_under_media_root(anchor, media_root=media_root):
            result.skipped_path += 1
            continue
        result.torrents_under_root += 1
        matched, reason, _ = match_cleanup_rule(torrent, rules)
        if matched:
            result.skipped_invalid += 1
            if log_fn:
                log_fn(
                    f"пропуск невалидной раздачи {torrent.hash[:12]}… reason={reason} "
                    f"name={torrent.name}"
                )
            continue
        if not torrent.hash:
            continue
        candidate_hashes.add(torrent.hash)
        candidates.append(torrent)

    meta = archive_meta_by_hash(db, candidate_hashes)

    for index, torrent in enumerate(candidates, start=1):
        save_path = torrent.save_path or getattr(torrent, "content_path", None)
        content_path = getattr(torrent, "content_path", None)
        assert save_path
        base = Path(save_path).resolve()
        folder_key = str(base if base.is_dir() else base.parent)
        torrent_id, release_id = meta.get(torrent.hash, (0, 0))

        try:
            qb_files = qb.torrents_files(torrent_hash=torrent.hash) or []
        except Exception as exc:
            if log_fn:
                log_fn(f"torrents_files {torrent.hash[:12]}…: {exc}")
            # Не в valid_hashes и помечаем failed — prune сохранит старые строки.
            result.failed_hashes.add(torrent.hash)
            continue

        result.valid_hashes.add(torrent.hash)
        priorities = extract_qb_file_priorities(qb_files)
        for qb_file in qb_files:
            rel_name = extract_qb_file_name(qb_file)
            if not rel_name:
                continue
            file_index = getattr(qb_file, "index", None)
            if file_index is None and isinstance(qb_file, dict):
                file_index = qb_file.get("index", 0)
            try:
                file_index = int(file_index or 0)
            except (TypeError, ValueError):
                file_index = 0
            size_raw = getattr(qb_file, "size", None)
            if size_raw is None and isinstance(qb_file, dict):
                size_raw = qb_file.get("size", 0)
            try:
                size = int(size_raw or 0)
            except (TypeError, ValueError):
                size = 0
            selected = True
            if priorities:
                selected = priorities.get(file_index, 0) > 0
            resolved = resolve_full_path(
                save_path,
                rel_name,
                content_path=content_path,
                media_root=media_root,
            )
            if resolved is None:
                continue
            result.files.append(
                InventoryFile(
                    info_hash=torrent.hash,
                    torrent_id=torrent_id,
                    release_id=release_id,
                    relative_path=normalize_rel_path(rel_name),
                    size=size,
                    file_index=file_index,
                    selected=selected,
                    full_path=str(resolved.resolve()),
                    folder_key=folder_key,
                )
            )

        if log_fn and index % 50 == 0:
            log_fn(
                f"inventory: торрентов {index}/{len(candidates)}, файлов={len(result.files)}"
            )

    if log_fn:
        log_fn(
            f"inventory: под {media_root} torrents={result.torrents_under_root}, "
            f"valid={len(result.valid_hashes)}, failed_files={len(result.failed_hashes)}, "
            f"invalid={result.skipped_invalid}, files={len(result.files)}"
        )
    return result


def upsert_torrent_files_inventory(db: Session, inventory: InventoryResult) -> int:
    """Upsert torrent_files по inventory; возвращает число upsert."""
    now = utcnow()
    by_hash: dict[str, list[InventoryFile]] = {}
    for item in inventory.files:
        by_hash.setdefault(item.info_hash, []).append(item)

    # Ленивый импорт: file_tracker импортирует из этого модуля не нужно, но избегаем циклов.
    from app.services.file_tracker import FileTrackerService, apply_checking_flag, checking_flag_from_path

    tracker = FileTrackerService(db)

    upserted = 0
    for info_hash, files in by_hash.items():
        existing = {
            row.relative_path: row
            for row in db.scalars(select(TorrentFile).where(TorrentFile.info_hash == info_hash)).all()
        }
        # Начальный ui_status: «новый» только если есть prior и файла там не было.
        # Первый торрент (без prior): _baseline_provisional_status —
        # clean / mixed=уже были ok|changed в составе.
        torrent_id = next((f.torrent_id for f in files if f.torrent_id), None)
        release_id = next((f.release_id for f in files if f.release_id), None)
        current_paths = {f.relative_path for f in files if f.relative_path}
        if torrent_id:
            has_prior_version, prior_version_paths = tracker.prior_version_composition(
                torrent_id=torrent_id,
                info_hash=info_hash,
                release_id=release_id,
                current_paths=current_paths,
            )
        else:
            has_prior_version, prior_version_paths = False, set()
        hashed_paths = set()
        baseline_had_known = False
        if not has_prior_version:
            hashed_paths = tracker._load_hashed_canonical_paths(
                [f.full_path for f in files if f.full_path]
            )
            baseline_had_known = tracker._baseline_has_known_among(
                existing.values(), hashed_paths=hashed_paths
            )
        seen_paths: set[str] = set()
        heal_transitions: list = []
        for item in files:
            seen_paths.add(item.relative_path)
            row = existing.get(item.relative_path)
            if row is None:
                if has_prior_version:
                    first_seen = tracker.first_seen_for_path(
                        has_prior_version=True,
                        prior_version_paths=prior_version_paths,
                        relative_path=item.relative_path,
                    )
                    initial_status = "new" if first_seen else "ok"
                else:
                    initial_status = tracker._baseline_provisional_status(
                        item.full_path,
                        hashed_paths=hashed_paths,
                        mixed=baseline_had_known,
                    )
                db.add(
                    TorrentFile(
                        torrent_id=item.torrent_id,
                        info_hash=item.info_hash,
                        release_id=item.release_id,
                        relative_path=item.relative_path,
                        size=item.size,
                        file_index=item.file_index,
                        selected=item.selected,
                        full_path=item.full_path,
                        ui_status=initial_status,
                        is_checking=checking_flag_from_path(item.full_path),
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                row.torrent_id = item.torrent_id or row.torrent_id
                row.release_id = item.release_id or row.release_id
                row.size = item.size
                row.file_index = item.file_index
                row.selected = item.selected
                row.full_path = item.full_path
                row.updated_at = now
                apply_checking_flag(row, checking_flag_from_path(item.full_path))
                # Лечим ложный sticky new: inventory мог создать строки до появления prior.
                # Пустой состав prior — не лечим (first_seen=True).
                if has_prior_version:
                    first_seen = tracker.first_seen_for_path(
                        has_prior_version=True,
                        prior_version_paths=prior_version_paths,
                        relative_path=item.relative_path,
                    )
                    before = (row.ui_status or "").strip().lower()
                    if first_seen:
                        if before not in {"new", "changed"}:
                            row.ui_status = "new"
                    elif prior_version_paths and before != "changed" and before != "ok":
                        row.ui_status = "ok"
                    after = (row.ui_status or "").strip().lower()
                    if before and after and before != after:
                        tr = tracker._note_ui_transition(
                            relative_path=item.relative_path,
                            from_status=before,
                            to_status=after,
                            phase="inventory_heal",
                            reason="heal_prior_path" if after == "ok" else "first_seen",
                        )
                        if tr is not None:
                            heal_transitions.append(tr)
            upserted += 1
        for rel, row in existing.items():
            if rel not in seen_paths:
                db.delete(row)
        if heal_transitions and torrent_id:
            prior_hash = tracker._prior_version_hash(
                torrent_id=torrent_id,
                current_hash=info_hash,
                release_id=release_id,
                current_paths=current_paths,
            )
            tracker._emit_ui_status_pipeline_event(
                info_hash=info_hash,
                torrent_id=torrent_id,
                phase="inventory_heal",
                transitions=heal_transitions,
                has_prior_version=has_prior_version,
                prior_info_hash=prior_hash,
                prior_archive_id=None,
            )
    db.commit()
    return upserted


def prune_stale_inventory(db: Session, inventory: InventoryResult) -> dict[str, int]:
    """Удалить torrent_files / disk_file_hashes, которых нет в актуальном inventory.

    Hash с failed torrents_files не трогаем (временный сбой qB).
    Hash, сохранённые в torrent_archive (в т.ч. superseded/архивные) — не трогаем:
    это история состава по каждому торренту релиза.
    """
    if not inventory.valid_hashes:
        # Пустой inventory (сбой qB / всё отфильтровано) — не трогаем БД.
        return {"torrent_files": 0, "disk_hashes": 0, "skipped": 1}

    protect_hashes = {(h or "").strip().lower() for h in inventory.failed_hashes if h}
    known_hashes = {(h or "").strip().lower() for h in inventory.valid_hashes if h}
    known_paths = {item.full_path for item in inventory.files if item.full_path}

    tf_rows = list(db.scalars(select(TorrentFile)).all())
    # Архивные hash защищаем точечно: только кандидаты на удаление (не весь archive).
    candidate_hashes = {
        (row.info_hash or "").strip().lower()
        for row in tf_rows
        if (row.info_hash or "").strip()
    } - known_hashes - protect_hashes
    if candidate_hashes:
        archive_protect = {
            (h or "").strip().lower()
            for h in db.scalars(
                select(TorrentArchive.info_hash).where(TorrentArchive.info_hash.in_(candidate_hashes))
            ).all()
            if h
        }
        protect_hashes |= archive_protect

    # Пути failed-раздач и архивной истории защищаем и в disk_file_hashes.
    for row in tf_rows:
        info_hash = (row.info_hash or "").strip().lower()
        if info_hash in protect_hashes and row.full_path:
            known_paths.add(row.full_path)

    tf_deleted = 0
    for row in tf_rows:
        info_hash = (row.info_hash or "").strip().lower()
        full_path = row.full_path
        if info_hash in protect_hashes:
            continue
        if info_hash not in known_hashes:
            db.delete(row)
            tf_deleted += 1
            continue
        if full_path and full_path not in known_paths:
            db.delete(row)
            tf_deleted += 1

    # disk_file_hashes под media_root, которых нет в known (+ protected)
    media_root = resolve_media_root()
    dh_rows = list(db.scalars(select(DiskFileHash)).all())
    dh_deleted = 0
    for row in dh_rows:
        path = Path(row.full_path)
        try:
            resolved = path.resolve()
        except OSError:
            db.delete(row)
            dh_deleted += 1
            continue
        if not is_under_media_root(resolved, media_root=media_root):
            continue
        if str(resolved) not in known_paths:
            db.delete(row)
            dh_deleted += 1

    db.commit()
    return {"torrent_files": tf_deleted, "disk_hashes": dh_deleted, "skipped": 0}


def hash_inventory_files(
    db: Session,
    files: list[InventoryFile],
    *,
    selected_only: bool = True,
    workers: int | None = None,
    log_fn: Callable[[str], None] | None = None,
    progress_total: int | None = None,
    progress_start: int = 0,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, int]:
    """BLAKE3+gate для файлов inventory, которые есть на диске.

    workers>1 — чтение/хеш в пуле потоков; запись в БД только из вызывающего потока.
    should_stop — кооперативная остановка между файлами (текущие дожимаются).
    """
    from app.services.file_hasher import clamp_hash_workers, hash_paths_parallel

    paths: list[Path] = []
    missing = 0
    for item in files:
        if selected_only and not item.selected:
            continue
        path = Path(item.full_path)
        if path.is_file():
            paths.append(path)
        else:
            missing += 1

    worker_count = clamp_hash_workers(1 if workers is None else workers)
    if log_fn and paths:
        log_fn(f"хеширование: файлов={len(paths)}, workers={worker_count}")

    stats = hash_paths_parallel(
        db,
        paths,
        workers=worker_count,
        log_fn=log_fn,
        progress_total=progress_total,
        progress_start=progress_start,
        should_stop=should_stop,
    )
    return {
        "hashed": stats["hashed"],
        "gated": stats["gated"],
        "missing": missing,
        "errors": stats.get("errors", 0),
        "progress_index": stats.get("progress_index", progress_start),
        "stopped": int(stats.get("stopped", 0) or 0),
    }
