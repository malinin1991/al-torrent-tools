"""Geo-only Nova sources still reconcile policy when no source tags are desired."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Job, Release, SeenTorrent, Setting, TorrentArchive, TorrentFile
from app.services.qbittorrent import torrent_info_hash
from app.services.release_source import apply_release_policy
from app.services.torrent_policy import raw_torrent_fields, tracker_urls
from app.services.torrent_processor import TorrentProcessor


RAW = b"d8:announce10:http://old4:infod4:name4:test6:lengthi1eee"
HASH = torrent_info_hash(RAW)


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(*args, **kwargs):
    return "JSON"


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


class PolicyQb:
    def __init__(self):
        # geo is an ordinary user tag, not a newly reserved source tag.
        self.tags = {"site", "hidden", "blocked", "manual", "geo", "Драма"}
        self.trackers = ["http://old"]

    def torrents_info(self, **kwargs):
        return [SimpleNamespace(tags=", ".join(sorted(self.tags)))]

    def torrents_add_tags(self, *, tags, **kwargs):
        self.tags.update(tags)

    def torrents_remove_tags(self, *, tags, **kwargs):
        self.tags.difference_update(tags)

    def torrents_trackers(self, **kwargs):
        return [{"url": "** [DHT] **"}] + [{"url": url} for url in self.trackers]

    def torrents_add_trackers(self, *, urls, **kwargs):
        self.trackers.extend(urls.split("\n"))

    def torrents_remove_trackers(self, *, urls, **kwargs):
        self.trackers = [url for url in self.trackers if url not in urls.split("|")]


def test_all_seen_geo_only_source_repairs_policy_and_return_to_site_keeps_archive(db, tmp_path, monkeypatch):
    path = tmp_path / "current.torrent"
    path.write_bytes(RAW)
    created_at = datetime(2026, 9, 1, 0, 0)
    sla = datetime(2026, 9, 2, 0, 0)
    archive = TorrentArchive(
        release_id=1, torrent_id=10, info_hash=HASH, file_path=str(path),
        quality_json={"genres": ["Драма"]}, created_at=created_at, api_created_at=sla,
        notification_baseline=True, ignore_hevc=True, backoffice_trackers=False,
    )
    file = TorrentFile(release_id=1, torrent_id=10, info_hash=HASH, relative_path="test.mkv", size=1, ui_status="changed")
    db.add_all([
        Job(id=1, type="full_sync", status="running", params_json={}), archive, file,
        Release(release_id=1, source="site", is_hidden=False, is_blocked_by_copyrights=False),
        SeenTorrent(torrent_id=10, info_hash=HASH),
        Setting(key="anilibria_passkey", value="current-key"),
    ])
    db.commit()
    archive_id, file_id = archive.id, file.id
    torrent = {"id": 10, "hash": HASH, "updated_at": "2026-10-25T01:00:00Z"}
    payload = {
        "id": 1, "_source": "backoffice", "alias": "geo-show", "name": {"main": "Geo show"},
        "is_hidden": False, "is_blocked_by_copyrights": False, "is_blocked_by_geo": True,
        "genres": [{"name": "Драма"}],
    }
    client = SimpleNamespace(
        base_url="https://site.test/api/v1", passkey="current-key",
        get_release=AsyncMock(return_value=payload),
        get_torrents_for_release=AsyncMock(return_value=[torrent]),
        download_torrent_file=AsyncMock(side_effect=AssertionError("A known version must not be downloaded")),
    )
    processor = TorrentProcessor(db, 1, client)
    qbs = [PolicyQb(), PolicyQb()]
    monkeypatch.setattr(processor, "_qb_clients_for_meta", lambda: list(zip(("master", "slave"), qbs)))
    for method in ("_refresh_qb_comments", "_refresh_qb_tags", "_refresh_qb_renames"):
        monkeypatch.setattr(processor, method, lambda **kwargs: 0)

    asyncio.run(processor.process_release(1, refresh_qb_meta=True))

    assert db.get(Release, 1).source == "backoffice"
    assert db.get(Release, 1).is_blocked_by_geo
    assert not db.get(Release, 1).is_blocked_by_copyrights
    for qb in qbs:
        assert qb.tags == {"manual", "geo", "Драма"}
        assert qb.trackers == tracker_urls("current-key")
    db.refresh(archive)
    assert archive.backoffice_trackers
    assert (archive.id, archive.info_hash, archive.created_at, archive.api_created_at) == (archive_id, HASH, created_at, sla)
    assert archive.notification_baseline and archive.ignore_hevc and not archive.superseded
    assert db.get(TorrentFile, file_id).ui_status == "changed"
    assert raw_torrent_fields(path.read_bytes())[b"info"] == raw_torrent_fields(RAW)[b"info"]

    # A publicly available geo-marked card returns to site; the hash's tracker
    # policy remains sticky and repairs later qB drift on both clients.
    client.get_release.return_value = {**payload, "_source": "site"}
    for qb in qbs:
        qb.tags.add("blocked")
        qb.trackers = ["http://stale"]
    asyncio.run(processor.process_release(1, refresh_qb_meta=True))

    assert db.get(Release, 1).source == "site"
    assert db.get(Release, 1).is_blocked_by_geo
    for qb in qbs:
        assert qb.tags == {"site", "manual", "geo", "Драма"}
        assert qb.trackers == tracker_urls("current-key")
    db.refresh(archive)
    assert archive.backoffice_trackers and archive.notification_baseline and archive.ignore_hevc
    assert (archive.id, archive.info_hash, archive.created_at, archive.api_created_at) == (archive_id, HASH, created_at, sla)
    assert len(db.scalars(select(TorrentArchive)).all()) == 1
    assert db.get(TorrentFile, file_id).ui_status == "changed"
    assert torrent_info_hash(path.read_bytes()) == HASH
    client.download_torrent_file.assert_not_awaited()


def test_unconfirmed_source_does_not_clear_reserved_tags_or_change_trackers(db, tmp_path):
    path = tmp_path / "unknown.torrent"
    path.write_bytes(RAW)
    archive = TorrentArchive(release_id=1, torrent_id=10, info_hash=HASH, file_path=str(path), quality_json={})
    db.add_all([Release(release_id=1), archive])
    db.commit()
    qb = PolicyQb()
    before = set(qb.tags)

    apply_release_policy(db, 1, [("master", qb)], "current-key")

    assert qb.tags == before and qb.trackers == ["http://old"]
    assert path.read_bytes() == RAW and not archive.backoffice_trackers
