import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import ExtraUrl, JobLog, Setting
from app.services.runtime_settings import build_anilibria_client
from app.services.torrent_processor import TorrentProcessor


def _setting_int(db: Session, key: str, default: int) -> int:
    row = db.get(Setting, key)
    if row is None:
        return default
    try:
        return int(row.value)
    except (TypeError, ValueError):
        return default


def _add_log(db: Session, job_id: int, message: str, level: str = "info") -> None:
    db.add(JobLog(job_id=job_id, level=level, message=message))
    db.commit()


def _extract_releases_from_schedule(payload: Any) -> list[tuple[int, str | None]]:
    """Парсит ответ GET /anime/schedule/week (список releaseInSchedule)."""
    items: Any = payload
    if isinstance(payload, dict):
        items = payload.get("data") or payload.get("list") or payload.get("items") or []

    if not isinstance(items, list):
        return []

    found: list[tuple[int, str | None]] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue

        release = entry.get("release")
        if isinstance(release, dict):
            release_id = release.get("id")
            alias = release.get("alias")
        else:
            release_id = entry.get("id")
            alias = entry.get("alias")

        if isinstance(release_id, int):
            alias_text = alias.strip() if isinstance(alias, str) and alias.strip() else None
            found.append((release_id, alias_text))

    unique: dict[int, str | None] = {}
    for release_id, alias in found:
        if release_id not in unique:
            unique[release_id] = alias
    return [(release_id, unique[release_id]) for release_id in unique]


async def run_ongoing(db: Session, job_id: int, params: dict[str, Any]) -> None:
    _ = params
    _add_log(db, job_id, "Ongoing: инициализация клиента AniLibria и TorrentProcessor")
    al_client = build_anilibria_client(db)
    processor = TorrentProcessor(db=db, job_id=job_id, client=al_client)
    pause_every = _setting_int(db, "scrape_pause_every", settings.scrape_pause_every)
    pause_sec = _setting_int(db, "scrape_pause_sec", settings.scrape_pause_sec)

    schedule = await al_client.get_schedule_week(include=["release.id", "release.alias"])
    releases = _extract_releases_from_schedule(schedule)
    _add_log(db, job_id, f"Ongoing: из расписания получено релизов {len(releases)}")

    extra_rows = db.scalars(select(ExtraUrl).where(ExtraUrl.enabled.is_(True))).all()
    _add_log(db, job_id, f"Ongoing: активных extra URLs {len(extra_rows)}")
    for row in extra_rows:
        if row.release_id:
            releases.append((row.release_id, row.release_alias))
        elif row.release_alias:
            details = await al_client.get_releases_list(aliases=[row.release_alias], include=["id", "alias"])
            if isinstance(details, list):
                for item in details:
                    if isinstance(item, dict) and isinstance(item.get("id"), int):
                        releases.append((item["id"], item.get("alias")))

    unique_releases: dict[int, str | None] = {}
    for release_id, alias in releases:
        if release_id not in unique_releases:
            unique_releases[release_id] = alias

    total = len(unique_releases)
    _add_log(db, job_id, f"Ongoing: найдено релизов {total}")
    for index, (release_id, alias) in enumerate(unique_releases.items(), start=1):
        _add_log(
            db,
            job_id,
            f"Ongoing: обработка релиза {index}/{total} (id={release_id}, alias={alias or '-'})",
            level="debug",
        )
        await processor.process_release(release_id=release_id, release_alias=alias)
        if pause_every > 0 and pause_sec > 0 and index % pause_every == 0 and index < total:
            _add_log(db, job_id, f"Ongoing: пауза {pause_sec} сек после {index} релизов")
            await asyncio.sleep(pause_sec)
