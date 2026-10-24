"""Nova discovery and the last confirmed release source during public API outages."""

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Job, JobLog, JobSyncFailure, Release, Setting, TorrentArchive
from app.providers.anilibria.backoffice import BackofficeError
from app.providers.anilibria.client import AniLibriaNotFoundError
from app.providers.anilibria.sources import ReleaseSourceClient, SourceCheckDeferred
from app.services.release_lock import release_lock
from app.services.release_meta import upsert_release_meta
from app.services.runtime_settings import get_setting_value
from app.services.sync_outcomes import JobPartialFailure


CONFIRMED_AT = datetime(2026, 10, 20, 8, 30)
TORRENT_HASH = "a" * 40


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(*args, **kwargs):
    return "JSON"


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            Job(id=job_id, type="backoffice_sync", status="running", params_json={})
            for job_id in (1, 2, 3)
        ])
        session.add(Setting(key="scrape_pause_sec", value="0"))
        session.commit()
        yield session
    engine.dispose()


@pytest.fixture
def add_release(db):
    def add(*, release_id=1, source="backoffice", hidden=True, copyright=False):
        row = Release(
            release_id=release_id,
            release_alias=f"release-{release_id}",
            title="Confirmed title",
            source=source,
            source_checked_at=CONFIRMED_AT if source else None,
            is_hidden=hidden,
            is_blocked_by_copyrights=copyright,
        )
        db.add(row)
        db.commit()
        return row

    return add


@pytest.fixture
def add_archive(db):
    def add(*, release_id=1, baseline=False):
        row = TorrentArchive(
            info_hash=TORRENT_HASH,
            torrent_id=10,
            release_id=release_id,
            release_alias=f"release-{release_id}",
            file_path="unused.torrent",
            quality_json={},
            api_present=True,
            backoffice_trackers=True,
            notification_baseline=baseline,
        )
        db.add(row)
        db.commit()
        return row

    return add


@pytest.fixture
def make_client(db):
    def make(*, release_ids=(1,)):
        public = SimpleNamespace(
            base_url="https://public.test/api/v1",
            passkey="test-passkey",
            catalog_releases=AsyncMock(side_effect=RuntimeError("Public catalog unavailable")),
            get_release=AsyncMock(side_effect=RuntimeError("AniLibria API: запрос не выполнен (некорректный JSON)")),
            get_torrents_for_release=AsyncMock(return_value=[{"id": 10, "hash": TORRENT_HASH}]),
        )
        admin_payloads = {
            rid: {
                "id": rid,
                "alias": f"release-{rid}",
                "name": {"main": "Fresh Nova title"},
                "_source": "backoffice",
                "is_hidden": False,
                "is_blocked_by_copyrights": True,
                "genres": [{"id": 1, "name": "Драма"}],
            }
            for rid in release_ids
        }
        admin = SimpleNamespace(
            list_resources=AsyncMock(return_value=[{"id": {"value": rid}, "fields": []} for rid in release_ids]),
            get_release=AsyncMock(side_effect=lambda rid: dict(admin_payloads[rid])),
            get_torrents_for_release=AsyncMock(side_effect=lambda rid: [{
                "id": 10, "release_id": rid, "hash": TORRENT_HASH,
            }]),
        )
        client = ReleaseSourceClient(public, admin, db)
        client.test_admin_payloads = admin_payloads
        return client

    return make


@pytest.fixture
def configure_job(db, monkeypatch):
    from app.jobs import backoffice_sync as job

    def configure(client):
        processed = []

        async def process_release(*, release_id, **kwargs):
            with release_lock(db, release_id):
                payload = await client.get_release(release_id)
                # Processor persists card metadata again; stale-source protection
                # must survive both remember() and this subsequent upsert.
                upsert_release_meta(db, release_id, payload)
                torrents = await client.get_torrents_for_release(release_id)
            processed.append({"release_id": release_id, "payload": payload, "torrents": torrents})
            return {"updated": len(torrents)}

        processor = SimpleNamespace(process_release=AsyncMock(side_effect=process_release))
        monkeypatch.setattr(job, "build_anilibria_client", lambda _: client)
        monkeypatch.setattr(job, "is_stop_requested", lambda *args: False)
        monkeypatch.setattr(job, "TorrentProcessor", lambda **kwargs: processor)
        return SimpleNamespace(job=job, processor=processor, processed=processed)

    return configure


