"""Общая политика источника для qB и отображения."""

from sqlalchemy import select

from app.db.models import Release, TorrentArchive
from app.services.torrent_policy import reconcile_source_tags, reconcile_trackers, replace_trackers, source_tags


def tags_for_release(db, release_id: int) -> list[str]:
    row = db.get(Release, release_id)
    if row is None:
        return []
    return source_tags(getattr(row, "source", None), getattr(row, "is_hidden", False) is True,
                       getattr(row, "is_blocked_by_copyrights", False) is True)


def release_is_offsite(db, release_id: int) -> bool:
    row = db.get(Release, release_id)
    return row is not None and getattr(row, "source", None) == "backoffice"


def release_url(db, release_id: int, alias, *, site_url=None):
    from app.core.config import settings
    from app.services.runtime_settings import get_setting_value
    from app.services.torrent_qb_meta import build_release_admin_url, build_release_torrents_url
    if release_is_offsite(db, release_id):
        return build_release_admin_url(release_id, get_setting_value(db, "anilibria_admin_url_template", settings.anilibria_admin_url_template))
    return build_release_torrents_url(alias, site_url=site_url)


def apply_release_policy(db, release_id: int, clients, passkey: str, *, log=None,
                         torrent_targets=None, on_error=None):
    """Повторяем на каждом sync, включая done/историю; сбой qB не теряется в checkpoint."""
    tags = tags_for_release(db, release_id)
    if not tags:
        return
    offsite = release_is_offsite(db, release_id)
    from app.services.torrent_archive import TorrentArchiveService
    service = TorrentArchiveService(db)
    archives = db.scalars(select(TorrentArchive).where(TorrentArchive.release_id == release_id)).all()
    for archive in archives:
        if torrent_targets is not None and (archive.torrent_id, archive.info_hash.lower()) not in torrent_targets:
            continue
        try:
            if offsite:
                archive.backoffice_trackers = True
            if getattr(archive, "backoffice_trackers", False) is True:
                path = service.resolve_file_path(archive)
                if path.exists():
                    raw = path.read_bytes()
                    prepared = replace_trackers(raw, passkey)
                    if prepared != raw:
                        path.write_bytes(prepared)
        except Exception as exc:
            if on_error is None:
                raise
            on_error(archive, None, exc)
            continue
        for role, client in clients:
            try:
                if not client.torrents_info(torrent_hashes=archive.info_hash):
                    continue
                reconcile_source_tags(client, archive.info_hash, tags)
                if archive.backoffice_trackers:
                    reconcile_trackers(client, archive.info_hash, passkey)
            except Exception as exc:
                if on_error:
                    on_error(archive, role, exc)
                if log:
                    # Не выводим URL / pk из exception qB.
                    log(f"Политика релиза {release_id} на {role}: {type(exc).__name__}; повтор при sync", level="warning")
    db.commit()
