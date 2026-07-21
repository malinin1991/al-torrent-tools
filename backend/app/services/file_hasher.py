"""BLAKE3-хеширование файлов с gate size+mtime."""

from __future__ import annotations

import logging
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


@dataclass
class HashResult:
    full_path: str
    size: int
    mtime: float
    content_hash: str
    hashed: bool  # True если файл реально читали
    skipped_gate: bool


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
    now = datetime.utcnow()

    row = db.scalar(select(DiskFileHash).where(DiskFileHash.full_path == full_path).limit(1))
    if row is not None and int(row.size) == size and float(row.mtime) == mtime and row.content_hash:
        row.last_checked_at = now
        db.commit()
        db.refresh(row)
        return HashResult(
            full_path=full_path,
            size=size,
            mtime=mtime,
            content_hash=row.content_hash,
            hashed=False,
            skipped_gate=True,
        )

    message = f"хеширую `{full_path}` ({size} bytes)"
    if log_fn:
        log_fn(message)
    else:
        logger.info(message)

    content_hash = hash_file_blake3(resolved, open_fn=open_fn)
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
    db.commit()
    db.refresh(row)
    return HashResult(
        full_path=full_path,
        size=size,
        mtime=mtime,
        content_hash=content_hash,
        hashed=True,
        skipped_gate=False,
    )
