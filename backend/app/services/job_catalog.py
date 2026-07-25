"""Каталог типов джобов для UI: описание, режимы запуска, последний прогон."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Job, Setting
from app.services.job_runner import STATUS_PENDING, STATUS_RUNNING, STATUS_STOPPING


@dataclass(frozen=True, slots=True)
class JobTypeDef:
    type: str
    title: str
    description: str
    # Можно ли запускать вручную без спец. параметров.
    manual_run: bool = True
    # Кнопки: default | dry_run | apply
    run_modes: tuple[str, ...] = ("default",)
    manual_hint: str = ""
    # Ключ settings / default секунд для «следующий» (None = ручной, без расписания).
    interval_setting_key: str | None = None
    interval_default_sec: int | None = None


# Порядок = отображение на /jobs.
JOB_TYPE_DEFS: tuple[JobTypeDef, ...] = (
    JobTypeDef(
        type="ongoing",
        title="Ongoing sync",
        description="Периодическая проверка обновлений релизов AniLibria и постановка новых торрентов в pipeline.",
        interval_setting_key="ongoing_interval_sec",
        interval_default_sec=settings.ongoing_interval_sec,
    ),
    JobTypeDef(
        type="full_sync",
        title="Full sync",
        description="Полный проход по каталогу/расписанию: сверка торрентов с API, api_present, архив.",
    ),
    JobTypeDef(
        type="cleanup_master",
        title="Cleanup Master",
        description="Очистка раздач в master qB по правилам (незарегистрированные и т.п.). По умолчанию dry-run.",
        run_modes=("dry_run", "apply"),
        interval_setting_key="cleanup_interval_sec",
        interval_default_sec=settings.cleanup_interval_sec,
    ),
    JobTypeDef(
        type="cleanup_slave",
        title="Cleanup Slave",
        description="Очистка раздач в slave qB по правилам (незарегистрированные и т.п.). По умолчанию dry-run.",
        run_modes=("dry_run", "apply"),
        interval_setting_key="cleanup_interval_sec",
        interval_default_sec=settings.cleanup_interval_sec,
    ),
    JobTypeDef(
        type="orphan_cleanup",
        title="Orphan cleanup",
        description="Поиск orphan-медиа под /anilibria, мусора (.DS_Store и т.п.) и пустых папок.",
        run_modes=("dry_run", "apply"),
    ),
    JobTypeDef(
        type="cleanup_logs",
        title="Cleanup logs",
        description="Удаление завершённых jobs/job_logs и pipeline_events старше 30 дней.",
        interval_default_sec=86_400,
    ),
    JobTypeDef(
        type="hash_backfill",
        title="Hash backfill",
        description="Инвентаризация файлов из master qB, BLAKE3+gate, upsert torrent_files и prune БД.",
    ),
    JobTypeDef(
        type="pipeline_reconcile",
        title="Pipeline reconcile",
        description="Сверка pipeline с master: добить зависшие waiting/master_added без webhook.",
        interval_setting_key="pipeline_reconcile_interval_sec",
        interval_default_sec=settings.pipeline_reconcile_interval_sec,
    ),
    JobTypeDef(
        type="waiting_master_retry",
        title="Waiting master retry",
        description="Повторные попытки для pipeline в waiting_master (qB недоступен / таймаут).",
        interval_default_sec=300,
    ),
    JobTypeDef(
        type="waiting_slave_retry",
        title="Waiting slave retry",
        description="Повторные попытки добавить на slave и дослать hash_torrent при необходимости.",
        interval_default_sec=300,
    ),
    JobTypeDef(
        type="hash_torrent",
        title="Hash torrent",
        description="Хеширование файлов одной раздачи после master_complete (пути, BLAKE3, события).",
        manual_run=False,
        manual_hint="Запускается из pipeline после master_complete; вручную нужны info_hash / torrent_id / release_id.",
    ),
)


def job_type_def(job_type: str) -> JobTypeDef | None:
    for item in JOB_TYPE_DEFS:
        if item.type == job_type:
            return item
    return None


def _setting_int(db: Session, key: str, default: int) -> int:
    row = db.scalar(select(Setting).where(Setting.key == key).limit(1))
    if row is None:
        return default
    try:
        return int(row.value)
    except (TypeError, ValueError):
        return default


def _interval_sec(db: Session, definition: JobTypeDef) -> int | None:
    if definition.interval_default_sec is None and definition.interval_setting_key is None:
        return None
    default = definition.interval_default_sec or 0
    if definition.interval_setting_key:
        return _setting_int(db, definition.interval_setting_key, default)
    return default


def compute_next_run_at(
    last_job: Job | None,
    *,
    interval_sec: int | None,
) -> datetime | None:
    """Следующий ориентировочный запуск для интервальных джобов."""
    if interval_sec is None or interval_sec <= 0 or last_job is None:
        return None
    delta = timedelta(seconds=interval_sec)
    if last_job.status in (STATUS_PENDING, STATUS_RUNNING, STATUS_STOPPING):
        if last_job.started_at is not None:
            return last_job.started_at + delta
        return None
    if last_job.finished_at is not None:
        return last_job.finished_at + delta
    return None


def load_job_catalog(db: Session) -> list[dict[str, Any]]:
    """Последний запуск по каждому типу из каталога."""
    entries: list[dict[str, Any]] = []
    for definition in JOB_TYPE_DEFS:
        last_job = db.scalar(
            select(Job).where(Job.type == definition.type).order_by(Job.id.desc()).limit(1)
        )
        is_active = bool(
            last_job is not None
            and last_job.status in (STATUS_PENDING, STATUS_RUNNING, STATUS_STOPPING)
        )
        can_stop = bool(last_job is not None and last_job.status == STATUS_RUNNING)
        interval_sec = _interval_sec(db, definition)
        next_run_at = compute_next_run_at(last_job, interval_sec=interval_sec)
        entries.append(
            {
                "def": definition,
                "last_job": last_job,
                "is_active": is_active,
                "can_stop": can_stop,
                "next_run_at": next_run_at,
            }
        )
    return entries
