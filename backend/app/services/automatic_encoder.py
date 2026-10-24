"""Durable automatic AVC candidates. Never changes sticky, pipeline or HEVC state.

All permission changes and the dispatch intent share a short transaction lock.
The lock is never held across HTTP or hashing. Archive registration uses this
same lock after the processor's release lock, so activation has one boundary.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import uuid4
from functools import lru_cache

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.orm import Session

from app.db.models import (
    DiskFileHash, EncoderAutoEvent, EncoderAutoItem, EncoderAutoSource,
    EncoderFileReadiness, EncoderSubmission, EncoderSubmissionItem,
    ReleaseEncoderPolicy, Setting, TorrentArchive, TorrentFile, TrackedRelease,
)
from app.services.runtime_settings import get_setting_value, upsert_setting
from app.services.video_kensetsu import is_video_kensetsu_enabled, resolve_video_kensetsu_base_url
from app.utils.datetime_fmt import as_utc_iso, utcnow

AUTO_KEY = "video_kensetsu_auto_enabled"
ACTIVE = ("waiting", "ready", "claimed")
REASONS = {
    "preset": "Требуется пресет", "preset_changed": "Пресет изменился — сохраните назначение заново",
    "settings": "Параметры обновлены — ожидает проверки",
    "metadata": "Ожидает метаданных AVC", "hash": "Ожидает проверки хеша",
    "download": "Ожидает полного исходника", "selection": "Файл не выбран на master",
    "server": "Ожидает подключения Kensetsu", "paused": "Автокодирование на паузе",
    "ready": "Готов к отправке", "sending": "Отправляется",
    "accepted": "Принят Kensetsu", "receipt_pending": "Проверяется приём Kensetsu",
    "unknown": "Приём требует проверки", "already_submitted": "Уже отправлен",
    "superseded": "Версия больше не актуальна", "untracked": "Отслеживание выключено",
    "removed": "Исключён из состава", "not_changed": "Содержимое не изменилось",
    "not_avc": "Исходник не AVC", "not_video": "Служебный файл",
    "rejected": "Kensetsu отклонил исходник — требуется действие",
    "source_conflict": "Исходник изменился во время приёма — требуется проверка",
}


def is_video_file(path):
    from app.services.torrent_files_meta import is_junk_file
    return not is_junk_file(path) and Path(path).suffix.lower() in {".mkv", ".mp4", ".m2ts", ".ts", ".avi", ".mov", ".webm"}


def lock(db: Session) -> None:
    if db.get_bind().dialect.name == "postgresql":
        with db.no_autoflush:
            db.execute(text("SELECT pg_advisory_xact_lock(1937006965)"))


def enabled(db: Session) -> bool:
    return get_setting_value(db, AUTO_KEY, "false").lower() in {"1", "true", "yes", "on"}


def initialized(db: Session) -> bool:
    row = db.get(Setting, "encoder_auto_initialized")
    return getattr(row, "value", None) == "true"


def audit(db, code, *, item=None, release_id=None, **details):
    from app.services.encoder_history import safe_json
    db.add(EncoderAutoEvent(item_id=item.id if item else None,
                           release_id=item.release_id if item else release_id,
                           code=code, details_json=safe_json(details)))


def transition(db, item, state, reason, detail=None):
    if (item.state, item.block_reason, item.detail) != (state, reason, detail):
        audit(db, reason, item=item, before=item.state, after=state, detail=detail)
        item.state, item.block_reason, item.detail = state, reason, detail
        item.updated_at = utcnow()


def policy_for(db, release_id):
    row = db.get(ReleaseEncoderPolicy, release_id)
    if row is None:
        row = ReleaseEncoderPolicy(release_id=release_id, revision=0, generation=0,
                                   participating=False, audio_downmix="none", presets_json={})
        db.add(row)
        db.flush()
    return row


def tracking_changed(db, release_id, tracked):
    """Called in the same transaction as UI/bot tracking changes, under lock."""
    if not initialized(db):
        return
    policy = policy_for(db, release_id)
    if policy.participating == tracked:
        return
    policy.participating = tracked
    policy.updated_at = utcnow()
    if tracked:
        policy.generation += 1
        hashes = db.scalars(select(TorrentArchive.info_hash).where(
            TorrentArchive.release_id == release_id).distinct()).all()
        db.add_all([EncoderAutoSource(release_id=release_id, generation=policy.generation,
                                     info_hash=h, baseline=True) for h in hashes])
        audit(db, "activation_baseline", release_id=release_id,
              generation=policy.generation, versions=len(hashes))
    else:
        for item in db.scalars(select(EncoderAutoItem).where(
            EncoderAutoItem.release_id == release_id, EncoderAutoItem.state.in_(ACTIVE))).all():
            group = db.get(EncoderSubmission, item.submission_id) if item.submission_id else None
            if group is None or group.request_started_at is None:
                transition(db, item, "cancelled", "untracked")
        audit(db, "untracked", release_id=release_id)


def activate(db):
    """Flush caller's settings first. Pausing never resets this boundary."""
    lock(db)
    db.flush()
    if enabled(db) and not initialized(db):
        upsert_setting(db, "encoder_auto_initialized", "true")
        db.flush()
        for rid in db.scalars(select(TrackedRelease.release_id).where(TrackedRelease.enabled.is_(True))).all():
            tracking_changed(db, rid, True)


