"""Фоновая джоба синхронизации MediaInfo для файлов библиотеки."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import JobLog, TorrentFile
from app.services.file_hasher import clamp_hash_workers
from app.services.job_runner import JobStopRequested, is_stop_requested
from app.services.mediainfo import DEFAULT_MEDIAINFO_WORKERS, mediainfo_paths_parallel
from app.services.torrent_files_meta import resolve_media_root

logger = logging.getLogger(__name__)

DEFAULT_FULL_WORKERS = DEFAULT_MEDIAINFO_WORKERS


def _add_log(db: Session, job_id: int, message: str, level: str = "info") -> None:
    db.add(JobLog(job_id=job_id, level=level, message=message))
    db.commit()


def _clamp_mediainfo_workers(value: Any, *, default: int = DEFAULT_FULL_WORKERS) -> int:
    return clamp_hash_workers(value, default=default)


async def run_mediainfo_sync(db: Session, job_id: int, params: dict[str, Any]) -> None:
    mode = str(params.get("mode") or "").strip().lower()
    # Режимы запуска: "full" или "incremental" (по умолчанию)
    force = mode == "full" or bool(params.get("force", False))
    if not mode:
        mode = "full" if force else "incremental"

    # Полный и инкрементальный проходы: 4 потока по умолчанию; явный workers сохраняется.
    default_workers = DEFAULT_MEDIAINFO_WORKERS
    workers = _clamp_mediainfo_workers(params.get("workers"), default=default_workers)

    media_root = resolve_media_root()
    _add_log(
        db,
        job_id,
        f"mediainfo_sync: запуск, mode={mode} (force={force}), workers={workers}, media_root={media_root}",
    )

    if is_stop_requested(db, job_id):
        _add_log(db, job_id, "mediainfo_sync: остановка по запросу", "warning")
        raise JobStopRequested()

    rows = db.scalars(
        select(TorrentFile.full_path)
        .where(TorrentFile.full_path.is_not(None))
        .distinct()
    ).all()

    def log_parse(message: str) -> None:
        # Session stays on caller, and debug logs commit together with metadata.
        # A per-file _add_log() commit would silently defeat the batch of25.
        db.add(JobLog(job_id=job_id, level="debug", message=f"mediainfo_sync: {message}"))

    stats = mediainfo_paths_parallel(
        db, rows, workers=workers, force=force,
        should_stop=lambda: is_stop_requested(db, job_id), log_fn=log_parse,
    )
    if stats["stopped"]:
        _add_log(
            db, job_id,
            f"mediainfo_sync: остановка по запросу (обработано {stats['updated']}/{stats['total']})",
            "warning",
        )
        raise JobStopRequested()

    _add_log(
        db, job_id,
        f"mediainfo_sync: завершено. Всего={stats['total']}, обновлено={stats['updated']}, "
        f"пропущено={stats['skipped']}, отсутствуют={stats['missing']}, ошибок={stats['errors']}",
    )
