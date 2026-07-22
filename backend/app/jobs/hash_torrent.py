"""Джоб хеширования одного торрента после master_complete."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import JobLog
from app.services.file_tracker import FileTrackerService
from app.services.job_runner import JobStopRequested, is_stop_requested

# Мягкий пропуск: джоб success (нечего делать, retry не нужен).
_SOFT_SKIP_REASONS = frozenset(
    {
        "торрент не api_present (архивный)",
    }
)


def _add_log(db: Session, job_id: int, message: str, level: str = "info") -> None:
    db.add(JobLog(job_id=job_id, level=level, message=message))
    db.commit()


async def run_hash_torrent(db: Session, job_id: int, params: dict[str, Any]) -> None:
    info_hash = str(params.get("info_hash") or "").strip().lower()
    torrent_id = params.get("torrent_id")
    release_id = params.get("release_id")
    if not info_hash or not isinstance(torrent_id, int) or not isinstance(release_id, int):
        raise ValueError("hash_torrent: нужны info_hash, torrent_id, release_id")

    if is_stop_requested(db, job_id):
        _add_log(db, job_id, "hash_torrent: остановка по запросу", "warning")
        raise JobStopRequested()

    _add_log(
        db,
        job_id,
        f"hash_torrent: start hash={info_hash[:12]}… torrent_id={torrent_id} release_id={release_id}",
    )
    tracker = FileTrackerService(db, job_id=job_id)
    result = tracker.track_torrent(
        info_hash=info_hash,
        torrent_id=torrent_id,
        release_id=release_id,
    )
    if result.skipped_reason:
        _add_log(db, job_id, f"hash_torrent: пропуск — {result.skipped_reason}", "warning")
        if result.skipped_reason in _SOFT_SKIP_REASONS:
            return
        # Retriable: нет .torrent / пустой hash и т.п. → failed, можно поставить снова.
        raise RuntimeError(f"hash_torrent: {result.skipped_reason}")
    kinds: dict[str, int] = {}
    for change in result.changes:
        kinds[change.kind] = kinds.get(change.kind, 0) + 1
    kinds_text = ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())) or "нет"
    _add_log(
        db,
        job_id,
        f"hash_torrent: готово files={result.files_upserted}, "
        f"hashed={result.hashed}, gated={result.gated}, errors={result.errors}, "
        f"changes: {kinds_text}",
    )
