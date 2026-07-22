import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import JobLog, Setting, TorrentArchive
from app.services.file_tracker import mark_missing_api_present_false
from app.services.job_runner import JobStopRequested, is_stop_requested
from app.services.release_checkpoint import normalize_api_datetime
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


def _extract_total_releases(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return None
    pagination = meta.get("pagination")
    if not isinstance(pagination, dict):
        return None
    value = pagination.get("total")
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _flush_batch_summary(
    db: Session,
    job_id: int,
    *,
    prefix: str,
    batch: dict[str, int],
    releases: int,
) -> dict[str, int]:
    if releases <= 0:
        return TorrentProcessor.empty_release_stats()
    _add_log(
        db,
        job_id,
        TorrentProcessor.format_batch_summary(prefix, batch, releases=releases),
    )
    return TorrentProcessor.empty_release_stats()


async def run_full_sync(db: Session, job_id: int, params: dict[str, Any]) -> None:
    _ = params
    _add_log(db, job_id, "Full sync: инициализация клиента AniLibria и TorrentProcessor")
    al_client = build_anilibria_client(db)
    processor = TorrentProcessor(db=db, job_id=job_id, client=al_client)
    pause_every = _setting_int(db, "scrape_pause_every", settings.scrape_pause_every)
    pause_sec = _setting_int(db, "scrape_pause_sec", settings.scrape_pause_sec)
    catalog_limit = 50
    _add_log(
        db,
        job_id,
        f"Full sync: каталог limit={catalog_limit}, "
        f"пауза каждые {pause_every} релизов по {pause_sec} сек",
    )

    page = 1
    total_pages = 1
    total_releases: int | None = None
    processed_releases = 0
    total_stats = TorrentProcessor.empty_release_stats()
    batch_stats = TorrentProcessor.empty_release_stats()
    batch_releases = 0
    seen_torrent_ids: set[int] = set()

    while page <= total_pages:
        if is_stop_requested(db, job_id):
            _add_log(db, job_id, "Full sync: остановка по запросу", "warning")
            raise JobStopRequested()
        payload = await al_client.catalog_releases(
            page=page,
            limit=catalog_limit,
            include=["id", "alias", "names", "season", "updated_at", "fresh_at"],
        )
        total_pages = _extract_total_pages(payload)
        if total_releases is None:
            total_releases = _extract_total_releases(payload)
            if total_releases is not None:
                _add_log(
                    db,
                    job_id,
                    f"Full sync: всего релизов в каталоге={total_releases}, "
                    f"страниц={total_pages}",
                )
        releases = _extract_list(payload)
        _add_log(
            db,
            job_id,
            f"Full sync: страница {page}/{total_pages}, релизов на странице: {len(releases)}",
            level="debug",
        )

        for release in releases:
            release_id = release.get("id")
            if not isinstance(release_id, int):
                continue
            processed_releases += 1
            batch_releases += 1
            release_alias = release.get("alias") if isinstance(release.get("alias"), str) else None
            updated_at = normalize_api_datetime(release.get("updated_at"))
            fresh_at = normalize_api_datetime(release.get("fresh_at"))

            # Не пропускаем по markers: нужно обновить comment у уже известных торрентов.
            _add_log(
                db,
                job_id,
                f"Full sync: обработка релиза id={release_id}, alias={release_alias or '-'} (#{processed_releases})",
                level="debug",
            )
            part = await processor.process_release(
                release_id=release_id,
                release_alias=release_alias,
                list_updated_at=updated_at,
                list_fresh_at=fresh_at,
                refresh_qb_meta=True,
            )
            # present torrent_ids уже выставлены в process_release; собираем для финального sweep
            present = db.scalars(
                select(TorrentArchive.torrent_id).where(
                    TorrentArchive.release_id == release_id,
                    TorrentArchive.api_present.is_(True),
                )
            ).all()
            seen_torrent_ids.update(int(x) for x in present)

            TorrentProcessor.merge_release_stats(batch_stats, part)
            TorrentProcessor.merge_release_stats(total_stats, part)

            if pause_every > 0 and pause_sec > 0 and processed_releases % pause_every == 0:
                batch_stats = _flush_batch_summary(
                    db,
                    job_id,
                    prefix=f"Full sync: сводка за {batch_releases} релизов (пауза)",
                    batch=batch_stats,
                    releases=batch_releases,
                )
                batch_releases = 0
                _add_log(
                    db,
                    job_id,
                    f"Full sync: пауза {pause_sec} сек после {processed_releases}"
                    + (f"/{total_releases}" if total_releases is not None else "")
                    + f" релизов (scrape_pause_every={pause_every})",
                )
                await asyncio.sleep(pause_sec)
        page += 1

    if seen_torrent_ids:
        archived = mark_missing_api_present_false(db, seen_torrent_ids)
        _add_log(db, job_id, f"Full sync: api_present=false для отсутствующих в API: {archived}")
    else:
        _add_log(
            db,
            job_id,
            "Full sync: пропуск api_present sweep — каталог API не вернул торренты",
            "warning",
        )

    if batch_releases > 0:
        _flush_batch_summary(
            db,
            job_id,
            prefix="Full sync: сводка",
            batch=batch_stats,
            releases=batch_releases,
        )

    _add_log(
        db,
        job_id,
        "Full sync: финальный backfill comment/tags из torrent_archive на master/slave",
    )
    backfill = processor.backfill_qb_comments_from_archive()
    _add_log(
        db,
        job_id,
        "Full sync: готово, "
        + (
            f"каталог={total_releases}, "
            if total_releases is not None
            else ""
        )
        + TorrentProcessor.format_batch_summary("итого", total_stats, releases=processed_releases)
        + f"; backfill: архивов={backfill['archives']}, "
        f"comments={backfill['updated']}, tags={backfill.get('tags_updated', 0)}, "
        f"пропущено={backfill['missing']}",
    )