@pytest.mark.parametrize("nova_hidden,nova_copyright", [(False, True), (False, False)])
def test_nova_scan_keeps_confirmed_backoffice_during_public_outage(
    db, add_release, add_archive, make_client, configure_job, nova_hidden, nova_copyright,
):
    row = add_release()
    archive = add_archive()
    client = make_client()
    client.test_admin_payloads[1].update(is_hidden=nova_hidden, is_blocked_by_copyrights=nova_copyright)
    # Watched releases remain candidates even if Nova's index already looks
    # ordinary; only a successful public check may confirm their return.
    client.backoffice.list_resources.return_value[0]["fields"] = [
        {"attribute": key, "component": "boolean-field", "value": False}
        for key in ("is_hidden", "is_blocked_by_copyrights")
    ]
    run = configure_job(client)

    asyncio.run(run.job.run_backoffice_sync(db, 1, {}))

    client.public.catalog_releases.assert_not_awaited()
    client.backoffice.list_resources.assert_awaited_once()
    client.backoffice.get_torrents_for_release.assert_awaited_once_with(1)
    assert [item["release_id"] for item in run.processed] == [1]
    assert run.processed[0]["torrents"][0]["hash"] == TORRENT_HASH
    assert run.processed[0]["payload"]["_source_confirmed"] is False
    db.refresh(row)
    assert (row.source, row.is_hidden, row.is_blocked_by_copyrights, row.source_checked_at) == (
        "backoffice", True, False, CONFIRMED_AT,
    )
    assert row.title == "Fresh Nova title"
    assert row.genres_json == ["Драма"]
    db.refresh(archive)
    assert archive.api_present and archive.backoffice_trackers
    assert client.source_check_errors
    assert db.scalar(select(JobLog.id).where(JobLog.job_id == 1, JobLog.level == "warning")) is not None
    assert db.get(Job, 1).result_json["issue_count"] == 0
    assert get_setting_value(db, "backoffice_baseline_completed_at")


@pytest.mark.parametrize("source", ["site", None])
def test_public_error_does_not_select_nova_for_site_or_unconfirmed_release(db, add_release, make_client, source):
    row = add_release(source=source, hidden=False)
    before = (row.source, row.is_hidden, row.is_blocked_by_copyrights, row.source_checked_at)
    client = make_client()

    with client.backoffice_scan({1}):
        with pytest.raises(SourceCheckDeferred):
            asyncio.run(client.get_release(1))

    db.refresh(row)
    assert (row.source, row.is_hidden, row.is_blocked_by_copyrights, row.source_checked_at) == before
    assert row.title == "Confirmed title"
    client.backoffice.get_torrents_for_release.assert_not_awaited()
    assert client.source_check_errors


def test_unknown_release_is_not_created_from_nova_when_public_check_is_deferred(db, make_client):
    client = make_client()

    with client.backoffice_scan(set()):
        with pytest.raises(SourceCheckDeferred):
            asyncio.run(client.get_release(1))

    assert db.get(Release, 1) is None
    client.backoffice.get_torrents_for_release.assert_not_awaited()


def test_deferred_source_check_does_not_block_known_nova_or_complete_baseline(
    db, add_release, add_archive, make_client, configure_job,
):
    add_release()
    add_archive(baseline=True)
    unknown = add_release(release_id=2, source=None, hidden=False)
    site = add_release(release_id=3, source="site", hidden=False)
    client = make_client(release_ids=(1, 2, 3))
    run = configure_job(client)

    with pytest.raises(JobPartialFailure):
        asyncio.run(run.job.run_backoffice_sync(db, 1, {}))

    client.public.catalog_releases.assert_awaited_once()
    assert [item["release_id"] for item in run.processed] == [1]
    client.backoffice.get_torrents_for_release.assert_awaited_once_with(1)
    client.public.get_release.assert_awaited_once_with(1)
    assert set(client.source_check_errors) == {1, 2, 3}
    assert set(client.source_check_errors.values()) == {"RuntimeError"}
    db.refresh(unknown)
    assert unknown.source is None and unknown.source_checked_at is None
    db.refresh(site)
    assert site.source == "site" and site.source_checked_at == CONFIRMED_AT
    failures = db.scalars(select(JobSyncFailure).where(JobSyncFailure.job_id == 1)).all()
    assert len(failures) == 1
    assert failures[0].scope == "discovery"
    assert failures[0].code == "source_check_deferred"
    assert get_setting_value(db, "backoffice_baseline_started_at")
    assert not get_setting_value(db, "backoffice_baseline_completed_at")


