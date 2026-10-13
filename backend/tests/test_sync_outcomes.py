"""Durable outcomes and exact retries; PostgreSQL uses its own disposable schema."""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Job, JobSyncFailure, Release, TorrentArchive
from app.services.job_runner import JobStopRequested
from app.services.sync_outcomes import (JobPartialFailure, SyncOutcome, build_retry_params,
    current_outcome, get_failure_page, sync_job, validate_retry_request)
from app.services.torrent_processor import TorrentProcessor


@compiles(JSONB, "sqlite")
def _jsonb(*args, **kwargs):
    return "JSON"


@pytest.fixture(params=["sqlite", "postgres"])
def db(request):
    admin = schema = None
    if request.param == "postgres":
        url = os.getenv("ALTT_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("ALTT_TEST_POSTGRES_URL is not configured")
        schema = "sync_outcomes_" + uuid4().hex
        admin = create_engine(url)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    else:
        engine = create_engine("sqlite://")
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(Job(id=1, type="full_sync", status="running", params_json={}))
            session.commit()
            yield session
    finally:
        engine.dispose()
        if admin is not None:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()


def test_nested_errors_are_not_lost():
    total = TorrentProcessor.empty_release_stats()
    TorrentProcessor.merge_release_stats(total, {"total": 2, "new": 1, "errors": 1})
    assert total["errors"] == 1
    assert "ошибок=1" in TorrentProcessor.format_batch_summary("итого", total, releases=1)


def test_partial_is_durable_and_object_counts_do_not_include_metadata_operations(db):
    @sync_job
    async def handler(db, jid, params):
        o = current_outcome(db, jid)
        o.torrent(10, 11, "a" * 40, "processed")
        o.failure(release_id=10, torrent_id=12, info_hash="b" * 40, source="site",
            message=RuntimeError("password=private ?pk=hidden"), retry_params={"force_qb_load": False, "password": "private"})
        o.release(10, {"new": 1, "comments": 8, "errors": 1})
    with pytest.raises(JobPartialFailure):
        asyncio.run(handler(db, 1, {}))
    db.rollback()
    db.expire_all()
    result = db.get(Job, 1).result_json
    assert result["scan_complete"] and result["releases"]["errors"] == 1
    assert result["torrents"] == {"visited": 2, "processed": 1, "skipped": 0, "waiting": 0, "errors": 1}
    assert result["operations"]["comments"] == 8
    failure = db.scalar(select(JobSyncFailure))
    assert failure.torrent_id == 12 and failure.info_hash == "b" * 40
    assert "private" not in failure.message and "hidden" not in failure.message
    assert failure.retry_params == {"force_qb_load": False}
    assert db.get(Job, 1).result_updated_at is not None


def test_cancel_preserves_completed_results(db):
    @sync_job
    async def handler(db, jid, params):
        o = current_outcome(db, jid)
        o.failure(release_id=10, message="Ошибка чтения")
        o.release(20, {"new": 1})
        raise JobStopRequested()
    with pytest.raises(JobStopRequested):
        asyncio.run(handler(db, 1, {}))
    db.rollback()
    db.expire_all()
    result = db.get(Job, 1).result_json
    assert not result["scan_complete"]
    assert result["issue_count"] == 1 and result["releases"]["processed"] == 1


def test_nested_handler_does_not_end_parent_early(db):
    @sync_job
    async def child(db, jid, params):
        current_outcome(db, jid).failure(release_id=20, message="Ошибка")
    @sync_job
    async def parent(db, jid, params):
        current_outcome(db, jid).release(10, {"new": 1})
        await child(db, jid, {})
        current_outcome(db, jid).release(30, {"new": 1})
    with pytest.raises(JobPartialFailure):
        asyncio.run(parent(db, 1, {}))
    assert db.get(Job, 1).result_json["releases"]["processed"] == 2
    assert db.get(Job, 1).result_json["issue_count"] == 1


def test_waiting_is_not_failure_and_has_priority(db):
    @sync_job
    async def handler(db, jid, params):
        o = current_outcome(db, jid)
        o.torrent(10, 11, "a" * 40, "waiting")
        o.torrent(10, 11, "a" * 40, "processed")
        o.release(10, {"new": 1})
    asyncio.run(handler(db, 1, {}))
    result = db.get(Job, 1).result_json
    assert result["releases"]["waiting"] == result["torrents"]["waiting"] == 1
    assert result["issue_count"] == 0


def test_full_failure_list_paginates_and_retry_is_bounded(db):
    o = SyncOutcome(db, 1)
    for rid in range(103):
        o.failure(release_id=rid + 1, torrent_id=rid + 1000, info_hash="a" * 40, source="site", message="Ошибка")
    db.get(Job, 1).status = "partial"
    db.commit()
    first = get_failure_page(db, 1)
    second = get_failure_page(db, 1, after_id=first["next_after_id"])
    third = get_failure_page(db, 1, after_id=second["next_after_id"])
    ids = [item["id"] for page in (first, second, third) for item in page["items"]]
    assert len(ids) == len(set(ids)) == 103
    assert third["next_after_id"] is None and first["total"] == 103
    with pytest.raises(ValueError, match="100"):
        build_retry_params(db, 1)
    params = build_retry_params(db, 1, ids[:100])
    assert len(params["targets"]) == 100 and params["retry_of"] == 1
    assert params["targets"][0]["source"] == "site"
    assert db.get(Job, 1).params_json == {} and db.get(Job, 1).status == "partial"


@pytest.mark.parametrize("params", [{"retry_of": 1, "targets": []}, {"retry_of": 1, "force_qb_load": True}, {"retry_of": "1"}, {"targets": []}])
def test_public_validator_rejects_arbitrary_snapshots(db, params):
    with pytest.raises(ValueError):
        validate_retry_request(db, params)


def test_retry_rejects_active_foreign_nonretryable_and_duplicate_failure_is_one_object(db):
    o = SyncOutcome(db, 1)
    for _ in range(3):
        o.failure(release_id=10, torrent_id=11, info_hash="a" * 40, message="Ошибка")
    o.failure(operation="schedule", message="Расписание недоступно", retryable=False)
    rows = list(db.scalars(select(JobSyncFailure).order_by(JobSyncFailure.id)).all())
    assert rows[0].attempts == 3 and db.get(Job, 1).result_json["issue_count"] == 2
    with pytest.raises(ValueError, match="завершённой"):
        build_retry_params(db, 1, [rows[0].id])
    db.get(Job, 1).status = "partial"
    db.commit()
    for selected in ([999], [r.id for r in rows], [], [True]):
        with pytest.raises(ValueError):
            build_retry_params(db, 1, selected)
    params = build_retry_params(db, 1, [rows[0].id])
    assert params["targets"][0]["release_id"] == 10
    assert not db.get(Job, 1).result_json["discovery_complete"]


def test_scoped_retry_validates_source_hash_and_does_not_touch_checkpoint_or_presence(db, monkeypatch):
    from app.services import torrent_processor as mod
    client = SimpleNamespace(base_url="https://site.test", passkey="", get_release=AsyncMock(return_value={"id": 10, "_source": "site"}),
        get_torrents_for_release=AsyncMock(return_value=[{"id": 11, "info_hash": "a" * 40}, {"id": 12, "info_hash": "b" * 40}]))
    p = TorrentProcessor(db, 1, client)
    meta = AsyncMock(return_value={"comments": 1})
    monkeypatch.setattr(p, "_refresh_release_meta", meta)
    marker, present = MagicMock(), MagicMock()
    monkeypatch.setattr(mod, "mark_release_processed", marker)
    monkeypatch.setattr(mod, "update_api_present_for_release", present)
    target = {"torrent_id": 11, "info_hash": "a" * 40, "source": "site"}
    result = asyncio.run(p.process_release(10, retry_targets=[target], retry_operation="comment", retry_qb_role="slave"))
    assert result["total"] == 1 and meta.call_args.args[2] == [{"id": 11, "info_hash": "a" * 40}]
    marker.assert_not_called()
    present.assert_not_called()
    for changes in ({"info_hash": "c" * 40}, {"source": "backoffice"}, {"torrent_id": 99}):
        meta.reset_mock()
        result = asyncio.run(p.process_release(10, retry_targets=[{**target, **changes}], retry_operation="comment"))
        assert result["total"] == 0
        meta.assert_not_called()
    assert p._retry_targets is None and p._retry_qb_role is None


def test_retry_hash_candidates_cannot_fall_back_to_other_archive_versions():
    p = TorrentProcessor(MagicMock(), 1, MagicMock())
    p._retry_targets = [{}]
    assert p._candidate_hashes_for_torrent(torrent_id=11, api_hash="a" * 40, seen=SimpleNamespace(info_hash="b" * 40)) == ["a" * 40]
    assert p._candidate_hashes_for_torrent(torrent_id=11, api_hash=None, seen=SimpleNamespace(info_hash="b" * 40)) == []


def test_source_policy_subset_preserves_unselected_archive(db, monkeypatch, tmp_path):
    from app.services import release_source as mod
    db.add(Release(release_id=10, source="backoffice", is_hidden=True))
    for tid, hash_ in [(11, "a" * 40), (12, "b" * 40)]:
        db.add(TorrentArchive(torrent_id=tid, info_hash=hash_, release_id=10, file_path=f"{hash_}.torrent", quality_json={}))
        (tmp_path / f"{hash_}.torrent").write_bytes(b"original")
    db.commit()
    monkeypatch.setattr("app.services.torrent_archive.TorrentArchiveService.resolve_file_path", lambda self, archive: tmp_path / f"{archive.info_hash}.torrent")
    monkeypatch.setattr(mod, "replace_trackers", lambda raw, pk: b"changed")
    tags, trackers = MagicMock(), MagicMock()
    monkeypatch.setattr(mod, "reconcile_source_tags", tags)
    monkeypatch.setattr(mod, "reconcile_trackers", trackers)
    client = MagicMock()
    client.torrents_info.return_value = [SimpleNamespace(tags="custom, drama")]
    mod.apply_release_policy(db, 10, [("slave", client)], "key", torrent_targets={(11, "a" * 40)})
    assert (tmp_path / ("a" * 40 + ".torrent")).read_bytes() == b"changed"
    assert (tmp_path / ("b" * 40 + ".torrent")).read_bytes() == b"original"
    assert not db.scalar(select(TorrentArchive).where(TorrentArchive.torrent_id == 12)).backoffice_trackers
    assert tags.call_args.args[1] == "a" * 40 and tags.call_count == trackers.call_count == 1


@pytest.mark.parametrize("module_name,handler_name", [("full_sync", "run_full_sync"), ("meta_sync", "run_full_meta_sync")])
def test_catalog_sync_continues_after_one_release_failure_and_never_sweeps_partial(db, monkeypatch, module_name, handler_name):
    from importlib import import_module
    module = import_module("app.jobs." + module_name)
    client = SimpleNamespace(catalog_releases=AsyncMock(return_value={"data": [{"id": 10}, {"id": 20}], "meta": {"pagination": {"total": 2, "total_pages": 1}}}))
    processor = SimpleNamespace(process_release=AsyncMock(side_effect=[RuntimeError("release failed"), {"new": 1, "total": 1}]),
        backfill_qb_comments_from_archive=lambda: {"archives": 0, "updated": 0, "missing": 0})
    factory = MagicMock(return_value=processor)
    for name in ("empty_release_stats", "merge_release_stats", "format_batch_summary"):
        setattr(factory, name, getattr(TorrentProcessor, name))
    monkeypatch.setattr(module, "TorrentProcessor", factory)
    monkeypatch.setattr(module, "build_anilibria_client", lambda db: client)
    monkeypatch.setattr(module, "_setting_int", lambda *a: 0)
    sweep = MagicMock()
    if module_name == "full_sync":
        monkeypatch.setattr(module, "mark_missing_api_present_false", sweep)
    with pytest.raises(JobPartialFailure):
        asyncio.run(getattr(module, handler_name)(db, 1, {}))
    assert processor.process_release.await_count == 2
    result = db.get(Job, 1).result_json
    assert result["releases"]["errors"] == result["releases"]["processed"] == 1
    assert db.scalar(select(JobSyncFailure)).release_id == 10
    sweep.assert_not_called()


def test_retry_metadata_dual_write_is_limited_to_selected_version(db):
    for tid, hash_ in [(11, "a" * 40), (12, "b" * 40)]:
        db.add(TorrentArchive(torrent_id=tid, info_hash=hash_, release_id=10, file_path=f"{hash_}.torrent", quality_json={"genres": ["old"], "members": []}))
    db.commit()
    p = TorrentProcessor(db, 1, MagicMock())
    p._retry_targets = [{}]
    p._policy_targets = {(11, "a" * 40)}
    p._persist_genres_to_archives(10, ["new"])
    p._persist_members_and_blocks_to_archives(10, members=[{"nickname": "Name"}], is_blocked_by_geo=True)
    first, second = db.scalars(select(TorrentArchive).order_by(TorrentArchive.torrent_id)).all()
    assert first.quality_json["genres"] == ["new"] and first.quality_json["is_blocked_by_geo"]
    assert second.quality_json == {"genres": ["old"], "members": []}


def test_missing_parent_is_a_failure_instead_of_unpersisted_success(db):
    @sync_job
    async def handler(db, jid, params):
        raise AssertionError("must not run")
    with pytest.raises(ValueError, match="не найден"):
        asyncio.run(handler(db, 9999, {}))


@pytest.mark.parametrize("operation,desired", [("comment", "https://site.test/show"), ("rename", "Title"), ("tags", ["Drama"])])
def test_metadata_noop_is_not_reported_as_error(db, operation, desired):
    client = SimpleNamespace(torrents_info=lambda **kwargs: [{"name": "Title", "tags": "Drama, site"}],
        torrents_properties=lambda **kwargs: {"comment": "https://site.test/show"})
    p = TorrentProcessor(db, 1, MagicMock())
    @sync_job
    async def handler(db, jid, params):
        p._metadata_failed(10, 11, "a" * 40, "master", client, operation, desired)
    asyncio.run(handler(db, 1, {}))
    assert db.get(Job, 1).result_json["issue_count"] == 0


def test_rename_helper_silent_failure_is_reported(db, monkeypatch):
    from app.services import torrent_processor as mod
    client = SimpleNamespace(torrents_info=lambda **kwargs: [{"name": "Old"}])
    p = TorrentProcessor(db, 1, MagicMock())
    monkeypatch.setattr(p, "_qb_clients_for_meta", lambda: [("slave", client)])
    monkeypatch.setattr(p, "_display_name_for_torrent", lambda *args, **kwargs: "New")
    monkeypatch.setattr(p, "_candidate_hashes_for_torrent", lambda **kwargs: ["a" * 40])
    monkeypatch.setattr(mod, "ensure_torrent_rename", lambda *args, **kwargs: True)
    @sync_job
    async def handler(db, jid, params):
        p._refresh_qb_renames(release_id=10, torrents=[{"id": 11, "info_hash": "a" * 40}])
    with pytest.raises(JobPartialFailure):
        asyncio.run(handler(db, 1, {}))
    failure = db.scalar(select(JobSyncFailure))
    assert failure.operation == "rename" and failure.qb_role == "slave"


@pytest.mark.parametrize("change", [None, "source", "hash", "removed"])
def test_real_retry_handler_uses_server_snapshot_and_exact_source_version(db, monkeypatch, change):
    from app.jobs import retry_sync_failures as retry
    from app.db.models import ReleaseCheckpoint
    o = SyncOutcome(db, 1)
    o.failure(release_id=10, torrent_id=11, info_hash="a" * 40, source="site", operation="comment", qb_role="slave", message="Ошибка")
    db.get(Job, 1).status = "partial"
    db.commit()
    params = build_retry_params(db, 1)
    db.add(Job(id=2, type="retry_sync_failures", status="running", params_json=params))
    db.add(ReleaseCheckpoint(release_id=10, torrents_fingerprint="original", api_updated_at="original", api_fresh_at="original"))
    db.commit()
    # A queued retry's validated snapshot remains usable after parent retention.
    db.delete(db.get(Job, 1))
    db.commit()
    torrents = [{"id": 11, "info_hash": "c" * 40 if change == "hash" else "a" * 40}, {"id": 12, "info_hash": "b" * 40}]
    if change == "removed":
        torrents = torrents[1:]
    client = SimpleNamespace(base_url="https://site.test", passkey="", get_release=AsyncMock(return_value={"id": 10, "_source": "backoffice" if change == "source" else "site"}),
        get_torrents_for_release=AsyncMock(return_value=torrents))
    monkeypatch.setattr(retry, "build_anilibria_client", lambda db: client)
    applied = []
    class Processor(TorrentProcessor):
        async def _refresh_release_meta(self, rid, alias, items, **kwargs):
            applied.append((rid, list(items), self._retry_qb_role, self._retry_operation))
            return {"comments": 1}
    monkeypatch.setattr(retry, "TorrentProcessor", Processor)
    asyncio.run(retry.run_retry_sync_failures(db, 2, params))
    if change is None:
        assert applied == [(10, [{"id": 11, "info_hash": "a" * 40}], "slave", "comment")]
        assert db.get(Job, 2).result_json["torrents"]["processed"] == 1
    else:
        assert applied == []
        assert db.get(Job, 2).result_json["notes"][0]["code"] == "stale_target"
    checkpoint = db.get(ReleaseCheckpoint, 10)
    assert checkpoint.torrents_fingerprint == checkpoint.api_updated_at == "original"
    assert db.get(Job, 2).params_json == params
    client.get_torrents_for_release.assert_awaited_once_with(10, include=TorrentProcessor.RELEASE_TORRENTS_INCLUDE)


def test_retry_baseline_is_preserved_from_snapshot(db, monkeypatch):
    from app.jobs import retry_sync_failures as retry
    o = SyncOutcome(db, 1)
    o.failure(release_id=10, torrent_id=11, info_hash="a" * 40, source="backoffice", operation="meta", message="Ошибка",
        retry_params={"notification_baseline": True})
    db.get(Job, 1).status = "partial"
    db.commit()
    params = build_retry_params(db, 1)
    db.add(Job(id=2, type="retry_sync_failures", status="running", params_json=params))
    db.commit()
    payload = {"id": 10, "_source": "backoffice"}
    client = SimpleNamespace(base_url="https://site.test", passkey="", get_release=AsyncMock(return_value=payload),
        get_torrents_for_release=AsyncMock(return_value=[{"id": 11, "info_hash": "a" * 40}]))
    monkeypatch.setattr(retry, "build_anilibria_client", lambda db: client)
    class Processor(TorrentProcessor):
        async def _refresh_release_meta(self, rid, alias, items, **kwargs):
            assert kwargs["release_payload"]["_notification_baseline"] is True
            return {}
    monkeypatch.setattr(retry, "TorrentProcessor", Processor)
    asyncio.run(retry.run_retry_sync_failures(db, 2, params))
    assert db.get(Job, 2).result_json["issue_count"] == 0


def test_process_retry_archives_only_target_and_keeps_baseline_suppression(db, monkeypatch, tmp_path):
    from app.jobs import retry_sync_failures as retry
    from app.db.models import SeenTorrent, Setting, TorrentPipeline, ReleaseCheckpoint
    from app.providers.anilibria.client import AniLibriaNotFoundError
    from app.providers.anilibria.sources import ReleaseSourceClient
    from app.services import torrent_processor as mod
    from app.services.hevc_notifications import _baseline_only, enqueue_new_release_notification
    from app.services.qbittorrent import torrent_info_hash
    raw = b"d8:announce10:http://old4:infod4:name4:test6:lengthi1eee"
    hash_ = torrent_info_hash(raw)
    o = SyncOutcome(db, 1)
    o.failure(release_id=10, torrent_id=11, info_hash=hash_, source="backoffice", message="Ошибка",
        retry_params={"notification_baseline": True})
    db.get(Job, 1).status = "partial"
    db.commit()
    params = build_retry_params(db, 1)
    db.add(Job(id=2, type="retry_sync_failures", status="running", params_json=params))
    db.add(Setting(key="backoffice_baseline_completed_at", value="already-completed"))
    db.add(Setting(key="anilibria_passkey", value="key"))
    db.commit()
    public = SimpleNamespace(base_url="https://site.test", passkey="key", get_release=AsyncMock(side_effect=AniLibriaNotFoundError()))
    admin = SimpleNamespace(get_release=AsyncMock(return_value={"id": 10, "alias": "show", "name": {"main": "Title"}, "is_hidden": True, "_source": "backoffice"}),
        get_torrents_for_release=AsyncMock(return_value=[{"id": 11, "hash": hash_, "codec": {"label": "AVC"}}, {"id": 12, "hash": "b" * 40}]),
        download_torrent_file=AsyncMock(return_value=raw))
    client = ReleaseSourceClient(public, admin, db)
    monkeypatch.setattr(retry, "build_anilibria_client", lambda db: client)
    monkeypatch.setattr(mod, "ensure_passkey_stored", AsyncMock(return_value="key"))
    monkeypatch.setenv("TORRENT_STORAGE_DIR", str(tmp_path))
    asyncio.run(retry.run_retry_sync_failures(db, 2, params))
    archives = list(db.scalars(select(TorrentArchive)).all())
    assert len(archives) == 1 and archives[0].torrent_id == 11 and archives[0].notification_baseline
    assert archives[0].backoffice_trackers and archives[0].api_present
    assert db.get(SeenTorrent, 11).info_hash == hash_ and db.get(SeenTorrent, 12) is None
    assert db.scalar(select(TorrentPipeline)).status == "waiting_master"
    assert db.get(Job, 2).result_json["releases"]["waiting"] == 1
    assert db.get(ReleaseCheckpoint, 10) is None
    assert _baseline_only(db, 10)
    assert enqueue_new_release_notification(db, release_id=10, info_hash=hash_, hashed_files=1) == 0
    admin.download_torrent_file.assert_awaited_once_with(11)


def test_release_precedence_preserves_processed_when_later_target_is_stale(db):
    o = SyncOutcome(db, 1)
    o.release(10, {"comments": 1})
    o.stale(10, 12, "Версия изменилась")
    o.release(10, status="skipped")
    assert db.get(Job, 1).result_json["releases"]["processed"] == 1
    assert db.get(Job, 1).result_json["releases"]["skipped"] == 0


@pytest.mark.parametrize("helper", ["_refresh_qb_tags", "_refresh_qb_comments", "_refresh_qb_renames"])
def test_metadata_unavailable_clients_are_waiting_and_do_not_reconnect(db, monkeypatch, helper):
    p = TorrentProcessor(db, 1, SimpleNamespace(base_url="https://site.test"))
    clients = MagicMock(side_effect=[[], AssertionError("must not reconnect")])
    monkeypatch.setattr(p, "_qb_clients_for_meta", clients)
    monkeypatch.setattr(p, "_alias_for_release", lambda *args: "show")
    kwargs = {"release_id": 10, "torrents": [{"id": 11, "info_hash": "a" * 40}]}
    if helper == "_refresh_qb_tags":
        kwargs["genre_tags"] = ["Drama"]
    elif helper == "_refresh_qb_comments":
        kwargs["release_alias"] = "show"
    @sync_job
    async def handler(db, jid, params):
        assert getattr(p, helper)(**kwargs) == 0
        current_outcome(db, jid).release(10, {"new": 1})
    asyncio.run(handler(db, 1, {}))
    assert clients.call_count == 1
    assert db.get(Job, 1).result_json["torrents"]["waiting"] == 1
    assert db.get(Job, 1).result_json["releases"]["waiting"] == 1


def test_torrent_retry_without_exact_hash_is_not_available(db):
    o = SyncOutcome(db, 1)
    o.failure(release_id=10, torrent_id=11, message="Ошибка загрузки")
    row = db.scalar(select(JobSyncFailure))
    assert not row.retryable and "версия неизвестна" in row.message
    db.get(Job, 1).status = "partial"
    db.commit()
    with pytest.raises(ValueError):
        build_retry_params(db, 1, [row.id])
    p = TorrentProcessor(db, 1, MagicMock())
    with pytest.raises(ValueError, match="info_hash"):
        asyncio.run(p.process_release(10, retry_targets=[{"torrent_id": 11, "info_hash": None, "source": "site"}]))


@pytest.mark.parametrize("mode", ["missing_pages", "raw_list", "shrink", "duplicate", "empty_middle", "wrong_count"])
@pytest.mark.parametrize("module_name,handler_name", [("full_sync", "run_full_sync"), ("meta_sync", "run_full_meta_sync")])
def test_incomplete_catalogue_is_partial_and_keeps_unvisited_archives(db, monkeypatch, mode, module_name, handler_name):
    from importlib import import_module
    module = import_module("app.jobs." + module_name)
    for rid, tid, hash_ in [(10, 11, "a" * 40), (20, 21, "b" * 40)]:
        db.add(TorrentArchive(release_id=rid, torrent_id=tid, info_hash=hash_, file_path=f"{hash_}.torrent", quality_json={}, api_present=True))
    db.commit()
    def envelope(ids, page=1, pages=1, total=1, **kwargs):
        return {"data": [{"id": x} for x in ids], "meta": {"pagination": {"total": total, "total_pages": pages, "current_page": page, **kwargs}}}
    pages = {
        "missing_pages": [{"data": [{"id": 10}], "meta": {"pagination": {"total": 2}}}],
        "raw_list": [[{"id": 10}]],
        "shrink": [envelope([10], pages=2, total=2), envelope([20], page=2, pages=1, total=2)],
        "duplicate": [envelope([10], pages=2, total=2), envelope([10], page=2, pages=2, total=2)],
        "empty_middle": [envelope([10], pages=3, total=3), envelope([], page=2, pages=3, total=3)],
        "wrong_count": [envelope([10], total=1, count=2)],
    }[mode]
    client = SimpleNamespace(catalog_releases=AsyncMock(side_effect=pages))
    processor = SimpleNamespace(process_release=AsyncMock(return_value={"new": 1, "total": 1}),
        backfill_qb_comments_from_archive=lambda: {"archives": 0, "updated": 0, "missing": 0})
    factory = MagicMock(return_value=processor)
    for name in ("empty_release_stats", "merge_release_stats", "format_batch_summary"):
        setattr(factory, name, getattr(TorrentProcessor, name))
    monkeypatch.setattr(module, "TorrentProcessor", factory)
    monkeypatch.setattr(module, "build_anilibria_client", lambda db: client)
    monkeypatch.setattr(module, "_setting_int", lambda *a: 0)
    with pytest.raises(JobPartialFailure):
        asyncio.run(getattr(module, handler_name)(db, 1, {}))
    result = db.get(Job, 1).result_json
    assert not result["scan_complete"] and not result["discovery_complete"]
    assert db.scalar(select(JobSyncFailure)).code == "catalog_incomplete"
    assert all(archive.api_present for archive in db.scalars(select(TorrentArchive)).all())
    if mode == "duplicate":
        assert processor.process_release.await_count == 1


def test_consistent_catalogue_confirms_all_pages(db):
    from app.services.sync_outcomes import CatalogScan
    o = SyncOutcome(db, 1)
    scan = CatalogScan(o)
    for page in (1, 2):
        items, pages = scan.accept({"data": [{"id": page}], "meta": {"pagination": {"total": 2, "total_pages": 2, "count": 1, "current_page": page, "per_page": 1}}}, page)
        assert items == [{"id": page}] and pages == 2
        assert scan.complete == (page == 2)
    assert not o.failures


def test_stale_baseline_target_does_not_suppress_current_site_target(db, monkeypatch):
    payload = {"id": 10, "_source": "site"}
    client = SimpleNamespace(base_url="https://site.test", passkey="", get_release=AsyncMock(return_value=payload),
        get_torrents_for_release=AsyncMock(return_value=[{"id": 12, "info_hash": "b" * 40}]))
    p = TorrentProcessor(db, 1, client)
    meta = AsyncMock(return_value={"comments": 1})
    monkeypatch.setattr(p, "_refresh_release_meta", meta)
    asyncio.run(p.process_release(10, retry_operation="meta", retry_targets=[
        {"torrent_id": 11, "info_hash": "a" * 40, "source": "backoffice", "notification_baseline": True},
        {"torrent_id": 12, "info_hash": "b" * 40, "source": "site", "notification_baseline": False}]))
    assert not meta.call_args.kwargs["release_payload"].get("_notification_baseline", False)
    assert meta.call_args.args[2] == [{"id": 12, "info_hash": "b" * 40}]


def test_shared_meta_clients_connect_once_and_configured_slave_failure_is_waiting(db, monkeypatch):
    from app.db.models import QbClient
    from app.services import torrent_processor as mod
    from app.services.release_source import release_url
    from app.services.torrent_qb_meta import resolve_anilibria_site_url
    for role in ("master", "slave"):
        db.add(QbClient(name=role, role=role, host=role, port=8080, username="u", password_encrypted="p", enabled=True))
    db.commit()
    expected_comment = release_url(db, 10, "show", site_url=resolve_anilibria_site_url("https://site.test"))
    class Client:
        def __init__(self, host, **kwargs):
            self.host = host
        def auth_log_in(self):
            if self.host == "slave":
                raise ConnectionError("Connection refused")
        def torrents_info(self, **kwargs):
            return [{"name": "Title", "tags": "Drama, site"}]
        def torrents_properties(self, **kwargs):
            return {"comment": expected_comment}
    factory = MagicMock(side_effect=Client)
    monkeypatch.setattr(mod.qbittorrentapi, "Client", factory)
    p = TorrentProcessor(db, 1, SimpleNamespace(base_url="https://site.test", passkey=""))
    monkeypatch.setattr(p, "_resolve_genres_for_meta", AsyncMock(return_value=["Drama"]))
    monkeypatch.setattr(p, "_archive_hash_mismatches", lambda torrent: False)
    monkeypatch.setattr(p, "_display_name_for_torrent", lambda *args, **kwargs: "Title")
    monkeypatch.setattr(p, "_alias_for_release", lambda *args: "show")
    @sync_job
    async def handler(db, jid, params):
        part = await p._refresh_release_meta(10, "show", [{"id": 11, "info_hash": "a" * 40}],
            release_payload={"id": 10, "alias": "show", "name": {"main": "Title"}})
        current_outcome(db, jid).torrent(10, 11, "a" * 40, "processed")
        current_outcome(db, jid).release(10, part)
    asyncio.run(handler(db, 1, {}))
    assert factory.call_count == 2  # one attempt for each configured role, shared across all metadata operations
    result = db.get(Job, 1).result_json
    assert result["issue_count"] == 0
    assert result["torrents"]["waiting"] == result["releases"]["waiting"] == 1
