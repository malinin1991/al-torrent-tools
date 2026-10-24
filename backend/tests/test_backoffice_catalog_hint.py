"""Optional public catalog pruning never gates Nova discovery or writes sources."""

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
from app.db.models import Job, JobLog, JobSyncFailure, Release, Setting, TorrentArchive
from app.jobs import backoffice_sync as job
from app.providers.anilibria.sources import BackofficeReleaseSkipped, ReleaseSourceClient, SourceCheckDeferred
from app.services.job_runner import JobStopRequested
from app.services.runtime_settings import get_setting_value
from app.services.sync_outcomes import JobPartialFailure


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(*args, **kwargs):
    return "JSON"


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            Job(id=1, type="backoffice_sync", status="running", params_json={}),
            Setting(key="scrape_pause_sec", value="0"),
        ])
        session.commit()
        yield session
    engine.dispose()


def catalog_pages(items, *, per_page=50):
    total = len(items)
    pages = max(1, (total + per_page - 1) // per_page)
    return [{
        "data": items[(page - 1) * per_page:page * per_page],
        "meta": {"pagination": {
            "total": total, "total_pages": pages, "current_page": page,
            "per_page": per_page,
            "count": len(items[(page - 1) * per_page:page * per_page]),
        }},
    } for page in range(1, pages + 1)]


@pytest.fixture
def make_run(db, monkeypatch):
    def make(*, ids=(1, 2), pages=None, watched=(), processor=None):
        resources = [{"id": {"value": rid}, "fields": []} for rid in ids]

        async def read_index(resource, *, on_page):
            assert resource == "anime-releases"
            on_page(1)
            return resources

        public = SimpleNamespace(
            catalog_releases=AsyncMock(side_effect=pages if pages is not None else RuntimeError("catalog unavailable")),
        )
        admin = SimpleNamespace(list_resources=AsyncMock(side_effect=read_index))
        client = ReleaseSourceClient(public, admin, db)
        for rid in watched:
            db.add(Release(release_id=rid, source="backoffice", is_hidden=True))
        db.commit()
        processor = processor or AsyncMock(return_value={"updated": 1})
        monkeypatch.setattr(job, "build_anilibria_client", lambda _: client)
        monkeypatch.setattr(job, "is_stop_requested", lambda *args: False)
        monkeypatch.setattr(job, "TorrentProcessor", lambda **kwargs: SimpleNamespace(process_release=processor))
        return SimpleNamespace(client=client, processor=processor)

    return make


def log_messages(db, level=None):
    query = select(JobLog.message).where(JobLog.job_id == 1).order_by(JobLog.id)
    if level is not None:
        query = query.where(JobLog.level == level)
    return list(db.scalars(query))


def test_complete_real_public_shape_reduces_2425_candidates_to_576(db, make_run):
    # Real public API omits is_hidden entirely, including with include=is_hidden.
    items = [{"id": rid, "is_blocked_by_copyrights": False} for rid in range(1, 1850)]
    run = make_run(ids=range(1, 2426), pages=catalog_pages(items))

    asyncio.run(job.run_backoffice_sync(db, 1, {}))

    assert [call.kwargs["release_id"] for call in run.processor.await_args_list] == list(range(1850, 2426))
    assert run.client.public.catalog_releases.await_count == 37
    for page, call in enumerate(run.client.public.catalog_releases.await_args_list, 1):
        assert call.kwargs == {
            "page": page, "limit": 50,
            "include": ["id", "is_hidden", "is_blocked_by_copyrights"],
        }
    messages = log_messages(db, "info")
    assert any("страница 10/37" in message for message in messages)
    assert any("страница 20/37" in message for message in messages)
    assert any("страница 30/37" in message for message in messages)
    assert any("кандидатов=576" in message for message in messages)
    assert db.get(Job, 1).result_json["issue_count"] == 0


@pytest.mark.parametrize("item,pruned", [
    ({"id": 1, "is_blocked_by_copyrights": False}, True),
    ({"id": 1, "is_hidden": False, "is_blocked_by_copyrights": False}, True),
    ({"id": 1, "is_hidden": None, "is_blocked_by_copyrights": False}, False),
    ({"id": 1, "is_hidden": "invalid", "is_blocked_by_copyrights": False}, False),
    ({"id": 1, "is_hidden": True, "is_blocked_by_copyrights": False}, False),
    ({"id": 1, "is_hidden": False}, False),
    ({"id": 1, "is_blocked_by_copyrights": None}, False),
    ({"id": 1, "is_blocked_by_copyrights": "invalid"}, False),
    ({"id": 1, "is_blocked_by_copyrights": True}, False),
])
def test_only_confirmed_ordinary_public_shapes_are_pruned(db, make_run, item, pruned):
    run = make_run(ids=(1,), pages=catalog_pages([item]))

    asyncio.run(job.run_backoffice_sync(db, 1, {}))

    assert run.processor.await_count == (0 if pruned else 1)


@pytest.mark.parametrize("pages", [
    [RuntimeError("private upstream response")],
    [{"data": [{"id": 1, "is_blocked_by_copyrights": False}]}],
    [catalog_pages([{"id": 1}, {"id": 2}], per_page=1)[0], {"data": []}],
    [catalog_pages([{"id": 1}, {"id": 2}], per_page=1)[0], RuntimeError("private upstream response")],
])
def test_outage_or_incomplete_catalog_discards_all_pruning_without_source_failure(db, make_run, pages):
    # First-page items are confirmed visible so a later error must undo pruning.
    if isinstance(pages[0], dict) and "meta" in pages[0]:
        pages[0]["data"][0]["is_blocked_by_copyrights"] = False
    run = make_run(pages=pages)

    asyncio.run(job.run_backoffice_sync(db, 1, {}))

    assert [call.kwargs["release_id"] for call in run.processor.await_args_list] == [1, 2]
    assert not run.client.source_check_errors
    assert run.client._source_check_error is None
    assert db.get(Job, 1).result_json["issue_count"] == 0
    assert db.scalar(select(JobSyncFailure.id)) is None
    assert get_setting_value(db, "backoffice_baseline_completed_at")
    warnings = log_messages(db, "warning")
    assert any("полный обход Nova" in message for message in warnings)
    assert all("private upstream response" not in message for message in warnings)


def test_catalog_total_timeout_cancels_request_and_continues_nova(db, make_run, monkeypatch):
    run = make_run()
    cancelled = []
    first = catalog_pages([
        {"id": 1, "is_blocked_by_copyrights": False},
        {"id": 2, "is_blocked_by_copyrights": False},
    ], per_page=1)[0]

    async def blocked_catalog(*, page, **kwargs):
        if page == 1:
            return first
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    run.client.public.catalog_releases.side_effect = blocked_catalog
    monkeypatch.setattr(job, "_CATALOG_HINT_TIMEOUT_SEC", 0.01)
    asyncio.run(asyncio.wait_for(job.run_backoffice_sync(db, 1, {}), timeout=1))

    assert cancelled == [True]
    assert run.processor.await_count == 2
    assert run.client.public.catalog_releases.await_count == 2
    assert any("TimeoutError" in message for message in log_messages(db, "warning"))
    assert db.get(Job, 1).result_json["issue_count"] == 0


def test_watched_releases_are_not_pruned_by_public_catalog(db, make_run):
    run = make_run(watched=(1,), pages=catalog_pages([
        {"id": 1, "is_blocked_by_copyrights": False},
        {"id": 2, "is_blocked_by_copyrights": False},
    ]))

    asyncio.run(job.run_backoffice_sync(db, 1, {}))

    run.processor.assert_awaited_once_with(release_id=1, refresh_qb_meta=True)


@pytest.mark.parametrize("ids,watched", [((), ()), ((1,), (1,))])
def test_empty_or_only_watched_candidates_skip_public_catalog(db, make_run, ids, watched):
    run = make_run(ids=ids, watched=watched)

    asyncio.run(job.run_backoffice_sync(db, 1, {}))

    run.client.public.catalog_releases.assert_not_awaited()


def test_pruning_does_not_refresh_release_source_or_modify_archive(db, make_run):
    before = datetime(2026, 10, 20, 12, 0)
    row = Release(release_id=1, source="site", is_hidden=False, is_blocked_by_copyrights=False,
                  source_checked_at=before, updated_at=before, title="Unchanged")
    archive = TorrentArchive(release_id=1, torrent_id=10, info_hash="a" * 40,
                             file_path="unused.torrent", quality_json={"title": "Unchanged"}, api_present=True)
    db.add_all([row, archive])
    db.commit()
    run = make_run(ids=(1,), pages=catalog_pages([{"id": 1, "is_blocked_by_copyrights": False}]))

    asyncio.run(job.run_backoffice_sync(db, 1, {}))

    run.processor.assert_not_awaited()
    db.refresh(row)
    db.refresh(archive)
    assert (row.source, row.source_checked_at, row.updated_at, row.title) == ("site", before, before, "Unchanged")
    assert archive.api_present and not archive.superseded and not archive.backoffice_trackers
    assert archive.quality_json == {"title": "Unchanged"}


def test_stop_during_optional_catalog_propagates_without_processing(db, make_run, monkeypatch):
    run = make_run()
    stopped = False

    async def catalog_then_stop(**kwargs):
        nonlocal stopped
        stopped = True
        return catalog_pages([{"id": 1, "is_blocked_by_copyrights": False}])[0]

    run.client.public.catalog_releases.side_effect = catalog_then_stop
    monkeypatch.setattr(job, "is_stop_requested", lambda *args: stopped)

    with pytest.raises(JobStopRequested):
        asyncio.run(job.run_backoffice_sync(db, 1, {}))

    run.processor.assert_not_awaited()
    assert not log_messages(db, "warning")
    assert db.scalar(select(JobSyncFailure.id)) is None


def test_info_progress_is_visible_before_processing_and_counts_every_outcome(db, make_run, monkeypatch):
    outcomes = {1: BackofficeReleaseSkipped(), 2: SourceCheckDeferred(), 3: RuntimeError("processing failed")}

    async def process(*, release_id, **kwargs):
        progress = [message for message in log_messages(db, "info") if "проверено " in message]
        assert progress
        assert f"проверено {release_id - 1}/3, текущий релиз={release_id}" in progress[-1]
        raise outcomes[release_id]

    run = make_run(ids=(1, 2, 3), processor=AsyncMock(side_effect=process))
    monkeypatch.setattr(job, "_PROGRESS_INTERVAL_SEC", 0)

    with pytest.raises(JobPartialFailure):
        asyncio.run(job.run_backoffice_sync(db, 1, {}))

    assert run.processor.await_count == 3
    messages = log_messages(db, "info")
    assert any("индекс Nova, страница 1" in message for message in messages)
    final = [message for message in messages if "проверено " in message][-1]
    assert "проверено 3/3" in final
    assert "пропущено=1, отложено=1, ошибок=1" in final
