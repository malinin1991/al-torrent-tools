"""Фоновая джоба синхронизации MediaInfo для файлов библиотеки."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import FileMediaInfo, JobLog, TorrentFile
from app.db.session import SessionLocal
from app.services.file_hasher import clamp_hash_workers
from app.services.job_runner import JobStopRequested, is_stop_requested
from app.services.mediainfo import (
    get_canonical_path,
    is_media_filename,
    upsert_file_mediainfo,
)
from app.services.torrent_files_meta import (
    complete_path_for,
    is_partial_only,
    resolve_media_root,
)

logger = logging.getLogger(__name__)

DEFAULT_FULL_WORKERS = 4


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

    # Полный прогон — многопоточный (по умолчанию 4); инкремент — 1 поток, если не задано иное.
    default_workers = DEFAULT_FULL_WORKERS if force else 1
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
        # Соседний .!qB при complete не отбрасываем; сам incomplete без complete — да.
        if is_partial_only(raw):
            continue
        p = complete_path_for(raw)
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

    # Сначала gate / проверка существования — на основном потоке.
    to_process: list[Path] = []
    for canonical, path in sorted(candidates.items()):
        if is_stop_requested(db, job_id):
            _add_log(
                db,
                job_id,
                f"mediainfo_sync: остановка по запросу (обработано {scanned}/{total})",
                "warning",
            )
            raise JobStopRequested()

        scanned += 1
        work_path = path
        if not work_path.is_file():
            c_path = Path(canonical)
            if c_path.is_file():
                work_path = c_path
            else:
                missing += 1
                continue

        try:
            stat = work_path.stat()
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

        to_process.append(work_path)

    if not to_process:
        _add_log(
            db,
            job_id,
            f"mediainfo_sync: завершено. Всего={total}, обновлено={updated}, пропущено={skipped}, отсутствуют={missing}, ошибок={errors}",
            "info",
        )
        return

    _add_log(
        db,
        job_id,
        f"mediainfo_sync: к обработке {len(to_process)} файлов (workers={workers})",
    )

    progress_lock = threading.Lock()
    done_count = 0
    started_count = 0
    process_total = len(to_process)
    pending_logs: Queue[tuple[str, str]] = Queue()

    def _flush_logs() -> None:
        while True:
            try:
                level, message = pending_logs.get_nowait()
            except Empty:
                break
            _add_log(db, job_id, message, level)

    def _worker(path: Path, worker_id: int, *, session: Session | None = None) -> str:
        """Парсинг+upsert. При session=None создаёт собственную сессию (для потоков)."""
        nonlocal started_count
        with progress_lock:
            started_count += 1
            idx = started_count
        pending_logs.put(
            (
                "debug",
                f"mediainfo_sync: [w{worker_id}] [{idx}/{process_total}] {path.parent.name}/{path.name}",
            )
        )
        own_session = session is None
        sess = session if session is not None else SessionLocal()
        try:
            result = upsert_file_mediainfo(sess, str(path), force=force)
            return "updated" if result is not None else "error"
        except Exception as exc:
            logger.warning("Ошибка обработки MediaInfo для %s: %s", path, exc)
            return "error"
        finally:
            if own_session:
                sess.close()

    def _record(status: str) -> None:
        nonlocal done_count, updated, errors
        done_count += 1
        if status == "updated":
            updated += 1
        else:
            errors += 1
        _flush_logs()
        if done_count % 100 == 0:
            _add_log(
                db,
                job_id,
                f"mediainfo_sync: сводка {done_count}/{process_total} обработано "
                f"(обновлено={updated}, пропущено={skipped}, не найдено={missing}, ошибок={errors})",
                "info",
            )

    if workers <= 1:
        for path in to_process:
            if is_stop_requested(db, job_id):
                _flush_logs()
                _add_log(
                    db,
                    job_id,
                    f"mediainfo_sync: остановка по запросу (обработано {done_count}/{process_total})",
                    "warning",
                )
                raise JobStopRequested()
            status = _worker(path, 1, session=db)
            _record(status)
    else:
        slots: Queue[int] = Queue()
        for wid in range(1, workers + 1):
            slots.put(wid)

        def _worker_with_slot(path: Path) -> str:
            worker_id = slots.get()
            try:
                return _worker(path, worker_id)
            finally:
                slots.put(worker_id)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            items_iter = iter(to_process)
            futures: dict[Any, Path] = {}

            def _submit_next() -> bool:
                if is_stop_requested(db, job_id):
                    return False
                try:
                    item = next(items_iter)
                except StopIteration:
                    return False
                fut = pool.submit(_worker_with_slot, item)
                futures[fut] = item
                return True

            for _ in range(workers):
                if not _submit_next():
                    break

            stopped = False
            while futures:
                if is_stop_requested(db, job_id):
                    stopped = True
                done, _pending = wait(futures.keys(), return_when=FIRST_COMPLETED, timeout=0.5)
                if not done:
                    _flush_logs()
                    continue
                for fut in done:
                    path = futures.pop(fut)
                    try:
                        status = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Ошибка MediaInfo future для %s: %s", path, exc)
                        status = "error"
                    _record(status)
                    if not stopped:
                        _submit_next()
                if stopped and not futures:
                    break
                if stopped:
                    # Дожимаем уже запущенные, новые не ставим.
                    continue

            _flush_logs()
            if stopped:
                _add_log(
                    db,
                    job_id,
                    f"mediainfo_sync: остановка по запросу (обработано {done_count}/{process_total})",
                    "warning",
                )
                raise JobStopRequested()

    _flush_logs()
    _add_log(
        db,
        job_id,
        f"mediainfo_sync: завершено. Всего={total}, обновлено={updated}, пропущено={skipped}, отсутствуют={missing}, ошибок={errors}",
        "info",
    )
