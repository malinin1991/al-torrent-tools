"""Full sync keeps Nova discovery and source selection inside the real job flow."""

import asyncio
from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Job, JobSyncFailure, Release, Setting, TorrentArchive
from app.jobs import backoffice_sync, full_sync
from app.providers.anilibria.client import AniLibriaNotFoundError
from app.providers.anilibria.sources import ReleaseSourceClient
from app.services.job_runner import JobStopRequested
from app.services.release_lock import release_lock
from app.services.release_meta import upsert_release_meta
from app.services.sync_outcomes import JobPartialFailure
from app.services.torrent_processor import TorrentProcessor


_CONFIRMED_AT = datetime(2026, 10, 20, 12, 30)
_TORRENTS = {
    rid: [{"id": rid * 11, "release_id": rid, "hash": letter * 40}]
    for rid, letter in ((1, "a"), (2, "b"), (3, "c"), (4, "d"), (6, "f"))
}


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(*args, **kwargs):
    return "JSON"


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            Job(id=1, type="full_sync", status="running", params_json={}),
            Setting(key="scrape_pause_sec", value="0"),
            Setting(key="backoffice_baseline_completed_at", value="2026-10-20T12:30:00"),
            Release(release_id=1, source="site", source_checked_at=_CONFIRMED_AT),
            Release(release_id=2, source="backoffice", is_hidden=True, source_checked_at=_CONFIRMED_AT),
            Release(release_id=4, source="site", source_checked_at=_CONFIRMED_AT),
        ])
        for rid in (1, 2, 4):
            torrent = _TORRENTS[rid][0]
            session.add(TorrentArchive(
                release_id=rid, torrent_id=torrent["id"], info_hash=torrent["hash"],
                file_path="unused.torrent", api_present=True,
                backoffice_trackers=rid == 2,
            ))
        session.commit()
        yield session
    engine.dispose()


def _catalog(items, *, total=None, pages=1, page=1):
    return {
        "data": items,
        "meta": {"pagination": {
            "total": len(items) if total is None else total,
            "total_pages": pages, "current_page": page, "count": len(items),
        }},
    }


def _release(rid, *, hidden=False, copyright=False, geo=False):
    return {
        "id": rid, "alias": f"release-{rid}", "name": {"main": f"Fresh release {rid}"},
        "is_hidden": hidden, "is_blocked_by_copyrights": copyright,
        "is_blocked_by_geo": geo, "genres": [],
    }


def _resource(payload):
    return {"id": {"value": payload["id"]}, "fields": [
        {"attribute": key, "component": "boolean-field", "value": payload[key]}
        for key in ("is_hidden", "is_blocked_by_copyrights")
    ] + [{
        "attribute": "is_blocked_by_geo", "component": "multiselect-field",
        "value": ["RU"] if payload["is_blocked_by_geo"] else [],
    }]}


def _http_error():
    request = httpx.Request("GET", "https://public.test/api/v1/anime/catalog/releases")
    return httpx.HTTPStatusError("upstream unavailable", request=request, response=httpx.Response(503, request=request))


@pytest.fixture
def make_run(db, monkeypatch):
    def make(*, catalog_mode="complete", stop_at=None):
        stopped = {"value": stop_at == "before_catalog"}
        processed = []
        site = _release(1, geo=True)
        nova = {
            1: _release(1, geo=True), 2: _release(2, hidden=True),
            3: _release(3, geo=True), 6: _release(6, copyright=True),
        }
        for payload in nova.values():
            payload["_source"] = "backoffice"  # BackofficeClient's normalized response.
        admin_ids = (1, 2, 3, 6) if catalog_mode == "complete" else (2,)

        async def catalog(*, page, limit, **kwargs):
            assert limit <= 50
            if stop_at == "catalog":
                raise JobStopRequested()
            if catalog_mode == "http_first":
                raise _http_error()
            if catalog_mode == "json_first":
                raise json.JSONDecodeError("invalid upstream JSON", "{", 1)
            if catalog_mode in {"http_later", "incomplete_later"}:
                if page == 1:
                    return _catalog([site], total=2, pages=2)
                if catalog_mode == "http_later":
                    raise _http_error()
                return _catalog([], total=2, pages=2, page=2)
            return _catalog([site])

        async def public_release(rid, **kwargs):
            if int(rid) == 1:
                return dict(site)
            if catalog_mode != "complete":
                raise _http_error()
            raise AniLibriaNotFoundError("not present on the public site")

        async def admin_index(resource, *, on_page):
            assert resource == "anime-releases"
            on_page(1)
            return [_resource(nova[rid]) for rid in admin_ids]

        public = SimpleNamespace(
            base_url="https://public.test/api/v1", passkey="test-passkey",
            catalog_releases=AsyncMock(side_effect=catalog),
            get_release=AsyncMock(side_effect=public_release),
            get_torrents_for_release=AsyncMock(side_effect=lambda rid: list(_TORRENTS[rid])),
        )
        admin = SimpleNamespace(
            list_resources=AsyncMock(side_effect=admin_index),
            get_release=AsyncMock(side_effect=lambda rid: dict(nova[rid])),
            get_torrents_for_release=AsyncMock(side_effect=lambda rid: list(_TORRENTS[rid])),
        )
        client = ReleaseSourceClient(public, admin, db)

        class RecordingProcessor:
            empty_release_stats = staticmethod(TorrentProcessor.empty_release_stats)
            merge_release_stats = staticmethod(TorrentProcessor.merge_release_stats)
            format_batch_summary = staticmethod(TorrentProcessor.format_batch_summary)

            def __init__(self, *, db, job_id, client):
                self.db, self.client = db, client

            async def process_release(self, *, release_id, **kwargs):
                in_backoffice_scan = self.client._backoffice_watched is not None
                with release_lock(self.db, release_id):
                    payload = await self.client.get_release(release_id)
                    upsert_release_meta(self.db, release_id, payload)
                    torrents = await self.client.get_torrents_for_release(release_id)
                    for torrent in torrents:
                        archive = self.db.scalar(select(TorrentArchive).where(
                            TorrentArchive.release_id == release_id,
                            TorrentArchive.torrent_id == torrent["id"],
                        ))
                        if archive is None:
                            archive = TorrentArchive(
                                release_id=release_id, torrent_id=torrent["id"],
                                info_hash=torrent["hash"], file_path="unused.torrent",
                            )
                            self.db.add(archive)
                        archive.api_present = True
                        if payload["_source"] == "backoffice":
                            archive.backoffice_trackers = True
                    self.db.commit()
                processed.append({
                    "release_id": release_id, "source": payload["_source"],
                    "backoffice_scan": in_backoffice_scan, "kwargs": kwargs,
                })
                if stop_at == "after_site" and not in_backoffice_scan:
                    stopped["value"] = True
                return {**TorrentProcessor.empty_release_stats(), "updated": len(torrents)}

            def backfill_qb_comments_from_archive(self):
                return {"archives": 0, "updated": 0, "missing": 0, "tags_updated": 0}

        for module in (full_sync, backoffice_sync):
            monkeypatch.setattr(module, "build_anilibria_client", lambda _: client)
            monkeypatch.setattr(module, "is_stop_requested", lambda *args: stopped["value"])
            monkeypatch.setattr(module, "TorrentProcessor", RecordingProcessor)
        sweep = MagicMock(wraps=full_sync.mark_missing_api_present_false)
        monkeypatch.setattr(full_sync, "mark_missing_api_present_false", sweep)
        return SimpleNamespace(client=client, processed=processed, sweep=sweep)

    return make