def observe_archive(db, archive):
    """Register a version before archive commit, including versions without files."""
    lock(db)
    if not initialized(db):
        return
    tracked = db.get(TrackedRelease, archive.release_id)
    if not tracked or not tracked.enabled:
        return
    policy = policy_for(db, archive.release_id)
    if not policy.participating:
        tracking_changed(db, archive.release_id, True)
    known = db.scalar(select(EncoderAutoSource).where(
        EncoderAutoSource.release_id == archive.release_id,
        EncoderAutoSource.generation == policy.generation,
        EncoderAutoSource.info_hash == archive.info_hash))
    if known is None:
        db.add(EncoderAutoSource(release_id=archive.release_id, generation=policy.generation,
                                 info_hash=archive.info_hash, baseline=False))
        audit(db, "version_observed", release_id=archive.release_id, info_hash=archive.info_hash)


def register_file(db, file, *, explicit=False):
    policy = db.get(ReleaseEncoderPolicy, file.release_id)
    if not policy or not policy.participating:
        return None
    source = db.scalar(select(EncoderAutoSource).where(
        EncoderAutoSource.release_id == file.release_id, EncoderAutoSource.generation == policy.generation,
        EncoderAutoSource.info_hash == file.info_hash))
    if source is None or (source.baseline and not explicit):
        return None
    item = db.scalar(select(EncoderAutoItem).where(
        EncoderAutoItem.release_id == file.release_id, EncoderAutoItem.generation == policy.generation,
        EncoderAutoItem.info_hash == file.info_hash, EncoderAutoItem.relative_path == file.relative_path))
    if item is None:
        item = EncoderAutoItem(release_id=file.release_id, generation=policy.generation,
                               info_hash=file.info_hash, relative_path=file.relative_path, file_id=file.id,
                               reason="explicit_backfill" if explicit else file.ui_status,
                               state="waiting", block_reason="hash")
        db.add(item)
        db.flush()
        audit(db, "candidate_registered", item=item, reason=item.reason)
    elif item.state in ACTIVE:
        item.file_id = file.id
    return item


