"""Полный read-only обход: discovery / статусы / soft-delete, независимо от ongoing."""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import select

from app.core.config import settings
from app.db.models import JobLog, Release, TorrentArchive
from app.logging_filters import redact_secrets
from app.providers.anilibria.backoffice import BackofficeClient, BackofficeError, resource_id
from app.providers.anilibria.client import AniLibriaNotFoundError
from app.providers.anilibria.sources import BackofficeReleaseSkipped, ReleaseSourceClient, SourceCheckDeferred
from app.services.job_runner import JobStopRequested, is_stop_requested
from app.services.runtime_settings import build_anilibria_client, get_setting_value, upsert_setting
from app.services.torrent_processor import TorrentProcessor
from app.services.sync_outcomes import CatalogScan, current_outcome, sync_job
from app.utils.datetime_fmt import utcnow


_CATALOG_HINT_TIMEOUT_SEC = 30.0
_PROGRESS_INTERVAL_SEC = 30.0


class _CatalogHintCollector:
    """CatalogScan validation without changing the durable job outcome."""

    failed = False

    def failure(self, **kwargs):
        self.failed = True


def _catalog_item_is_visible(item):
    from app.services.torrent_qb_meta import parse_api_bool

    # Public API omits Nova-only is_hidden even when explicitly requested.
    # An explicit null/malformed flag remains inconclusive, unlike omission.
    # Geo must be explicitly false: an ID in the catalogue can still have a
    # region-restricted card that requires Nova from this host.
    return (parse_api_bool(item.get("is_blocked_by_copyrights")) is False
            and ("is_hidden" not in item or parse_api_bool(item["is_hidden"]) is False)
            and parse_api_bool(item.get("is_blocked_by_geo")) is False)


async def _catalog_visible_hint(public, *, check_stop, log, timeout_sec=30.0):
    """Return confirmed ordinary site IDs, or None when pruning is unsafe.

    Nova remains the discovery source. This optional, bounded read never
    changes release metadata or the source-card circuit breaker.
    """
    collector = _CatalogHintCollector()
    scan = CatalogScan(collector)
    visible = set()
    page = 1
    try:
        check_stop()
        log("Backoffice: необязательная сверка публичного каталога")
        async with asyncio.timeout(timeout_sec):
            while True:
                check_stop()
                payload = await public.catalog_releases(
                    page=page, limit=50,
                    include=["id", "is_hidden", "is_blocked_by_copyrights", "is_blocked_by_geo"],
                )
                check_stop()
                items, total_pages = scan.accept(payload, page)
                if collector.failed:
                    raise ValueError("Неполный публичный каталог")
                visible.update(item["id"] for item in items if _catalog_item_is_visible(item))
                if page % 10 == 0:
                    log(f"Backoffice: подсказка публичного каталога, страница {page}/{total_pages}")
                if scan.complete:
                    return visible
                page += 1
    except JobStopRequested:
        raise
    except Exception as exc:
        check_stop()
        log(f"Backoffice: подсказка публичного каталога недоступна ({type(exc).__name__}); полный обход Nova", "warning")
        return None


def _index_geo_blocked(resource):
    """Nova geo is a country multiselect; unknown fields cannot prove absence."""
    fields = resource.get("fields")
    if not isinstance(fields, list):
        return None
    geo = [item for item in fields if isinstance(item, dict)
           and item.get("attribute") == "is_blocked_by_geo"]
    if len(geo) != 1 or geo[0].get("component") != "multiselect-field":
        return None
    countries = geo[0].get("value")
    if countries is None:
        return False
    if isinstance(countries, list) and all(isinstance(country, str) and country.strip() for country in countries):
        return bool(countries)
    return None


