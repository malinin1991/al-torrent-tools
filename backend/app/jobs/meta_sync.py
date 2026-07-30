"""Тонкие джобы meta_sync / full_meta_sync поверх refresh_release_meta / process_release."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import JobLog, Setting, TorrentArchive
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


def _to_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _resolve_scope(db: Session, params: dict[str, Any]) -> tuple[int | None, int | None]:
    release_id = _to_int(params.get("release_id"))
    torrent_id = _to_int(params.get("torrent_id"))
    if release_id is None and torrent_id is not None:
        release_id = db.scalar(
            select(TorrentArchive.release_id)
            .where(TorrentArchive.torrent_id == torrent_id)
            .order_by(TorrentArchive.id.desc())
            .limit(1)
        )
        if release_id is not None:
            release_id = int(release_id)
    return release_id, torrent_id


async def _process_release_or_meta(
    processor: TorrentProcessor,
    *,
    release_id: int,
    release_alias: str | None,
    list_updated_at: str | None = None,
    list_fresh_at: str | None = None,
    torrent_id_filter: int | None = None,
) -> dict[str, int]:
    """Scoped meta: refresh_release_meta; unseen → process_release (тот же discovery)."""
    if torrent_id_filter is not None:
        torrents_payload = await processor._al_client.get_torrents_for_release(
            release_id,
            include=list(TorrentProcessor.RELEASE_TORRENTS_INCLUDE),
        )
        torrents = TorrentProcessor._iter_torrents(torrents_payload)
        target = None
        for torrent in torrents:
            if TorrentProcessor._to_int(torrent.get("id") or torrent.get("torrent_id")) == torrent_id_filter:
                target = torrent
                break
        if target is None:
            processor._add_log(
                f"meta_sync: torrent_id={torrent_id_filter} не найден в релизе {release_id}",
                "warning",
            )
            return TorrentProcessor.empty_release_stats()

        raw_hash = target.get("info_hash") or target.get("hash")
        info_hash = processor._normalize_api_info_hash(raw_hash)
        seen = processor._db.scalar(
            processor.build_seen_exists_query(torrent_id_filter, info_hash).limit(1)
        )
        if seen is None or processor._archive_hash_mismatches(target):
            # Unseen / hash change → обычный process_release (не clear seen).
            return await processor.process_release(
                release_id=release_id,
                release_alias=release_alias,
                list_updated_at=list_updated_at,
                list_fresh_at=list_fresh_at,
                refresh_qb_meta=True,
            )

        meta = await processor.refresh_release_meta(
            release_id,
            release_alias,
            torrents,
            torrent_id_filter=torrent_id_filter,
        )
        return {
            **TorrentProcessor.empty_release_stats(),
            "total": 1,
            "updated": 0,
            "new": 0,
            "skipped": 1,
            "comments": int(meta.get("comments", 0) or 0),
            "tags": int(meta.get("tags", 0) or 0),
            "renames": int(meta.get("renames", 0) or 0),
        }

    return await processor.process_release(
        release_id=release_id,
        release_alias=release_alias,
        list_updated_at=list_updated_at,
        list_fresh_at=list_fresh_at,
        refresh_qb_meta=True,
    )


async def run_meta_sync(db: Session, job_id: int, params: dict[str, Any]) -> None:
    """Scoped meta refresh: обязателен release_id и/или torrent_id."""
    release_id, torrent_id = _resolve_scope(db, params or {})
    if release_id is None and torrent_id is None:
        raise ValueError("meta_sync: укажите release_id и/или torrent_id")
    if release_id is None:
        raise ValueError(
            f"meta_sync: не удалось определить release_id для torrent_id={torrent_id}"
        )

    _add_log(
        db,
        job_id,
        f"meta_sync: release_id={release_id}"
        + (f", torrent_id={torrent_id}" if torrent_id is not None else ""),
    )
    al_client = build_anilibria_client(db)
    processor = TorrentProcessor(db=db, job_id=job_id, client=al_client)
    alias = None
    if isinstance(params, dict):
        raw_alias = params.get("release_alias")
        if isinstance(raw_alias, str) and raw_alias.strip():
            alias = raw_alias.strip()

    part = await _process_release_or_meta(
        processor,
        release_id=release_id,
        release_alias=alias,
        torrent_id_filter=torrent_id,
    )
    _add_log(
        db,
        job_id,
        TorrentProcessor.format_batch_summary("meta_sync: готово", part, releases=1),
    )


async def run_full_meta_sync(db: Session, job_id: int, params: dict[str, Any]) -> None:
    """Каталог API: только meta-helper (+ handoff unseen через process_release). Без backfill."""
    _ = params
    _add_log(db, job_id, "full_meta_sync: инициализация клиента AniLibria и TorrentProcessor")
    al_client = build_anilibria_client(db)
    processor = TorrentProcessor(db=db, job_id=job_id, client=al_client)
    pause_every = _setting_int(db, "scrape_pause_every", settings.scrape_pause_every)
    pause_sec = _setting_int(db, "scrape_pause_sec", settings.scrape_pause_sec)
    catalog_limit = 50
    _add_log(
        db,
        job_id,
        f"full_meta_sync: каталог limit={catalog_limit}, "
        f"пауза каждые {pause_every} релизов по {pause_sec} сек",
    )

    page = 1
    total_pages = 1
    total_releases: int | None = None
    processed_releases = 0
    total_stats = TorrentProcessor.empty_release_stats()
    batch_stats = TorrentProcessor.empty_release_stats()
    batch_releases = 0

    while page <= total_pages:
        if is_stop_requested(db, job_id):
            _add_log(db, job_id, "full_meta_sync: остановка по запросу", "warning")
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
                    f"full_meta_sync: всего релизов в каталоге={total_releases}, "
                    f"страниц={total_pages}",
                )
        releases = _extract_list(payload)
        _add_log(
            db,
            job_id,
            f"full_meta_sync: страница {page}/{total_pages}, релизов на странице: {len(releases)}",
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

            _add_log(
                db,
                job_id,
                f"full_meta_sync: релиз id={release_id}, alias={release_alias or '-'} "
                f"(#{processed_releases})",
                level="debug",
            )
            part = await processor.process_release(
                release_id=release_id,
                release_alias=release_alias,
                list_updated_at=updated_at,
                list_fresh_at=fresh_at,
                refresh_qb_meta=True,
            )
            TorrentProcessor.merge_release_stats(batch_stats, part)
            TorrentProcessor.merge_release_stats(total_stats, part)

            if pause_every > 0 and pause_sec > 0 and processed_releases % pause_every == 0:
                batch_stats = _flush_batch_summary(
                    db,
                    job_id,
                    prefix=f"full_meta_sync: сводка за {batch_releases} релизов (пауза)",
                    batch=batch_stats,
                    releases=batch_releases,
                )
                batch_releases = 0
                _add_log(
                    db,
                    job_id,
                    f"full_meta_sync: пауза {pause_sec} сек после {processed_releases}"
                    + (f"/{total_releases}" if total_releases is not None else "")
                    + f" релизов",
                )
                await asyncio.sleep(pause_sec)
        page += 1

    if batch_releases > 0:
        _flush_batch_summary(
            db,
            job_id,
            prefix="full_meta_sync: сводка",
            batch=batch_stats,
            releases=batch_releases,
        )

    _add_log(
        db,
        job_id,
        "full_meta_sync: готово, "
        + (
            f"каталог={total_releases}, "
            if total_releases is not None
            else ""
        )
        + TorrentProcessor.format_batch_summary(
            "итого", total_stats, releases=processed_releases
        ),
    )
