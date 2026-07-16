import asyncio
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import JobLog, Setting
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


def _extract_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("list", "items", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _extract_total_pages(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 1
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return 1
    pagination = meta.get("pagination")
    if not isinstance(pagination, dict):
        return 1
    value = pagination.get("total_pages")
    if isinstance(value, int) and value > 0:
        return value
    return 1


async def run_full_sync(db: Session, job_id: int, params: dict[str, Any]) -> None:
    _ = params
    _add_log(db, job_id, "Full sync: инициализация клиента AniLibria и TorrentProcessor")
    al_client = build_anilibria_client(db)
    processor = TorrentProcessor(db=db, job_id=job_id, client=al_client)
    pause_every = _setting_int(db, "scrape_pause_every", settings.scrape_pause_every)
    pause_sec = _setting_int(db, "scrape_pause_sec", settings.scrape_pause_sec)

    page = 1
    total_pages = 1
    processed_releases = 0
    while page <= total_pages:
        payload = await al_client.catalog_releases(page=page, include=["id", "alias", "names", "season"])
        total_pages = _extract_total_pages(payload)
        releases = _extract_list(payload)
        _add_log(db, job_id, f"Full sync: страница {page}/{total_pages}, релизов на странице: {len(releases)}")

        for release in releases:
            release_id = release.get("id")
            if not isinstance(release_id, int):
                continue
            processed_releases += 1
            release_alias = release.get("alias") if isinstance(release.get("alias"), str) else None
            _add_log(
                db,
                job_id,
                f"Full sync: обработка релиза id={release_id}, alias={release_alias or '-'} (#{processed_releases})",
                level="debug",
            )
            await processor.process_release(release_id=release_id, release_alias=release_alias)

            if pause_every > 0 and pause_sec > 0 and processed_releases % pause_every == 0:
                _add_log(db, job_id, f"Full sync: пауза {pause_sec} сек после {processed_releases} релизов")
                await asyncio.sleep(pause_sec)
        page += 1
