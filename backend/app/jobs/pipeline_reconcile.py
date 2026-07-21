from typing import Any

from sqlalchemy.orm import Session

from app.db.models import JobLog, TorrentPipeline
from app.services.anilibria_auth import ensure_passkey_stored
from app.services.pipeline import TorrentPipelineService
from app.services.qbittorrent import ensure_announce_passkey
from app.services.runtime_settings import build_anilibria_client


async def load_torrent_bytes_with_fallback(
    db: Session,
    pipeline_service: TorrentPipelineService,
    pipeline: TorrentPipeline,
) -> bytes | None:
    """Архив → fallback скачивание с AniLibria."""
    torrent_bytes = pipeline_service.load_torrent_bytes_from_archive(pipeline)
    if torrent_bytes is not None:
        return torrent_bytes
    al_client = build_anilibria_client(db)
    passkey = await ensure_passkey_stored(db)
    if passkey:
        al_client.passkey = passkey
    torrent_bytes = await al_client.download_torrent_file(pipeline.torrent_id)
    return ensure_announce_passkey(torrent_bytes, al_client.passkey)


async def run_pipeline_reconcile(db: Session, job_id: int, params: dict[str, Any]) -> None:
    """Сверка pipeline без slave с master (кнопка / scheduler)."""
    _ = params
    service = TorrentPipelineService(db, job_id=job_id)
    cache: dict[int, bytes] = {}

    candidates = service.get_pipelines_awaiting_slave()
    for pipeline in candidates:
        try:
            if service.classify_master_torrent(pipeline) != "complete":
                continue
            torrent_bytes = await load_torrent_bytes_with_fallback(db, service, pipeline)
            if torrent_bytes is not None:
                cache[pipeline.id] = torrent_bytes
        except Exception:
            # Ошибки загрузки обработает reconcile (mark_failed).
            continue

    def load_cached(pipeline: TorrentPipeline) -> bytes | None:
        if pipeline.id in cache:
            return cache[pipeline.id]
        return service.load_torrent_bytes_from_archive(pipeline)

    stats = service.reconcile_with_master(load_torrent_bytes=load_cached)
    db.add(
        JobLog(
            job_id=job_id,
            level="info",
            message=(
                "Reconcile с master: "
                f"checked={stats['checked']}, sent={stats['sent_to_slave']}, "
                f"waiting={stats['waiting']}, cancelled={stats['cancelled']}, "
                f"recovered={stats.get('recovered', 0)}, errors={stats['errors']}"
            ),
        )
    )
    db.commit()
