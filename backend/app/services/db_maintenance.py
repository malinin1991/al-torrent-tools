from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import Job, JobLog, SeenTorrent, TorrentArchive, TorrentPipeline
from app.services.torrent_archive import resolve_torrent_storage_root


def reset_operational_state(db: Session) -> dict[str, int]:
    """Сброс джобов, seen, pipeline. Архив .torrent и настройки сохраняются."""
    jobs = db.scalar(select(func.count()).select_from(Job)) or 0
    logs = db.scalar(select(func.count()).select_from(JobLog)) or 0
    seen = db.scalar(select(func.count()).select_from(SeenTorrent)) or 0
    pipeline = db.scalar(select(func.count()).select_from(TorrentPipeline)) or 0

    db.execute(delete(JobLog))
    db.execute(delete(Job))
    db.execute(delete(SeenTorrent))
    db.execute(delete(TorrentPipeline))
    db.commit()

    return {
        "jobs_removed": jobs,
        "job_logs_removed": logs,
        "seen_torrents_removed": seen,
        "pipeline_removed": pipeline,
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
