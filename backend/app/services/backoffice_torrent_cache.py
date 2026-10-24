"""Bounded Nova detail cache, never a release/version or processing checkpoint.

The live index has an upload timestamp, title, description and rounded size,
but no infohash. A changed index always fetches detail; unchanged entries are
also revalidated every six hours to pick up changes invisible in that index.
"""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import re

from sqlalchemy import select

from app.db.models import BackofficeTorrentCache
from app.providers.anilibria.backoffice import resource_id, torrent_index_fingerprint, torrent_index_snapshot
from app.services.torrent_archive import TorrentArchiveService
from app.utils.datetime_fmt import utcnow


_VERSION = 1
_HASH = re.compile(r"[0-9a-f]{40}")


def _digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _valid_payload(payload, release_id, torrent_id):
    return (isinstance(payload, dict)
            and type(payload.get("id")) is int and payload["id"] == torrent_id
            and type(payload.get("release_id")) is int and payload["release_id"] == release_id
            and isinstance(payload.get("hash"), str) and _HASH.fullmatch(payload["hash"]) is not None)


class BackofficeTorrentCacheStore:
    """Stage cache writes in the caller's transaction, without committing it."""

    def __init__(self, db, provider_url, *, max_age=timedelta(hours=6), now=None):
        self.db = db
        self.provider_key = hashlib.sha256(provider_url.rstrip("/").encode()).hexdigest()
        self.max_age = max_age
        self.now = now or utcnow
        self._rows = {}

    def _release_rows(self, release_id):
        if release_id not in self._rows:
            rows = self.db.scalars(select(BackofficeTorrentCache).where(
                BackofficeTorrentCache.provider_key == self.provider_key,
                BackofficeTorrentCache.release_id == release_id,
            )).all()
            # SessionLocal uses autoflush=False; reuse pending inserts too.
            rows = [*rows, *(row for row in self.db.new if isinstance(row, BackofficeTorrentCache)
                    and row.provider_key == self.provider_key and row.release_id == release_id)]
            self._rows[release_id] = {row.torrent_id: row for row in rows}
        return self._rows[release_id]

    def lookup(self, resource, release_id):
        fingerprint = torrent_index_fingerprint(resource, release_id)
        if fingerprint is None:
            return None
        torrent_id = resource_id(resource)
        row = self._release_rows(release_id).get(torrent_id)
        if row is None or row.version != _VERSION or row.index_fingerprint != fingerprint:
            return None
        fetched_at = TorrentArchiveService._parse_api_datetime(row.fetched_at)
        if fetched_at is None:
            return None
        age = self.now() - fetched_at
        if age < timedelta(0) or age >= self.max_age:
            return None
        payload = row.payload_json
        if not _valid_payload(payload, release_id, torrent_id):
            return None
        try:
            if _digest(payload) != row.payload_digest:
                return None
        except (TypeError, ValueError):
            return None
        return deepcopy(payload)

    def remember(self, resource, release_id, payload):
        torrent_id = resource_id(resource)
        previous = self._release_rows(release_id).get(torrent_id)
        if previous is not None:
            # A fresh detail supersedes our old observation even when its
            # index markers cannot authorize a reusable replacement. Retain
            # the row for an in-transaction update, but make it uncacheable.
            previous.version = 0
        snapshot = torrent_index_snapshot(resource, release_id)
        fingerprint = torrent_index_fingerprint(resource, release_id)
        if snapshot is None or fingerprint is None:
            return
        if not _valid_payload(payload, release_id, torrent_id):
            return
        # Do not attach a stale index marker to a different freshly read version.
        parse_date = TorrentArchiveService._parse_api_datetime
        if parse_date(snapshot["updated_at"]) != parse_date(payload.get("updated_at")):
            return
        if snapshot["description"] != payload.get("description"):
            return
        if snapshot.get("hash") is not None and snapshot["hash"] != payload["hash"]:
            return
        try:
            payload_digest = _digest(payload)
        except (TypeError, ValueError):
            return
        rows = self._release_rows(release_id)
        row = rows.get(torrent_id)
        if row is None:
            row = BackofficeTorrentCache(provider_key=self.provider_key,
                release_id=release_id, torrent_id=torrent_id)
            self.db.add(row)
            rows[torrent_id] = row
        row.version = _VERSION
        row.index_fingerprint = fingerprint
        row.payload_json = deepcopy(payload)
        row.payload_digest = payload_digest
        row.fetched_at = self.now()
