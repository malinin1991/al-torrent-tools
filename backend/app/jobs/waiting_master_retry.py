"""Досылка pipeline в статусе waiting_master, когда master снова доступен."""

from __future__ import annotations

from typing import Any

import qbittorrentapi
from sqlalchemy.orm import Session

from app.db.models import JobLog, TorrentPipeline
from app.providers.anilibria.client import AniLibriaClient
from app.services.anilibria_auth import ensure_passkey_stored
from app.services.job_runner import JobStopRequested, is_stop_requested
from app.services.pipeline import TorrentPipelineService
from app.services.qbittorrent import (
    ensure_announce_passkey,
    qb_add_torrent,
    qb_client_wait_message,
    should_wait_for_qb,
    test_qb_connection,
)
from app.services.runtime_settings import build_anilibria_client


def _check_stop(db: Session, job_id: int | None) -> None:
    if job_id is None:
        return
    if is_stop_requested(db, job_id):
        db.add(
            JobLog(
                job_id=job_id,
                level="warning",
                message="waiting_master_retry: остановка по запросу",
            )
        )
        db.commit()
        raise JobStopRequested()


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
                f"recovered={stats['recovered']}, errors={stats['errors']}, "
                f"master_up={stats['master_up']}"
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
        "recovered": 0,
        "errors": 0,
        "master_up": False,
    }

    waiting = pipeline_service.get_waiting_master_pipelines()
    failed_recoverable = pipeline_service.get_failed_qb_wait_pipelines()
    if not waiting and not failed_recoverable:
        return stats

    master = pipeline_service._get_qb_client("master")
    if master is None:
        stats["still_waiting"] = len(waiting) + len(failed_recoverable)
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
        for pipeline in waiting:
            try:
                pipeline_service.mark_waiting_master(pipeline, reason)
            except Exception:
                db.rollback()
        stats["still_waiting"] = len(waiting) + len(failed_recoverable)
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

    # Recovery: failed из‑за connection → если уже на master, вернуть в master_added;
    # если нет — повторить add (как waiting_master).
    for pipeline in failed_recoverable:
        _check_stop(db, job_id)
        stats["checked"] += 1
        try:
            recovered = await _recover_failed_pipeline(
                pipeline_service,
                al_client,
                qb,
                pipeline,
                stats,
            )
            if not recovered:
                stats["still_waiting"] += 1
        except JobStopRequested:
            raise
        except Exception as exc:
            if should_wait_for_qb(exc):
                stats["still_waiting"] += 1
                stats["master_up"] = False
                break
            stats["errors"] += 1
            try:
                pipeline_service.mark_failed(pipeline, f"waiting_master retry: {exc}")
            except Exception:
                db.rollback()

    for pipeline in waiting:
        _check_stop(db, job_id)
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

            await _add_pipeline_to_master(pipeline_service, al_client, qb, pipeline)
            stats["submitted"] += 1
        except JobStopRequested:
            raise
        except Exception as exc:
            if should_wait_for_qb(exc):
                pipeline_service.mark_waiting_master(
                    pipeline, qb_client_wait_message("master", exc)
                )
                stats["still_waiting"] += 1
                stats["master_up"] = False
                break
            stats["errors"] += 1
            try:
                pipeline_service.mark_failed(pipeline, f"waiting_master retry: {exc}")
            except Exception:
                db.rollback()

    return stats


async def _recover_failed_pipeline(
    pipeline_service: TorrentPipelineService,
    al_client: AniLibriaClient,
    qb: qbittorrentapi.Client,
    pipeline: TorrentPipeline,
    stats: dict[str, Any],
) -> bool:
    """True если pipeline выведен из failed (master_added / slave / cancelled)."""
    state = pipeline_service.classify_master_torrent(pipeline)
    if state in {"complete", "in_progress"}:
        pipeline_service.mark_master_added(pipeline)
        stats["recovered"] += 1
        if state == "complete":
            torrent_bytes = pipeline_service.load_torrent_bytes_from_archive(pipeline)
            if torrent_bytes is None:
                torrent_bytes = await al_client.download_torrent_file(pipeline.torrent_id)
                torrent_bytes = ensure_announce_passkey(torrent_bytes, al_client.passkey)
            updated = pipeline_service.process_completion(pipeline, torrent_bytes)
            if updated.status == pipeline_service.STATUS_WAITING_SLAVE:
                stats["still_waiting"] += 1
            elif updated.status in pipeline_service._SLAVE_REACHED:
                stats["submitted"] += 1
        return True

    # Нет на master — повторить add, если торрент ещё есть в API.
    exists = await torrent_still_in_api(al_client, pipeline)
    if not exists:
        pipeline_service.mark_cancelled(
            pipeline,
            "Торрент отсутствует в AniLibria API (по hash) — pipeline cancelled",
        )
        stats["cancelled"] += 1
        return True

    await _add_pipeline_to_master(pipeline_service, al_client, qb, pipeline)
    stats["recovered"] += 1
    stats["submitted"] += 1
    return True


async def _add_pipeline_to_master(
    pipeline_service: TorrentPipelineService,
    al_client: AniLibriaClient,
    qb: qbittorrentapi.Client,
    pipeline: TorrentPipeline,
) -> None:
    torrent_bytes = pipeline_service.load_torrent_bytes_from_archive(pipeline)
    if torrent_bytes is None:
        torrent_bytes = await al_client.download_torrent_file(pipeline.torrent_id)
        torrent_bytes = ensure_announce_passkey(torrent_bytes, al_client.passkey)

    rename, comment, category, tags = pipeline_service._resolve_qb_meta(pipeline)
    try:
        qb_add_torrent(
            qb,
            torrent_bytes,
            rename=rename,
            comment=comment,
            category=category,
            tags=tags,
        )
    except Exception as add_exc:
        if should_wait_for_qb(add_exc):
            pipeline_service.mark_waiting_master(
                pipeline, qb_client_wait_message("master", add_exc)
            )
        raise

    pipeline_service.mark_master_added(pipeline)


async def torrent_still_in_api(client: AniLibriaClient, pipeline: TorrentPipeline) -> bool:
    """Проверка существования торрента в API: сначала по hash, затем по torrent_id."""
    if await client.torrent_exists(pipeline.info_hash):
        return True
    if pipeline.torrent_id:
        return await client.torrent_exists(pipeline.torrent_id)
    return False