def record_readiness(db, file, disk, *, job_id=None, prior_info_hash=None, confirmed=True):
    """Only hash-settle calls this, never inventory or a generic successful job."""
    if not confirmed or not disk or not getattr(disk, "content_hash", None) or getattr(disk, "hash_algo", None) != "blake3":
        return
    try:
        path = Path(file.full_path).resolve()
        stat = path.stat()
    except (OSError, TypeError):
        return
    if not path.is_file() or disk.size != stat.st_size or disk.mtime != stat.st_mtime:
        return
    lock(db)
    row = db.scalar(select(EncoderFileReadiness).where(
        EncoderFileReadiness.info_hash == file.info_hash, EncoderFileReadiness.relative_path == file.relative_path))
    if row is None:
        row = EncoderFileReadiness(info_hash=file.info_hash, relative_path=file.relative_path)
        db.add(row)
    row.full_path, row.content_hash, row.hash_algo = str(path), disk.content_hash, disk.hash_algo
    row.size, row.mtime_ns, row.ui_status = stat.st_size, stat.st_mtime_ns, file.ui_status
    row.checked_at, row.job_id, row.prior_info_hash = utcnow(), job_id, prior_info_hash
    db.flush()
    if file.ui_status in {"new", "changed"}:
        register_file(db, file)


def source_readiness(db, file):
    return db.scalar(select(EncoderFileReadiness).where(
        EncoderFileReadiness.info_hash == file.info_hash,
        EncoderFileReadiness.relative_path == file.relative_path).execution_options(populate_existing=True))


def readiness_matches(file, ready):
    if not ready or not file.full_path or ready.ui_status != file.ui_status:
        return False
    try:
        path = Path(file.full_path).resolve()
        stat = path.stat()
        return (str(path) == ready.full_path and path.is_file()
                and stat.st_size == ready.size and stat.st_mtime_ns == ready.mtime_ns)
    except OSError:
        return False


def previous_receipt(db, file, *, exclude_submission_id=None):
    """Read-only duplicate preview; the common sender repeats it under locks."""
    ready = source_readiness(db, file)
    rows = db.execute(select(EncoderSubmissionItem, EncoderSubmission).join(EncoderSubmission).where(
        EncoderSubmissionItem.release_id == file.release_id,
        EncoderSubmissionItem.relative_path == file.relative_path,
        EncoderSubmissionItem.status.in_(("sending", "unknown", "accepted"))
    ).order_by(EncoderSubmissionItem.id.desc())).all()
    for old, group in rows:
        if group.id == exclude_submission_id:
            continue
        old_hash = old.source_json.get("content_hash")
        same_content = bool(ready and old_hash and old_hash == ready.content_hash
                            and old.source_json.get("hash_algo") == ready.hash_algo
                            and old.source_json.get("size") == ready.size)
        if (old.info_hash == file.info_hash or same_content
                or old.source_json.get("source_conflict")
                or old.status == "unknown" and not (ready and old_hash)):
            return old, group
    return None


def assess(db, item):
    from app.services.hevc_pairing import classify_archive_codec
    from app.services.releases_view import _info_hashes_with_active_hash_job, resolve_media_file_for_download
    policy = db.get(ReleaseEncoderPolicy, item.release_id, populate_existing=True)
    tracked = db.get(TrackedRelease, item.release_id, populate_existing=True)
    if not policy or not tracked or not tracked.enabled or not policy.participating or policy.generation != item.generation:
        return "cancelled", "untracked"
    archive = db.scalar(select(TorrentArchive).where(
        TorrentArchive.release_id == item.release_id, TorrentArchive.info_hash == item.info_hash,
        TorrentArchive.api_present.is_(True), TorrentArchive.superseded.is_(False)).execution_options(populate_existing=True))
    if not archive:
        return "cancelled", "superseded"
    codec = classify_archive_codec(quality_json=archive.quality_json, torrent_type=archive.torrent_type)
    if codec is None:
        return "waiting", "metadata"
    if codec != "AVC":
        return "cancelled", "not_avc"
    file = db.scalar(select(TorrentFile).where(TorrentFile.info_hash == item.info_hash,
                                               TorrentFile.relative_path == item.relative_path).execution_options(populate_existing=True))
    if not file or file.ui_status == "removed":
        return "cancelled", "removed"
    item.file_id = file.id
    if not is_video_file(file.relative_path):
        return "cancelled", "not_video"
    if file.ui_status not in {"new", "changed"}:
        return "cancelled", "not_changed"
    if not file.selected:
        return "waiting", "selection"
    # A completely empty assignment follows Kensetsu defaults. A partial pair
    # remains invalid rather than silently discarding the saved choice.
    if bool(policy.video_id) != bool(policy.audio_id):
        return "waiting", "preset"
    if not enabled(db):
        return "waiting", "paused"
    if not is_video_kensetsu_enabled(db) or not resolve_video_kensetsu_base_url(db):
        return "waiting", "server"
    if file.is_checking or _info_hashes_with_active_hash_job(db, [file.info_hash]):
        return "waiting", "hash"
    if resolve_media_file_for_download(file) is None:
        return "waiting", "download"
    if not readiness_matches(file, source_readiness(db, file)):
        return "waiting", "hash"
    previous = previous_receipt(db, file, exclude_submission_id=item.submission_id if item.state == "claimed" else None)
    if previous:
        old, group = previous
        item.submission_id = group.id
        item.receipt_item_id = old.id
        if old.source_json.get("source_conflict"):
            return "needs_attention", "source_conflict"
        if old.status == "accepted":
            return "accepted", "already_submitted"
        return ("receipt_pending", "receipt_pending") if old.status == "sending" else ("needs_attention", "unknown")
    return "ready", "ready"


