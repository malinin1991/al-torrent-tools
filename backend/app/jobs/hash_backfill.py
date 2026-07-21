"""Backfill хеширования: seeding на master, api_present, чекпоинт по папкам."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import qbittorrentapi
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job, JobLog, QbClient, Setting, TorrentArchive
from app.services.file_tracker import FileTrackerService
from app.services.torrent_files_meta import extract_qb_save_path, is_under_media_root, resolve_media_root

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


def _master_client(db: Session) -> qbittorrentapi.Client | None:
    master = db.scalar(select(QbClient).where(QbClient.role == "master", QbClient.enabled.is_(True)).limit(1))
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


def _is_seeding(torrent: Any) -> bool:
    progress = float(getattr(torrent, "progress", 0.0) or 0.0)
    state = str(getattr(torrent, "state", "") or "").lower()
    if progress >= 1.0:
        return True
    return state in {
        "uploading",
        "stalledup",
        "queuedup",
        "forcedup",
        "pausedup",
        "stoppedup",
    }


async def run_hash_backfill(db: Session, job_id: int, params: dict[str, Any]) -> None:
    reset = bool(params.get("reset_checkpoint", False))
    if reset:
        _set_checkpoint(db, "")
        _add_log(db, job_id, "hash_backfill: чекпоинт сброшен")

    media_root = resolve_media_root()
    _add_log(db, job_id, f"hash_backfill: media_root={media_root}")

    qb = _master_client(db)
    if qb is None:
        raise RuntimeError("hash_backfill: master qB не настроен")

    archives = list(
        db.scalars(
            select(TorrentArchive)
            .where(TorrentArchive.api_present.is_(True))
            .order_by(TorrentArchive.info_hash.asc(), TorrentArchive.id.asc())
        ).all()
    )
    # Уникальные hash → последний archive
    by_hash: dict[str, TorrentArchive] = {}
    for archive in archives:
        by_hash[archive.info_hash.lower()] = archive

    checkpoint = _get_checkpoint(db)
    _add_log(
        db,
        job_id,
        f"hash_backfill: кандидатов api_present={len(by_hash)}, checkpoint={checkpoint or '(start)'}",
    )

    # Группируем по save_path (папка), обход в 1 поток
    folders: dict[str, list[TorrentArchive]] = {}
    skipped_path = 0
    skipped_seed = 0
    for info_hash, archive in sorted(by_hash.items(), key=lambda x: x[0]):
        try:
            torrents = qb.torrents_info(hashes=info_hash)
        except Exception as exc:
            _add_log(db, job_id, f"hash_backfill: qB error {info_hash[:12]}…: {exc}", "warning")
            continue
        if not torrents:
            continue
        torrent = torrents[0]
        if not _is_seeding(torrent):
            skipped_seed += 1
            continue
        save_path = extract_qb_save_path(torrent)
        if not save_path:
            skipped_path += 1
            continue
        from pathlib import Path

        base = Path(save_path).resolve()
        if not is_under_media_root(base, media_root=media_root):
            skipped_path += 1
            continue
        folder_key = str(base if base.is_dir() else base.parent)
        folders.setdefault(folder_key, []).append(archive)

    folder_keys = sorted(folders.keys())
    if checkpoint:
        folder_keys = [k for k in folder_keys if k > checkpoint]

    tracker = FileTrackerService(db, job_id=job_id)
    processed = 0
    for folder in folder_keys:
        _add_log(db, job_id, f"hash_backfill: папка {folder}")
        for archive in folders[folder]:
            result = tracker.track_torrent(
                info_hash=archive.info_hash,
                torrent_id=archive.torrent_id,
                release_id=archive.release_id,
                notify=False,
            )
            processed += 1
            if result.skipped_reason:
                _add_log(
                    db,
                    job_id,
                    f"hash_backfill: пропуск {archive.info_hash[:12]}… — {result.skipped_reason}",
                    "debug",
                )
            else:
                _add_log(
                    db,
                    job_id,
                    f"hash_backfill: {archive.info_hash[:12]}… "
                    f"files={result.files_upserted} hashed={result.hashed} gated={result.gated}",
                    "debug",
                )
            # Обновляем статус джоба чтобы stale-reclaim не убил долгий backfill
            job = db.get(Job, job_id)
            if job is not None:
                job.error = None
                db.commit()
        _set_checkpoint(db, folder)

    _add_log(
        db,
        job_id,
        f"hash_backfill: готово processed={processed}, "
        f"skipped_seed={skipped_seed}, skipped_path={skipped_path}, folders={len(folder_keys)}",
    )
