import asyncio
import logging
import threading
import zlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Job, JobLog
from app.db.session import SessionLocal

JobHandler = Callable[[Session, int, dict[str, Any]], Awaitable[None]]

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# После Ctrl+C / рестарта остаются в БД и блокируют новые джобы того же типа.
_STALE_STATUSES = (STATUS_RUNNING, STATUS_PENDING)

# Session-level advisory lock namespace: живой процесс держит lock на job_id.
# После kill соединение рвётся → lock свободен → orphan reclaim сразу отменяет джоб.
_JOB_RUN_LOCK_CLASS = 8721

# Чтобы background tasks не собрал GC до завершения.
_background_tasks: set[asyncio.Task[Any]] = set()
_active_job_ids: set[int] = set()
_active_lock = threading.Lock()


class JobAlreadyRunningError(Exception):
    """Уже есть running-джоб того же типа — второй не создаём."""

    def __init__(self, job_type: str, running_job_id: int) -> None:
        self.job_type = job_type
        self.running_job_id = running_job_id
        super().__init__(f"Джоб типа {job_type} уже выполняется (id={running_job_id})")


class UnknownJobTypeError(Exception):
    """Тип джоба не зарегистрирован в JobRunner."""

    def __init__(self, job_type: str) -> None:
        self.job_type = job_type
        super().__init__(f"Неизвестный тип джоба: {job_type}")


def _advisory_lock_key(job_type: str) -> int:
    """Стабильный 31-bit ключ для pg_advisory_xact_lock."""
    return zlib.crc32(job_type.encode("utf-8")) & 0x7FFFFFFF


def get_active_job_ids() -> list[int]:
    with _active_lock:
        return list(_active_job_ids)


def _mark_job_cancelled(db: Session, job: Job, *, reason: str, now: datetime) -> None:
    job.status = STATUS_CANCELLED
    job.error = reason
    if job.finished_at is None:
        job.finished_at = now
    db.add(
        JobLog(
            job_id=job.id,
            level="warning",
            message=f"Джоб переведён в cancelled: {reason}",
        )
    )


def cancel_stale_jobs(
    db: Session,
    *,
    reason: str = "Процесс остановлен (Ctrl+C / рестарт), джоб помечен как cancelled",
) -> list[int]:
    """Пометить все pending/running как cancelled (явный сброс, без порога возраста)."""
    stale = list(
        db.scalars(select(Job).where(Job.status.in_(_STALE_STATUSES)).order_by(Job.id.asc())).all()
    )
    if not stale:
        return []

    now = datetime.utcnow()
    cancelled_ids: list[int] = []
    for job in stale:
        _mark_job_cancelled(db, job, reason=reason, now=now)
        cancelled_ids.append(job.id)
    db.commit()
    return cancelled_ids


def cancel_jobs_by_ids(
    db: Session,
    job_ids: list[int],
    *,
    reason: str = "Процесс остановлен, джоб помечен как cancelled",
) -> list[int]:
    """Отменить конкретные джобы (graceful shutdown текущего процесса)."""
    if not job_ids:
        return []
    jobs = list(
        db.scalars(
            select(Job).where(Job.id.in_(job_ids), Job.status.in_(_STALE_STATUSES)).order_by(Job.id.asc())
        ).all()
    )
    if not jobs:
        return []
    now = datetime.utcnow()
    cancelled_ids: list[int] = []
    for job in jobs:
        _mark_job_cancelled(db, job, reason=reason, now=now)
        cancelled_ids.append(job.id)
    db.commit()
    return cancelled_ids


def _job_last_activity_at(db: Session, job: Job) -> datetime | None:
    """Якорь «живости»: для running — последний лог, иначе started_at/created_at."""
    if job.status == STATUS_RUNNING:
        last_log_at = db.scalar(select(func.max(JobLog.created_at)).where(JobLog.job_id == job.id))
        if last_log_at is not None:
            return last_log_at
        return job.started_at or job.created_at
    return job.created_at or job.started_at


def _try_run_lock(db: Session, job_id: int) -> bool:
    return bool(
        db.execute(
            text("SELECT pg_try_advisory_lock(:cls, :oid)"),
            {"cls": _JOB_RUN_LOCK_CLASS, "oid": job_id},
        ).scalar()
    )


def _unlock_run_lock(db: Session, job_id: int) -> None:
    db.execute(
        text("SELECT pg_advisory_unlock(:cls, :oid)"),
        {"cls": _JOB_RUN_LOCK_CLASS, "oid": job_id},
    )


def _acquire_run_lock(db: Session, job_id: int) -> None:
    db.execute(
        text("SELECT pg_advisory_lock(:cls, :oid)"),
        {"cls": _JOB_RUN_LOCK_CLASS, "oid": job_id},
    )


def reclaim_orphan_jobs(
    db: Session,
    *,
    reason: str = "Джоб-сирота (процесс умер) помечен как cancelled",
    pending_grace_sec: int = 60,
) -> list[int]:
    """Отменить pending/running, которые больше никто не держит.

    running: session advisory lock свободен → runner мёртв (kill/crash).
    pending: старше grace — create без старта (падение между create и run).
    Живые джобы другого контейнера lock держат — не трогаем.
    """
    candidates = list(
        db.scalars(select(Job).where(Job.status.in_(_STALE_STATUSES)).order_by(Job.id.asc())).all()
    )
    if not candidates:
        return []

    now = datetime.utcnow()
    pending_threshold = now - timedelta(seconds=max(0, pending_grace_sec))
    cancelled_ids: list[int] = []
    for job in candidates:
        if job.status == STATUS_PENDING:
            created = job.created_at or now
            if created > pending_threshold:
                continue
            _mark_job_cancelled(db, job, reason=reason, now=now)
            cancelled_ids.append(job.id)
            continue

        # RUNNING: lock свободен только если runner мёртв.
        if not _try_run_lock(db, job.id):
            continue
        try:
            _mark_job_cancelled(db, job, reason=reason, now=now)
            cancelled_ids.append(job.id)
        finally:
            _unlock_run_lock(db, job.id)

    if cancelled_ids:
        db.commit()
    return cancelled_ids


