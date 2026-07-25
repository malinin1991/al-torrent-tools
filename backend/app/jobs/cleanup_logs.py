"""Retention: удалять завершённые jobs и pipeline_events старше N дней."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import Job, JobLog, PipelineEvent
from app.services.job_runner import (
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_STOPPING,
    JobStopRequested,
    is_stop_requested,
)
from app.utils.datetime_fmt import utcnow

RETAIN_DAYS = 30
_ACTIVE = (STATUS_PENDING, STATUS_RUNNING, STATUS_STOPPING)


def prune_old_jobs(
    db: Session,
    *,
    retain_days: int = RETAIN_DAYS,
    protect_job_id: int | None = None,
    check_stop=None,
) -> dict[str, int]:
    """Удалить завершённые jobs старше retain_days (cascade job_logs).

    Возраст: finished_at, иначе created_at. Активные и protect_job_id не трогаем.
    """
    days = max(1, int(retain_days))
    cutoff = utcnow() - timedelta(days=days)
    if check_stop is not None:
        check_stop()

    age_expr = func.coalesce(Job.finished_at, Job.created_at)
    query = select(Job).where(
        Job.status.notin_(_ACTIVE),
        age_expr < cutoff,
    )
    stale = list(db.scalars(query).all())
    if protect_job_id is not None:
        stale = [job for job in stale if job.id != protect_job_id]

    deleted_jobs = 0
    for job in stale:
        if check_stop is not None:
            check_stop()
        db.delete(job)
        deleted_jobs += 1
    if deleted_jobs:
        db.commit()
    return {
        "deleted_jobs": deleted_jobs,
        "retain_days": days,
        "cutoff": cutoff.isoformat(),
    }


def prune_pipeline_events(
    db: Session,
    *,
    retain_days: int = RETAIN_DAYS,
    check_stop=None,
) -> dict[str, int]:
    """Удалить pipeline_events старше retain_days."""
    days = max(1, int(retain_days))
    cutoff = utcnow() - timedelta(days=days)
    if check_stop is not None:
        check_stop()
    result = db.execute(delete(PipelineEvent).where(PipelineEvent.created_at < cutoff))
    deleted = int(result.rowcount or 0)
    if deleted:
        db.commit()
    return {"deleted_events": deleted, "retain_days": days, "cutoff": cutoff.isoformat()}


async def run_cleanup_logs(db: Session, job_id: int, params: dict[str, Any]) -> None:
    retain = RETAIN_DAYS
    if params:
        if params.get("retain_days") is not None:
            retain = int(params["retain_days"])
        elif params.get("keep_per_type") is not None:
            # Совместимость со старым параметром: игнорируем число запусков, берём дни.
            retain = RETAIN_DAYS
    retain = max(1, retain)

    db.add(
        JobLog(
            job_id=job_id,
            level="info",
            message=f"cleanup_logs: удаляем завершённые jobs и pipeline_events старше {retain} дн.",
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

    job_stats = prune_old_jobs(
        db, retain_days=retain, protect_job_id=job_id, check_stop=check_stop
    )
    event_stats = prune_pipeline_events(db, retain_days=retain, check_stop=check_stop)
    db.add(
        JobLog(
            job_id=job_id,
            level="info",
            message=(
                f"cleanup_logs: удалено jobs={job_stats['deleted_jobs']}, "
                f"pipeline_events={event_stats['deleted_events']} "
                f"(retain_days={retain})"
            ),
        )
    )
    db.commit()
