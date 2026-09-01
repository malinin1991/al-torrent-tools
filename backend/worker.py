import asyncio
import logging
import signal
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError

from app.api.rest import job_runner
from app.core.config import settings
from app.db.models import Setting
from app.db.session import SessionLocal
from app.jobs.pipeline_reconcile import load_torrent_bytes_with_fallback
from app.services.job_catalog import FULL_SYNC_DAILY_HOUR, FULL_SYNC_DAILY_MINUTE
from app.services.job_runner import (
    JobAlreadyRunningError,
    reclaim_orphan_jobs,
    reclaim_stale_jobs,
    shutdown_cancel_active_jobs,
)
from app.services.pipeline import TorrentPipelineService
from app.services.qbittorrent import qb_client_wait_message, should_wait_for_qb

logger = logging.getLogger(__name__)

_HEALTH_FILE = Path("/tmp/altt_worker_healthy")

# Интервалы ongoing/cleanup/reconcile читаются при старте и периодически перечитываются
# в main-loop (reschedule). pipeline_master_min_age_min — на каждом тике _poll_master_pipeline.


def _touch_health() -> None:
    try:
        _HEALTH_FILE.touch()
    except OSError:
        logger.exception("Не удалось обновить worker health-файл")


async def _run_by_type(job_type: str) -> None:
    params: dict = {}
    if job_type == "cleanup_master":
        params = {"dry_run": True, "target_role": "master"}
    elif job_type == "cleanup_slave":
        params = {"dry_run": True, "target_role": "slave"}
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
        pipeline_service = TorrentPipelineService(db, actor="poll")
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
                if should_wait_for_qb(exc):
                    # Не failed: master/slave временно недоступен — оставить master_added
                    # или waiting (process_completion уже мог выставить waiting_slave).
                    logger.warning(
                        "Pipeline %s: %s — статус не меняем на failed",
                        pipeline.id,
                        qb_client_wait_message("master", exc),
                    )
                    continue
                pipeline_service.mark_failed(pipeline, str(exc))


async def _poll_slave_pipeline() -> None:
    """Частый fallback: aged slave_added → done; missing → cancelled."""
    with SessionLocal() as db:
        age_minutes = _setting_int("pipeline_master_min_age_min", settings.pipeline_master_min_age_min)
        pipeline_service = TorrentPipelineService(db, actor="poll")
        candidates = pipeline_service.get_slave_added_older_than(age_minutes)
        for pipeline in candidates:
            try:
                pipeline_service.process_slave_completion(pipeline)
            except Exception as exc:
                if should_wait_for_qb(exc):
                    logger.warning(
                        "Pipeline %s: %s — статус не меняем на failed",
                        pipeline.id,
                        qb_client_wait_message("slave", exc),
                    )
                    continue
                pipeline_service.mark_failed(pipeline, str(exc))


async def _pipeline_reconcile() -> None:
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
        orphans = reclaim_orphan_jobs(db)
        if orphans:
            logger.warning("Reclaim orphans: отменены джобы-сироты: %s", orphans)
        cancelled = reclaim_stale_jobs(db)
        if cancelled:
            logger.warning("Reclaim: отменены зависшие джобы: %s", cancelled)


async def main() -> None:
    from app.logging_filters import setup_redacted_logging

    setup_redacted_logging(level=logging.INFO)
    with SessionLocal() as db:
        orphans = reclaim_orphan_jobs(
            db,
            reason="Worker перезапущен — джоб-сирота помечен как cancelled",
        )
        if orphans:
            logger.warning(
                "При старте worker помечены cancelled джобы-сироты: %s",
                orphans,
            )
        cancelled = reclaim_stale_jobs(db)
        if cancelled:
            logger.warning(
                "При старте worker помечены cancelled зависшие джобы: %s",
                cancelled,
            )

    ongoing_interval = _setting_int("ongoing_interval_sec", settings.ongoing_interval_sec)
    cleanup_interval = _setting_int("cleanup_interval_sec", settings.cleanup_interval_sec)
    reconcile_interval = _setting_int(
        "pipeline_reconcile_interval_sec", settings.pipeline_reconcile_interval_sec
    )
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
        args=["cleanup_master"],
        id="cleanup_master",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _run_by_type,
        "interval",
        seconds=cleanup_interval,
        args=["cleanup_slave"],
        id="cleanup_slave",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _run_by_type,
        "interval",
        seconds=86_400,
        args=["cleanup_logs"],
        id="cleanup_logs",
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
    scheduler.add_job(
        _poll_slave_pipeline,
        "interval",
        seconds=60,
        id="pipeline_slave_poll",
        max_instances=1,
        coalesce=True,
    )
    # Полная сверка master↔pipeline без slave (пропущенный webhook).
    scheduler.add_job(
        _pipeline_reconcile,
        "interval",
        seconds=max(60, reconcile_interval),
        id="pipeline_reconcile",
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
        _run_by_type,
        CronTrigger(hour=FULL_SYNC_DAILY_HOUR, minute=FULL_SYNC_DAILY_MINUTE),
        args=["full_sync"],
        id="full_sync_daily",
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
    _touch_health()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows / ограниченные среды — полагаемся на KeyboardInterrupt.
            pass

    try:
        while not stop.is_set():
            _touch_health()
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                break
            # Перечитываем интервалы из settings и при необходимости reschedule.
            _reschedule_if_needed(
                scheduler, "ongoing", _setting_int("ongoing_interval_sec", settings.ongoing_interval_sec)
            )
            cleanup_sec = _setting_int("cleanup_interval_sec", settings.cleanup_interval_sec)
            _reschedule_if_needed(scheduler, "cleanup_master", cleanup_sec)
            _reschedule_if_needed(scheduler, "cleanup_slave", cleanup_sec)
            _reschedule_if_needed(
                scheduler,
                "pipeline_reconcile",
                max(
                    60,
                    _setting_int(
                        "pipeline_reconcile_interval_sec", settings.pipeline_reconcile_interval_sec
                    ),
                ),
            )
    finally:
        try:
            _HEALTH_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        scheduler.shutdown(wait=False)
        cancelled = shutdown_cancel_active_jobs(
            reason="Worker остановлен — джоб помечен как cancelled"
        )
        if cancelled:
            logger.warning("При остановке worker помечены cancelled активные джобы: %s", cancelled)


if __name__ == "__main__":
    asyncio.run(main())