def test_source_returns_to_site_when_public_api_recovers(
    db, add_release, add_archive, make_client, configure_job,
):
    row = add_release()
    archive = add_archive(baseline=True)
    offline = make_client()
    run = configure_job(offline)
    asyncio.run(run.job.run_backoffice_sync(db, 1, {}))
    db.refresh(row)
    assert row.source == "backoffice" and row.source_checked_at == CONFIRMED_AT

    # Reuse the client so the second scan must reset the first scan's circuit
    # breaker rather than relying on a newly constructed client.
    restored = offline
    restored.test_admin_payloads[1].update(is_hidden=False, is_blocked_by_copyrights=False)
    restored.public.get_release.reset_mock()
    restored.public.get_release.side_effect = None
    restored.public.get_release.return_value = {
        "id": 1, "alias": "release-1", "name": {"main": "Restored site title"},
        "is_hidden": False, "is_blocked_by_copyrights": False,
    }
    restored.backoffice.get_torrents_for_release.reset_mock()
    run = configure_job(restored)
    asyncio.run(run.job.run_backoffice_sync(db, 2, {}))

    db.refresh(row)
    assert row.source == "site"
    assert row.source_checked_at > CONFIRMED_AT
    assert row.is_hidden is False and row.is_blocked_by_copyrights is False
    assert row.title == "Restored site title"
    restored.public.get_release.assert_awaited_once_with(1)
    restored.public.get_torrents_for_release.assert_awaited_once_with(1)
    restored.backoffice.get_torrents_for_release.assert_not_awaited()
    assert not restored.source_check_errors
    db.refresh(archive)
    assert archive.info_hash == TORRENT_HASH
    assert archive.backoffice_trackers and archive.notification_baseline and archive.api_present


def test_nova_discovery_imports_new_confirmed_release_without_public_catalog(db, make_client, configure_job):
    client = make_client()
    client.public.get_release.side_effect = AniLibriaNotFoundError()
    run = configure_job(client)

    asyncio.run(run.job.run_backoffice_sync(db, 1, {}))

    client.public.catalog_releases.assert_awaited_once()
    assert [item["release_id"] for item in run.processed] == [1]
    row = db.get(Release, 1)
    assert row.source == "backoffice"
    assert row.source_checked_at is not None
    assert run.processed[0]["payload"]["_notification_baseline"] is True
    assert get_setting_value(db, "backoffice_baseline_completed_at")


@pytest.mark.parametrize("copyright_value,expected_processed", [(False, False), (None, True), ({"invalid": True}, True)])
def test_nova_index_only_excludes_releases_with_both_confirmed_false_flags(
    db, make_client, configure_job, copyright_value, expected_processed,
):
    client = make_client()
    fields = [{"attribute": "is_hidden", "component": "boolean-field", "value": False}]
    if copyright_value is not None:
        fields.append({"attribute": "is_blocked_by_copyrights", "component": "boolean-field", "value": copyright_value})
    fields.append({"attribute": "is_blocked_by_geo", "component": "multiselect-field", "value": []})
    client.backoffice.list_resources.return_value[0]["fields"] = fields
    client.public.get_release.side_effect = AniLibriaNotFoundError()
    run = configure_job(client)

    asyncio.run(run.job.run_backoffice_sync(db, 1, {}))

    assert client.public.catalog_releases.await_count == int(expected_processed)
    assert bool(run.processed) is expected_processed
    assert client.public.get_release.await_count == int(expected_processed)
    assert client.backoffice.get_release.await_count == int(expected_processed)
    assert (db.get(Release, 1) is not None) is expected_processed


def test_stale_source_from_scan_does_not_allow_nova_fallback_outside_scan(db, add_release, make_client):
    row = add_release()
    client = make_client()

    with client.backoffice_scan({1}):
        payload = asyncio.run(client.get_release(1))
        assert payload["_source_confirmed"] is False

    client.backoffice.get_release.reset_mock()
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(client.get_release(1))

    assert type(exc.value) is RuntimeError
    client.backoffice.get_release.assert_not_awaited()
    db.refresh(row)
    assert row.source == "backoffice" and row.source_checked_at == CONFIRMED_AT


