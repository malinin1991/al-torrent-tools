import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import ExtraUrl, JobLog, Setting
from app.services.release_checkpoint import ReleaseRef, normalize_api_datetime, should_skip_unchanged
from app.services.runtime_settings import build_anilibria_client
from app.services.telegram_notify import list_enabled_tracked_releases
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


def _extract_releases_from_schedule(payload: Any) -> list[ReleaseRef]:
    """Парсит ответ GET /anime/schedule/week (список releaseInSchedule)."""
    items: Any = payload
    if isinstance(payload, dict):
        items = payload.get("data") or payload.get("list") or payload.get("items") or []

    if not isinstance(items, list):
        return []

    found: list[ReleaseRef] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue

        release = entry.get("release")
        if isinstance(release, dict):
            release_id = release.get("id")
            alias = release.get("alias")
            updated_at = normalize_api_datetime(release.get("updated_at"))
            fresh_at = normalize_api_datetime(release.get("fresh_at"))
        else:
            release_id = entry.get("id")
            alias = entry.get("alias")
            updated_at = normalize_api_datetime(entry.get("updated_at"))
            fresh_at = normalize_api_datetime(entry.get("fresh_at"))

        if isinstance(release_id, int):
            alias_text = alias.strip() if isinstance(alias, str) and alias.strip() else None
            found.append(
                ReleaseRef(
                    release_id=release_id,
                    alias=alias_text,
                    updated_at=updated_at,
                    fresh_at=fresh_at,
                )
            )

    unique: dict[int, ReleaseRef] = {}
    for item in found:
        if item.release_id not in unique:
            unique[item.release_id] = item
    return list(unique.values())


async def run_ongoing(db: Session, job_id: int, params: dict[str, Any]) -> None:
    _ = params
    _add_log(db, job_id, "Ongoing: инициализация клиента AniLibria и TorrentProcessor")
    al_client = build_anilibria_client(db)
    processor = TorrentProcessor(db=db, job_id=job_id, client=al_client)
    pause_every = _setting_int(db, "scrape_pause_every", settings.scrape_pause_every)
    pause_sec = _setting_int(db, "scrape_pause_sec", settings.scrape_pause_sec)

    schedule = await al_client.get_schedule_week(
        include=["release.id", "release.alias", "release.updated_at", "release.fresh_at"]
    )
    releases = _extract_releases_from_schedule(schedule)
    _add_log(db, job_id, f"Ongoing: из расписания получено релизов {len(releases)}")

    extra_rows = db.scalars(select(ExtraUrl).where(ExtraUrl.enabled.is_(True))).all()
    _add_log(db, job_id, f"Ongoing: активных extra URLs {len(extra_rows)}")
    for row in extra_rows:
        if row.release_id:
            releases.append(ReleaseRef(release_id=row.release_id, alias=row.release_alias))
        elif row.release_alias:
            details = await al_client.get_releases_list(
                aliases=[row.release_alias],
                include=["id", "alias", "updated_at", "fresh_at"],
            )
            if isinstance(details, list):
                for item in details:
                    if isinstance(item, dict) and isinstance(item.get("id"), int):
                        releases.append(
                            ReleaseRef(
                                release_id=item["id"],
                                alias=item.get("alias") if isinstance(item.get("alias"), str) else None,
                                updated_at=normalize_api_datetime(item.get("updated_at")),
                                fresh_at=normalize_api_datetime(item.get("fresh_at")),
                            )
                        )

    tracked_rows = list_enabled_tracked_releases(db)
    _add_log(db, job_id, f"Ongoing: отслеживаемых релизов {len(tracked_rows)}")
    for row in tracked_rows:
        releases.append(ReleaseRef(release_id=row.release_id, alias=row.release_alias or None))

    unique_releases: dict[int, ReleaseRef] = {}
    for item in releases:
        if item.release_id not in unique_releases:
            unique_releases[item.release_id] = item

    total = len(unique_releases)
    skipped_unchanged = 0
    processed = 0
    total_stats = TorrentProcessor.empty_release_stats()
    batch_stats = TorrentProcessor.empty_release_stats()
    batch_releases = 0
    _add_log(db, job_id, f"Ongoing: найдено релизов {total}")
    for index, ref in enumerate(unique_releases.values(), start=1):
        if should_skip_unchanged(
            db,
            ref.release_id,
            updated_at=ref.updated_at,
            fresh_at=ref.fresh_at,
        ):
            skipped_unchanged += 1
            _add_log(
                db,
                job_id,
                f"Ongoing: пропуск без изменений id={ref.release_id} "
                f"(updated_at={ref.updated_at or '-'}, fresh_at={ref.fresh_at or '-'})",
                level="debug",
            )
            # Актуальность торрентов в UI всё равно обновляем (лёгкий запрос id).
            await processor.refresh_api_present_only(ref.release_id)
            continue

        processed += 1
        batch_releases += 1
        _add_log(
            db,
            job_id,
            f"Ongoing: обработка релиза {index}/{total} (id={ref.release_id}, alias={ref.alias or '-'})",
            level="debug",
        )
        part = await processor.process_release(
            release_id=ref.release_id,
            release_alias=ref.alias,
            list_updated_at=ref.updated_at,
            list_fresh_at=ref.fresh_at,
        )
        TorrentProcessor.merge_release_stats(batch_stats, part)
        TorrentProcessor.merge_release_stats(total_stats, part)

        if pause_every > 0 and pause_sec > 0 and processed % pause_every == 0 and index < total:
            _add_log(
                db,
                job_id,
                TorrentProcessor.format_batch_summary(
                    "Ongoing: сводка",
                    batch_stats,
                    releases=batch_releases,
                ),
            )
            batch_stats = TorrentProcessor.empty_release_stats()
            batch_releases = 0
            _add_log(db, job_id, f"Ongoing: пауза {pause_sec} сек после {processed} обработанных релизов")
            await asyncio.sleep(pause_sec)

    if batch_releases > 0:
        _add_log(
            db,
            job_id,
            TorrentProcessor.format_batch_summary(
                "Ongoing: сводка",
                batch_stats,
                releases=batch_releases,
            ),
        )

    _add_log(
        db,
        job_id,
        f"Ongoing: готово, релизов={total}, пропущено без изменений (по markers)={skipped_unchanged}, "
        + TorrentProcessor.format_batch_summary("итого", total_stats, releases=processed),
    )
