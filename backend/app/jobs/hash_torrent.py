"""Джоб хеширования одного торрента после master_complete."""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import JobLog, TorrentFile
from app.services.file_tracker import FileTrackerService
from app.services.job_runner import JobStopRequested, is_stop_requested
from app.services.pipeline import TorrentPipelineService, record_pipeline_event

# Мягкий пропуск: джоб success (нечего делать, retry не нужен).
_SOFT_SKIP_REASONS = frozenset(
    {
        "торрент не api_present (архивный)",
    }
)


def _add_log(db: Session, job_id: int, message: str, level: str = "info") -> None:
    db.add(JobLog(job_id=job_id, level=level, message=message))
    db.commit()


def _pipeline_for_hash(db: Session, info_hash: str, job_id: int):
    return TorrentPipelineService(db, job_id=job_id).get_latest_by_hash(info_hash)


def _ui_status_counts(db: Session, info_hash: str) -> dict[str, int]:
    statuses = list(
        db.scalars(select(TorrentFile.ui_status).where(TorrentFile.info_hash == info_hash)).all()
    )
    counts = Counter((s or "").strip().lower() or "unknown" for s in statuses)
    return {
        "new": int(counts.get("new", 0)),
        "changed": int(counts.get("changed", 0)),
        "ok": int(counts.get("ok", 0)),
        "total": len(statuses),
    }


async def run_hash_torrent(db: Session, job_id: int, params: dict[str, Any]) -> None:
    info_hash = str(params.get("info_hash") or "").strip().lower()
    torrent_id = params.get("torrent_id")
    release_id = params.get("release_id")
    if not info_hash or not isinstance(torrent_id, int) or not isinstance(release_id, int):
        raise ValueError("hash_torrent: нужны info_hash, torrent_id, release_id")

    if is_stop_requested(db, job_id):
        _add_log(db, job_id, "hash_torrent: остановка по запросу", "warning")
        raise JobStopRequested()

    pipeline = _pipeline_for_hash(db, info_hash, job_id)
    start_msg = (
        f"hash_torrent: start hash={info_hash[:12]}… torrent_id={torrent_id} release_id={release_id}"
    )
    _add_log(db, job_id, start_msg)
    if pipeline is not None:
        record_pipeline_event(
            db,
            pipeline.id,
            event_type="hash_progress",
            message=start_msg,
            job_id=job_id,
            details={"actor": "job", "phase": "start", "info_hash": info_hash},
        )

    tracker = FileTrackerService(db, job_id=job_id)
    result = tracker.track_torrent(
        info_hash=info_hash,
        torrent_id=torrent_id,
        release_id=release_id,
    )
    if result.skipped_reason:
        skip_msg = f"hash_torrent: пропуск — {result.skipped_reason}"
        _add_log(db, job_id, skip_msg, "warning")
        if result.skipped_reason in _SOFT_SKIP_REASONS:
            if pipeline is not None:
                record_pipeline_event(
                    db,
                    pipeline.id,
                    event_type="hash_done",
                    message=skip_msg,
                    job_id=job_id,
                    details={
                        "actor": "job",
                        "skipped": True,
                        "reason": result.skipped_reason,
                    },
                )
            return
        # Retriable: нет .torrent / пустой hash и т.п. → failed, можно поставить снова.
        fail_msg = f"hash_torrent: {result.skipped_reason}"
        if pipeline is not None:
            record_pipeline_event(
                db,
                pipeline.id,
                event_type="hash_fail",
                message=fail_msg,
                job_id=job_id,
                from_status=pipeline.status,
                details={
                    "actor": "job",
                    "reason": result.skipped_reason,
                },
            )
        raise RuntimeError(fail_msg)
    kinds: dict[str, int] = {}
    for change in result.changes:
        kinds[change.kind] = kinds.get(change.kind, 0) + 1
    kinds_text = ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())) or "нет"
    ui_counts = _ui_status_counts(db, info_hash)
    done_msg = (
        f"hash_torrent: готово files={result.files_upserted}, "
        f"hashed={result.hashed}, gated={result.gated}, errors={result.errors}, "
        f"changes: {kinds_text}; ui: new={ui_counts['new']} "
        f"changed={ui_counts['changed']} ok={ui_counts['ok']}"
    )
    _add_log(db, job_id, done_msg)
    if pipeline is not None:
        transitions = [
            {
                "relative_path": t.relative_path,
                "from": t.from_status,
                "to": t.to_status,
                "phase": t.phase,
                "reason": t.reason,
                "content_hash": t.content_hash_short,
            }
            for t in (getattr(result, "hash_ui_transitions", None) or [])[:40]
        ]
        record_pipeline_event(
            db,
            pipeline.id,
            event_type="hash_done",
            message=done_msg,
            job_id=job_id,
            details={
                "actor": "job",
                "files_upserted": result.files_upserted,
                "hashed": result.hashed,
                "gated": result.gated,
                "errors": result.errors,
                "change_kinds": kinds,
                "ui_status": ui_counts,
                "has_prior": bool(getattr(result, "has_prior_version", False)),
                "prior_info_hash": getattr(result, "prior_info_hash", None),
                "prior_archive_id": getattr(result, "prior_archive_id", None),
                "ui_transitions": transitions,
                "ui_transitions_total": len(getattr(result, "hash_ui_transitions", None) or []),
            },
        )
