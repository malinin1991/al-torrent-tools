"""Полный read-only обход: discovery / статусы / soft-delete, независимо от ongoing."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core.config import settings
from app.db.models import JobLog, Release, TorrentArchive
from app.logging_filters import redact_secrets
from app.providers.anilibria.backoffice import BackofficeError, resource_id
from app.providers.anilibria.client import AniLibriaNotFoundError
from app.providers.anilibria.sources import BackofficeReleaseSkipped, ReleaseSourceClient, SourceCheckDeferred
from app.services.job_runner import JobStopRequested, is_stop_requested
from app.services.runtime_settings import build_anilibria_client, get_setting_value, upsert_setting
from app.services.torrent_processor import TorrentProcessor
from app.services.sync_outcomes import current_outcome, sync_job
from app.utils.datetime_fmt import utcnow


def _index_candidate(resource):
    """Отсекаем только явно обычные карточки; неполные поля проверяем детально."""
    from app.services.torrent_qb_meta import parse_api_bool
    fields = resource.get("fields")
    if not isinstance(fields, list):
        return True
    flags = {f.get("attribute"): parse_api_bool(f.get("value")) for f in fields
             if isinstance(f, dict) and f.get("component") == "boolean-field"
             and f.get("attribute") in ("is_hidden", "is_blocked_by_copyrights")}
    return not all(flags.get(key) is False for key in ("is_hidden", "is_blocked_by_copyrights"))


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
    check_stop()
    log("Backoffice: чтение индекса Nova")
    def on_page(page):
        check_stop()
        log(f"Backoffice: индекс Nova, страница {page}", "debug")
    resources = await client.backoffice.list_resources("anime-releases", on_page=on_page)
    ids = {resource_id(item) for item in resources}
    candidates = sorted({resource_id(item) for item in resources if _index_candidate(item)} | watched)
    processor = TorrentProcessor(db=db, job_id=job_id, client=client)
    errors = processed = deferred = retained = 0
    pause_every = max(1, int(get_setting_value(db, "scrape_pause_every", str(settings.scrape_pause_every))))
    pause_sec = max(0, int(get_setting_value(db, "scrape_pause_sec", str(settings.scrape_pause_sec))))
    log(f"Backoffice: индекс={len(ids)}, кандидатов={len(candidates)}, baseline={initial}")
    with client.backoffice_scan(watched):
        for index, rid in enumerate(candidates, 1):
            check_stop()
            try:
                # Выбор/сохранение источника выполняется внутри release_lock
                # processor. Не пишем карточку при prefetch до блокировки.
                part = await processor.process_release(release_id=rid, refresh_qb_meta=True)
                outcome.release(rid, part)
                if part.get("errors", 0):
                    errors += 1
                processed += 1
                if rid in client.source_check_errors:
                    retained += 1
                    log(f"Backoffice: релиз {rid}: проверка сайта недоступна ({client.source_check_errors[rid]}); сохранён источник backoffice", "warning")
                log(f"Backoffice: {index}/{len(candidates)}, релиз {rid}", "debug")
            except BackofficeReleaseSkipped:
                outcome.release(rid, status="skipped")
            except SourceCheckDeferred:
                deferred += 1
                outcome.release(rid, status="skipped")
                log(f"Backoffice: релиз {rid}: проверка сайта недоступна; источник сохранён, обработка отложена", "warning")
            except AniLibriaNotFoundError:
                # Не трогаем архив и qB при исчезновении карточки / .torrent.
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
    if retained or deferred:
        outcome.notes.append({"code": "source_checks_deferred", "retained": retained, "deferred": deferred,
            "message": f"Проверка сайта недоступна: {retained} релизов обновлены из админки с прежним источником; обработка {deferred} релизов отложена до следующего обхода."})
    if deferred:
        outcome.failure(operation="source_check", code="source_check_deferred", retryable=False,
            message=f"Проверка источника отложена для {deferred} релизов: сайт недоступен; последние подтверждённые источники и архивы сохранены")
    if initial and errors == 0 and not deferred and not outcome.failures:
        upsert_setting(db, "backoffice_baseline_completed_at", utcnow().isoformat())
        db.commit()
    log(f"Backoffice: обработано={processed}, ошибок={errors}, источник сохранён={retained}, отложено={deferred}")
