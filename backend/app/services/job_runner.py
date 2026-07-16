import asyncio
import logging
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

# Чтобы background tasks не собрал GC до завершения.
_background_tasks: set[asyncio.Task[Any]] = set()


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


def cancel_stale_jobs(
    db: Session,
    *,
    reason: str = "Процесс остановлен (Ctrl+C / рестарт), джоб помечен как cancelled",
) -> list[int]:
    """Пометить зависшие pending/running как cancelled. Возвращает id затронутых джобов.

    Агрессивная отмена без порога возраста — только для явного сброса состояния,
    не вызывать при старте api/worker (общая БД: чужой процесс может ещё крутить джоб).
    """
    stale = list(
        db.scalars(select(Job).where(Job.status.in_(_STALE_STATUSES)).order_by(Job.id.asc())).all()
    )
    if not stale:
        return []

    now = datetime.utcnow()
    cancelled_ids: list[int] = []
    for job in stale:
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


def reclaim_stale_jobs(
    db: Session,
    *,
    max_age_minutes: int | None = None,
    reason: str | None = None,
) -> list[int]:
    """Отменить pending/running без активности дольше порога.

    Долгие живые джобы (full_sync и т.п.) пишут логи — их не трогаем.
    """
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
        job.status = STATUS_CANCELLED
        job.error = msg
        if job.finished_at is None:
            job.finished_at = now
        db.add(
            JobLog(
                job_id=job.id,
                level="warning",
                message=f"Джоб переведён в cancelled: {msg}",
            )
        )
        cancelled_ids.append(job.id)
    if cancelled_ids:
        db.commit()
    return cancelled_ids


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

        job.status = STATUS_RUNNING
        job.started_at = datetime.utcnow()
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
            db.refresh(job)
            # Reclaim мог уже пометить cancelled — не воскрешаем слот.
            if job.status != STATUS_CANCELLED or final_status == STATUS_CANCELLED:
                job.status = final_status
                job.error = final_error
            if job.finished_at is None:
                job.finished_at = datetime.utcnow()
            db.commit()
            db.refresh(job)
        return job

    def schedule_job(self, job_id: int) -> asyncio.Task[Any]:
        """Запустить джоб в фоне со своей DB-сессией (UI/API не ждут завершения)."""

        async def _runner() -> None:
            with SessionLocal() as db:
                try:
                    await self.run_job(db, job_id)
                except Exception:
                    logger.exception("Фоновый джоб id=%s завершился с ошибкой", job_id)

        task = asyncio.create_task(_runner(), name=f"job-{job_id}")
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return task
