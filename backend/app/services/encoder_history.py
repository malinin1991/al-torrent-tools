"""Durable Kensetsu receipts with separately observed remote execution.

"sending" is committed before the external POST. A request_id is consumed
once even if the response is lost. No retry of an uncertain external side
effect is automatic. Snapshot identities never use basename matching. A legacy
single-file response may echo only its exact filename inside that one request;
batch responses still require an exact path to associate a source with a job.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from sqlalchemy import Text, and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db.models import DiskFileHash, EncoderJob, EncoderSubmission, EncoderSubmissionItem, Release, TorrentArchive, TorrentFile
from app.logging_filters import redact_secrets
from app.utils.datetime_fmt import as_utc_iso, utcnow

LABELS = {
    "sending": "Отправляется",
    "accepted": "Принят Kensetsu",
    "not_sent": "Не отправлен",
    "rejected": "Отклонён Kensetsu",
    "unknown": "Приём не подтверждён",
}
ATTENTION = ("not_sent", "rejected", "unknown")
_SENSITIVE = re.compile(r"token|password|secret|authorization|cookie|passkey|api[_-]?key", re.I)
_UNCONFIRMED_RECEIPT = "Ответ не подтверждает приём именно этого файла"


class SubmissionConflict(ValueError):
    def __init__(self, message: str, previous: list[dict] | None = None):
        super().__init__(message)
        self.previous = previous or []


def safe_target(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        return ""
    # Credentials/query strings are not needed to identify the history target.
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path.rstrip("/"), "", ""))


def safe_json(value: Any, *, depth: int = 0) -> Any:
    """Bound and redact diagnostic snapshots; never store arbitrary secrets."""
    if depth > 5:
        return "…"
    if isinstance(value, dict):
        return {
            str(k)[:200]: "***" if _SENSITIVE.search(str(k)) else safe_json(v, depth=depth + 1)
            for k, v in list(value.items())[:120]
        }
    if isinstance(value, (list, tuple)):
        return [safe_json(v, depth=depth + 1) for v in value[:120]]
    if isinstance(value, str):
        value = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1***@", value)
        value = re.sub(r"([?&](?:token|access_token|api_key|password|secret)=)[^&\s\"\']+", r"\1***", value, flags=re.I)
        value = re.sub(r"(Bearer\s+)[^\s\"\']+", r"\1***", value, flags=re.I)
        return redact_secrets(value)[:2000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return safe_json(str(value), depth=depth + 1)


def request_digest(file_ids: list[int], video_id: str, audio_id: str, downmix: str, retry_of: int | None) -> str:
    value = [sorted(set(file_ids)), video_id.strip(), audio_id.strip(), downmix, retry_of]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def get_submission(db: Session, submission_id: int) -> EncoderSubmission | None:
    return db.scalar(select(EncoderSubmission).where(EncoderSubmission.id == submission_id).options(selectinload(EncoderSubmission.items).selectinload(EncoderSubmissionItem.encoding_jobs)))


def replay(db: Session, request_id: str, digest: str) -> EncoderSubmission | None:
    row = db.scalar(select(EncoderSubmission).where(EncoderSubmission.request_id == request_id).options(selectinload(EncoderSubmission.items).selectinload(EncoderSubmissionItem.encoding_jobs)))
    if row is not None and row.request_digest != digest:
        raise SubmissionConflict("Этот request_id уже использован с другими файлами или параметрами")
    return row


def recover_expired(db: Session) -> None:
    now = utcnow()
    rows = db.scalars(select(EncoderSubmission).where(EncoderSubmission.finished_at.is_(None), EncoderSubmission.expires_at < now).options(selectinload(EncoderSubmission.items))).all()
    for row in rows:
        for item in row.items:
            if item.status == "sending":
                item.status = "unknown" if row.request_started_at else "not_sent"
                item.error = "Результат потерян после начала отправки; проверьте Kensetsu" if row.request_started_at else "Подготовка прервана до отправки запроса"
        row.finished_at = now
        row.updated_at = now
    if rows:
        db.commit()


def source_item(db: Session, file_id: int) -> EncoderSubmissionItem:
    row = db.get(TorrentFile, file_id)
    if row is None:
        return EncoderSubmissionItem(file_id=file_id, status="not_sent", error="Файл не найден", source_json={}, external_job_ids=[])
    rid = getattr(row, "release_id", None)
    release = db.scalar(select(Release).where(Release.release_id == rid)) if rid else None
    archive = db.scalar(select(TorrentArchive).where(TorrentArchive.info_hash == row.info_hash).order_by(TorrentArchive.id.desc()).limit(1))
    title = release.title if release else (archive.anime_name if archive else "")
    return EncoderSubmissionItem(
        file_id=file_id, release_id=rid, torrent_id=getattr(row, "torrent_id", None),
        info_hash=row.info_hash or "", relative_path=getattr(row, "relative_path", "") or "",
        full_path=getattr(row, "full_path", "") or "", status="sending", external_job_ids=[],
        source_json={"title": title or "", "archive_id": archive.id if archive else None, "size": getattr(row, "size", None)},
    )


def capture_path(db: Session, item: EncoderSubmissionItem, path: str) -> None:
    item.full_path = path
    snapshot = dict(item.source_json)
    stat = Path(path).stat()
    snapshot.update(size=stat.st_size, mtime=stat.st_mtime, mtime_ns=stat.st_mtime_ns)
    disk = db.scalar(select(DiskFileHash).where(DiskFileHash.full_path == path))
    if disk and disk.size == stat.st_size and disk.mtime == stat.st_mtime and disk.content_hash:
        snapshot.update(content_hash=disk.content_hash, hash_algo=disk.hash_algo, hashed_at=as_utc_iso(disk.last_hashed_at))
    item.source_json = snapshot


def claim(
    db: Session, *, file_ids: list[int], request_id: str | None, digest: str,
    base_url: str, video_id: str, audio_id: str, downmix: str,
    retry_of: int | None = None, allow_repeat: bool = False, acknowledge_unknown: bool = False,
) -> tuple[EncoderSubmission, bool]:
    recover_expired(db)
    key = request_id or str(uuid4())
    existing = replay(db, key, digest)
    if existing is not None:
        return existing, False
    target = safe_target(base_url)
    items = [source_item(db, fid) for fid in file_ids]
    if retry_of is not None and get_submission(db, retry_of) is None:
        raise SubmissionConflict("Предыдущая отправка не найдена")
    # Serialize competing actions for overlapping sources, not only equal batches.
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        locks = set()
        for item in items:
            identity = [target, item.info_hash, item.relative_path, video_id, audio_id, downmix]
            locks.add(int.from_bytes(hashlib.sha256(json.dumps(identity).encode()).digest()[:8], "big", signed=True))
        for key_hash in sorted(locks):
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key_hash})
        existing = replay(db, key, digest)
        if existing is not None:
            db.commit()
            return existing, False
    previous = []
    for item in items:
        if not item.info_hash:
            continue
        old = db.scalar(
            select(EncoderSubmissionItem).join(EncoderSubmission)
            .where(EncoderSubmission.base_url == target, EncoderSubmission.video_id == video_id,
                   EncoderSubmission.audio_id == audio_id, EncoderSubmission.audio_downmix == downmix,
                   EncoderSubmissionItem.info_hash == item.info_hash,
                   EncoderSubmissionItem.relative_path == item.relative_path,
                   EncoderSubmissionItem.status.in_(("sending", "accepted", "unknown")))
            .order_by(EncoderSubmissionItem.id.desc()).limit(1)
        )
        if old and (old.status == "sending" or old.status == "accepted" and not allow_repeat or old.status == "unknown" and not acknowledge_unknown):
            previous.append({"file_id": item.file_id, "status": old.status, "submission_id": old.submission_id, "path": item.relative_path})
    if previous:
        db.rollback()
        raise SubmissionConflict("Выбранные файлы уже отправлялись с этими пресетами. Проверьте историю перед повтором.", previous)
    now = utcnow()
    group = EncoderSubmission(
        request_id=key, request_digest=digest, base_url=target,
        video_id=video_id, audio_id=audio_id, audio_downmix=downmix,
        retry_of_id=retry_of, created_at=now, updated_at=now,
        # Preset preflight + maximum 300s encode + margin. Not a 60s job reclaim.
        expires_at=now + timedelta(seconds=600), items=items, presets_json={}, response_json={},
    )
    db.add(group)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = replay(db, key, digest)
        if existing is None:
            raise
        return existing, False
    return group, True


def _job_id(value: Any) -> str | None:
    if isinstance(value, dict):
        raw = value.get("job_id", value.get("id"))
    else:
        raw = value
    return str(raw)[:255] if isinstance(raw, (str, int)) and not isinstance(raw, bool) and str(raw) else None


def settle_response(group: EncoderSubmission, status_code: int, body: Any) -> None:
    """Only positive evidence accepts files; never infer from missing errors."""
    sent = [i for i in group.items if i.status == "sending"]
    payload = body if isinstance(body, dict) else {}
    errors = payload.get("errors") or []
    errors = errors if isinstance(errors, list) else [errors]
    jobs = payload.get("jobs")
    jobs = jobs if isinstance(jobs, list) else []
    if payload.get("job") is not None:
        jobs = [*jobs, payload["job"]]
    elif payload.get("job_id") is not None:
        jobs = [*jobs, {"job_id": payload["job_id"]}]

    def match(value: Any) -> EncoderSubmissionItem | None:
        marker = str(value.get("path") or value.get("input_path") or value.get("filename") or "") if isinstance(value, dict) else ""
        matches = [i for i in sent if marker and i.full_path == marker]
        if len(matches) == 1:
            return matches[0]
        # Current Kensetsu single-file replies contain job.filename, not the
        # requested path. Bind only to the sole actually posted source, with an
        # exact filename echo and no conflicting explicit path. Never infer a
        # batch mapping from basenames or response order.
        if (len(sent) == 1 and isinstance(value, dict)
                and not value.get("path") and not value.get("input_path")
                and isinstance(value.get("filename"), str)
                and sent[0].full_path
                and value["filename"] == Path(sent[0].full_path).name):
            return sent[0]
        if not marker and len(sent) == 1:
            return sent[0]
        return None

    unmatched_error = False
    contradictory = set()
    unmatched_job_marker = False
    for error in errors:
        item = match(error)
        if item is None:
            unmatched_error = True
        else:
            item.status = "rejected"
            item.error = str(safe_json(error.get("error") or error.get("message") or error)) if isinstance(error, dict) else str(safe_json(error))
    for job in jobs:
        jid = _job_id(job)
        item = match(job)
        if isinstance(job, dict) and any(job.get(k) for k in ("path", "input_path", "filename")) and item is None:
            unmatched_job_marker = True
        if jid and item is not None:
            if item.status == "rejected" or item.file_id in contradictory:
                contradictory.add(item.file_id)
                item.status = "unknown"
                item.error = "Противоречивый ответ: для файла получены и задание, и ошибка"
                unmatched_error = True
            else:
                item.status = "accepted"
                item.external_job_ids = sorted(set([*item.external_job_ids, jid]))
    remaining = [i for i in sent if i.status == "sending"]
    accepted = sum(i.status == "accepted" for i in sent)
    created = payload.get("created")
    count = int(created) if isinstance(created, (int, bool)) and created >= 0 else None
    job_ids = list(dict.fromkeys(jid for value in jobs if (jid := _job_id(value))))
    all_positive = not unmatched_error and not unmatched_job_marker and (
        count == len(remaining) + accepted and count is not None and count > 0
        or len(job_ids) == len(remaining) + accepted and bool(job_ids)
    )
    if all_positive:
        for item in remaining:
            item.status = "accepted"
            # An array position is not a source-to-job mapping.
            if len(sent) == 1 and len(job_ids) == 1:
                item.external_job_ids = job_ids
    for item in sent:
        if item.status == "sending":
            item.status = "unknown"
            item.error = _UNCONFIRMED_RECEIPT
    group.http_status = status_code
    group.response_json = safe_json(payload)
    group.finished_at = utcnow()
    group.updated_at = group.finished_at
    from app.services.encoder_tracking import ensure_item_jobs
    for item in sent:
        ensure_item_jobs(item)


def repair_saved_receipt(group: EncoderSubmission) -> list[int]:
    """Reparse a saved positive acknowledgement without contacting Kensetsu.

    Only parser-produced unknowns can be repaired. Reconstruct the whole posted
    scope so a partly successful batch never becomes a single-file request.
    Callers own the transaction and may roll it back for a dry run.
    """
    if (group.finished_at is None or group.request_started_at is None
            or group.http_status not in (200, 207) or not isinstance(group.response_json, dict)):
        return []
    eligible = {i.id for i in group.items if i.status == "unknown" and i.error == _UNCONFIRMED_RECEIPT}
    if not eligible:
        return []
    posted = [i for i in group.items if i.status != "not_sent"]
    probe = EncoderSubmission(items=[EncoderSubmissionItem(
        file_id=i.file_id, full_path=i.full_path, relative_path=i.relative_path,
        status="sending", external_job_ids=[],
    ) for i in posted])
    settle_response(probe, group.http_status, group.response_json)
    repaired = []
    for original, parsed in zip(posted, probe.items):
        if original.id in eligible and parsed.status == "accepted" and parsed.external_job_ids:
            original.status = "accepted"
            original.external_job_ids = parsed.external_job_ids
            original.error = None
            from app.services.encoder_tracking import ensure_item_jobs
            ensure_item_jobs(original)
            repaired.append(original.file_id)
    if repaired:
        group.updated_at = utcnow()
    return repaired


def finish_failure(db: Session, group: EncoderSubmission, error: Any, *, started: bool) -> None:
    for item in group.items:
        if item.status == "sending":
            item.status = "unknown" if started else "not_sent"
            item.error = str(safe_json(error))
    group.error = str(safe_json(error))
    group.finished_at = utcnow()
    group.updated_at = group.finished_at
    db.commit()


def serialize(group: EncoderSubmission) -> dict:
    from app.services.encoder_tracking import serialize_job
    counts = dict(Counter(i.status for i in group.items))
    payload = group.response_json or {}
    result = {
        "id": group.id, "submission_id": group.id, "request_id": group.request_id,
        "history_url": f"/encoder-history?submission_id={group.id}",
        "created_at": as_utc_iso(group.created_at), "request_started_at": as_utc_iso(group.request_started_at),
        "finished_at": as_utc_iso(group.finished_at), "base_url": group.base_url,
        "retry_of_id": group.retry_of_id, "video_id": group.video_id, "audio_id": group.audio_id,
        "audio_downmix": group.audio_downmix, "presets": group.presets_json,
        "counts": counts, "ok": bool(group.items) and counts.get("accepted", 0) == len(group.items),
        "ready_count": sum(any(j.status == "done" and j.output_available is True and j.poll_error_kind != "not_found" for j in i.encoding_jobs) for i in group.items),
        "partial": any(i.status != "accepted" for i in group.items),
        "file_ids": [i.file_id for i in group.items if i.status == "accepted"],
        "created": payload.get("created"), "job": payload.get("job"), "jobs": payload.get("jobs"),
        "result": payload, "http_status": group.http_status, "error": group.error,
        "items": [{"id": i.id, "file_id": i.file_id, "release_id": i.release_id, "torrent_id": i.torrent_id,
                   "info_hash": i.info_hash, "relative_path": i.relative_path, "full_path": i.full_path,
                   "source": i.source_json, "status": i.status, "label": LABELS[i.status],
                   "error": i.error, "external_job_ids": i.external_job_ids,
                   "encoding_jobs": [serialize_job(j, group.base_url) for j in i.encoding_jobs]} for i in group.items],
    }
    result["local_errors"] = [{"file_id": i.file_id, "error": i.error} for i in group.items if i.status == "not_sent"] or None
    result["errors"] = [{"file_id": i.file_id, "error": i.error} for i in group.items if i.status in ("rejected", "unknown")] or None
    return result


def history_page(db: Session, *, page: int = 1, search: str = "", status: str = "", encoding_state: str = "", release_id: int | None = None, submission_id: int | None = None) -> dict:
    recover_expired(db)
    query = select(EncoderSubmission)
    conditions = []
    if status == "attention":
        conditions.append(or_(EncoderSubmissionItem.status.in_(ATTENTION), EncoderSubmissionItem.encoding_jobs.any(or_(
            EncoderJob.status.in_(("error", "cancelled")), EncoderJob.poll_error.is_not(None),
            and_(EncoderJob.status == "done", EncoderJob.output_available.is_(False)),
        ))))
    elif status in LABELS:
        conditions.append(EncoderSubmissionItem.status == status)
    if release_id is not None:
        conditions.append(EncoderSubmissionItem.release_id == release_id)
    execution_filters = {
        "active": EncoderJob.status.in_(("unknown", "planned", "in_progress")),
        "ready": and_(EncoderJob.status == "done", EncoderJob.output_available.is_(True), or_(EncoderJob.poll_error_kind.is_(None), EncoderJob.poll_error_kind != "not_found")),
        "error": EncoderJob.status.in_(("error", "cancelled")),
        "unavailable": or_(EncoderJob.poll_error.is_not(None), and_(EncoderJob.status == "done", EncoderJob.output_available.is_(False))),
    }
    if encoding_state in execution_filters:
        conditions.append(EncoderSubmissionItem.encoding_jobs.any(execution_filters[encoding_state]))
    if search:
        term = f"%{search[:200]}%"
        conditions.append(or_(EncoderSubmissionItem.relative_path.ilike(term), EncoderSubmissionItem.source_json.cast(Text).ilike(term), EncoderSubmissionItem.external_job_ids.cast(Text).ilike(term)))
    if conditions:
        query = query.where(EncoderSubmission.items.any(and_(*conditions)))
    if submission_id is not None:
        query = query.where(EncoderSubmission.id == submission_id)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    pages = max(1, (total + 19) // 20)
    page = min(max(1, page), pages)
    groups = db.scalars(query.order_by(EncoderSubmission.id.desc()).offset((page - 1) * 20).limit(20).options(selectinload(EncoderSubmission.items).selectinload(EncoderSubmissionItem.encoding_jobs))).all()
    return {"submissions": [serialize(g) for g in groups], "total": total, "page": page, "total_pages": pages,
            "search": search, "status_filter": status, "encoding_state": encoding_state, "release_id": release_id, "submission_id": submission_id, "encoder_labels": LABELS}


def latest_receipts(db: Session, file_ids: list[int]) -> dict:
    recover_expired(db)
    files = db.scalars(select(TorrentFile).where(TorrentFile.id.in_(file_ids))).all()
    sources = {f.id: (f.info_hash, f.relative_path) for f in files}
    rows = db.scalars(select(EncoderSubmissionItem).where(EncoderSubmissionItem.info_hash.in_({h for h, _ in sources.values()})).order_by(EncoderSubmissionItem.id.desc())).all()
    by_source = {}
    for item in rows:
        identity = (item.info_hash, item.relative_path)
        if identity not in by_source:
            by_source[identity] = {"status": item.status, "label": LABELS[item.status], "submission_id": item.submission_id,
                                  "history_url": f"/encoder-history?submission_id={item.submission_id}"}
    return {fid: by_source[source] for fid, source in sources.items() if source in by_source}
