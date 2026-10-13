"""Explicit bounded retry of persisted sync failures; never a catalogue sweep."""
from __future__ import annotations

from collections import defaultdict

from app.services.job_runner import JobStopRequested, is_stop_requested
from app.services.runtime_settings import build_anilibria_client
from app.services.sync_outcomes import RETRY_LIMIT, current_outcome, sync_job
from app.services.torrent_processor import TorrentProcessor
from app.providers.anilibria.client import AniLibriaNotFoundError


@sync_job
async def run_retry_sync_failures(db, job_id: int, params: dict) -> None:
    targets = params.get("targets")
    if type(params.get("retry_of")) is not int or not isinstance(targets, list) or not targets or len(targets) > RETRY_LIMIT:
        raise ValueError("Некорректный сохранённый план повтора")
    groups = defaultdict(list)
    for target in targets:
        if not isinstance(target, dict) or type(target.get("release_id")) is not int:
            raise ValueError("Некорректная цель повтора")
        groups[(target["release_id"], target.get("operation", "process"), target.get("qb_role"), bool(target.get("force_qb_load")), bool(target.get("notification_baseline")), target.get("source", "unknown"))].append(target)
    outcome = current_outcome(db, job_id)
    for (release_id, operation, role, force, baseline, source), selected in groups.items():
        if is_stop_requested(db, job_id):
            raise JobStopRequested()
        # SourceClient caches only within this group. Every acquired release lock
        # sees a fresh full source list rather than another group's cached card.
        processor = TorrentProcessor(db, job_id, build_anilibria_client(db))
        try:
            part = await processor.process_release(release_id=release_id,
                refresh_qb_meta=True, force_qb_load=force,
                retry_targets=selected, retry_qb_role=role, retry_operation=operation)
            outcome.release(release_id, part)
        except JobStopRequested:
            raise
        except AniLibriaNotFoundError:
            for target in selected:
                outcome.stale(release_id, target.get("torrent_id"), "Объект больше не доступен в выбранном источнике")
        except Exception as exc:
            db.rollback()
            for target in selected:
                outcome.failure(release_id=release_id, torrent_id=target.get("torrent_id"),
                    info_hash=target.get("info_hash"), source=target.get("source", "unknown"),
                    operation=operation, qb_role=role, message=exc,
                    retry_params={"force_qb_load": force, "notification_baseline": target.get("notification_baseline", False)})
