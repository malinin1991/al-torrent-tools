"""Retention: оставить последние N запусков jobs на каждый тип."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job, JobLog
from app.services.job_runner import (
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_STOPPING,
    JobStopRequested,
    is_stop_requested,
)

KEEP_RUNS_PER_TYPE = 100
_ACTIVE = (STATUS_PENDING, STATUS_RUNNING, STATUS_STOPPING)


def prune_old_jobs(
    db: Session,
    *,
    keep_per_type: int = KEEP_RUNS_PER_TYPE,
    protect_job_id: int | None = None,
    check_stop=None,
) -> dict[str, int]:
    """Удалить старые jobs (cascade job_logs). Активные и protect_job_id не трогаем."""
    keep = max(1, keep_per_type)
    types = list(db.scalars(select(Job.type).distinct().order_by(Job.type.asc())).all())
    deleted_jobs = 0
    deleted_types = 0
    for job_type in types:
        if check_stop is not None:
            check_stop()
        keep_ids = list(
            db.scalars(
                select(Job.id).where(Job.type == job_type).order_by(Job.id.desc()).limit(keep)
            ).all()
        )
        if not keep_ids:
            continue
        stale = list(
            db.scalars(
                select(Job).where(
                    Job.type == job_type,
                    Job.id.notin_(keep_ids),
                    Job.status.notin_(_ACTIVE),
                )
            ).all()
        )
        if protect_job_id is not None:
            stale = [job for job in stale if job.id != protect_job_id]
        if not stale:
            continue
        deleted_types += 1
        for job in stale:
            db.delete(job)
            deleted_jobs += 1
    if deleted_jobs:
        db.commit()
    return {"types_touched": deleted_types, "deleted_jobs": deleted_jobs, "keep_per_type": keep}


async def run_cleanup_logs(db: Session, job_id: int, params: dict[str, Any]) -> None:
    keep = int(params.get("keep_per_type") or KEEP_RUNS_PER_TYPE) if params else KEEP_RUNS_PER_TYPE
    db.add(
        JobLog(
            job_id=job_id,
            level="info",
            message=f"cleanup_logs: оставляем последние {keep} запусков на тип",
        )
    )
    db.commit()

    def check_stop() -> None:
        if is_stop_requested(db, job_id):
            db.add(
                JobLog(
                    job_id=job_id,
                    level="warning",
                    message="cleanup_logs: остановка по запросу",
                )
            )
            db.commit()
            raise JobStopRequested()

    stats = prune_old_jobs(
        db, keep_per_type=keep, protect_job_id=job_id, check_stop=check_stop
    )
    db.add(
        JobLog(
            job_id=job_id,
            level="info",
            message=(
                f"cleanup_logs: удалено jobs={stats['deleted_jobs']}, "
                f"типов затронуто={stats['types_touched']}"
            ),
        )
    )
    db.commit()