def _index_candidate(resource):
    """Отсекаем только явно обычные карточки; неполные поля проверяем детально."""
    from app.services.torrent_qb_meta import parse_api_bool
    fields = resource.get("fields")
    if not isinstance(fields, list):
        return True
    flags = {f.get("attribute"): parse_api_bool(f.get("value")) for f in fields
             if isinstance(f, dict) and f.get("component") == "boolean-field"
             and f.get("attribute") in ("is_hidden", "is_blocked_by_copyrights")}
    return not (all(flags.get(key) is False for key in ("is_hidden", "is_blocked_by_copyrights"))
                and _index_geo_blocked(resource) is False)


@sync_job
async def run_backoffice_sync(db, job_id: int, params: dict):
    outcome = current_outcome(db, job_id)
    client = build_anilibria_client(db)
    if not isinstance(client, ReleaseSourceClient):
        db.add(JobLog(job_id=job_id, level="info", message="Backoffice: URL админки не настроен, пропуск"))
        db.commit()
        return
    def log(message, level="info"):
        db.add(JobLog(job_id=job_id, level=level, message=message))
        db.commit()
    def check_stop():
        if is_stop_requested(db, job_id):
            raise JobStopRequested()
    # Начало baseline фиксируется до сети: повтор после сбоя не меняет границу.
    initial = not get_setting_value(db, "backoffice_baseline_completed_at", "")
    geo_initial = not get_setting_value(db, "backoffice_geo_baseline_completed_at", "")
    if initial and not get_setting_value(db, "backoffice_baseline_started_at", ""):
        upsert_setting(db, "backoffice_baseline_started_at", utcnow().isoformat())
        db.commit()
    if geo_initial and not get_setting_value(db, "backoffice_geo_baseline_started_at", ""):
        upsert_setting(db, "backoffice_geo_baseline_started_at", utcnow().isoformat())
        db.commit()
    previously_known = set(db.scalars(select(TorrentArchive.release_id).distinct()).all())
    previous_offsite = set(db.scalars(select(Release.release_id).where(Release.source == "backoffice")).all())
    # После возврата на сайт продолжаем ремонт меток/трекеров при сбоях qB.
    previously_imported = set(db.scalars(select(TorrentArchive.release_id).where(
        TorrentArchive.backoffice_trackers.is_(True)).distinct()).all())
    baseline_releases = set(db.scalars(select(TorrentArchive.release_id).where(
        TorrentArchive.notification_baseline.is_(True)).distinct()).all())
    watched = previous_offsite | previously_imported
    check_stop()
    log("Backoffice: чтение индекса Nova")
    def on_page(page):
        check_stop()
        log(f"Backoffice: индекс Nova, страница {page}")
    resources = await client.backoffice.list_resources("anime-releases", on_page=on_page)
    ids = {resource_id(item) for item in resources}
    candidate_ids = {resource_id(item) for item in resources if _index_candidate(item)} | watched
    geo_ids = {resource_id(item) for item in resources if _index_geo_blocked(item) is True}
    if candidate_ids - watched:
        visible = await _catalog_visible_hint(
            client.public, check_stop=check_stop, log=log, timeout_sec=_CATALOG_HINT_TIMEOUT_SEC,
        )
        if visible is not None:
            before = len(candidate_ids)
            # A catalogue may list a geo-restricted ID whose card is 404 from
            # this host. Confirm those cards individually before choosing site.
            candidate_ids = (candidate_ids - visible) | watched | geo_ids
            log(f"Backoffice: публичный каталог исключил обычных site-релизов={before - len(candidate_ids)}")
    candidates = sorted(candidate_ids)
    processor = TorrentProcessor(db=db, job_id=job_id, client=client)
    errors = processed = skipped = deferred = retained = checked = 0
    pause_every = max(1, int(get_setting_value(db, "scrape_pause_every", str(settings.scrape_pause_every))))
    pause_sec = max(0, int(get_setting_value(db, "scrape_pause_sec", str(settings.scrape_pause_sec))))
    log(f"Backoffice: индекс={len(ids)}, кандидатов={len(candidates)}, baseline={initial}, geo_baseline={geo_initial}")
    last_progress = None

    def progress(rid=None, *, force=False):
        nonlocal last_progress
        now = time.monotonic()
        if force or last_progress is None or now - last_progress >= _PROGRESS_INTERVAL_SEC:
            cards = (f"; карточки Nova: прочитано={client.backoffice.torrent_detail_reads}, "
                     f"из кэша={client.backoffice.torrent_cache_hits}") if isinstance(client.backoffice, BackofficeClient) else ""
            log(f"Backoffice: проверено {checked}/{len(candidates)}, текущий релиз={rid if rid is not None else '-'}; "
                f"обработано={processed}, пропущено={skipped}, отложено={deferred}, ошибок={errors}{cards}")
            last_progress = now

    with client.backoffice_scan(watched):
        for index, rid in enumerate(candidates, 1):
            check_stop()
            progress(rid)
            try:
                # Выбор/сохранение источника выполняется внутри release_lock
                # processor. Не пишем карточку при prefetch до блокировки.
                options = {"force_qb_load": True} if params.get("force_qb_load") else {}
                part = await processor.process_release(release_id=rid, refresh_qb_meta=True, **options)
                outcome.release(rid, part)
                if part.get("errors", 0):
                    errors += 1
                processed += 1
                if rid in client.source_check_errors:
                    retained += 1
                    log(f"Backoffice: релиз {rid}: проверка сайта недоступна ({client.source_check_errors[rid]}); сохранён источник backoffice", "warning")
                log(f"Backoffice: {index}/{len(candidates)}, релиз {rid}", "debug")
            except BackofficeReleaseSkipped:
                skipped += 1
                outcome.release(rid, status="skipped")
            except SourceCheckDeferred:
                deferred += 1
                outcome.release(rid, status="skipped")
                log(f"Backoffice: релиз {rid}: проверка сайта недоступна; источник сохранён, обработка отложена", "warning")
            except AniLibriaNotFoundError:
                # Не трогаем архив и qB при исчезновении карточки / .torrent.
                skipped += 1
                log(f"Backoffice: релиз {rid} не подходит; архив и раздачи сохранены", "debug")
                outcome.release(rid, status="skipped")
            except JobStopRequested:
                raise
            except Exception as exc:
                db.rollback()
                errors += 1
                payload_baseline = bool(client._releases.get(rid, {}).get("_notification_baseline"))
                outcome.failure(release_id=rid, source="backoffice", message=exc,
                    retry_params={"force_qb_load": bool(params.get("force_qb_load")),
                                  "notification_baseline": payload_baseline or (
                                      (initial or (geo_initial and rid in geo_ids))
                                      and (rid not in previously_known or rid in baseline_releases))})
                detail = f": {redact_secrets(str(exc))}" if isinstance(exc, BackofficeError) else ""
                log(f"Backoffice: релиз {rid}: {type(exc).__name__}{detail}; повтор в следующем обходе", "warning")
            checked = index
            progress(rid)
            if index % pause_every == 0 and pause_sec:
                await asyncio.sleep(pause_sec)
    progress(force=True)
    if retained or deferred:
        outcome.notes.append({"code": "source_checks_deferred", "retained": retained, "deferred": deferred,
            "message": f"Проверка сайта недоступна: {retained} релизов обновлены из админки с прежним источником; обработка {deferred} релизов отложена до следующего обхода."})
    if deferred:
        outcome.failure(operation="source_check", code="source_check_deferred", retryable=False,
            message=f"Проверка источника отложена для {deferred} релизов: сайт недоступен; последние подтверждённые источники и архивы сохранены")
    if errors == 0 and not deferred and not outcome.failures:
        if initial:
            upsert_setting(db, "backoffice_baseline_completed_at", utcnow().isoformat())
        if geo_initial:
            upsert_setting(db, "backoffice_geo_baseline_completed_at", utcnow().isoformat())
        if initial or geo_initial:
            db.commit()
    log(f"Backoffice: обработано={processed}, ошибок={errors}, источник сохранён={retained}, отложено={deferred}")
