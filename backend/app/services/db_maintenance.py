from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import (
    FileChangeEvent,
    Job,
    JobLog,
    ReleaseCheckpoint,
    SeenTorrent,
    TelegramOutbox,
    TorrentArchive,
    TorrentFile,
    TorrentPipeline,
)
from app.services.torrent_archive import resolve_torrent_storage_root


def reset_operational_state(db: Session) -> dict[str, int]:
    """Сброс джобов, seen, pipeline, checkpoints, outbox. Архив .torrent и настройки сохраняются."""
    jobs = db.scalar(select(func.count()).select_from(Job)) or 0
    logs = db.scalar(select(func.count()).select_from(JobLog)) or 0
    seen = db.scalar(select(func.count()).select_from(SeenTorrent)) or 0
    pipeline = db.scalar(select(func.count()).select_from(TorrentPipeline)) or 0
    checkpoints = db.scalar(select(func.count()).select_from(ReleaseCheckpoint)) or 0
    outbox = db.scalar(select(func.count()).select_from(TelegramOutbox)) or 0

    db.execute(delete(JobLog))
    db.execute(delete(Job))
    db.execute(delete(SeenTorrent))
    db.execute(delete(TelegramOutbox))
    db.execute(delete(TorrentPipeline))
    db.execute(delete(ReleaseCheckpoint))
    db.commit()

    return {
        "jobs_removed": jobs,
        "job_logs_removed": logs,
        "seen_torrents_removed": seen,
        "pipeline_removed": pipeline,
        "checkpoints_removed": checkpoints,
        "outbox_removed": outbox,
        "archive_kept": db.scalar(select(func.count()).select_from(TorrentArchive)) or 0,
    }


def reset_full(db: Session, storage_root: Path | None = None) -> dict[str, int]:
    """Полный сброс: архив в БД, .torrent файлы и операционное состояние."""
    root = storage_root or resolve_torrent_storage_root()
    archives = db.scalars(select(TorrentArchive)).all()
    files_removed = 0
    for item in archives:
        path = root / Path(item.file_path).name
        if path.is_file():
            path.unlink()
            files_removed += 1

    archive_count = len(archives)
    db.execute(delete(TorrentArchive))
    stats = reset_operational_state(db)
    stats["archive_removed"] = archive_count
    stats["torrent_files_removed"] = files_removed
    stats.pop("archive_kept", None)
    return stats


def purge_false_orphan_events(
    db: Session,
    *,
    media_root: Path | None = None,
    commit: bool = True,
) -> dict[str, int]:
    """Удаляет ложные orphan-события вне корня контента торрента.

    Раньше orphan-скан шёл по общему save_path года (/anilibria/2012) и писал
    в file_change_events чужие тайтлы. Также снимает дубликаты (torrent_id+path).
    """
    from app.services.file_tracker import KIND_ORPHAN, resolve_orphan_scan_root
    from app.services.torrent_files_meta import resolve_media_root

    root_media = media_root or resolve_media_root()
    # Выбираем только исторически существующие колонки: этот data-fix вызывается
    # из migration 0009, до добавления FileChangeEvent.info_hash в 0011.
    orphans = list(
        db.execute(
            select(
                FileChangeEvent.id,
                FileChangeEvent.torrent_id,
                FileChangeEvent.relative_path,
                FileChangeEvent.full_path,
            ).where(FileChangeEvent.kind == KIND_ORPHAN)
        ).all()
    )
    if not orphans:
        return {
            "orphan_events_scanned": 0,
            "orphan_events_removed": 0,
            "orphan_events_kept": 0,
            "orphan_duplicates_removed": 0,
        }

    torrent_ids = {int(e.torrent_id) for e in orphans if e.torrent_id is not None}
    files_by_torrent: dict[int, list[str]] = {}
    if torrent_ids:
        for row in db.execute(
            select(TorrentFile.torrent_id, TorrentFile.full_path).where(
                TorrentFile.torrent_id.in_(torrent_ids)
            )
        ).all():
            if row.full_path:
                files_by_torrent.setdefault(int(row.torrent_id), []).append(row.full_path)

    root_by_torrent: dict[int, Path | None] = {}
    for tid, paths in files_by_torrent.items():
        root_by_torrent[tid] = resolve_orphan_scan_root(
            save_path=None,
            content_path=None,
            known_full_paths=set(paths),
            media_root=root_media,
        )

    false_ids: set[int] = set()
    for event in orphans:
        if event.torrent_id is None:
            false_ids.add(event.id)
            continue
        path_raw = event.full_path or event.relative_path
        if not path_raw:
            false_ids.add(event.id)
            continue
        scan_root = root_by_torrent.get(int(event.torrent_id))
        if scan_root is None:
            # Нет якоря по torrent_files — orphan от широкого скана, удаляем.
            false_ids.add(event.id)
            continue
        try:
            Path(path_raw).resolve().relative_to(scan_root)
        except (ValueError, OSError):
            false_ids.add(event.id)

    # Дубликаты среди оставшихся: оставляем событие с наибольшим id.
    remaining = [e for e in orphans if e.id not in false_ids]
    keep_by_key: dict[tuple[int | None, str], int] = {}
    dup_ids: set[int] = set()
    for event in sorted(remaining, key=lambda item: item.id, reverse=True):
        key = (event.torrent_id, (event.full_path or event.relative_path or "").strip())
        if key in keep_by_key:
            dup_ids.add(event.id)
        else:
            keep_by_key[key] = event.id

    remove_ids = false_ids | dup_ids
    if remove_ids:
        db.execute(delete(FileChangeEvent).where(FileChangeEvent.id.in_(remove_ids)))
        if commit:
            db.commit()
        else:
            db.flush()

    return {
        "orphan_events_scanned": len(orphans),
        "orphan_events_removed": len(false_ids),
        "orphan_duplicates_removed": len(dup_ids),
        "orphan_events_kept": len(orphans) - len(remove_ids),
    }
