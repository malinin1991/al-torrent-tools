"""Список донаблюдения принадлежит ongoing, а не full_sync или Telegram tracking."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import OngoingWatch
from app.services.release_checkpoint import ReleaseRef

WATCH_PERIOD = timedelta(days=14)


def update_watch_list(
    db: Session, schedule: list[ReleaseRef] | None, *, now: datetime
) -> tuple[list[ReleaseRef], int]:
    """None = расписание недоступно; не меняем присутствие/момент исчезновения."""
    rows = {row.release_id: row for row in db.scalars(select(OngoingWatch)).all()}
    if schedule is not None:
        present = {ref.release_id for ref in schedule}
        for ref in schedule:
            row = rows.get(ref.release_id)
            if row is None:
                row = OngoingWatch(release_id=ref.release_id, last_seen_at=now)
                rows[ref.release_id] = row
                db.add(row)
            if ref.alias:
                row.release_alias = ref.alias
            row.last_seen_at = now
            row.missing_since = None
            row.expires_at = None
        for rid, row in rows.items():
            if rid not in present and row.missing_since is None:
                row.missing_since = now
                row.expires_at = now + WATCH_PERIOD

    expired = 0
    retained = []
    for row in rows.values():
        if row.expires_at is not None and row.expires_at <= now:
            db.delete(row)
            expired += 1
        else:
            retained.append(ReleaseRef(row.release_id, row.release_alias))
    db.commit()
    return retained, expired


def observe_torrents(
    db: Session, release_id: int, torrent_ids: list[int], *, now: datetime
) -> bool:
    """Новые ID продлевают срок один раз, даже если последующая загрузка/qB упадёт."""
    row = db.get(OngoingWatch, release_id)
    if row is None:
        return False
    latest = max(torrent_ids, default=0)
    new_torrent = latest > (row.max_torrent_id or 0)
    if new_torrent:
        row.max_torrent_id = latest
        if row.missing_since is not None:
            row.expires_at = now + WATCH_PERIOD
    row.last_checked_at = now
    db.commit()
    return new_torrent and row.missing_since is not None


def remove_from_watch(db: Session, release_id: int) -> bool:
    row = db.get(OngoingWatch, release_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True