def reconcile(db, *, limit=100):
    """Bounded page, round-robin via next_attempt_at; no filesystem catalog scan."""
    lock(db)
    from app.services.encoder_history import recover_expired
    recover_expired(db, commit=False)
    activate(db)
    upsert_setting(db, "encoder_auto_heartbeat_at", as_utc_iso(utcnow()))
    if not initialized(db):
        db.commit()
        return
    # Repair a missed candidate insertion using only observed versions, not dates.
    files = db.scalars(select(TorrentFile).join(
        EncoderAutoSource, EncoderAutoSource.info_hash == TorrentFile.info_hash).join(
        ReleaseEncoderPolicy, ReleaseEncoderPolicy.release_id == EncoderAutoSource.release_id).where(
        EncoderAutoSource.generation == ReleaseEncoderPolicy.generation,
        EncoderAutoSource.baseline.is_(False), ReleaseEncoderPolicy.participating.is_(True),
        TorrentFile.ui_status.in_(("new", "changed")),
        ~select(EncoderAutoItem.id).where(EncoderAutoItem.release_id == TorrentFile.release_id,
            EncoderAutoItem.generation == ReleaseEncoderPolicy.generation,
            EncoderAutoItem.info_hash == TorrentFile.info_hash,
            EncoderAutoItem.relative_path == TorrentFile.relative_path).exists()
    ).limit(limit)).all()
    for file in files:
        register_file(db, file)
    now = utcnow()
    rows = db.scalars(select(EncoderAutoItem).where(
        or_(EncoderAutoItem.state.in_((*ACTIVE, "receipt_pending")),
            and_(EncoderAutoItem.state == "needs_attention", EncoderAutoItem.block_reason == "unknown")),
        EncoderAutoItem.next_attempt_at <= now,
        or_(EncoderAutoItem.claim_token.is_(None), EncoderAutoItem.lease_until <= now)
    ).order_by(EncoderAutoItem.next_attempt_at, EncoderAutoItem.id).limit(limit)).all()
    for item in rows:
        if item.submission_id:
            group = db.get(EncoderSubmission, item.submission_id)
            if group and any(i.status in {"sending", "accepted", "unknown", "rejected"} for i in item_receipts(item, group)):
                settle_item(db, item, group)
                continue
        item.claim_token, item.lease_until = None, None
        state, reason = assess(db, item)
        transition(db, item, state, reason)
        item.next_attempt_at = now + timedelta(seconds=30)
    db.commit()


def item_receipts(item, group):
    return [row for row in group.items if row.id == item.receipt_item_id] if item.receipt_item_id else [
        row for row in group.items if row.info_hash == item.info_hash and row.relative_path == item.relative_path]


