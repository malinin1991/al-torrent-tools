"""BLAKE3-хеширование файлов с gate size+mtime."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import blake3
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import DiskFileHash

logger = logging.getLogger(__name__)

HASH_ALGO = "blake3"
# Лимит для Unraid/массива: не разгонять I/O сверх разумного.
HASH_WORKERS_MIN = 1
HASH_WORKERS_MAX = 8


@dataclass
class HashResult:
    full_path: str
    size: int
    mtime: float
    content_hash: str
    hashed: bool  # True если файл реально читали
    skipped_gate: bool


def clamp_hash_workers(value: int | str | None, *, default: int | None = None) -> int:
    """1..8; default из settings.file_hash_workers."""
    fallback = settings.file_hash_workers if default is None else default
    try:
        n = int(fallback if value is None else value)
    except (TypeError, ValueError):
        try:
            n = int(fallback)
        except (TypeError, ValueError):
            n = 3
    return max(HASH_WORKERS_MIN, min(HASH_WORKERS_MAX, n))


def normalize_file_hash_workers_setting(value: str | int | None) -> str:
    """Значение для хранения в Setting / ответа API."""
    return str(clamp_hash_workers(value))


def hash_file_blake3(
    path: Path,
    *,
    chunk_size: int | None = None,
    open_fn: Callable[..., object] | None = None,
) -> str:
    """Стриминг BLAKE3. open_fn — для тестов (mock)."""
    size = chunk_size if chunk_size is not None else settings.file_hash_chunk_size
    opener = open_fn or open
    hasher = blake3.blake3()
    with opener(path, "rb") as handle:  # type: ignore[arg-type]
        while True:
            chunk = handle.read(size)  # type: ignore[union-attr]
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _apply_hash_row(
    db: Session,
    *,
    full_path: str,
    size: int,
    mtime: float,
    content_hash: str,
    hashed: bool,
) -> HashResult:
    now = datetime.utcnow()
    row = db.scalar(select(DiskFileHash).where(DiskFileHash.full_path == full_path).limit(1))
    if not hashed and row is not None:
        row.last_checked_at = now
        return HashResult(
            full_path=full_path,
            size=size,
            mtime=mtime,
            content_hash=row.content_hash,
            hashed=False,
            skipped_gate=True,
        )

    if row is None:
        row = DiskFileHash(
            full_path=full_path,
            size=size,
            mtime=mtime,
            content_hash=content_hash,
            hash_algo=HASH_ALGO,
            last_checked_at=now,
            last_hashed_at=now,
        )
        db.add(row)
    else:
        row.size = size
        row.mtime = mtime
        row.content_hash = content_hash
        row.hash_algo = HASH_ALGO
        row.last_checked_at = now
        row.last_hashed_at = now
    return HashResult(
        full_path=full_path,
        size=size,
        mtime=mtime,
        content_hash=content_hash,
        hashed=True,
        skipped_gate=False,
    )


def upsert_disk_hash(
    db: Session,
    path: Path,
    *,
    log_fn: Callable[[str], None] | None = None,
    open_fn: Callable[..., object] | None = None,
) -> HashResult:
    """Хеширует файл или обновляет last_checked_at при совпадении size+mtime."""
    resolved = path.resolve()
    full_path = str(resolved)
    stat = resolved.stat()
    size = int(stat.st_size)
    mtime = float(stat.st_mtime)

    row = db.scalar(select(DiskFileHash).where(DiskFileHash.full_path == full_path).limit(1))
    if row is not None and int(row.size) == size and float(row.mtime) == mtime and row.content_hash:
        result = _apply_hash_row(
            db,
            full_path=full_path,
            size=size,
            mtime=mtime,
            content_hash=row.content_hash,
            hashed=False,
        )
        db.commit()
        db.refresh(row)
        return result

    message = f"хеширую `{full_path}` ({size} bytes)"
    if log_fn:
        log_fn(message)
    else:
        logger.info(message)

    content_hash = hash_file_blake3(resolved, open_fn=open_fn)
    result = _apply_hash_row(
        db,
        full_path=full_path,
        size=size,
        mtime=mtime,
        content_hash=content_hash,
        hashed=True,
    )
    db.commit()
    row = db.scalar(select(DiskFileHash).where(DiskFileHash.full_path == full_path).limit(1))
    if row is not None:
        db.refresh(row)
    return result


def hash_paths_parallel(
    db: Session,
    paths: list[Path],
    *,
    workers: int = 1,
    log_fn: Callable[[str], None] | None = None,
    open_fn: Callable[..., object] | None = None,
) -> dict[str, int]:
    """Gate на вызывающем потоке; BLAKE3 в ThreadPoolExecutor; upsert в БД последовательно.

    Ошибка одного файла не валит весь проход (errors++).
    """
    worker_count = clamp_hash_workers(workers)
    hashed = 0
    gated = 0
    errors = 0
    if not paths:
        return {"hashed": 0, "gated": 0, "errors": 0}

    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            key = str(path.resolve())
        except OSError:
            errors += 1
            continue
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)

    need_hash: list[tuple[str, int, float]] = []
    for path in unique:
        try:
            resolved = path.resolve()
            full_path = str(resolved)
            stat = resolved.stat()
        except OSError as exc:
            errors += 1
            message = f"хеш пропуск `{path}`: {exc}"
            if log_fn:
                log_fn(message)
            else:
                logger.warning(message)
            continue
        size = int(stat.st_size)
        mtime = float(stat.st_mtime)
        row = db.scalar(select(DiskFileHash).where(DiskFileHash.full_path == full_path).limit(1))
        if row is not None and int(row.size) == size and float(row.mtime) == mtime and row.content_hash:
            _apply_hash_row(
                db,
                full_path=full_path,
                size=size,
                mtime=mtime,
                content_hash=row.content_hash,
                hashed=False,
            )
            gated += 1
            continue
        need_hash.append((full_path, size, mtime))

    if gated:
        db.commit()

    def _worker(item: tuple[str, int, float]) -> tuple[str, int, float, str]:
        full_path, size, mtime = item
        digest = hash_file_blake3(Path(full_path), open_fn=open_fn)
        return full_path, size, mtime, digest

    def _record_success(full_path: str, size: int, mtime: float, digest: str) -> None:
        nonlocal hashed
        message = f"хеширую `{full_path}` ({size} bytes)"
        if log_fn:
            log_fn(message)
        else:
            logger.info(message)
        _apply_hash_row(
            db,
            full_path=full_path,
            size=size,
            mtime=mtime,
            content_hash=digest,
            hashed=True,
        )
        hashed += 1
        if hashed % 25 == 0:
            db.commit()

    def _record_error(full_path: str, exc: BaseException) -> None:
        nonlocal errors
        errors += 1
        message = f"хеш ошибка `{full_path}`: {exc}"
        if log_fn:
            log_fn(message)
        else:
            logger.warning(message)

    if not need_hash:
        return {"hashed": hashed, "gated": gated, "errors": errors}

    if worker_count <= 1:
        for item in need_hash:
            full_path, size, mtime = item
            try:
                _, _, _, digest = _worker(item)
                _record_success(full_path, size, mtime, digest)
            except Exception as exc:  # noqa: BLE001 — изоляция одного файла
                _record_error(full_path, exc)
        db.commit()
        return {"hashed": hashed, "gated": gated, "errors": errors}

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {pool.submit(_worker, item): item[0] for item in need_hash}
        for fut in as_completed(futures):
            full_path = futures[fut]
            try:
                path, size, mtime, digest = fut.result()
                _record_success(path, size, mtime, digest)
            except Exception as exc:  # noqa: BLE001 — изоляция одного файла
                _record_error(full_path, exc)
    db.commit()
    return {"hashed": hashed, "gated": gated, "errors": errors}
