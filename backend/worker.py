import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError

from app.api.rest import job_runner
from app.core.config import settings
from app.db.models import Setting
from app.db.session import SessionLocal
from app.jobs.pipeline_reconcile import load_torrent_bytes_with_fallback
from app.services.job_runner import JobAlreadyRunningError, reclaim_stale_jobs
from app.services.pipeline import TorrentPipelineService

logger = logging.getLogger(__name__)

# Интервалы ongoing/cleanup читаются при старте и периодически перечитываются в main-loop
# (reschedule). pipeline_master_min_age_min читается на каждом тике _poll_master_pipeline.


async def _run_by_type(job_type: str) -> None:
    params = {"dry_run": True} if job_type == "cleanup" else {}
    with SessionLocal() as db:
        try:
            job = job_runner.create_job(db, job_type, params)
        except JobAlreadyRunningError as exc:
            logger.info("Пропуск %s: уже running id=%s", job_type, exc.running_job_id)
            return
        await job_runner.run_job(db, job.id)


async def _poll_master_pipeline() -> None:
    """Частый fallback: aged master_added → slave; missing → cancelled."""
    with SessionLocal() as db:
        age_minutes = _setting_int("pipeline_master_min_age_min", settings.pipeline_master_min_age_min)
        pipeline_service = TorrentPipelineService(db)
        candidates = pipeline_service.get_master_added_older_than(age_minutes)
        for pipeline in candidates:
            try:
                state = pipeline_service.classify_master_torrent(pipeline)
                if state == "in_progress":
                    continue
                if state == "missing":
                    pipeline_service.mark_cancelled(
                        pipeline,
                        "Торрент отсутствует на master (удалён) — pipeline cancelled",
                    )
                    continue
                torrent_bytes = await load_torrent_bytes_with_fallback(db, pipeline_service, pipeline)
                if torrent_bytes is None:
                    raise RuntimeError("Нет .torrent в архиве и не удалось загрузить файл")
                pipeline_service.process_completion(pipeline, torrent_bytes)
            except Exception as exc:
                pipeline_service.mark_failed(pipeline, str(exc))


async def _daily_pipeline_reconcile() -> None:
    with SessionLocal() as db:
        try:
            job = job_runner.create_job(db, "pipeline_reconcile", {})
        except JobAlreadyRunningError as exc:
            logger.info("Пропуск pipeline_reconcile: уже running id=%s", exc.running_job_id)
            return
        await job_runner.run_job(db, job.id)


async def _retry_waiting_master() -> None:
    """Раз в 5 мин: если master ожил — дослать waiting_master (или cancel если нет в API)."""
    with SessionLocal() as db:
        try:
            job = job_runner.create_job(db, "waiting_master_retry", {})
        except JobAlreadyRunningError as exc:
            logger.info("Пропуск waiting_master_retry: уже running id=%s", exc.running_job_id)
            return
        await job_runner.run_job(db, job.id)


async def _retry_waiting_slave() -> None:
    """Раз в 5 мин: если slave ожил — проверить API/master и дослать waiting_slave."""
    with SessionLocal() as db:
        try:
            job = job_runner.create_job(db, "waiting_slave_retry", {})
        except JobAlreadyRunningError as exc:
            logger.info("Пропуск waiting_slave_retry: уже running id=%s", exc.running_job_id)
            return
        await job_runner.run_job(db, job.id)


def _setting_int(key: str, default: int) -> int:
    with SessionLocal() as db:
        try:
            row = db.scalar(select(Setting).where(Setting.key == key).limit(1))
        except ProgrammingError:
            # Таблица `settings` может отсутствовать, если миграции не применены к worker-контейнеру.
            # В этом случае используем значение из конфигурации по умолчанию.
            return default
        if row is None:
            return default
        try:
            return int(row.value)
        except (TypeError, ValueError):
            return default


def _reschedule_if_needed(scheduler: AsyncIOScheduler, job_id: str, seconds: int) -> None:
    job = scheduler.get_job(job_id)
    if job is None:
        return
    current = int(job.trigger.interval.total_seconds())  # type: ignore[attr-defined]
    if current != seconds:
        scheduler.reschedule_job(job_id, trigger="interval", seconds=seconds)
        logger.info("Scheduler %s: interval %s → %s сек", job_id, current, seconds)


async def _reclaim_stale_jobs() -> None:
    with SessionLocal() as db:
        cancelled = reclaim_stale_jobs(db)
        if cancelled:
            logger.warning("Reclaim: отменены зависшие джобы: %s", cancelled)


async def main() -> None:
    with SessionLocal() as db:
        # Порог бездействия, не blanket-cancel: api может ещё выполнять джоб в памяти.
        cancelled = reclaim_stale_jobs(db)
        if cancelled:
            logger.warning(
                "При старте worker помечены cancelled зависшие джобы: %s",
                cancelled,
            )

    ongoing_interval = _setting_int("ongoing_interval_sec", settings.ongoing_interval_sec)
    cleanup_interval = _setting_int("cleanup_interval_sec", settings.cleanup_interval_sec)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _run_by_type,
        "interval",
        seconds=ongoing_interval,
        args=["ongoing"],
        id="ongoing",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _run_by_type,
        "interval",
        seconds=cleanup_interval,
        args=["cleanup"],
        id="cleanup",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _poll_master_pipeline,
        "interval",
        seconds=60,
        id="pipeline_poll",
        max_instances=1,
        coalesce=True,
    )
    # Полная сверка master↔pipeline без slave (пропущенный webhook).
    scheduler.add_job(
        _daily_pipeline_reconcile,
        "interval",
        hours=24,
        id="pipeline_reconcile_daily",
        max_instances=1,
        coalesce=True,
    )
    # Master лежал → waiting_master; проверяем ожил ли (и жив ли торрент в API).
    scheduler.add_job(
        _retry_waiting_master,
        "interval",
        minutes=5,
        id="waiting_master_retry",
        max_instances=1,
        coalesce=True,
    )
    # Slave лежал → waiting_slave; API + master + досылка на slave.
    scheduler.add_job(
        _retry_waiting_slave,
        "interval",
        minutes=5,
        id="waiting_slave_retry",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _reclaim_stale_jobs,
        "interval",
        minutes=5,
        id="job_stale_reclaim",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()

    while True:
        await asyncio.sleep(60)
        # Перечитываем интервалы из settings и при необходимости reschedule.
        _reschedule_if_needed(
            scheduler, "ongoing", _setting_int("ongoing_interval_sec", settings.ongoing_interval_sec)
        )
        _reschedule_if_needed(
            scheduler, "cleanup", _setting_int("cleanup_interval_sec", settings.cleanup_interval_sec)
        )


if __name__ == "__main__":
    asyncio.run(main())
