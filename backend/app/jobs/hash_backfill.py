"""Полный проход: qB-inventory → BLAKE3+gate → prune БД."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Job, JobLog, Setting
from app.services.qb_inventory import (
    build_inventory,
    connect_master,
    hash_inventory_files,
    prune_stale_inventory,
    upsert_torrent_files_inventory,
)
from app.services.torrent_files_meta import resolve_media_root

CHECKPOINT_KEY = "hash_backfill_checkpoint"


def _add_log(db: Session, job_id: int, message: str, level: str = "info") -> None:
    db.add(JobLog(job_id=job_id, level=level, message=message))
    db.commit()


def _get_checkpoint(db: Session) -> str:
    row = db.get(Setting, CHECKPOINT_KEY)
    return (row.value if row else "") or ""


def _set_checkpoint(db: Session, value: str) -> None:
    row = db.get(Setting, CHECKPOINT_KEY)
    if row is None:
        db.add(Setting(key=CHECKPOINT_KEY, value=value, updated_at=datetime.utcnow()))
    else:
        row.value = value
        row.updated_at = datetime.utcnow()
    db.commit()


def _touch_job(db: Session, job_id: int) -> None:
    job = db.get(Job, job_id)
    if job is not None:
        job.error = None
        db.commit()


async def run_hash_backfill(db: Session, job_id: int, params: dict[str, Any]) -> None:
    reset = bool(params.get("reset_checkpoint", False))
    if reset:
        _set_checkpoint(db, "")
        _add_log(db, job_id, "hash_backfill: чекпоинт сброшен")

    media_root = resolve_media_root()
    _add_log(db, job_id, f"hash_backfill: media_root={media_root}")

    qb = connect_master(db)
    if qb is None:
        raise RuntimeError("hash_backfill: master qB не настроен")

    def log_fn(message: str) -> None:
        _add_log(db, job_id, f"hash_backfill: {message}", "debug")

    inventory = build_inventory(db, qb, log_fn=log_fn)
    _add_log(
        db,
        job_id,
        f"hash_backfill: inventory valid={len(inventory.valid_hashes)}, "
        f"files={len(inventory.files)}, invalid={inventory.skipped_invalid}, "
        f"skipped_path={inventory.skipped_path}",
    )

    upserted = upsert_torrent_files_inventory(db, inventory)
    _add_log(db, job_id, f"hash_backfill: upsert torrent_files={upserted}")

    # Группировка по папкам, обход в 1 поток с чекпоинтом
    folders: dict[str, list] = {}
    for item in inventory.files:
        folders.setdefault(item.folder_key, []).append(item)

    folder_keys = sorted(folders.keys())
    checkpoint = _get_checkpoint(db)
    if checkpoint:
        folder_keys = [k for k in folder_keys if k > checkpoint]
        _add_log(db, job_id, f"hash_backfill: resume после checkpoint={checkpoint}")

    total_hashed = 0
    total_gated = 0
    total_missing = 0
    for folder in folder_keys:
        _add_log(db, job_id, f"hash_backfill: папка {folder}")
        stats = hash_inventory_files(
            db,
            folders[folder],
            selected_only=True,
            log_fn=lambda msg: _add_log(db, job_id, msg, "info"),
        )
        total_hashed += stats["hashed"]
        total_gated += stats["gated"]
        total_missing += stats["missing"]
        _set_checkpoint(db, folder)
        _touch_job(db, job_id)

    pruned = prune_stale_inventory(db, inventory)
    if pruned.get("skipped"):
        _add_log(
            db,
            job_id,
            "hash_backfill: prune пропущен — пустой inventory (защита БД)",
            "warning",
        )
    else:
        _add_log(
            db,
            job_id,
            f"hash_backfill: prune torrent_files={pruned['torrent_files']}, "
            f"disk_hashes={pruned['disk_hashes']}",
        )

    # Полный проход завершён — сбрасываем checkpoint для следующего запуска.
    _set_checkpoint(db, "")
    _add_log(
        db,
        job_id,
        f"hash_backfill: готово folders={len(folder_keys)}, "
        f"hashed={total_hashed}, gated={total_gated}, missing={total_missing}, "
        f"invalid_skipped={inventory.skipped_invalid}",
    )