@pytest.mark.parametrize("force_qb_load", [False, True])
def test_full_sync_imports_nova_geo_hidden_copyright_and_preserves_them_in_sweep(db, make_run, force_qb_load):
    run = make_run()

    asyncio.run(full_sync.run_full_sync(db, 1, {"force_qb_load": force_qb_load}))

    run.client.backoffice.list_resources.assert_awaited_once()
    bo_calls = [call for call in run.processed if call["backoffice_scan"]]
    assert {call["release_id"] for call in bo_calls} >= {2, 3, 6}
    assert all(call["kwargs"].get("force_qb_load", False) is force_qb_load for call in bo_calls)
    assert db.get(Release, 1).source == "site"  # Public 200 wins even with geo blocking.
    assert db.get(Release, 1).is_blocked_by_geo is True
    assert (db.get(Release, 3).source, db.get(Release, 3).is_blocked_by_geo) == ("backoffice", True)
    assert db.get(Release, 6).source == "backoffice"
    archives = {row.release_id: row for row in db.scalars(select(TorrentArchive))}
    assert all(archives[rid].api_present for rid in (1, 2, 3, 6))
    assert archives[4].api_present is False  # A real completed public sweep still runs.
    run.sweep.assert_called_once()
    assert {2, 3, 6} <= set(run.sweep.call_args.kwargs["preserve_release_ids"])
    assert db.get(Job, 1).result_json["issue_count"] == 0


@pytest.mark.parametrize("catalog_mode", [
    "http_first", "json_first", "http_later", "incomplete_later",
])
def test_full_sync_continues_confirmed_nova_after_public_catalog_failure_without_sweep(db, make_run, catalog_mode):
    run = make_run(catalog_mode=catalog_mode)

    with pytest.raises(JobPartialFailure):
        asyncio.run(full_sync.run_full_sync(db, 1, {}))

    run.client.backoffice.list_resources.assert_awaited_once()
    assert any(call["release_id"] == 2 and call["backoffice_scan"] for call in run.processed)
    run.client.backoffice.get_torrents_for_release.assert_awaited_once_with(2)
    confirmed = db.get(Release, 2)
    assert (confirmed.source, confirmed.source_checked_at, confirmed.is_hidden) == (
        "backoffice", _CONFIRMED_AT, True,
    )
    assert confirmed.title == "Fresh release 2"
    assert all(row.api_present for row in db.scalars(select(TorrentArchive)))
    run.sweep.assert_not_called()
    result = db.get(Job, 1).result_json
    assert result["issue_count"] >= 1 and result["discovery_complete"] is False
    assert result["releases"]["processed"] >= 1
    assert db.scalar(select(JobSyncFailure).where(JobSyncFailure.job_id == 1)) is not None


@pytest.mark.parametrize("stop_at", ["before_catalog", "catalog", "after_site"])
def test_full_sync_stop_never_enters_nova_discovery_or_global_sweep(db, make_run, stop_at):
    run = make_run(stop_at=stop_at)

    with pytest.raises(JobStopRequested):
        asyncio.run(full_sync.run_full_sync(db, 1, {}))

    run.client.backoffice.list_resources.assert_not_awaited()
    run.client.backoffice.get_release.assert_not_awaited()
    run.client.backoffice.get_torrents_for_release.assert_not_awaited()
    assert not any(call["backoffice_scan"] for call in run.processed)
    assert all(row.api_present for row in db.scalars(select(TorrentArchive)))
    run.sweep.assert_not_called()
    assert db.get(Job, 1).result_json["issue_count"] == 0
