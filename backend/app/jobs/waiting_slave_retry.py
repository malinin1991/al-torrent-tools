"""Досылка pipeline в статусе waiting_slave, когда slave снова доступен."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import JobLog
from app.jobs.waiting_master_retry import torrent_still_in_api
from app.services.anilibria_auth import ensure_passkey_stored
from app.services.pipeline import TorrentPipelineService
from app.services.qbittorrent import (
    ensure_announce_passkey,
    qb_client_wait_message,
    should_wait_for_qb,
    test_qb_connection,
)
from app.services.runtime_settings import build_anilibria_client

_STATUS_WAITING_SLAVE = TorrentPipelineService.STATUS_WAITING_SLAVE
_TERMINAL_OK = TorrentPipelineService._TERMINAL_OK


async def run_waiting_slave_retry(db: Session, job_id: int, params: dict[str, Any]) -> None:
    _ = params
    stats = await retry_waiting_slave_pipelines(db, job_id=job_id)
    db.add(
        JobLog(
            job_id=job_id,
            level="info",
            message=(
                "Retry waiting_slave: "
                f"checked={stats['checked']}, submitted={stats['submitted']}, "
                f"cancelled={stats['cancelled']}, still_waiting={stats['still_waiting']}, "
                f"errors={stats['errors']}, slave_up={stats['slave_up']}"
            ),
        )
    )
    db.commit()


async def retry_waiting_slave_pipelines(db: Session, *, job_id: int | None = None) -> dict[str, Any]:
    pipeline_service = TorrentPipelineService(db, job_id=job_id)
    stats: dict[str, Any] = {
        "checked": 0,
        "submitted": 0,
        "cancelled": 0,
        "still_waiting": 0,
        "errors": 0,
        "slave_up": False,
    }

    candidates = pipeline_service.get_waiting_slave_pipelines()
    if not candidates:
        return stats

    slave = pipeline_service._get_qb_client("slave")
    if slave is None:
        stats["still_waiting"] = len(candidates)
        return stats

    try:
        test_qb_connection(
            host=slave.host,
            port=slave.port,
            username=slave.username,
            password=slave.password_encrypted,
        )
    except Exception as exc:
        reason = qb_client_wait_message("slave", exc) if should_wait_for_qb(exc) else str(exc)
        for pipeline in candidates:
            try:
                pipeline_service.mark_waiting_slave(pipeline, reason)
            except Exception:
                db.rollback()
        stats["still_waiting"] = len(candidates)
        return stats

    stats["slave_up"] = True

    al_client = build_anilibria_client(db)
    passkey = await ensure_passkey_stored(db)
    if passkey:
        al_client.passkey = passkey

    for pipeline in candidates:
        stats["checked"] += 1
        try:
            if not await torrent_still_in_api(al_client, pipeline):
                pipeline_service.mark_cancelled(
                    pipeline,
                    "Торрент отсутствует в AniLibria API (по hash) — pipeline cancelled",
                )
                stats["cancelled"] += 1
                continue

            master_state = pipeline_service.classify_master_torrent(pipeline)
            if master_state == "missing":
                pipeline_service.mark_cancelled(
                    pipeline,
                    "Торрент отсутствует на master — pipeline cancelled",
                )
                stats["cancelled"] += 1
                continue

            torrent_bytes = pipeline_service.load_torrent_bytes_from_archive(pipeline)
            if torrent_bytes is None:
                torrent_bytes = await al_client.download_torrent_file(pipeline.torrent_id)
                torrent_bytes = ensure_announce_passkey(torrent_bytes, al_client.passkey)

            updated = pipeline_service.process_completion(pipeline, torrent_bytes)
            if updated.status == _STATUS_WAITING_SLAVE:
                stats["still_waiting"] += 1
                stats["slave_up"] = False
                break
            if updated.status in _TERMINAL_OK:
                stats["submitted"] += 1
            else:
                stats["errors"] += 1
                pipeline_service.mark_failed(
                    pipeline, f"waiting_slave retry: неожиданный status={updated.status}"
                )
        except Exception as exc:
            if should_wait_for_qb(exc):
                pipeline_service.mark_waiting_slave(pipeline, qb_client_wait_message("slave", exc))
                stats["still_waiting"] += 1
                stats["slave_up"] = False
                break
            stats["errors"] += 1
            try:
                pipeline_service.mark_failed(pipeline, f"waiting_slave retry: {exc}")
            except Exception:
                db.rollback()

    return stats
