"""Полный read-only обход: discovery / статусы / soft-delete, независимо от ongoing."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core.config import settings
from app.db.models import JobLog, Release, TorrentArchive
from app.logging_filters import redact_secrets
from app.providers.anilibria.backoffice import BackofficeError, resource_id
from app.providers.anilibria.client import AniLibriaNotFoundError
from app.providers.anilibria.sources import ReleaseSourceClient, eligible
from app.services.job_runner import JobStopRequested, is_stop_requested
from app.services.runtime_settings import build_anilibria_client, get_setting_value, upsert_setting
from app.services.torrent_processor import TorrentProcessor
from app.services.sync_outcomes import CatalogScan, current_outcome, sync_job
from app.utils.datetime_fmt import utcnow


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
    if initial and not get_setting_value(db, "backoffice_baseline_started_at", ""):
        upsert_setting(db, "backoffice_baseline_started_at", utcnow().isoformat())
        db.commit()
    previously_known = set(db.scalars(select(TorrentArchive.release_id).distinct()).all())
    previous_offsite = set(db.scalars(select(Release.release_id).where(Release.source == "backoffice")).all())
    # После возврата на сайт продолжаем ремонт меток/трекеров при сбоях qB.
    previously_imported = set(db.scalars(select(TorrentArchive.release_id).where(
        TorrentArchive.backoffice_trackers.is_(True)).distinct()).all())
    baseline_releases = set(db.scalars(select(TorrentArchive.release_id).where(
        TorrentArchive.notification_baseline.is_(True)).distinct()).all())
    watched = previous_offsite | previously_imported
    # Каталог сайта позволяет не читать карточки админки для обычных site-релизов.
    visible, page = set(), 1
    scan = CatalogScan(outcome)
    log("Backoffice: чтение публичного каталога")
    while True:
        check_stop()
        payload = await client.public.catalog_releases(page=page, limit=50,
            include=["id", "is_hidden", "is_blocked_by_copyrights"])
        items, total_pages = scan.accept(payload, page)
        if page >= total_pages and not scan.complete:
            # Unknown catalogue completeness cannot authorize baseline discovery.
            return
        visible.update(item["id"] for item in items if not eligible(item))
        if page >= total_pages:
            break
        page += 1
        log(f"Backoffice: публичный каталог, страница {page}", "debug")
    check_stop()
    log("Backoffice: чтение индекса Nova")
    def on_page(page):
        check_stop()
        log(f"Backoffice: индекс Nova, страница {page}", "debug")
    resources = await client.backoffice.list_resources("anime-releases", on_page=on_page)
    ids = {resource_id(item) for item in resources}
    candidates = sorted((ids - visible) | watched)
    processor = TorrentProcessor(db=db, job_id=job_id, client=client)
    errors = processed = 0
    pause_every = max(1, int(get_setting_value(db, "scrape_pause_every", str(settings.scrape_pause_every))))
    pause_sec = max(0, int(get_setting_value(db, "scrape_pause_sec", str(settings.scrape_pause_sec))))
    log(f"Backoffice: индекс={len(ids)}, кандидатов={len(candidates)}, baseline={initial}")
    for index, rid in enumerate(candidates, 1):
        check_stop()
        try:
            payload = await client.get_release(rid)
            if payload["_source"] == "site" and rid not in watched:
                outcome.release(rid, status="skipped")
                continue
            if initial and (rid not in previously_known or rid in baseline_releases) and payload["_source"] == "backoffice":
                payload["_notification_baseline"] = True
            part = await processor.process_release(release_id=rid, release_alias=payload.get("alias"), refresh_qb_meta=True)
            outcome.release(rid, part)
            if part.get("errors", 0):
                errors += 1
            processed += 1
            log(f"Backoffice: {index}/{len(candidates)}, релиз {rid}, source={payload['_source']}", "debug")
        except AniLibriaNotFoundError:
            # В админке не скрыт и не copyright-blocked / удалён: не трогаем архив и qB.
            log(f"Backoffice: релиз {rid} не подходит; архив и раздачи сохранены", "debug")
            outcome.release(rid, status="skipped")
        except JobStopRequested:
            raise
        except Exception as exc:
            db.rollback()
            errors += 1
            outcome.failure(release_id=rid, source="backoffice", message=exc,
                retry_params={"notification_baseline": initial and (rid not in previously_known or rid in baseline_releases)})
            detail = f": {redact_secrets(str(exc))}" if isinstance(exc, BackofficeError) else ""
            log(f"Backoffice: релиз {rid}: {type(exc).__name__}{detail}; повтор в следующем обходе", "warning")
        finally:
            if index % pause_every == 0 and pause_sec:
                await asyncio.sleep(pause_sec)
    if initial and errors == 0 and not outcome.failures:
        upsert_setting(db, "backoffice_baseline_completed_at", utcnow().isoformat())
        db.commit()
    log(f"Backoffice: обработано={processed}, ошибок={errors}")
