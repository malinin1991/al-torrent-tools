"""SSE change-token'ы для бесшовного обновления UI (без HTMX interval-poll)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.db.models import (
    FileChangeEvent,
    Job,
    JobLog,
    PipelineEvent,
    Release,
    ReleaseMember,
    Setting,
    TelegramOutbox,
    TorrentArchive,
    TorrentFile,
    TorrentPipeline,
    TrackedRelease,
)
from app.db.session import SessionLocal
from app.services.job_runner import STATUS_PENDING, STATUS_RUNNING, STATUS_STOPPING
from app.services.telegram_notify import OUTBOX_PENDING

logger = logging.getLogger(__name__)

SIMPLE_CHANNELS = frozenset(
    {"dashboard", "jobs", "pipeline", "releases", "archive", "info"}
)

# Статусы, при которых на pipeline нужен live progress с master/slave (qB).
_ACTIVE_PIPELINE_STATUSES = frozenset(
    {
        "discovered",
        "waiting_master",
        "master_added",
        "master_complete",
        "waiting_slave",
        "slave_added",
    }
)

# 3 с — как _PIPELINE_PROGRESS_TICK_SEC: достаточно для live UI, меньше нагрузки.
_POLL_INTERVAL_SEC = 3.0
_HEARTBEAT_SEC = 15.0
_PIPELINE_PROGRESS_TICK_SEC = 3
_INFO_PROBE_TICK_SEC = 30


def parse_channels(raw: str | None) -> list[str]:
    """Разобрать ?channels=jobs,pipeline,pipeline_detail:12 → уникальный список."""
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.split(","):
        ch = part.strip()
        if not ch or ch in seen:
            continue
        if ch in SIMPLE_CHANNELS:
            seen.add(ch)
            out.append(ch)
            continue
        if ch.startswith("pipeline_detail:"):
            suffix = ch.split(":", 1)[1].strip()
            if suffix.isdigit():
                seen.add(ch)
                out.append(ch)
    return out


def channel_token(db: Session, channel: str) -> str:
    """Дешёвый fingerprint канала; меняется при реальных изменениях данных."""
    if channel in {"dashboard", "jobs"}:
        return _jobs_token(db)
    if channel == "pipeline":
        return _pipeline_list_token(db)
    if channel.startswith("pipeline_detail:"):
        pid = int(channel.split(":", 1)[1])
        return _pipeline_detail_token(db, pid)
    if channel == "releases":
        return _releases_token(db)
    if channel == "archive":
        return _archive_token(db)
    if channel == "info":
        return _info_token(db)
    return ""


def _max_id(db: Session, column) -> int:
    return int(db.scalar(select(func.coalesce(func.max(column), 0))) or 0)


def _jobs_token(db: Session) -> str:
    max_job = _max_id(db, Job.id)
    max_log = _max_id(db, JobLog.id)
    # Сигнатура статусов последних джобов (каталог + таблица).
    rows = db.execute(
        select(Job.id, Job.status, Job.finished_at, Job.started_at, Job.error)
        .order_by(Job.id.desc())
        .limit(40)
    ).all()
    status_sig = "|".join(
        f"{r.id}:{r.status}:{r.finished_at}:{r.started_at}:{bool(r.error)}:{(r.error or '')[:80]}"
        for r in rows
    )
    active = db.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.status.in_((STATUS_PENDING, STATUS_RUNNING, STATUS_STOPPING)))
    ) or 0
    tick = ""
    if active:
        # Пока есть активные — подтягиваем логи/статусы чаще (max_log уже ловит новые строки).
        tick = f"|a:{int(active)}"
    return f"j:{max_job}|l:{max_log}|s:{status_sig}{tick}"


def _pipeline_list_token(db: Session) -> str:
    max_pipe = _max_id(db, TorrentPipeline.id)
    max_ev = _max_id(db, PipelineEvent.id)
    rows = db.execute(
        select(
            TorrentPipeline.id,
            TorrentPipeline.status,
            TorrentPipeline.tg_status,
            TorrentPipeline.error,
            TorrentPipeline.master_added_at,
            TorrentPipeline.slave_added_at,
            TorrentPipeline.slave_completed_at,
        )
        .order_by(TorrentPipeline.id.desc())
        .limit(300)
    ).all()
    sig = "|".join(
        f"{r.id}:{r.status}:{r.tg_status}:{(r.error or '')[:80]}:"
        f"{r.master_added_at}:{r.slave_added_at}:{r.slave_completed_at}"
        for r in rows
    )
    # Список больше не показывает live % с qB — tick не нужен (достаточно status/events).
    active = db.scalar(
        select(func.count())
        .select_from(TorrentPipeline)
        .where(TorrentPipeline.status.in_(_ACTIVE_PIPELINE_STATUSES))
    ) or 0
    return f"p:{max_pipe}|e:{max_ev}|s:{sig}|a:{int(active)}"


def _pipeline_detail_token(db: Session, pipeline_id: int) -> str:
    row = db.get(TorrentPipeline, pipeline_id)
    if row is None:
        return f"pd:{pipeline_id}:missing"
    max_ev = db.scalar(
        select(func.coalesce(func.max(PipelineEvent.id), 0)).where(
            PipelineEvent.pipeline_id == pipeline_id
        )
    ) or 0
    tick = ""
    if row.status in _ACTIVE_PIPELINE_STATUSES:
        tick = f"|t:{int(time.time() // _PIPELINE_PROGRESS_TICK_SEC)}"
    return (
        f"pd:{pipeline_id}:{row.status}:{row.tg_status}:{row.error}:"
        f"{row.master_added_at}:{row.slave_added_at}:{row.slave_completed_at}:{max_ev}{tick}"
    )


def _releases_token(db: Session) -> str:
    max_arch = _max_id(db, TorrentArchive.id)
    max_file_ev = _max_id(db, FileChangeEvent.id)
    max_pipe_ev = _max_id(db, PipelineEvent.id)
    max_tf_upd = db.scalar(select(func.max(TorrentFile.updated_at)))
    max_tf_id = _max_id(db, TorrentFile.id)
    tracked = db.scalar(
        select(func.count()).select_from(TrackedRelease).where(TrackedRelease.enabled.is_(True))
    ) or 0
    # Карточка релиза (жанры/блокировки/состав) — отдельно от архива.
    max_rel_upd = db.scalar(select(func.max(Release.updated_at)))
    max_member = _max_id(db, ReleaseMember.id)
    release_flags = db.execute(
        select(
            func.count().filter(Release.is_blocked_by_geo.is_(True)),
            func.count().filter(Release.is_blocked_by_copyrights.is_(True)),
        )
    ).one()
    # ignore_hevc / api_present / superseded влияют на фильтры и бейджи.
    flag_counts = db.execute(
        select(
            func.count().filter(TorrentArchive.ignore_hevc.is_(True)),
            func.count().filter(TorrentArchive.api_present.is_(True)),
            func.count().filter(TorrentArchive.superseded.is_(True)),
        )
    ).one()
    return (
        f"r:{max_arch}|fe:{max_file_ev}|pe:{max_pipe_ev}|tf:{max_tf_upd}:{max_tf_id}|"
        f"tr:{tracked}|rel:{max_rel_upd}|rm:{max_member}|"
        f"geo:{release_flags[0]}|cr:{release_flags[1]}|"
        f"ig:{flag_counts[0]}|ap:{flag_counts[1]}|su:{flag_counts[2]}"
    )


def _archive_token(db: Session) -> str:
    max_arch = _max_id(db, TorrentArchive.id)
    max_file_ev = _max_id(db, FileChangeEvent.id)
    max_tf_upd = db.scalar(select(func.max(TorrentFile.updated_at)))
    max_tf_id = _max_id(db, TorrentFile.id)
    flag_counts = db.execute(
        select(
            func.count().filter(TorrentArchive.api_present.is_(True)),
            func.count().filter(TorrentArchive.superseded.is_(True)),
        )
    ).one()
    return f"a:{max_arch}|fe:{max_file_ev}|tf:{max_tf_upd}:{max_tf_id}|ap:{flag_counts[0]}|su:{flag_counts[1]}"


def _info_token(db: Session) -> str:
    pending = db.scalar(
        select(func.count())
        .select_from(TelegramOutbox)
        .where(TelegramOutbox.status == OUTBOX_PENDING)
    ) or 0
    max_outbox = _max_id(db, TelegramOutbox.id)
    tracked = db.scalar(
        select(func.count()).select_from(TrackedRelease).where(TrackedRelease.enabled.is_(True))
    ) or 0
    heartbeat = db.scalar(
        select(Setting.value).where(Setting.key == "telegram_bot_heartbeat_at")
    ) or ""
    # Внешние пробы (qB / AniLibria) — редкий tick, без постоянного HTML-poll.
    tick = int(time.time() // _INFO_PROBE_TICK_SEC)
    return f"i:{pending}|o:{max_outbox}|tr:{tracked}|hb:{heartbeat}|t:{tick}"


async def sse_event_stream(
    request: Request,
    channels: Iterable[str],
    *,
    poll_interval: float = _POLL_INTERVAL_SEC,
    heartbeat_sec: float = _HEARTBEAT_SEC,
) -> AsyncIterator[str]:
    """Генератор SSE: event=<channel> при смене token; heartbeat comment."""
    channel_list = list(channels)
    if not channel_list:
        yield ": no-channels\n\n"
        return

    # Сразу первый байт: иначе nginx/OpenResty буферит пустой stream,
    # EventSource/HAR видят status 0 / 0 bytes до первого heartbeat (~15 с).
    yield "retry: 3000\n"
    yield ": connected\n\n"

    last: dict[str, str | None] = {ch: None for ch in channel_list}
    last_hb = time.monotonic()

    while True:
        if await request.is_disconnected():
            break
        try:
            with SessionLocal() as db:
                for ch in channel_list:
                    try:
                        token = channel_token(db, ch)
                    except Exception:
                        logger.exception("ui_events: token failed for %s", ch)
                        continue
                    prev = last[ch]
                    if prev is None:
                        last[ch] = token
                        continue
                    if token != prev:
                        last[ch] = token
                        yield f"event: {ch}\ndata: {ch}\n\n"
        except Exception:
            logger.exception("ui_events: poll loop error")

        now = time.monotonic()
        if now - last_hb >= heartbeat_sec:
            last_hb = now
            yield ": ping\n\n"

        await asyncio.sleep(poll_interval)