def test_public_error_keeps_source_confirmed_by_another_session_during_request(db, add_release, make_client):
    row = add_release()
    client = make_client()
    refreshed_at = datetime(2026, 10, 23, 12, 0)

    async def fail_after_source_update(release_id):
        with Session(db.get_bind()) as other:
            fresh = other.get(Release, release_id)
            fresh.source = "site"
            fresh.source_checked_at = refreshed_at
            fresh.is_hidden = False
            fresh.is_blocked_by_copyrights = False
            other.commit()
        raise RuntimeError("Public API unavailable")

    client.public.get_release.side_effect = fail_after_source_update
    with client.backoffice_scan({1}):
        with pytest.raises(SourceCheckDeferred):
            asyncio.run(client.get_release(1))

    db.refresh(row)
    assert row.source == "site" and row.source_checked_at == refreshed_at
    assert row.is_hidden is False and row.is_blocked_by_copyrights is False
    client.backoffice.get_torrents_for_release.assert_not_awaited()


@pytest.mark.parametrize("source", ["site", None])
def test_nova_error_is_not_hidden_by_deferred_public_check(db, add_release, make_client, source):
    row = add_release(source=source, hidden=False)
    client = make_client()
    nova_error = BackofficeError("Backoffice: некорректная карточка релиза")
    client.backoffice.get_release.side_effect = nova_error

    with client.backoffice_scan({1}):
        with pytest.raises(BackofficeError) as exc:
            asyncio.run(client.get_release(1))

    assert exc.value is nova_error
    db.refresh(row)
    assert row.source == source
    assert row.source_checked_at == (CONFIRMED_AT if source else None)
    client.backoffice.get_torrents_for_release.assert_not_awaited()


def test_job_outcome_renders_discovery_note_without_a_release_prefix():
    templates = Path(__file__).resolve().parents[1] / "app" / "templates"
    environment = Environment(loader=FileSystemLoader(templates), autoescape=True)
    message = "Проверка сайта недоступна; сохранены последние подтверждённые источники."
    job = SimpleNamespace(id=1, status="success", result_json={
        "discovery_complete": True,
        "scan_complete": True,
        "notes": [{"code": "source_checks_deferred", "message": message}],
    })

    html = environment.get_template("partials/job_outcome.html").render(selected_job=job, failure_page=None)

    assert f"<li>{message}</li>" in html
    assert "Релиз :" not in html
    assert "Релиз None" not in html


def test_sparse_geo_index_failure_preserves_initial_baseline_for_delayed_retry(
    db, make_client, configure_job,
):
    from app.services.sync_outcomes import build_retry_params

    db.add(Setting(key="backoffice_baseline_completed_at", value="global-baseline-completed"))
    db.commit()
    client = make_client()
    # The real Nova index can omit eligibility fields even though its detail
    # proves that this is an initial import of the newly supported geo scope.
    assert client.backoffice.list_resources.return_value == [{"id": {"value": 1}, "fields": []}]
    client.test_admin_payloads[1].update(
        is_hidden=False, is_blocked_by_copyrights=False, is_blocked_by_geo=True,
    )
    client.public.get_release.side_effect = AniLibriaNotFoundError()
    actual_baselines = []

    async def fail_torrent_list(release_id):
        actual_baselines.append(client._releases[release_id].get("_notification_baseline"))
        raise BackofficeError("Backoffice: полный список торрентов временно недоступен")

    client.backoffice.get_torrents_for_release.side_effect = fail_torrent_list
    run = configure_job(client)

    with pytest.raises(JobPartialFailure):
        asyncio.run(run.job.run_backoffice_sync(db, 1, {}))

    assert actual_baselines == [True]
    failure = db.scalar(select(JobSyncFailure).where(JobSyncFailure.job_id == 1))
    assert failure.retry_params["notification_baseline"] is True
    assert not get_setting_value(db, "backoffice_geo_baseline_completed_at")

    # A later successful scan may close the geo baseline before an explicit
    # retry of this failure; its saved provenance must still suppress notices.
    db.get(Job, 1).status = "failed"
    db.add(Setting(key="backoffice_geo_baseline_completed_at", value="later-geo-baseline-completed"))
    db.commit()
    targets = build_retry_params(db, 1)["targets"]
    assert len(targets) == 1 and targets[0]["notification_baseline"] is True
