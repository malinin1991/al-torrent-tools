"""Каталог типов джобов для UI: описание, режимы запуска, последний прогон."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job
from app.services.job_runner import STATUS_PENDING, STATUS_RUNNING


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


# Порядок = отображение на /jobs.
JOB_TYPE_DEFS: tuple[JobTypeDef, ...] = (
    JobTypeDef(
        type="ongoing",
        title="Ongoing sync",
        description="Периодическая проверка обновлений релизов AniLibria и постановка новых торрентов в pipeline.",
    ),
    JobTypeDef(
        type="full_sync",
        title="Full sync",
        description="Полный проход по каталогу/расписанию: сверка торрентов с API, api_present, архив.",
    ),
    JobTypeDef(
        type="cleanup",
        title="Cleanup (qB)",
        description="Очистка раздач в qB по правилам (незарегистрированные и т.п.). По умолчанию dry-run.",
        run_modes=("dry_run", "apply"),
    ),
    JobTypeDef(
        type="orphan_cleanup",
        title="Orphan cleanup",
        description="Поиск orphan-медиа под /anilibria, мусора (.DS_Store и т.п.) и пустых папок.",
        run_modes=("dry_run", "apply"),
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
    ),
    JobTypeDef(
        type="waiting_master_retry",
        title="Waiting master retry",
        description="Повторные попытки для pipeline в waiting_master (qB недоступен / таймаут).",
    ),
    JobTypeDef(
        type="waiting_slave_retry",
        title="Waiting slave retry",
        description="Повторные попытки добавить на slave и дослать hash_torrent при необходимости.",
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


def load_job_catalog(db: Session) -> list[dict[str, Any]]:
    """Последний запуск по каждому типу из каталога."""
    entries: list[dict[str, Any]] = []
    for definition in JOB_TYPE_DEFS:
        last_job = db.scalar(
            select(Job).where(Job.type == definition.type).order_by(Job.id.desc()).limit(1)
        )
        is_active = bool(
            last_job is not None and last_job.status in (STATUS_PENDING, STATUS_RUNNING)
        )
        entries.append(
            {
                "def": definition,
                "last_job": last_job,
                "is_active": is_active,
            }
        )
    return entries
