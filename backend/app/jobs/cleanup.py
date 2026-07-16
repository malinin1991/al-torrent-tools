from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.torrent_cleanup import TorrentCleanupService


async def run_cleanup(db: Session, job_id: int, params: dict[str, Any]) -> None:
    # По умолчанию dry_run=True (scheduler). Удаление: dry_run=false.
    # CLEANUP_ALLOW_DELETE=false принудительно оставляет только отчёт.
    requested_dry_run = bool(params.get("dry_run", True))
    if settings.cleanup_allow_delete:
        dry_run = requested_dry_run
    else:
        dry_run = True

    service = TorrentCleanupService(db=db, job_id=job_id)
    if not settings.cleanup_allow_delete and not requested_dry_run:
        service.add_log(
            "Cleanup: запрошено удаление, но CLEANUP_ALLOW_DELETE=false — режим только информирования"
        )
    stats = service.run(dry_run=dry_run)
    mode = "inform" if dry_run else "delete"
    service.add_log(
        f"Cleanup завершен: mode={mode}, dry_run={dry_run}, "
        f"checked={stats['checked']}, matched={stats['matched']}, deleted={stats['deleted']}"
    )