def settle_item(db, item, group):
    receipts = item_receipts(item, group)
    if not receipts:
        transition(db, item, "needs_attention", "unknown", "Квитанция не сопоставлена с точной версией файла")
        item.claim_token, item.lease_until = None, None
        return
    if item.state == "cancelled":
        item.claim_token, item.lease_until = None, None
        return
    if item.block_reason == "preset_changed" and group.request_started_at is None:
        item.claim_token, item.lease_until = None, None
        return
    if any(i.status == "accepted" for i in receipts):
        transition(db, item, "accepted", "accepted")
        upsert_setting(db, "encoder_auto_server_retry_at", "")
    elif any(i.status == "sending" for i in receipts):
        transition(db, item, "receipt_pending", "receipt_pending")
    elif any(i.status == "unknown" for i in receipts):
        transition(db, item, "needs_attention", "unknown")
    elif any(i.status == "rejected" for i in receipts):
        transition(db, item, "needs_attention", "rejected", group.error)
    else:
        transition(db, item, "waiting", "server", group.error)
        item.next_attempt_at = utcnow() + timedelta(seconds=min(900, 30 * 2 ** min(item.attempts, 5)))
        upsert_setting(db, "encoder_auto_server_retry_at", item.next_attempt_at.isoformat())
    if any(i.source_json.get("source_conflict") for i in receipts):
        transition(db, item, "needs_attention", "source_conflict")
    item.claim_token, item.lease_until = None, None


def claim_next(db):
    lock(db)
    now = utcnow()
    from datetime import datetime
    try:
        retry_at = datetime.fromisoformat(get_setting_value(db, "encoder_auto_server_retry_at", ""))
        if retry_at > now:
            db.commit()
            return None
    except ValueError:
        pass
    # One POST/receipt preparation per server, including a crashed sender lease.
    if db.scalar(select(EncoderAutoItem.id).where(
        EncoderAutoItem.state == "claimed", EncoderAutoItem.lease_until > now).limit(1)):
        db.commit()
        return None
    item = db.scalar(select(EncoderAutoItem).where(
        EncoderAutoItem.state == "ready").order_by(EncoderAutoItem.id).limit(1))
    if item is None:
        db.commit()
        return None
    state, reason = assess(db, item)
    if state != "ready":
        transition(db, item, state, reason)
        db.commit()
        return None
    item.claim_token, item.lease_until = uuid4().hex, now + timedelta(seconds=660)
    item.attempts += 1
    item.policy_revision = db.get(ReleaseEncoderPolicy, item.release_id).revision
    transition(db, item, "claimed", "sending")
    result = item.id, item.claim_token
    db.commit()
    return result


@lru_cache(maxsize=1)
def hash_runner():
    from app.services.job_runner import JobRunner
    from app.jobs.hash_torrent import run_hash_torrent
    runner = JobRunner()
    runner.register("hash_torrent", run_hash_torrent)
    return runner


def plan_hash_checks(db, runner):
    """Repair missing proof using the normal hash job, with a persistent cooldown."""
    from datetime import datetime
    from app.services.job_runner import JobAlreadyRunningError
    from app.services.releases_view import _info_hashes_with_active_hash_job, resolve_media_file_for_download
    rows = db.scalars(select(EncoderAutoItem).where(
        EncoderAutoItem.state == "waiting", EncoderAutoItem.block_reason == "hash"
    ).order_by(EncoderAutoItem.next_attempt_at, EncoderAutoItem.id).limit(100)).all()
    planned, seen = [], set()
    for item in rows:
        if len(planned) >= 4:
            break
        if item.info_hash in seen:
            continue
        seen.add(item.info_hash)
        file = db.get(TorrentFile, item.file_id)
        if not file or file.is_checking or resolve_media_file_for_download(file) is None:
            continue
        if _info_hashes_with_active_hash_job(db, [item.info_hash]):
            continue
        key = "encoder_auto_hash_" + item.info_hash
        try:
            last = datetime.fromisoformat(get_setting_value(db, key, ""))
            if (utcnow()-last).total_seconds() < 300:
                continue
        except ValueError:
            pass
        upsert_setting(db, key, utcnow().isoformat())
        try:
            job = runner.create_job(db, "hash_torrent", {
                "info_hash": item.info_hash, "torrent_id": file.torrent_id, "release_id": item.release_id,
            })
            planned.append(job.id)
        except JobAlreadyRunningError:
            db.rollback()
    db.commit()
    return planned


