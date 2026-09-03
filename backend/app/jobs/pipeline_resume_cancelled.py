"""Возобновить cancelled pipeline, если торрент снова есть в qB."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import JobLog, TorrentPipeline
from app.jobs.pipeline_reconcile import load_torrent_bytes_with_fallback
from app.services.job_runner import JobStopRequested, is_stop_requested
from app.services.pipeline import TorrentPipelineService


def _merge_resume_stats(total: dict[str, Any], part: dict[str, Any]) -> None:
    for key in ("checked", "resumed", "sent_to_slave", "done", "waiting", "skipped", "errors"):
        total[key] = int(total.get(key, 0) or 0) + int(part.get(key, 0) or 0)
    total.setdefault("details", []).extend(part.get("details") or [])


async def run_pipeline_resume_cancelled(db: Session, job_id: int, params: dict[str, Any]) -> None:
    raw_id = params.get("pipeline_id")
    pipeline_id: int | None
    if raw_id is None or raw_id == "":
        pipeline_id = None
    else:
        try:
            pipeline_id = int(raw_id)
        except (TypeError, ValueError):
            db.add(
                JobLog(
                    job_id=job_id,
                    level="error",
                    message=f"pipeline_resume_cancelled: некорректный pipeline_id={raw_id!r}",
                )
            )
            db.commit()
            return

    service = TorrentPipelineService(db, job_id=job_id)
    candidates = service.get_cancelled_resume_candidates(pipeline_id=pipeline_id)
    stats: dict[str, Any] = {
        "checked": 0,
        "resumed": 0,
        "sent_to_slave": 0,
        "done": 0,
        "waiting": 0,
        "skipped": 0,
        "errors": 0,
        "details": [],
    }

    if pipeline_id is not None and not candidates:
        row = db.get(TorrentPipeline, pipeline_id)
        if row is None:
            stats["errors"] += 1
            stats["details"].append(
                {"id": pipeline_id, "action": "error", "error": "pipeline не найден"}
            )
        elif row.status != service.STATUS_CANCELLED:
            stats["skipped"] += 1
            stats["details"].append(
                {
                    "id": row.id,
                    "hash": row.info_hash,
                    "action": "skipped_not_cancelled",
                    "status": row.status,
                }
            )
        else:
            stats["skipped"] += 1
            stats["details"].append(
                {
                    "id": row.id,
                    "hash": row.info_hash,
                    "action": "skipped_not_qb_missing",
                }
            )

    for pipeline in candidates:
        if is_stop_requested(db, job_id):
            db.add(
                JobLog(
                    job_id=job_id,
                    level="warning",
                    message="pipeline_resume_cancelled: остановка по запросу",
                )
            )
            db.commit()
            raise JobStopRequested()

        torrent_bytes: bytes | None = None
        try:
            torrent_bytes = await load_torrent_bytes_with_fallback(db, service, pipeline)
        except Exception:
            torrent_bytes = service.load_torrent_bytes_from_archive(pipeline)

        def load_one(p, cached=torrent_bytes, svc=service, pid=pipeline.id):
            if p.id == pid and cached is not None:
                return cached
            return svc.load_torrent_bytes_from_archive(p)

        part = service.resume_cancelled_from_qb(
            load_torrent_bytes=load_one,
            pipeline_id=pipeline.id,
        )
        _merge_resume_stats(stats, part)

    db.add(
        JobLog(
            job_id=job_id,
            level="info",
            message=(
                "Resume cancelled: "
                f"checked={stats['checked']}, resumed={stats['resumed']}, "
                f"sent={stats['sent_to_slave']}, done={stats['done']}, "
                f"waiting={stats['waiting']}, skipped={stats['skipped']}, "
                f"errors={stats['errors']}"
            ),
        )
    )
    db.commit()
