"""Read-only Kensetsu polling with persisted leases and separate execution state."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import math
from urllib.parse import quote
from uuid import uuid4

import httpx
from sqlalchemy import or_, select, update

from app.db.models import EncoderJob, EncoderSubmission, EncoderSubmissionItem
from app.db.session import SessionLocal
from app.utils.datetime_fmt import as_utc_iso, utcnow

POLL_INTERVAL = 30
TERMINAL_INTERVAL = 300
BATCH_LIMIT = 20
LEASE_SECONDS = 120
LABELS = {"unknown": "Статус ещё не получен", "planned": "В очереди", "in_progress": "Кодируется",
          "done": "Кодирование завершено", "error": "Ошибка кодирования", "cancelled": "Остановлено"}


def ensure_item_jobs(item):
    if item.status != "accepted":
        return
    known = {job.external_job_id for job in item.encoding_jobs}
    for external_id in item.external_job_ids or []:
        if external_id not in known:
            item.encoding_jobs.append(EncoderJob(external_job_id=external_id, status="unknown", poll_failures=0))
            known.add(external_id)


def serialize_job(job, base_url):
    available = job.status == "done" and job.output_available is True and job.poll_error_kind != "not_found"
    return {"id": job.id, "external_job_id": job.external_job_id, "status": job.status,
            "label": LABELS[job.status], "progress": job.progress,
            "output_filename": job.output_filename, "output_available": job.output_available,
            "output_size_bytes": job.output_size_bytes,
            "download_url": download_url(base_url, job.external_job_id) if available else None,
            "created_at": as_utc_iso(job.remote_created_at), "started_at": as_utc_iso(job.remote_started_at),
            "completed_at": as_utc_iso(job.remote_completed_at), "error_message": job.error_message,
            "poll_error": job.poll_error, "poll_error_kind": job.poll_error_kind,
            "last_checked_at": as_utc_iso(job.last_checked_at), "last_success_at": as_utc_iso(job.last_success_at),
            "output_checked_at": as_utc_iso(job.output_checked_at)}


def download_url(base_url, external_id):
    return base_url.rstrip("/") + "/api/jobs/" + quote(external_id, safe="") + "/download"


def _date(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Некорректная дата Kensetsu")
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return date.replace(tzinfo=timezone.utc).astimezone(timezone.utc).replace(tzinfo=None) if date.tzinfo is None else date.astimezone(timezone.utc).replace(tzinfo=None)


def _parse(payload, external_id, expected_path=None):
    if not isinstance(payload, dict) or str(payload.get("id", "")) != external_id:
        raise ValueError("Ответ Kensetsu не соответствует ID задания")
    if payload.get("input_path") is not None and expected_path and payload["input_path"] != expected_path:
        raise ValueError("Исходник Kensetsu не соответствует сохранённой квитанции")
    status = payload.get("status")
    if status not in LABELS or status == "unknown":
        raise ValueError("Kensetsu вернул неизвестный статус")
    progress = payload.get("progress")
    if progress is not None and (isinstance(progress, bool) or not isinstance(progress, (int, float)) or not math.isfinite(progress) or not 0 <= progress <= 100):
        raise ValueError("Некорректный прогресс Kensetsu")
    from app.services.encoder_history import safe_json
    return {"status": status, "progress": float(progress) if progress is not None else (100.0 if status == "done" else None),
            "output_filename": safe_json(payload.get("output_filename")) if isinstance(payload.get("output_filename"), str) else None,
            "error_message": str(safe_json(payload["error_message"])) if payload.get("error_message") else None,
            "remote_created_at": _date(payload.get("created_at")), "remote_started_at": _date(payload.get("started_at")),
            "remote_completed_at": _date(payload.get("completed_at"))}


async def observe(client, target):
    """Return observed fields; never create, restart or download an encoding."""
    fields = {}
    kind = None
    error = None
    try:
        async with asyncio.timeout(15):
            response = await client.get(target["base_url"].rstrip("/") + "/api/jobs/" + quote(target["external_job_id"], safe=""))
            if response.status_code == 404:
                return fields, "not_found", "Задание не найдено в Kensetsu"
            response.raise_for_status()
            payload = response.json()
            fields = _parse(payload, target["external_job_id"], target.get("full_path"))
            fields["last_success_at"] = utcnow()
            if "output_available" in payload:
                available = payload["output_available"]
                if not isinstance(available, bool) or available and fields["status"] != "done":
                    fields["output_available"] = None
                    raise ValueError("Некорректный признак готовности Kensetsu")
                size = payload.get("output_size_bytes")
                if size is not None and (isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= 2**63-1):
                    fields["output_available"] = None
                    raise ValueError("Некорректный размер результата Kensetsu")
                fields.update(output_available=available, output_size_bytes=size if available else None,
                              output_checked_at=utcnow())
            elif fields["status"] == "done":
                fields["output_available"] = None
                try:
                    head = await client.head(download_url(target["base_url"], target["external_job_id"]))
                    media_type = head.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
                    if head.status_code in (200, 206) and (media_type.startswith("text/") or media_type == "application/json"):
                        kind, error = "output_probe_failed", "Вместо файла Kensetsu вернул страницу или JSON"
                    elif head.status_code in (200, 206):
                        size = head.headers.get("Content-Length", "")
                        size = int(size) if size.isdigit() else None
                        size = size if size is not None and 0 <= size <= 2**63-1 else None
                        fields.update(output_available=True, output_size_bytes=size, output_checked_at=utcnow())
                    elif head.status_code in (404, 410):
                        fields.update(output_available=False, output_size_bytes=None, output_checked_at=utcnow())
                    else:
                        kind, error = "output_probe_failed", f"Доступность файла не подтверждена: HTTP {head.status_code}"
                except httpx.HTTPError:
                    kind, error = "output_probe_failed", "Не удалось проверить доступность готового файла"
            else:
                fields.update(output_available=False, output_size_bytes=None)
    except (httpx.TimeoutException, TimeoutError):
        kind, error = "connection", "Истекло время ожидания Kensetsu"
    except httpx.RequestError:
        kind, error = "connection", "Нет связи с Kensetsu"
    except httpx.HTTPStatusError as exc:
        kind, error = "http_error", f"Ошибка проверки Kensetsu: HTTP {exc.response.status_code}"
    except (ValueError, TypeError, OverflowError):
        kind, error = "invalid_response", "Некорректный ответ Kensetsu; последний известный статус сохранён"
    return fields, kind, error


def claim_due(db, *, now=None, limit=BATCH_LIMIT):
    now = now or utcnow()
    due = (EncoderJob.next_poll_at <= now, or_(EncoderJob.claim_token.is_(None), EncoderJob.lease_until <= now))
    ids = db.scalars(select(EncoderJob.id).where(*due).order_by(EncoderJob.next_poll_at, EncoderJob.id).limit(limit)).all()
    claimed = []
    for job_id in ids:
        token = uuid4().hex
        changed = db.execute(update(EncoderJob).where(EncoderJob.id == job_id, *due).values(
            claim_token=token, lease_until=now + timedelta(seconds=LEASE_SECONDS)
        )).rowcount
        if changed:
            row = db.execute(select(EncoderJob.external_job_id, EncoderJob.poll_failures, EncoderSubmission.base_url, EncoderSubmission.id, EncoderSubmissionItem.full_path).select_from(EncoderJob)
                             .join(EncoderSubmissionItem, EncoderSubmissionItem.id == EncoderJob.item_id)
                             .join(EncoderSubmission, EncoderSubmission.id == EncoderSubmissionItem.submission_id)
                             .where(EncoderJob.id == job_id)).one()
            claimed.append({"id": job_id, "token": token, "external_job_id": row[0], "failures": row[1], "base_url": row[2], "submission_id": row[3], "full_path": row[4]})
    db.commit()
    return claimed


def save_observation(db, target, fields, kind, error, *, now=None):
    now = now or utcnow()
    failures = target["failures"] + 1 if kind else 0
    delay = min(900, POLL_INTERVAL * 2 ** min(failures, 5)) if kind else (
        TERMINAL_INTERVAL if fields.get("status") in {"done", "error", "cancelled"} else POLL_INTERVAL)
    values = {**fields, "poll_error_kind": kind, "poll_error": error, "poll_failures": failures,
              "last_checked_at": now, "updated_at": now, "next_poll_at": now + timedelta(seconds=delay),
              "claim_token": None, "lease_until": None}
    changed = db.execute(update(EncoderJob).where(EncoderJob.id == target["id"], EncoderJob.claim_token == target["token"], EncoderJob.lease_until > now).values(**values)).rowcount
    if changed:
        db.execute(update(EncoderSubmission).where(EncoderSubmission.id == target["submission_id"]).values(updated_at=now))
    db.commit()
    return bool(changed)


async def poll_encoder_jobs(*, session_factory=SessionLocal, transport=None):
    from app.services.video_kensetsu import is_video_kensetsu_enabled
    with session_factory() as db:
        if not is_video_kensetsu_enabled(db):
            return 0
        targets = claim_due(db)
    if not targets:
        return 0
    semaphore = asyncio.Semaphore(4)

    async def check(target, client):
        async with semaphore:
            fields, kind, error = await observe(client, target)
            with session_factory() as db:
                return save_observation(db, target, fields, kind, error)

    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False, transport=transport) as client:
            results = await asyncio.gather(*(check(target, client) for target in targets))
        return sum(results)
    finally:
        with session_factory() as db:
            for target in targets:
                db.execute(update(EncoderJob).where(EncoderJob.id == target["id"], EncoderJob.claim_token == target["token"]).values(claim_token=None, lease_until=None))
            db.commit()