def validate_intent(db, item_id, token, preset_snapshot):
    """Final permission check; caller commits request_started_at under this lock."""
    lock(db)
    item = db.get(EncoderAutoItem, item_id, populate_existing=True)
    if not item or item.state != "claimed" or item.claim_token != token or item.lease_until <= utcnow():
        raise ValueError("Подготовка автоотправки больше не актуальна")
    policy = db.get(ReleaseEncoderPolicy, item.release_id, populate_existing=True)
    state, reason = assess(db, item)
    if state != "ready" or policy.revision != item.policy_revision:
        transition(db, item, state if state != "ready" else "waiting", reason if state != "ready" else "preset")
        raise ValueError("Настройки или исходник изменились во время подготовки")
    if policy.video_id and policy.audio_id and policy.presets_json != preset_snapshot:
        transition(db, item, "needs_attention", "preset_changed")
        raise ValueError("Пресет Kensetsu изменился; сохраните назначение заново")
    return item


async def tick(session_factory=None):
    import asyncio
    from types import SimpleNamespace
    from app.db.session import SessionLocal
    from app.services.encoder_submission import submit_files
    from app.services.encoder_receipts import recover_receipts
    factory = session_factory or SessionLocal
    runner = hash_runner() if session_factory is None else None
    def prepare():
        with factory() as db:
            reconcile(db)
            jobs = plan_hash_checks(db, runner) if runner else []
            return claim_next(db), jobs
    claimed, jobs = await asyncio.to_thread(prepare)
    for job_id in jobs:
        runner.schedule_job(job_id)
    if claimed:
        item_id, token = claimed
        def options():
            with factory() as db:
                item = db.get(EncoderAutoItem, item_id)
                policy = db.get(ReleaseEncoderPolicy, item.release_id)
                return item.file_id, SimpleNamespace(
                    video_id=policy.video_id, audio_id=policy.audio_id, audio_downmix=policy.audio_downmix,
                    request_id=uuid4().hex, retry_of=item.submission_id, allow_repeat=False,
                    acknowledge_unknown=False)
        file_id, body = await asyncio.to_thread(options)
        with factory() as db:
            try:
                await submit_files([file_id], body, db, auto_item_id=item_id, claim_token=token)
            except Exception as exc:
                # The durable receipt decides retryability, never the scheduler.
                db.rollback()
                from fastapi import HTTPException
                if isinstance(exc, HTTPException) and isinstance(exc.detail, dict) and exc.detail.get("previous"):
                    lock(db)
                    row = db.get(EncoderAutoItem, item_id)
                    prior = exc.detail["previous"][0]
                    if row and row.claim_token == token:
                        row.submission_id = prior["submission_id"]
                        row.receipt_item_id = prior.get("item_id")
                        group = db.get(EncoderSubmission, row.submission_id)
                        settle_item(db, row, group)
                        if row.state == "accepted":
                            transition(db, row, "accepted", "already_submitted")
                        db.commit()
            lock(db)
            item = db.get(EncoderAutoItem, item_id)
            if item and item.claim_token == token:
                group = db.get(EncoderSubmission, item.submission_id) if item.submission_id else None
                if group:
                    settle_item(db, item, group)
                else:
                    # Defaults are fetched before receipt creation. Tracking
                    # may be removed during that await, cancelling this claim.
                    if item.state != "cancelled":
                        transition(db, item, "waiting", "server")
                    item.claim_token, item.lease_until = None, None
                    item.next_attempt_at = utcnow() + timedelta(seconds=30)
            db.commit()
    await recover_receipts(factory)
