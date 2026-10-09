import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import ExtraUrl, JobLog, Setting
from app.providers.anilibria.client import AniLibriaNotFoundError
from app.services.ongoing_watch import (
    observe_torrents,
    remove_from_watch,
    update_watch_list,
)
from app.utils.datetime_fmt import utcnow
from app.services.job_runner import JobStopRequested, is_stop_requested
from app.services.release_checkpoint import (
    ReleaseRef,
    normalize_api_datetime,
    should_skip_unchanged,
)
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
            alias_text = (
                alias.strip() if isinstance(alias, str) and alias.strip() else None
            )
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


def _validated_items(payload: Any, *, schedule: bool = False) -> list[dict[str, Any]]:
    """Не принимаем сломанный ответ API за пустой список/исчезновение релизов."""
    items = payload
    if isinstance(payload, dict):
        items = next(
            (
                payload[key]
                for key in ("data", "list", "items", "results")
                if key in payload
            ),
            None,
        )
    if not isinstance(items, list):
        raise ValueError("API: ожидался список")
    for item in items:
        entity = (
            item.get("release", item) if schedule and isinstance(item, dict) else item
        )
        if (
            not isinstance(entity, dict)
            or type(entity.get("id")) is not int
            or entity["id"] <= 0
        ):
            raise ValueError("API: элемент списка без корректного id")
    return items


async def _confirm_removed(client: Any, release_id: int) -> bool:
    # Пустой список и 404 torrent endpoint сами по себе не означают удаление релиза.
    try:
        await client.get_release(release_id, include=["id"])
    except AniLibriaNotFoundError:
        return True
    return False


async def run_ongoing(db: Session, job_id: int, params: dict[str, Any]) -> None:
    _ = params
    _add_log(db, job_id, "Ongoing: инициализация клиента AniLibria и TorrentProcessor")
    al_client = build_anilibria_client(db)
    processor = TorrentProcessor(db=db, job_id=job_id, client=al_client)
    pause_every = _setting_int(db, "scrape_pause_every", settings.scrape_pause_every)
    pause_sec = _setting_int(db, "scrape_pause_sec", settings.scrape_pause_sec)

    schedule_refs = None
    try:
        payload = await al_client.get_schedule_week(
            include=[
                "release.id",
                "release.alias",
                "release.updated_at",
                "release.fresh_at",
            ]
        )
        schedule_refs = _extract_releases_from_schedule(
            _validated_items(payload, schedule=True)
        )
    except Exception as exc:
        _add_log(
            db,
            job_id,
            f"Ongoing: расписание недоступно ({type(exc).__name__}), сохраняем присутствие",
            "warning",
        )
    watches, expired = update_watch_list(db, schedule_refs, now=utcnow())
    releases = list(schedule_refs or []) + watches
    schedule_ids = {ref.release_id for ref in schedule_refs or []}
    _add_log(
        db,
        job_id,
        f"Ongoing: расписание={len(schedule_ids)}, "
        f"донаблюдение={sum(ref.release_id not in schedule_ids for ref in watches)}, истекло={expired}",
    )

    extra_rows = db.scalars(select(ExtraUrl).where(ExtraUrl.enabled.is_(True))).all()
    for row in extra_rows:
        if row.release_id:
            releases.append(
                ReleaseRef(release_id=row.release_id, alias=row.release_alias)
            )
        elif row.release_alias:
            try:
                details = await al_client.get_releases_list(
                    aliases=[row.release_alias],
                    include=["id", "alias", "updated_at", "fresh_at"],
                )
                releases.extend(
                    _extract_releases_from_schedule(_validated_items(details))
                )
            except Exception as exc:
                _add_log(
                    db,
                    job_id,
                    f"Ongoing: extra URL {row.release_alias}: {type(exc).__name__}",
                    "warning",
                )

    tracked_rows = list_enabled_tracked_releases(db)
    for row in tracked_rows:
        releases.append(
            ReleaseRef(release_id=row.release_id, alias=row.release_alias or None)
        )
    unique_releases: dict[int, ReleaseRef] = {}
    for ref in releases:
        unique_releases.setdefault(ref.release_id, ref)

    total = len(unique_releases)
    processed = errors = extended = removed = 0
    total_stats = TorrentProcessor.empty_release_stats()
    _add_log(
        db,
        job_id,
        f"Ongoing: найдено релизов {total}, extra URLs={len(extra_rows)}, tracked={len(tracked_rows)}",
    )
    for index, ref in enumerate(unique_releases.values(), start=1):
        if is_stop_requested(db, job_id):
            _add_log(db, job_id, "Ongoing: остановка по запросу", "warning")
            raise JobStopRequested()
        try:
            _add_log(
                db,
                job_id,
                f"Ongoing: проверка {index}/{total} (id={ref.release_id})",
                "debug",
            )
            try:
                payload = await al_client.get_torrents_for_release(
                    ref.release_id,
                    include=list(TorrentProcessor.RELEASE_TORRENTS_INCLUDE),
                )
            except AniLibriaNotFoundError:
                if await _confirm_removed(al_client, ref.release_id):
                    removed += int(remove_from_watch(db, ref.release_id))
                    _add_log(
                        db,
                        job_id,
                        f"Ongoing: релиз {ref.release_id} удалён из донаблюдения (404)",
                    )
                    continue
                raise
            torrents = _validated_items(payload)
            if not torrents and await _confirm_removed(al_client, ref.release_id):
                removed += int(remove_from_watch(db, ref.release_id))
                _add_log(
                    db,
                    job_id,
                    f"Ongoing: релиз {ref.release_id} удалён из донаблюдения (404)",
                )
                continue
            extended += int(
                observe_torrents(
                    db, ref.release_id, [t["id"] for t in torrents], now=utcnow()
                )
            )
            # Markers управляют только обновлением метаданных qB. Торренты проверяем всегда.
            refresh_meta = not should_skip_unchanged(
                db, ref.release_id, updated_at=ref.updated_at, fresh_at=ref.fresh_at
            )
            try:
                part = await processor.process_release(
                    release_id=ref.release_id,
                    release_alias=ref.alias,
                    list_updated_at=ref.updated_at,
                    list_fresh_at=ref.fresh_at,
                    refresh_qb_meta=refresh_meta,
                    prefetched_torrents=torrents,
                )
            except AniLibriaNotFoundError:
                # Релиз мог исчезнуть между запросом торрентов и чтением деталей.
                if await _confirm_removed(al_client, ref.release_id):
                    removed += int(remove_from_watch(db, ref.release_id))
                    _add_log(
                        db,
                        job_id,
                        f"Ongoing: релиз {ref.release_id} удалён из донаблюдения (404)",
                    )
                    continue
                raise
            processed += 1
            TorrentProcessor.merge_release_stats(total_stats, part)
        except JobStopRequested:
            raise
        except Exception as exc:
            db.rollback()
            errors += 1
            _add_log(
                db,
                job_id,
                f"Ongoing: ошибка релиза {ref.release_id}: {type(exc).__name__}; повтор в следующем обходе",
                "warning",
            )
        finally:
            if (
                pause_every > 0
                and pause_sec > 0
                and index % pause_every == 0
                and index < total
            ):
                _add_log(
                    db, job_id, f"Ongoing: пауза {pause_sec} сек после {index} проверок"
                )
                await asyncio.sleep(pause_sec)

    _add_log(
        db,
        job_id,
        f"Ongoing: готово, релизов={total}, ошибок={errors}, продлено={extended}, удалено по 404={removed}, "
        + TorrentProcessor.format_batch_summary(
            "итого", total_stats, releases=processed
        ),
    )
