import asyncio
from typing import Any

from app.core.config import settings
from app.db.models import Job
from app.db.session import SessionLocal
from app.services.torrent_cleanup import TorrentCleanupService


def _resolve_target_role(params: dict[str, Any], *, job_type: str | None = None) -> str:
    role = str(params.get("target_role") or "").strip().lower()
    if role in {"master", "slave"}:
        return role
    if job_type == "cleanup_master":
        return "master"
    if job_type == "cleanup_slave":
        return "slave"
    if job_type == "cleanup":
        return "master"
    raise ValueError(f"cleanup: нужен target_role=master|slave, получено {role!r}")


def _run_cleanup_sync(
    job_id: int,
    dry_run: bool,
    *,
    target_role: str,
    warn_forced_dry_run: bool,
) -> dict[str, int]:
    """Синхронный qBittorrent I/O — не вызывать из event loop напрямую."""
    with SessionLocal() as db:
        service = TorrentCleanupService(db=db, job_id=job_id)
        if warn_forced_dry_run:
            service.add_log(
                "Cleanup: запрошено удаление, но CLEANUP_ALLOW_DELETE=false — режим только информирования"
            )
        stats = service.run(dry_run=dry_run, target_role=target_role)
        mode = "inform" if dry_run else "delete"
        service.add_log(
            f"Cleanup ({target_role}) завершен: mode={mode}, dry_run={dry_run}, "
            f"checked={stats['checked']}, matched={stats['matched']}, deleted={stats['deleted']}"
        )
        return stats


async def run_cleanup(db: Any, job_id: int, params: dict[str, Any]) -> None:
    # По умолчанию dry_run=True (scheduler). Удаление: dry_run=false.
    # CLEANUP_ALLOW_DELETE=false принудительно оставляет только отчёт.
    # db из JobRunner здесь не используем для I/O: работа в thread со своей сессией.
    job = db.get(Job, job_id) if db is not None else None
    job_type = job.type if job is not None else None
    target_role = _resolve_target_role(params, job_type=job_type)
    requested_dry_run = bool(params.get("dry_run", True))
    if settings.cleanup_allow_delete:
        dry_run = requested_dry_run
    else:
        dry_run = True

    await asyncio.to_thread(
        _run_cleanup_sync,
        job_id,
        dry_run,
        target_role=target_role,
        warn_forced_dry_run=not settings.cleanup_allow_delete and not requested_dry_run,
    )
