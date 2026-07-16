"""Досылка pipeline в статусе waiting_master, когда master снова доступен."""

from __future__ import annotations

from typing import Any

import qbittorrentapi
from sqlalchemy.orm import Session

from app.db.models import JobLog, TorrentPipeline
from app.providers.anilibria.client import AniLibriaClient
from app.services.anilibria_auth import ensure_passkey_stored
from app.services.pipeline import TorrentPipelineService
from app.services.qbittorrent import (
    ensure_announce_passkey,
    qb_add_torrent,
    qb_client_wait_message,
    should_wait_for_qb,
    test_qb_connection,
)
from app.services.runtime_settings import build_anilibria_client


async def run_waiting_master_retry(db: Session, job_id: int, params: dict[str, Any]) -> None:
    _ = params
    stats = await retry_waiting_master_pipelines(db, job_id=job_id)
    db.add(
        JobLog(
            job_id=job_id,
            level="info",
            message=(
                "Retry waiting_master: "
                f"checked={stats['checked']}, submitted={stats['submitted']}, "
                f"cancelled={stats['cancelled']}, still_waiting={stats['still_waiting']}, "
                f"errors={stats['errors']}, master_up={stats['master_up']}"
            ),
        )
    )
    db.commit()


async def retry_waiting_master_pipelines(db: Session, *, job_id: int | None = None) -> dict[str, Any]:
    pipeline_service = TorrentPipelineService(db, job_id=job_id)
    stats: dict[str, Any] = {
        "checked": 0,
        "submitted": 0,
        "cancelled": 0,
        "still_waiting": 0,
        "errors": 0,
        "master_up": False,
    }

    candidates = pipeline_service.get_waiting_master_pipelines()
    if not candidates:
        return stats

    master = pipeline_service._get_qb_client("master")
    if master is None:
        stats["still_waiting"] = len(candidates)
        return stats

    try:
        test_qb_connection(
            host=master.host,
            port=master.port,
            username=master.username,
            password=master.password_encrypted,
        )
    except Exception as exc:
        reason = qb_client_wait_message("master", exc) if should_wait_for_qb(exc) else str(exc)
        for pipeline in candidates:
            try:
                pipeline_service.mark_waiting_master(pipeline, reason)
            except Exception:
                db.rollback()
        stats["still_waiting"] = len(candidates)
        return stats

    stats["master_up"] = True
    qb = qbittorrentapi.Client(
        host=master.host,
        port=master.port,
        username=master.username,
        password=master.password_encrypted,
    )
    qb.auth_log_in()

    al_client = build_anilibria_client(db)
    passkey = await ensure_passkey_stored(db)
    if passkey:
        al_client.passkey = passkey

    for pipeline in candidates:
        stats["checked"] += 1
        try:
            exists = await torrent_still_in_api(al_client, pipeline)
            if not exists:
                pipeline_service.mark_cancelled(
                    pipeline,
                    "Торрент отсутствует в AniLibria API (по hash) — pipeline cancelled",
                )
                stats["cancelled"] += 1
                continue

            torrent_bytes = pipeline_service.load_torrent_bytes_from_archive(pipeline)
            if torrent_bytes is None:
                torrent_bytes = await al_client.download_torrent_file(pipeline.torrent_id)
                torrent_bytes = ensure_announce_passkey(torrent_bytes, al_client.passkey)

            rename, comment, category = pipeline_service._resolve_qb_meta(pipeline)
            try:
                qb_add_torrent(
                    qb,
                    torrent_bytes,
                    rename=rename,
                    comment=comment,
                    category=category,
                )
            except Exception as add_exc:
                if should_wait_for_qb(add_exc):
                    pipeline_service.mark_waiting_master(
                        pipeline, qb_client_wait_message("master", add_exc)
                    )
                    stats["still_waiting"] += 1
                    stats["master_up"] = False
                    break
                raise

            pipeline_service.mark_master_added(pipeline)
            stats["submitted"] += 1
        except Exception as exc:
            stats["errors"] += 1
            try:
                pipeline_service.mark_failed(pipeline, f"waiting_master retry: {exc}")
            except Exception:
                db.rollback()

    return stats


async def torrent_still_in_api(client: AniLibriaClient, pipeline: TorrentPipeline) -> bool:
    """Проверка существования торрента в API: сначала по hash, затем по torrent_id."""
    if await client.torrent_exists(pipeline.info_hash):
        return True
    if pipeline.torrent_id:
        return await client.torrent_exists(pipeline.torrent_id)
    return False
