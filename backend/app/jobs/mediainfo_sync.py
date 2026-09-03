"""Фоновая джоба синхронизации MediaInfo для файлов библиотеки."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import FileMediaInfo, JobLog, TorrentFile
from app.services.job_runner import JobStopRequested, is_stop_requested
from app.services.mediainfo import (
    get_canonical_path,
    is_media_filename,
    upsert_file_mediainfo,
)
from app.services.torrent_files_meta import is_incomplete_path, resolve_media_root

logger = logging.getLogger(__name__)


def _add_log(db: Session, job_id: int, message: str, level: str = "info") -> None:
    db.add(JobLog(job_id=job_id, level=level, message=message))
    db.commit()


async def run_mediainfo_sync(db: Session, job_id: int, params: dict[str, Any]) -> None:
    mode = str(params.get("mode") or "").strip().lower()
    # Режимы запуска: "full" или "incremental" (по умолчанию)
    force = mode == "full" or bool(params.get("force", False))
    if not mode:
        mode = "full" if force else "incremental"

    media_root = resolve_media_root()
    _add_log(
        db,
        job_id,
        f"mediainfo_sync: запуск, mode={mode} (force={force}), media_root={media_root}",
    )

    if is_stop_requested(db, job_id):
        _add_log(db, job_id, "mediainfo_sync: остановка по запросу", "warning")
        raise JobStopRequested()

    # Собираем уникальные пути к файлам из torrent_files
    rows = db.scalars(
        select(TorrentFile.full_path)
        .where(TorrentFile.full_path.is_not(None))
        .distinct()
    ).all()

    candidates: dict[str, Path] = {}
    for raw in rows:
        if not raw:
            continue
        p = Path(raw)
        if is_incomplete_path(p):
            continue
        if not is_media_filename(p):
            continue
        canon = get_canonical_path(p)
        if canon not in candidates:
            candidates[canon] = p

    _add_log(
        db,
        job_id,
        f"mediainfo_sync: найдено {len(candidates)} уникальных кандидатов в torrent_files",
    )

    total = len(candidates)
    scanned = 0
    updated = 0
    skipped = 0
    missing = 0
    errors = 0

    for idx, (canonical, path) in enumerate(sorted(candidates.items()), start=1):
        if is_stop_requested(db, job_id):
            _add_log(
                db,
                job_id,
                f"mediainfo_sync: остановка по запросу (обработано {scanned}/{total})",
                "warning",
            )
            raise JobStopRequested()

        scanned += 1

        if not path.is_file():
            # Проверим, может файл существует по canonical
            c_path = Path(canonical)
            if c_path.is_file():
                path = c_path
            else:
                missing += 1
                continue

        try:
            stat = path.stat()
            size = stat.st_size
            mtime = float(stat.st_mtime)
        except OSError:
            errors += 1
            continue

        if not force:
            existing_row = db.execute(
                select(FileMediaInfo.file_size, FileMediaInfo.mtime)
                .where(FileMediaInfo.full_path == canonical)
                .limit(1)
            ).first()
            if existing_row is not None:
                ex_size, ex_mtime = int(existing_row[0] or 0), float(existing_row[1] or 0.0)
                if ex_size == size and abs(ex_mtime - mtime) < 0.001:
                    skipped += 1
                    continue

        try:
            result = upsert_file_mediainfo(db, str(path), force=force)
            if result is not None:
                updated += 1
            else:
                errors += 1
        except Exception as exc:
            logger.warning("Ошибка обработки MediaInfo для %s: %s", path, exc)
            errors += 1

        if scanned % 50 == 0 or scanned == total:
            _add_log(
                db,
                job_id,
                f"mediainfo_sync: прогресс {scanned}/{total} (обновлено={updated}, пропущено={skipped}, не найдено={missing}, ошибок={errors})",
                "debug",
            )

    _add_log(
        db,
        job_id,
        f"mediainfo_sync: завершено. Всего={total}, обновлено={updated}, пропущено={skipped}, отсутствуют={missing}, ошибок={errors}",
        "info",
    )
