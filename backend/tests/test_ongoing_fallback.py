import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import ExtraUrl, OngoingWatch
from app.jobs import ongoing
from app.providers.anilibria.client import AniLibriaNotFoundError
from app.services.ongoing_watch import observe_torrents, update_watch_list
from app.services.release_checkpoint import ReleaseRef
from app.services.torrent_processor import TorrentProcessor

NOW = datetime(2026, 10, 9)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    OngoingWatch.__table__.create(engine)
    ExtraUrl.__table__.create(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_watch_lifecycle_and_any_new_id_resets_window(db):
    update_watch_list(db, [ReleaseRef(1)], now=NOW)
    observe_torrents(db, 1, [100], now=NOW)
    missing = NOW + timedelta(days=1)
    update_watch_list(db, [], now=missing)
    row = db.get(OngoingWatch, 1)
    assert row.expires_at == missing + timedelta(days=14)
    assert not observe_torrents(db, 1, [99, 100], now=missing + timedelta(days=2))
    assert row.expires_at == missing + timedelta(days=14)
    assert observe_torrents(db, 1, [101], now=missing + timedelta(days=3))
    expiry = missing + timedelta(days=17)
    assert row.expires_at == expiry
    refs, expired = update_watch_list(db, [], now=expiry - timedelta(microseconds=1))
    assert len(refs) == 1 and expired == 0
    refs, expired = update_watch_list(db, [], now=expiry)
    assert refs == [] and expired == 1


def test_schedule_failure_and_return(db):
    update_watch_list(db, [ReleaseRef(1)], now=NOW)
    update_watch_list(db, None, now=NOW + timedelta(days=1))
    assert db.get(OngoingWatch, 1).missing_since is None
    update_watch_list(db, [], now=NOW + timedelta(days=2))
    update_watch_list(db, None, now=NOW + timedelta(days=3))
    assert db.get(OngoingWatch, 1).missing_since == NOW + timedelta(days=2)
    update_watch_list(db, [ReleaseRef(1)], now=NOW + timedelta(days=4))
    assert db.get(OngoingWatch, 1).expires_at is None
    update_watch_list(db, [], now=NOW + timedelta(days=5))
    assert db.get(OngoingWatch, 1).expires_at == NOW + timedelta(days=19)


@pytest.fixture
def job_env(db, monkeypatch):
    client = SimpleNamespace(
        get_schedule_week=AsyncMock(return_value=[]),
        get_torrents_for_release=AsyncMock(return_value=[{"id": 100, "codec": "AVC"}]),
        get_release=AsyncMock(return_value={"id": 1}),
    )
    calls = AsyncMock(return_value=TorrentProcessor.empty_release_stats())

    class Processor:
        RELEASE_TORRENTS_INCLUDE = TorrentProcessor.RELEASE_TORRENTS_INCLUDE
        empty_release_stats = staticmethod(TorrentProcessor.empty_release_stats)
        merge_release_stats = staticmethod(TorrentProcessor.merge_release_stats)
        format_batch_summary = staticmethod(TorrentProcessor.format_batch_summary)

        def __init__(self, **kwargs):
            self.process_release = calls

    monkeypatch.setattr(ongoing, "TorrentProcessor", Processor)
    monkeypatch.setattr(ongoing, "build_anilibria_client", lambda _: client)
    monkeypatch.setattr(ongoing, "_add_log", lambda *a, **k: None)
    monkeypatch.setattr(ongoing, "_setting_int", lambda *a: 0)
    monkeypatch.setattr(ongoing, "list_enabled_tracked_releases", lambda _: [])
    monkeypatch.setattr(ongoing, "is_stop_requested", lambda *a: False)
    monkeypatch.setattr(ongoing, "should_skip_unchanged", lambda *a, **k: True)
    monkeypatch.setattr(ongoing, "utcnow", lambda: NOW)
    return client, calls


def run(db):
    asyncio.run(ongoing.run_ongoing(db, 1, {}))


def test_late_hevc_is_processed_with_unchanged_markers(db, job_env, monkeypatch):
    client, calls = job_env
    client.get_schedule_week.return_value = [
        {"release": {"id": 1, "updated_at": "same"}}
    ]
    run(db)
    assert calls.call_args.kwargs["prefetched_torrents"] == [
        {"id": 100, "codec": "AVC"}
    ]
    client.get_schedule_week.return_value = []
    monkeypatch.setattr(ongoing, "utcnow", lambda: NOW + timedelta(hours=1))
    run(db)
    client.get_torrents_for_release.return_value = [
        {"id": 100, "codec": "AVC"},
        {"id": 101, "codec": "HEVC"},
    ]
    monkeypatch.setattr(ongoing, "utcnow", lambda: NOW + timedelta(hours=20))
    run(db)
    assert calls.call_args.kwargs["prefetched_torrents"][-1]["id"] == 101
    assert calls.await_count == client.get_torrents_for_release.await_count == 3
    assert db.get(OngoingWatch, 1).expires_at == NOW + timedelta(hours=20, days=14)


@pytest.mark.parametrize("schedule", [None, {}, {"data": None}, [{"release": {}}]])
def test_broken_schedule_does_not_start_expiry(db, job_env, schedule):
    client, calls = job_env
    update_watch_list(db, [ReleaseRef(1)], now=NOW)
    client.get_schedule_week.return_value = schedule
    run(db)
    assert db.get(OngoingWatch, 1).missing_since is None
    calls.assert_awaited_once()


@pytest.mark.parametrize("torrent_result", [[], AniLibriaNotFoundError()])
@pytest.mark.parametrize("release_result", ["exists", "404", "timeout"])
def test_only_confirmed_release_404_removes_watch(
    db, job_env, torrent_result, release_result
):
    client, calls = job_env
    update_watch_list(db, [ReleaseRef(1)], now=NOW)
    if isinstance(torrent_result, Exception):
        client.get_torrents_for_release.side_effect = torrent_result
    else:
        client.get_torrents_for_release.return_value = torrent_result
    if release_result != "exists":
        client.get_release.side_effect = (
            AniLibriaNotFoundError() if release_result == "404" else TimeoutError()
        )
    run(db)
    assert (db.get(OngoingWatch, 1) is None) == (release_result == "404")


def test_failure_isolated_and_does_not_refresh_deadline(db, job_env):
    client, calls = job_env
    update_watch_list(db, [ReleaseRef(1), ReleaseRef(2)], now=NOW - timedelta(days=2))
    update_watch_list(db, [], now=NOW - timedelta(days=1))
    expiry = db.get(OngoingWatch, 1).expires_at
    client.get_torrents_for_release.side_effect = [TimeoutError(), [{"id": 102}]]
    run(db)
    assert db.get(OngoingWatch, 1).expires_at == expiry
    assert db.get(OngoingWatch, 2).max_torrent_id == 102
    calls.assert_awaited_once()
    assert calls.call_args.kwargs["release_id"] == 2


def test_processing_failure_does_not_repeatedly_extend(db, job_env, monkeypatch):
    client, calls = job_env
    update_watch_list(db, [ReleaseRef(1)], now=NOW - timedelta(days=2))
    update_watch_list(db, [], now=NOW - timedelta(days=1))
    calls.side_effect = RuntimeError("torrent download failed")
    run(db)
    assert db.get(OngoingWatch, 1).expires_at == NOW + timedelta(days=14)
    monkeypatch.setattr(ongoing, "utcnow", lambda: NOW + timedelta(days=1))
    run(db)
    assert db.get(OngoingWatch, 1).expires_at == NOW + timedelta(days=14)


def test_expired_watch_still_processed_when_explicitly_tracked(
    db, job_env, monkeypatch
):
    _, calls = job_env
    update_watch_list(db, [ReleaseRef(1)], now=NOW - timedelta(days=20))
    update_watch_list(db, [], now=NOW - timedelta(days=15))
    monkeypatch.setattr(
        ongoing,
        "list_enabled_tracked_releases",
        lambda _: [SimpleNamespace(release_id=1, release_alias="x")],
    )
    run(db)
    assert db.get(OngoingWatch, 1) is None
    calls.assert_awaited_once()


def test_schedule_cache_extra_and_tracking_deduplicated(db, job_env, monkeypatch):
    client, calls = job_env
    client.get_schedule_week.return_value = [{"release": {"id": 1}}]
    db.add(ExtraUrl(release_alias="x", release_id=1, enabled=True))
    db.commit()
    monkeypatch.setattr(
        ongoing,
        "list_enabled_tracked_releases",
        lambda _: [SimpleNamespace(release_id=1, release_alias="x")],
    )
    run(db)
    calls.assert_awaited_once()
    client.get_torrents_for_release.assert_awaited_once()


def test_malformed_torrents_do_not_erase_or_extend(db, job_env):
    client, calls = job_env
    update_watch_list(db, [ReleaseRef(1)], now=NOW - timedelta(days=2))
    update_watch_list(db, [], now=NOW - timedelta(days=1))
    expiry = db.get(OngoingWatch, 1).expires_at
    client.get_torrents_for_release.return_value = {"error": "unavailable"}
    run(db)
    assert db.get(OngoingWatch, 1).expires_at == expiry
    calls.assert_not_awaited()


def test_watch_migration_upgrade_downgrade():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    path = Path(__file__).parents[1] / "alembic/versions/0023_ongoing_watch.py"
    spec = importlib.util.spec_from_file_location("watch_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            assert {
                c["name"] for c in inspect(conn).get_columns("ongoing_watch")
            } == set(OngoingWatch.__table__.columns.keys())
            migration.downgrade()
            assert "ongoing_watch" not in inspect(conn).get_table_names()
    engine.dispose()


def test_release_disappears_during_processing(db, job_env):
    client, calls = job_env
    update_watch_list(db, [ReleaseRef(1)], now=NOW)
    calls.side_effect = AniLibriaNotFoundError()
    client.get_release.side_effect = AniLibriaNotFoundError()
    run(db)
    assert db.get(OngoingWatch, 1) is None
