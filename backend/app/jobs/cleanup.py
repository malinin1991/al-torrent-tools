import asyncio
from typing import Any

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.torrent_cleanup import TorrentCleanupService


def _run_cleanup_sync(
    job_id: int,
    dry_run: bool,
    *,
    warn_forced_dry_run: bool,
) -> dict[str, int]:
    """Синхронный qBittorrent I/O — не вызывать из event loop напрямую."""
    with SessionLocal() as db:
        service = TorrentCleanupService(db=db, job_id=job_id)
        if warn_forced_dry_run:
            service.add_log(
                "Cleanup: запрошено удаление, но CLEANUP_ALLOW_DELETE=false — режим только информирования"
            )
        stats = service.run(dry_run=dry_run)
        mode = "inform" if dry_run else "delete"
        service.add_log(
            f"Cleanup завершен: mode={mode}, dry_run={dry_run}, "
            f"checked={stats['checked']}, matched={stats['matched']}, deleted={stats['deleted']}"
        )
        return stats


async def run_cleanup(db: Any, job_id: int, params: dict[str, Any]) -> None:
    # По умолчанию dry_run=True (scheduler). Удаление: dry_run=false.
    # CLEANUP_ALLOW_DELETE=false принудительно оставляет только отчёт.
    # db из JobRunner здесь не используем: работа в thread со своей сессией.
    _ = db
    requested_dry_run = bool(params.get("dry_run", True))
    if settings.cleanup_allow_delete:
        dry_run = requested_dry_run
    else:
        dry_run = True

    await asyncio.to_thread(
        _run_cleanup_sync,
        job_id,
        dry_run,
        warn_forced_dry_run=not settings.cleanup_allow_delete and not requested_dry_run,
    )
