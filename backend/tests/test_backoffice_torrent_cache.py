"""Durable Nova detail caching keeps fresh membership and ordinary sync effects."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import BackofficeTorrentCache, Job, Release, SeenTorrent, Setting, TorrentArchive
from app.providers.anilibria.backoffice import BackofficeClient, BackofficeError
from app.providers.anilibria.client import AniLibriaNotFoundError
from app.providers.anilibria.sources import ReleaseSourceClient
from app.services.backoffice_torrent_cache import BackofficeTorrentCacheStore
from app.services.qbittorrent import torrent_info_hash
from app.services.torrent_policy import tracker_urls
from app.utils.datetime_fmt import utcnow


RID = 10317
PROVIDER = "https://admin.test"
UPLOADED = "2026-10-24T12:22:13+00:00"
BASE_TIME = datetime(2026, 10, 25, 0, 0)
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


def index_resource(tid=40514, *, rid=RID, uploaded=UPLOADED, description="1-3"):
    """The real index exposes upload date/label/size/action text, but no hash."""
    title = f"WEB-DL • 1080p AVC • {description}"
    return {"id": {"value": tid}, "title": title, "fields": [
        {"attribute": "", "component": "action-button", "action": {
            "uriKey": "updateTorrent", "fields": [
                {"attribute": "description", "component": "text-field", "value": description},
            ],
        }},
        {"attribute": "название", "component": "stack-field", "lines": [
            {"attribute": "ComputedField", "component": "text-field", "value": title},
            {"attribute": "release", "component": "belongs-to-field", "belongsToId": rid},
        ]},
        {"attribute": "seeders", "component": "text-field", "value": 18},
        {"attribute": "leechers", "component": "text-field", "value": 11},
        {"attribute": "completed_times", "component": "text-field", "value": 443},
        {"attribute": "ComputedField", "component": "text-field", "name": "Размер торрента",
         "indexName": "Размер торрента", "value": "4.116 GB"},
        {"attribute": "updated_at", "component": "date-time-field", "value": uploaded},
    ]}


def top_field(resource, attribute):
    return next(item for item in resource["fields"] if item["attribute"] == attribute)


def detail_resource(tid=40514, *, rid=RID, info_hash=HASH, uploaded=UPLOADED, description="1-3"):
    return {"id": {"value": tid}, "fields": [
        {"attribute": "release", "component": "belongs-to-field", "belongsToId": rid},
        {"attribute": "hash", "component": "text-field", "value": info_hash},
        {"attribute": "description", "component": "text-field", "value": description},
        {"attribute": "created_at", "component": "date-time-field", "value": "2026-10-01T00:00:00Z"},
        {"attribute": "updated_at", "component": "date-time-field", "value": uploaded},
        {"attribute": "type", "component": "select-field", "value": "WEB-DL"},
        {"attribute": "quality", "component": "select-field", "value": "1080p"},
        {"attribute": "codec", "component": "select-field", "value": "x264/AVC"},
        {"attribute": "seeders", "component": "text-field", "value": 18},
        {"attribute": "leechers", "component": "text-field", "value": 11},
    ]}


class NovaServer:
    def __init__(self, tids=(40514,)):
        self.resources = [index_resource(tid) for tid in tids]
        self.details = {tid: detail_resource(tid) for tid in tids}
        self.detail_calls = []
        self.index_calls = []
        self.fail_details = set()
        self.page_size = 100
        self.broken_last_page = False
        self.duplicate_last_page = False

    def handle(self, request):
        assert request.method == "GET"
        if request.url.path == "/nova-api/anime-torrents":
            assert request.url.params["viaResource"] == "anime-releases"
            assert request.url.params["viaResourceId"] == str(RID)
            assert request.url.params["viaRelationship"] == "torrents"
            page = int(request.url.params["page"])
            self.index_calls.append(page)
            start = (page - 1) * self.page_size
            resources = deepcopy(self.resources[start:start + self.page_size])
            if self.duplicate_last_page and page > 1:
                resources = deepcopy(self.resources[:1])
            payload = {"resources": resources, "total": len(self.resources),
                       "next_page_url": "/nova-api/anime-torrents?page=2"
                       if start + self.page_size < len(self.resources) else None}
            if self.broken_last_page and page > 1:
                payload.pop("next_page_url")
            return httpx.Response(200, json=payload)
        if request.url.path == f"/nova-api/anime-releases/{RID}":
            return httpx.Response(200, json={"resource": {"id": {"value": RID}, "fields": [
                {"attribute": "is_hidden", "component": "boolean-field", "value": True},
                {"attribute": "is_blocked_by_copyrights", "component": "boolean-field", "value": False},
                {"attribute": "name", "component": "text-field", "value": "Cached release"},
                {"attribute": "alias", "component": "text-field", "value": "cached-release"},
            ]}})
        prefix = "/nova-api/anime-torrents/"
        assert request.url.path.startswith(prefix), request.url.path
        tid = int(request.url.path.removeprefix(prefix))
        self.detail_calls.append(tid)
        if tid in self.fail_details:
            return httpx.Response(503)
        return httpx.Response(200, json={"resource": deepcopy(self.details[tid])})

    def client(self, provider=PROVIDER):
        client = BackofficeClient(provider, "test-login", "test-password", retries=1,
                                  transport=httpx.MockTransport(self.handle))
        client._authenticated = True
        return client


def scan(db, server, clock, *, provider=PROVIDER):
    cache = BackofficeTorrentCacheStore(db, provider, now=lambda: clock.now)
    result = asyncio.run(server.client(provider).get_torrents_for_release(RID, cache=cache))
    # Real processor commits its work; the cache itself must not own this boundary.
    db.commit()
    return result


def test_new_clients_and_sessions_reuse_details_but_read_fresh_membership_and_peers(db):
    server = NovaServer((40514, 40515))
    clock = SimpleNamespace(now=BASE_TIME)
    first = scan(db, server, clock)
    assert server.detail_calls == [40514, 40515]
    assert len(db.scalars(select(BackofficeTorrentCache)).all()) == 2
    top_field(server.resources[0], "seeders")["value"] = 99
    top_field(server.resources[0], "leechers")["value"] = 0
    top_field(server.resources[0], "completed_times")["value"] = 1000
    server.detail_calls.clear()
    with Session(db.get_bind()) as fresh_db:
        second = scan(fresh_db, server, clock)
    assert server.detail_calls == []
    assert server.index_calls == [1, 1]
    assert [item["hash"] for item in second] == [item["hash"] for item in first]
    assert second[0]["seeders"] == 99 and second[0]["leechers"] == 0


def test_new_id_is_fetched_while_existing_id_uses_durable_cache(db):
    server = NovaServer()
    clock = SimpleNamespace(now=BASE_TIME)
    scan(db, server, clock)
    server.detail_calls.clear()
    server.resources.append(index_resource(40515))
    server.details[40515] = detail_resource(40515)
    result = scan(db, server, clock)
    assert server.detail_calls == [40515]
    assert [item["id"] for item in result] == [40514, 40515]


@pytest.mark.parametrize("changed", ["upload", "description", "title", "size", "hash"])
def test_visible_index_changes_refetch_detail_even_before_ttl(db, changed):
    server = NovaServer()
    clock = SimpleNamespace(now=BASE_TIME)
    scan(db, server, clock)
    server.detail_calls.clear()
    resource = server.resources[0]
    if changed == "upload":
        newer = "2026-10-24T13:22:13+00:00"
        top_field(resource, "updated_at")["value"] = newer
        top_field(server.details[40514], "updated_at")["value"] = newer
    elif changed == "description":
        resource["fields"][0]["action"]["fields"][0]["value"] = "1-4"
        top_field(server.details[40514], "description")["value"] = "1-4"
    elif changed == "title":
        resource["title"] += " исправлено"
    elif changed == "size":
        top_field(resource, "ComputedField")["value"] = "4.117 GB"
    else:
        resource["fields"].append({"attribute": "hash", "component": "text-field", "value": "b" * 40})
        top_field(server.details[40514], "hash")["value"] = "b" * 40
    result = scan(db, server, clock)
    assert server.detail_calls == [40514]
    if changed == "hash":
        assert result[0]["hash"] == "b" * 40


def test_opaque_same_id_hash_change_is_detected_at_six_hour_revalidation(db):
    server = NovaServer()
    clock = SimpleNamespace(now=BASE_TIME)
    scan(db, server, clock)
    top_field(server.details[40514], "hash")["value"] = "b" * 40
    server.detail_calls.clear()
    clock.now = BASE_TIME + timedelta(hours=6) - timedelta(microseconds=1)
    assert scan(db, server, clock)[0]["hash"] == HASH
    assert server.detail_calls == []
    clock.now = BASE_TIME + timedelta(hours=6)
    assert scan(db, server, clock)[0]["hash"] == "b" * 40
    assert server.detail_calls == [40514]


@pytest.mark.parametrize("marker", ["missing_upload", "null_upload", "invalid_upload", "missing_title", "missing_size"])
def test_missing_or_invalid_index_markers_do_not_authorize_detail_reuse(db, marker):
    server = NovaServer()
    clock = SimpleNamespace(now=BASE_TIME)
    resource = server.resources[0]
    if marker == "missing_upload":
        resource["fields"] = [field for field in resource["fields"] if field["attribute"] != "updated_at"]
    elif marker in {"null_upload", "invalid_upload"}:
        top_field(resource, "updated_at")["value"] = None if marker == "null_upload" else "not-a-date"
    elif marker == "missing_title":
        resource.pop("title")
    else:
        resource["fields"] = [field for field in resource["fields"] if field.get("name") != "Размер торрента"]
    scan(db, server, clock)
    server.detail_calls.clear()
    scan(db, server, clock)
    assert server.detail_calls == [40514]


@pytest.mark.parametrize("broken", ["payload", "digest", "version", "fingerprint", "future_clock"])
def test_corrupt_or_untrusted_cache_entries_are_refetched(db, broken):
    server = NovaServer()
    clock = SimpleNamespace(now=BASE_TIME)
    scan(db, server, clock)
    row = db.scalar(select(BackofficeTorrentCache))
    if broken == "payload":
        row.payload_json = {"id": 40514, "release_id": RID, "hash": "invalid"}
    elif broken == "digest":
        row.payload_digest = "0" * 64
    elif broken == "version":
        row.version = 999
    elif broken == "fingerprint":
        row.index_fingerprint = "0" * 64
    else:
        row.fetched_at = BASE_TIME + timedelta(hours=1)
    db.commit()
    server.detail_calls.clear()
    assert scan(db, server, clock)[0]["hash"] == HASH
    assert server.detail_calls == [40514]


def test_provider_change_does_not_reuse_another_servers_cache(db):
    server = NovaServer()
    clock = SimpleNamespace(now=BASE_TIME)
    scan(db, server, clock)
    server.detail_calls.clear()
    scan(db, server, clock, provider="https://another-admin.test")
    assert server.detail_calls == [40514]


def test_cache_population_obeys_the_callers_transaction(db):
    server = NovaServer()
    cache = BackofficeTorrentCacheStore(db, PROVIDER, now=lambda: BASE_TIME)
    asyncio.run(server.client().get_torrents_for_release(RID, cache=cache))
    assert db.scalar(select(BackofficeTorrentCache)) is not None
    db.rollback()
    assert db.scalar(select(BackofficeTorrentCache)) is None


@pytest.mark.parametrize("reset", ["operational", "full"])
def test_reset_discards_durable_cache_and_next_client_reads_detail(db, tmp_path, reset):
    from app.services.db_maintenance import reset_full, reset_operational_state

    server = NovaServer()
    clock = SimpleNamespace(now=BASE_TIME)
    assert scan(db, server, clock)[0]["hash"] == HASH
    assert db.scalar(select(BackofficeTorrentCache)) is not None
    storage = tmp_path / "torrents"
    storage.mkdir()
    torrent_file = storage / f"{HASH}.torrent"
    torrent_file.write_bytes(RAW)
    sla = datetime(2026, 10, 2, 0, 0)
    archive = TorrentArchive(
        release_id=RID, torrent_id=40514, info_hash=HASH,
        file_path=f"data/torrents/{HASH}.torrent", quality_json={},
        api_created_at=sla, notification_baseline=True, backoffice_trackers=True,
    )
    db.add(archive)
    db.commit()
    archive_id = archive.id

    if reset == "full":
        stats = reset_full(db, storage_root=storage)
        assert stats["archive_removed"] == stats["torrent_files_removed"] == 1
        assert db.get(TorrentArchive, archive_id) is None
        assert not torrent_file.exists()
    else:
        stats = reset_operational_state(db)
        assert stats["archive_kept"] == 1
        kept = db.get(TorrentArchive, archive_id)
        assert (kept.info_hash, kept.api_created_at, kept.notification_baseline) == (HASH, sla, True)
        assert kept.backoffice_trackers and torrent_file.read_bytes() == RAW
    assert db.scalar(select(BackofficeTorrentCache)) is None

    # Without this reset's invalidation, the unchanged index would hide this
    # otherwise invisible replacement until the ordinary six-hour TTL expires.
    top_field(server.details[40514], "hash")["value"] = "b" * 40
    server.detail_calls.clear()
    with Session(db.get_bind()) as fresh_db:
        result = scan(fresh_db, server, clock)
        if reset == "operational":
            kept = fresh_db.get(TorrentArchive, archive_id)
            assert (kept.info_hash, kept.api_created_at, kept.notification_baseline) == (HASH, sla, True)
            assert torrent_file.read_bytes() == RAW
        else:
            assert fresh_db.scalar(select(TorrentArchive)) is None
            assert not torrent_file.exists()
    assert server.detail_calls == [40514]
    assert server.index_calls == [1, 1]
    assert result[0]["hash"] == "b" * 40


def test_failed_changed_detail_never_falls_back_to_stale_cached_payload(db):
    server = NovaServer()
    clock = SimpleNamespace(now=BASE_TIME)
    scan(db, server, clock)
    top_field(server.resources[0], "updated_at")["value"] = "2026-10-24T13:22:13Z"
    server.fail_details.add(40514)
    cache = BackofficeTorrentCacheStore(db, PROVIDER, now=lambda: clock.now)
    with pytest.raises(BackofficeError, match="503"):
        asyncio.run(server.client().get_torrents_for_release(RID, cache=cache))
    db.rollback()
    assert db.scalar(select(BackofficeTorrentCache)).fetched_at == BASE_TIME


def test_partial_detail_failure_does_not_publish_a_partial_cache_batch(db):
    server = NovaServer((40514, 40515))
    server.fail_details.add(40515)
    cache = BackofficeTorrentCacheStore(db, PROVIDER, now=lambda: BASE_TIME)
    with pytest.raises(BackofficeError, match="503"):
        asyncio.run(server.client().get_torrents_for_release(RID, cache=cache))
    assert server.detail_calls == [40514, 40515]
    assert db.scalar(select(BackofficeTorrentCache)) is None


@pytest.mark.parametrize("mismatch", ["upload", "description", "hash"])
def test_index_detail_race_never_attaches_old_markers_to_a_new_detail(db, mismatch):
    server = NovaServer()
    if mismatch == "upload":
        top_field(server.details[40514], "updated_at")["value"] = "2026-10-24T13:22:13Z"
    elif mismatch == "description":
        top_field(server.details[40514], "description")["value"] = "1-4"
    else:
        server.resources[0]["fields"].append({
            "attribute": "hash", "component": "text-field", "value": "b" * 40,
        })
    clock = SimpleNamespace(now=BASE_TIME)
    scan(db, server, clock)
    assert db.scalar(select(BackofficeTorrentCache)) is None
    server.detail_calls.clear()
    scan(db, server, clock)
    assert server.detail_calls == [40514]


def test_uncacheable_fresh_detail_invalidates_warm_cache_before_index_markers_return(db):
    server = NovaServer()
    clock = SimpleNamespace(now=BASE_TIME)
    assert scan(db, server, clock)[0]["hash"] == HASH
    original_index = deepcopy(server.resources[0])
    top_field(server.resources[0], "updated_at")["value"] = None
    top_field(server.details[40514], "hash")["value"] = "b" * 40
    assert scan(db, server, clock)[0]["hash"] == "b" * 40

    # Restoring a formerly valid marker must not resurrect the superseded card.
    server.resources[0] = original_index
    server.detail_calls.clear()
    result = scan(db, server, clock)
    assert server.detail_calls == [40514]
    assert result[0]["hash"] == "b" * 40


def test_fresh_detail_upload_race_invalidates_warm_cache_before_old_index_returns(db):
    server = NovaServer()
    clock = SimpleNamespace(now=BASE_TIME)
    assert scan(db, server, clock)[0]["hash"] == HASH
    original_index = deepcopy(server.resources[0])
    # A changed observable field requires GET, but the index upload date lags
    # behind the newly read version, so this response cannot populate the cache.
    top_field(server.resources[0], "ComputedField")["value"] = "4.117 GB"
    top_field(server.details[40514], "hash")["value"] = "b" * 40
    top_field(server.details[40514], "updated_at")["value"] = "2026-10-24T13:22:13Z"
    assert scan(db, server, clock)[0]["hash"] == "b" * 40

    server.resources[0] = original_index
    server.detail_calls.clear()
    result = scan(db, server, clock)
    assert server.detail_calls == [40514]
    assert result[0]["hash"] == "b" * 40


@pytest.mark.parametrize("broken", ["incomplete", "duplicate"])
def test_bad_fresh_pagination_is_an_error_even_when_every_detail_is_cached(db, broken):
    server = NovaServer((40514, 40515))
    clock = SimpleNamespace(now=BASE_TIME)
    scan(db, server, clock)
    server.detail_calls.clear()
    server.page_size = 1
    server.broken_last_page = broken == "incomplete"
    server.duplicate_last_page = broken == "duplicate"
    cache = BackofficeTorrentCacheStore(db, PROVIDER, now=lambda: clock.now)
    with pytest.raises(BackofficeError):
        asyncio.run(server.client().get_torrents_for_release(RID, cache=cache))
    assert server.detail_calls == []
    assert server.index_calls[-2:] == [1, 2]


class PolicyQb:
    def __init__(self):
        self.tags = {"site", "manual"}
        self.trackers = ["http://old"]

    def torrents_info(self, **kwargs):
        return [SimpleNamespace(tags=", ".join(sorted(self.tags)))]

    def torrents_add_tags(self, *, tags, **kwargs):
        self.tags.update(tags)

    def torrents_remove_tags(self, *, tags, **kwargs):
        self.tags.difference_update(tags)

    def torrents_trackers(self, **kwargs):
        return [{"url": url} for url in self.trackers]

    def torrents_add_trackers(self, *, urls, **kwargs):
        self.trackers.extend(urls.split("\n"))

    def torrents_remove_trackers(self, *, urls, **kwargs):
        self.trackers = [url for url in self.trackers if url not in urls.split("|")]


def test_all_detail_hits_still_refresh_presence_and_repair_source_policy(db, monkeypatch, tmp_path):
    from app.services.torrent_processor import TorrentProcessor

    server = NovaServer()
    scan(db, server, SimpleNamespace(now=utcnow()))
    path = tmp_path / "current.torrent"
    path.write_bytes(RAW)
    sla = datetime(2026, 10, 2, 0, 0)
    archive = TorrentArchive(release_id=RID, torrent_id=40514, info_hash=HASH, file_path=str(path),
                             quality_json={}, api_present=False, api_created_at=sla)
    db.add_all([
        Job(id=1, type="backoffice_sync", status="running", params_json={}), archive,
        Release(release_id=RID, source="backoffice", is_hidden=True),
        SeenTorrent(torrent_id=40514, info_hash=HASH),
        Setting(key="anilibria_passkey", value="current-key"),
        Setting(key="backoffice_baseline_completed_at", value="already-completed"),
    ])
    db.commit()
    public = SimpleNamespace(base_url="https://site.test/api/v1", passkey="current-key",
                             get_release=AsyncMock(side_effect=AniLibriaNotFoundError()))
    client = ReleaseSourceClient(public, server.client(), db)
    processor = TorrentProcessor(db, 1, client)
    qbs = [PolicyQb(), PolicyQb()]
    monkeypatch.setattr(processor, "_qb_clients_for_meta", lambda: list(zip(("master", "slave"), qbs)))
    for method in ("_refresh_qb_comments", "_refresh_qb_tags", "_refresh_qb_renames"):
        monkeypatch.setattr(processor, method, lambda **kwargs: 0)
    server.detail_calls.clear()
    with client.backoffice_scan({RID}):
        asyncio.run(processor.process_release(RID, refresh_qb_meta=True))
    assert server.detail_calls == []
    assert server.index_calls == [1, 1]
    db.refresh(archive)
    assert archive.api_present and archive.backoffice_trackers
    assert archive.api_created_at == sla and not archive.notification_baseline
    assert len(db.scalars(select(TorrentArchive)).all()) == 1
    for qb in qbs:
        assert qb.tags == {"manual", "hidden"}
        assert qb.trackers == tracker_urls("current-key")
    assert torrent_info_hash(path.read_bytes()) == HASH