def reclaim_stale_jobs(
    db: Session,
    *,
    max_age_minutes: int | None = None,
    reason: str | None = None,
) -> list[int]:
    """Отменить pending/running без активности дольше порога (fallback без lock)."""
    age = max_age_minutes if max_age_minutes is not None else settings.job_stale_minutes
    if age <= 0:
        return []
    threshold = datetime.utcnow() - timedelta(minutes=age)
    msg = reason or (
        f"Джоб без активности дольше {age} мин (pending/running) и помечен как cancelled"
    )
    candidates = list(
        db.scalars(select(Job).where(Job.status.in_(_STALE_STATUSES)).order_by(Job.id.asc())).all()
    )
    cancelled_ids: list[int] = []
    now = datetime.utcnow()
    for job in candidates:
        anchor = _job_last_activity_at(db, job)
        if anchor is None or anchor > threshold:
            continue
        # Не отменяем живой джоб другого процесса (lock занят).
        held_lock = False
        if job.status == STATUS_RUNNING:
            if not _try_run_lock(db, job.id):
                continue
            held_lock = True
        try:
            _mark_job_cancelled(db, job, reason=msg, now=now)
            cancelled_ids.append(job.id)
        finally:
            if held_lock:
                try:
                    _unlock_run_lock(db, job.id)
                except Exception:
                    pass
    if cancelled_ids:
        db.commit()
    return cancelled_ids


def shutdown_cancel_active_jobs(
    *,
    reason: str = "Процесс остановлен, джоб помечен как cancelled",
) -> list[int]:
    """Graceful shutdown: отменить джобы, которые крутит этот процесс."""
    job_ids = get_active_job_ids()
    if not job_ids:
        return []
    with SessionLocal() as db:
        return cancel_jobs_by_ids(db, job_ids, reason=reason)


class JobRunner:
    def __init__(self) -> None:
        self._handlers: dict[str, JobHandler] = {}

    def register(self, job_type: str, handler: JobHandler) -> None:
        self._handlers[job_type] = handler

    def known_types(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def create_job(self, db: Session, job_type: str, params: dict[str, Any] | None = None) -> Job:
        if job_type not in self._handlers:
            raise UnknownJobTypeError(job_type)

        # Сериализует create одного типа между api/worker (PostgreSQL).
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _advisory_lock_key(job_type)})

        running = db.scalar(
            select(Job)
            .where(Job.type == job_type, Job.status.in_((STATUS_RUNNING, STATUS_PENDING)))
            .limit(1)
        )
        if running is not None:
            raise JobAlreadyRunningError(job_type, running.id)

        job = Job(type=job_type, status=STATUS_PENDING, params_json=params or {})
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    def add_log(self, db: Session, job_id: int, message: str, level: str = "info") -> None:
        db.add(JobLog(job_id=job_id, level=level, message=message))
        db.commit()

    async def run_job(self, db: Session, job_id: int) -> Job:
        job = db.get(Job, job_id)
        if job is None:
            raise ValueError(f"Джоб {job_id} не найден")
        if job.type not in self._handlers:
            raise ValueError(f"Тип джоба {job.type} не зарегистрирован")

        # Держим session-lock, пока живёт runner; после kill PG отпустит lock.
        _acquire_run_lock(db, job_id)
        with _active_lock:
            _active_job_ids.add(job_id)

        job.status = STATUS_RUNNING
        job.started_at = datetime.utcnow()
        job.error = None
        job.finished_at = None
        db.commit()
        self.add_log(db, job.id, f"Старт джоба {job.type}")

        final_status = STATUS_SUCCESS
        final_error: str | None = None
        try:
            await self._handlers[job.type](db, job.id, job.params_json or {})
            self.add_log(db, job.id, "Джоб завершен успешно")
        except asyncio.CancelledError:
            final_status = STATUS_CANCELLED
            final_error = "Прервано (Ctrl+C / shutdown)"
            self.add_log(db, job.id, "Джоб отменён (CancelledError)", level="warning")
            raise
        except Exception as exc:
            final_status = STATUS_FAILED
            final_error = str(exc)
            self.add_log(db, job.id, f"Ошибка выполнения: {exc}", level="error")
        finally:
            try:
                db.refresh(job)
                # Reclaim/shutdown мог уже пометить cancelled — не воскрешаем слот.
                if job.status != STATUS_CANCELLED or final_status == STATUS_CANCELLED:
                    job.status = final_status
                    job.error = final_error
                if job.finished_at is None:
                    job.finished_at = datetime.utcnow()
                db.commit()
                db.refresh(job)
            finally:
                with _active_lock:
                    _active_job_ids.discard(job_id)
                try:
                    _unlock_run_lock(db, job_id)
                    db.commit()
                except Exception:
                    logger.exception("Не удалось снять run-lock для job_id=%s", job_id)
        return job

    def schedule_job(self, job_id: int) -> asyncio.Task[Any]:
        """Запустить джоб в фоне в отдельном потоке (sync qB не блокирует UI)."""

        def _thread_main() -> None:
            with SessionLocal() as db:
                try:
                    asyncio.run(self.run_job(db, job_id))
                except Exception:
                    logger.exception("Фоновый джоб id=%s завершился с ошибкой", job_id)

        async def _runner() -> None:
            await asyncio.to_thread(_thread_main)

        task = asyncio.create_task(_runner(), name=f"job-{job_id}")
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return task
