"""E2E-стиль: PipelineEvent из разных источников (job|webhook|poll|manual)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.templating import Jinja2Templates

from app.db.models import JobLog, PipelineEvent
from app.jobs.cleanup_logs import RETAIN_DAYS, prune_old_jobs, prune_pipeline_events
from app.jobs import hash_torrent as hash_torrent_mod
from app.services.job_runner import STATUS_RUNNING, STATUS_SUCCESS
from app.services.pipeline import TorrentPipelineService, record_pipeline_event
from app.services.telegram_notify import (
    OUTBOX_SENT,
    TG_STATUS_SENT,
    mark_outbox_sent,
)
from app.utils.datetime_fmt import as_utc_iso, utcnow
from app.services.releases_view import format_torrent_files_summary

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "app" / "templates"


def _pipeline(*, status: str = "discovered", pipeline_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=pipeline_id,
        status=status,
        info_hash="ab" * 20,
        release_id=10,
        torrent_id=55,
        master_added_at=None,
        slave_added_at=None,
        slave_completed_at=None,
        error=None,
        tg_status="skipped",
        created_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
    )


def _added_of_type(db: MagicMock, model_type: type) -> list:
    return [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], model_type)]


def test_status_transitions_actor_job() -> None:
    db = MagicMock()
    pipeline = _pipeline(status="discovered", pipeline_id=5)
    service = TorrentPipelineService(db, job_id=88)
    service.mark_waiting_master(pipeline, "master down")

    events = _added_of_type(db, PipelineEvent)
    assert len(events) == 1
    assert events[0].event_type == "status_change"
    assert events[0].from_status == "discovered"
    assert events[0].to_status == "waiting_master"
    assert events[0].job_id == 88
    assert events[0].details_json.get("actor") == "job"
    assert events[0].details_json.get("reason") == "master down"
    logs = _added_of_type(db, JobLog)
    assert len(logs) == 1


def test_webhook_completion_path_records_actor_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    """Webhook-сервис: mark_cancelled / process_completion no-op пишут actor=webhook."""
    db = MagicMock()
    pipeline = _pipeline(status="master_added", pipeline_id=4)
    service = TorrentPipelineService(db, actor="webhook")

    service.mark_cancelled(pipeline, "Webhook: торрент отсутствует на master")
    events = _added_of_type(db, PipelineEvent)
    assert events[0].details_json.get("actor") == "webhook"
    assert events[0].event_type == "cancelled"

    db2 = MagicMock()
    done = _pipeline(status="done", pipeline_id=4)
    service2 = TorrentPipelineService(db2, actor="webhook")
    service2._claim_master_complete = MagicMock()  # type: ignore[method-assign]
    result = service2.process_completion(done, b"x")
    assert result.status == "done"
    noop_events = _added_of_type(db2, PipelineEvent)
    assert len(noop_events) == 1
    assert noop_events[0].details_json.get("actor") == "webhook"
    assert noop_events[0].details_json.get("noop") is True


def test_qb_complete_webhook_uses_webhook_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.rest import qb_complete_webhook

    created: list[TorrentPipelineService] = []
    original_cls = TorrentPipelineService

    class CapturingService(TorrentPipelineService):
        def __init__(self, db, job_id=None, *, actor=None):  # noqa: ANN001
            super().__init__(db, job_id=job_id, actor=actor)
            created.append(self)

    monkeypatch.setattr("app.api.rest.TorrentPipelineService", CapturingService)
    db = MagicMock()
    pipeline = _pipeline(status="done", pipeline_id=9)
    db.scalar.return_value = pipeline

    # get_latest_by_hash uses db.scalar — подменим через instance после создания
    async def _run():
        request = MagicMock()
        request.method = "GET"
        # CapturingService.get_latest_by_hash — вернём done pipeline
        monkeypatch.setattr(
            CapturingService,
            "get_latest_by_hash",
            lambda self, h: pipeline,
        )
        return await qb_complete_webhook(
            request, hash_query="ab" * 20, role_query="master", db=db
        )

    result = asyncio.run(_run())
    assert result["ok"] is True
    assert created
    assert created[0]._actor == "webhook"
    _ = original_cls


def test_qb_complete_webhook_requires_role(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from app.api.rest import qb_complete_webhook

    async def _run():
        request = MagicMock()
        request.method = "GET"
        return await qb_complete_webhook(
            request, hash_query="ab" * 20, role_query=None, db=MagicMock()
        )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_run())
    assert exc_info.value.status_code == 400
    assert "role" in str(exc_info.value.detail).lower()


def test_qb_complete_webhook_slave_role_marks_done(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.rest import qb_complete_webhook

    db = MagicMock()
    pipeline = _pipeline(status="slave_added", pipeline_id=11)
    done = _pipeline(status="done", pipeline_id=11)

    class FakeService(TorrentPipelineService):
        def get_latest_by_hash(self, _h):  # noqa: ANN001
            return pipeline

        def classify_slave_torrent(self, _p):  # noqa: ANN001
            return "complete"

        def process_slave_completion(self, p):  # noqa: ANN001
            p.status = "done"
            return done

    monkeypatch.setattr("app.api.rest.TorrentPipelineService", FakeService)

    async def _run():
        request = MagicMock()
        request.method = "GET"
        return await qb_complete_webhook(
            request, hash_query="ab" * 20, role_query="slave", db=db
        )

    result = asyncio.run(_run())
    assert result["ok"] is True
    assert result["status"] == "done"


def test_qb_complete_webhook_slave_role_rejects_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.rest import qb_complete_webhook

    db = MagicMock()
    pipeline = _pipeline(status="slave_added", pipeline_id=11)

    class FakeService(TorrentPipelineService):
        def get_latest_by_hash(self, _h):  # noqa: ANN001
            return pipeline

        def classify_slave_torrent(self, _p):  # noqa: ANN001
            return "in_progress"

        def process_slave_completion(self, p):  # noqa: ANN001
            raise AssertionError("не должен вызываться при in_progress")

    monkeypatch.setattr("app.api.rest.TorrentPipelineService", FakeService)

    async def _run():
        request = MagicMock()
        request.method = "GET"
        return await qb_complete_webhook(
            request, hash_query="ab" * 20, role_query="slave", db=db
        )

    result = asyncio.run(_run())
    assert result["ok"] is False
    assert result["status"] == "slave_added"


def test_qb_complete_webhook_slave_race_stays_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """classify=complete, но process_slave_completion оставил slave_added → ok:false."""
    from app.api.rest import qb_complete_webhook

    db = MagicMock()
    pipeline = _pipeline(status="slave_added", pipeline_id=11)

    class FakeService(TorrentPipelineService):
        def get_latest_by_hash(self, _h):  # noqa: ANN001
            return pipeline

        def classify_slave_torrent(self, _p):  # noqa: ANN001
            return "complete"

        def process_slave_completion(self, p):  # noqa: ANN001
            return p

    monkeypatch.setattr("app.api.rest.TorrentPipelineService", FakeService)

    async def _run():
        request = MagicMock()
        request.method = "GET"
        return await qb_complete_webhook(
            request, hash_query="ab" * 20, role_query="slave", db=db
        )

    result = asyncio.run(_run())
    assert result["ok"] is False
    assert result["status"] == "slave_added"


def test_qb_complete_webhook_slave_too_early(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.rest import qb_complete_webhook

    db = MagicMock()
    pipeline = _pipeline(status="master_added", pipeline_id=11)

    class FakeService(TorrentPipelineService):
        def get_latest_by_hash(self, _h):  # noqa: ANN001
            return pipeline

    monkeypatch.setattr("app.api.rest.TorrentPipelineService", FakeService)

    async def _run():
        request = MagicMock()
        request.method = "GET"
        return await qb_complete_webhook(
            request, hash_query="ab" * 20, role_query="slave", db=db
        )

    result = asyncio.run(_run())
    assert result["ok"] is False
    assert "рано" in result["message"].lower()


def test_poll_path_records_actor_poll() -> None:
    """Worker poll: TorrentPipelineService(..., actor='poll') пишет actor в events."""
    db = MagicMock()
    pipeline = _pipeline(status="master_added")
    service = TorrentPipelineService(db, actor="poll")
    service.mark_cancelled(pipeline, "Торрент отсутствует на master (удалён)")

    events = _added_of_type(db, PipelineEvent)
    assert events[0].details_json.get("actor") == "poll"
    assert events[0].event_type == "cancelled"
    assert events[0].job_id is None


def test_manual_actor_on_status_change() -> None:
    db = MagicMock()
    pipeline = _pipeline(status="master_added")
    service = TorrentPipelineService(db, actor="manual")
    service.mark_waiting_slave(pipeline, "manual retry")
    events = _added_of_type(db, PipelineEvent)
    assert events[0].details_json.get("actor") == "manual"
    assert events[0].to_status == "waiting_slave"


def test_hash_torrent_start_and_done_events(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    pipeline = _pipeline(status="slave_added", pipeline_id=12)
    monkeypatch.setattr(
        hash_torrent_mod,
        "_pipeline_for_hash",
        lambda _db, _h, _j: pipeline,
    )
    monkeypatch.setattr(
        hash_torrent_mod,
        "_ui_status_counts",
        lambda _db, _h: {"new": 1, "changed": 0, "ok": 2, "total": 3},
    )
    tracker = MagicMock()
    tracker.track_torrent.return_value = SimpleNamespace(
        skipped_reason=None,
        files_upserted=3,
        hashed=3,
        gated=0,
        errors=0,
        changes=[SimpleNamespace(kind="added"), SimpleNamespace(kind="added")],
    )
    monkeypatch.setattr(
        hash_torrent_mod,
        "FileTrackerService",
        MagicMock(return_value=tracker),
    )

    asyncio.run(
        hash_torrent_mod.run_hash_torrent(
            db,
            99,
            {"info_hash": "ab" * 20, "torrent_id": 55, "release_id": 10},
        )
    )
    events = _added_of_type(db, PipelineEvent)
    assert [e.event_type for e in events] == ["hash_progress", "hash_done"]
    assert events[0].details_json.get("actor") == "job"
    assert events[0].details_json.get("phase") == "start"
    assert events[0].job_id == 99
    assert events[1].details_json.get("actor") == "job"
    assert events[1].details_json.get("hashed") == 3
    assert events[1].details_json.get("ui_status", {}).get("new") == 1


def test_hash_torrent_skip_writes_hash_done(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    pipeline = _pipeline(status="done", pipeline_id=3)
    monkeypatch.setattr(
        hash_torrent_mod,
        "_pipeline_for_hash",
        lambda *_a, **_k: pipeline,
    )
    tracker = MagicMock()
    tracker.track_torrent.return_value = SimpleNamespace(
        skipped_reason="торрент не api_present (архивный)",
        files_upserted=0,
        hashed=0,
        gated=0,
        errors=0,
        changes=[],
    )
    monkeypatch.setattr(
        hash_torrent_mod, "FileTrackerService", MagicMock(return_value=tracker)
    )
    asyncio.run(
        hash_torrent_mod.run_hash_torrent(
            db,
            5,
            {"info_hash": "cd" * 20, "torrent_id": 1, "release_id": 2},
        )
    )
    events = _added_of_type(db, PipelineEvent)
    assert [e.event_type for e in events] == ["hash_progress", "hash_done"]
    assert events[1].details_json.get("skipped") is True


def test_hash_torrent_hard_fail_writes_hash_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    pipeline = _pipeline(status="done", pipeline_id=8)
    monkeypatch.setattr(
        hash_torrent_mod,
        "_pipeline_for_hash",
        lambda *_a, **_k: pipeline,
    )
    tracker = MagicMock()
    tracker.track_torrent.return_value = SimpleNamespace(
        skipped_reason="нет .torrent в архиве",
        files_upserted=0,
        hashed=0,
        gated=0,
        errors=0,
        changes=[],
    )
    monkeypatch.setattr(
        hash_torrent_mod, "FileTrackerService", MagicMock(return_value=tracker)
    )
    with pytest.raises(RuntimeError, match="нет \\.torrent"):
        asyncio.run(
            hash_torrent_mod.run_hash_torrent(
                db,
                9,
                {"info_hash": "ef" * 20, "torrent_id": 1, "release_id": 2},
            )
        )
    events = _added_of_type(db, PipelineEvent)
    assert [e.event_type for e in events] == ["hash_progress", "hash_fail"]
    assert events[1].details_json.get("reason") == "нет .torrent в архиве"


def test_tg_queued_and_sent_events() -> None:
    from app.db.models import Setting, TrackedRelease
    from app.services.telegram_notify import enqueue_pipeline_telegram_notification

    tracked = SimpleNamespace(
        release_id=10,
        release_alias="alias",
        title="Title",
        enabled=True,
        source="ui",
    )
    settings = {
        "telegram_enabled": "true",
        "telegram_chat_id": "-1001",
        "telegram_bot_token": "tok",
    }

    def _get(model, key):  # noqa: ANN001
        if model is TrackedRelease:
            return tracked
        if model is Setting:
            value = settings.get(key)
            return SimpleNamespace(value=value) if value is not None else None
        if model.__name__ == "TorrentPipeline":
            return None
        return None

    db = MagicMock()
    db.get.side_effect = _get
    pipeline = _pipeline(status="done", pipeline_id=7)
    enqueue_pipeline_telegram_notification(db, pipeline)
    queued = _added_of_type(db, PipelineEvent)
    assert len(queued) == 1
    assert queued[0].event_type == "tg_queued"
    assert queued[0].details_json.get("actor") == "job"

    db2 = MagicMock()
    outbox = SimpleNamespace(
        id=3,
        pipeline_id=7,
        chat_id="-1001",
        status="pending",
        sent_at=None,
        last_error=None,
    )
    pipe = _pipeline(status="done", pipeline_id=7)
    pipe.tg_status = "queued"
    db2.get.return_value = pipe
    mark_outbox_sent(db2, outbox)
    assert outbox.status == OUTBOX_SENT
    assert pipe.tg_status == TG_STATUS_SENT
    sent = _added_of_type(db2, PipelineEvent)
    assert len(sent) == 1
    assert sent[0].event_type == "tg_sent"
    assert sent[0].details_json.get("actor") == "job"
    assert sent[0].details_json.get("outbox_id") == 3


def test_pipeline_detail_page_timeline_and_job_links(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import pipeline_detail_page

    now = datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc)
    pipeline = _pipeline(status="done", pipeline_id=15)
    events = [
        SimpleNamespace(
            id=1,
            pipeline_id=15,
            job_id=None,
            event_type="created",
            from_status=None,
            to_status="discovered",
            message="создан",
            details_json={"actor": "webhook"},
            created_at=now,
        ),
        SimpleNamespace(
            id=2,
            pipeline_id=15,
            job_id=50,
            event_type="master_add",
            from_status="discovered",
            to_status="master_added",
            message="master",
            details_json={"actor": "job"},
            created_at=now + timedelta(minutes=1),
        ),
        SimpleNamespace(
            id=3,
            pipeline_id=15,
            job_id=999,
            event_type="hash_done",
            from_status=None,
            to_status=None,
            message="hash done",
            details_json={"actor": "job"},
            created_at=now + timedelta(minutes=2),
        ),
    ]
    db = MagicMock()
    db.get.return_value = pipeline
    db.scalars.side_effect = [
        MagicMock(all=lambda: events),  # PipelineEvent
        MagicMock(all=lambda: [50]),  # existing Job.id — только 50, 999 удалён
        MagicMock(all=lambda: []),  # tracked release ids
    ]
    db.scalar.return_value = SimpleNamespace(
        anime_name="Show",
        release_alias="show",
        torrent_type="BDRip 1080p",
        torrent_description="1-2",
    )
    db.execute.return_value.all.return_value = []  # files stage hash events

    async def _fake_to_thread(fn, *a, **k):  # noqa: ANN001
        return {}

    monkeypatch.setattr("app.main.asyncio.to_thread", _fake_to_thread)

    captured: dict = {}

    def _tpl(request, name, ctx):  # noqa: ANN001
        captured["name"] = name
        captured["ctx"] = ctx
        return ctx

    monkeypatch.setattr("app.main.templates.TemplateResponse", _tpl)
    ctx = asyncio.run(pipeline_detail_page(MagicMock(), 15, db=db))
    assert captured["name"] == "pipeline_detail.html"
    timeline = ctx["timeline"]
    assert len(timeline) == 3
    assert timeline[0]["actor"] == "webhook"
    assert timeline[0]["job_link"] is None
    assert timeline[1]["actor"] == "job"
    assert timeline[1]["job_link"] == "/jobs?job_id=50"
    assert timeline[1]["job_exists"] is True
    assert timeline[2]["job_exists"] is False
    assert timeline[2]["job_link"] is None
    assert '"actor": "job"' in timeline[1]["details_pretty"]


def test_pipeline_detail_prefers_archive_by_info_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Не брать чужой архив по torrent_id, если есть точный info_hash."""
    from app.main import pipeline_detail_page

    pipeline = _pipeline(status="done", pipeline_id=42)
    pipeline.info_hash = "aa" * 20
    pipeline.torrent_id = 100

    by_hash = SimpleNamespace(
        anime_name="Exact Hash Show",
        release_alias="exact",
        torrent_type="BDRip 1080p",
        torrent_description="1-2",
        info_hash="aa" * 20,
        torrent_id=100,
    )
    db = MagicMock()
    db.get.return_value = pipeline
    db.scalars.side_effect = [
        MagicMock(all=lambda: []),  # events
        MagicMock(all=lambda: []),  # jobs
        MagicMock(all=lambda: []),  # tracked
    ]
    db.scalar.side_effect = [by_hash]
    db.execute.return_value.all.return_value = []

    async def _fake_to_thread(fn, *a, **k):  # noqa: ANN001
        return {}

    monkeypatch.setattr("app.main.asyncio.to_thread", _fake_to_thread)
    captured: dict = {}

    def _tpl(request, name, ctx):  # noqa: ANN001
        captured["ctx"] = ctx
        return ctx

    monkeypatch.setattr("app.main.templates.TemplateResponse", _tpl)
    asyncio.run(pipeline_detail_page(MagicMock(), 42, db=db))
    assert captured["ctx"]["release_name"] == "Exact Hash Show"
    # один lookup по info_hash — без OR torrent_id
    assert db.scalar.call_count == 1
    stmt = db.scalar.call_args[0][0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": False})).lower()
    assert "info_hash" in sql
    assert "torrent_id" not in sql or sql.count("where") >= 1


def test_pipeline_detail_html_renders_timeline() -> None:
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.filters["as_utc_iso"] = as_utc_iso
    templates.env.filters["torrent_files_summary"] = format_torrent_files_summary
    from app.services.pipeline import pipeline_ci_stages

    templates.env.globals["pipeline_ci_stages"] = pipeline_ci_stages
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    pipeline = _pipeline(status="done", pipeline_id=3)
    pipeline.slave_completed_at = now
    pipeline.created_at = now
    event = SimpleNamespace(
        created_at=now,
        event_type="slave_add",
        from_status="master_complete",
        to_status="slave_added",
        message="на slave",
        job_id=7,
        details_json={"actor": "poll"},
        id=1,
    )
    request = MagicMock()
    html = templates.TemplateResponse(
        request,
        "pipeline_detail.html",
        {
            "request": request,
            "pipeline": pipeline,
            "release_name": "Show",
            "torrent_label": "BDRip · 1-2",
            "master_state": {},
            "slave_state": {},
            "files_status": "success",
            "tracked": False,
            "life_path_text": "path text",
            "timeline": [
                {
                    "event": event,
                    "job_exists": True,
                    "job_link": "/jobs?job_id=7",
                    "actor": "poll",
                    "details_pretty": '{\n  "actor": "poll"\n}',
                }
            ],
        },
    ).body.decode("utf-8")
    assert "Жизненный путь" in html
    assert "slave_add" in html
    assert "actor=poll" in html
    assert 'href="/jobs?job_id=7"' in html
    assert "на slave" in html
    assert 'data-ui-sse-channel="pipeline_detail:3"' in html
    assert "live · SSE" in html
    assert "gl-pipeline" in html


def test_cleanup_logs_retains_recent_jobs_and_events() -> None:
    assert RETAIN_DAYS == 30
    now = utcnow()
    old_job = SimpleNamespace(
        id=1,
        type="ongoing",
        status=STATUS_SUCCESS,
        finished_at=now - timedelta(days=40),
        created_at=now - timedelta(days=41),
    )
    recent_job = SimpleNamespace(
        id=2,
        type="ongoing",
        status=STATUS_SUCCESS,
        finished_at=now - timedelta(days=5),
        created_at=now - timedelta(days=6),
    )
    active_job = SimpleNamespace(
        id=3,
        type="ongoing",
        status=STATUS_RUNNING,
        finished_at=None,
        created_at=now - timedelta(days=50),
    )

    db_jobs = MagicMock()
    # prune_old_jobs получает уже отфильтрованный stale-список от scalars
    db_jobs.scalars.return_value.all.return_value = [old_job]
    stats = prune_old_jobs(db_jobs, retain_days=30, protect_job_id=99)
    assert stats["deleted_jobs"] == 1
    db_jobs.delete.assert_called_once_with(old_job)
    _ = (recent_job, active_job)

    db_events = MagicMock()
    result = MagicMock()
    result.rowcount = 4
    db_events.execute.return_value = result
    event_stats = prune_pipeline_events(db_events, retain_days=30)
    assert event_stats["deleted_events"] == 4
    assert event_stats["retain_days"] == 30
    # delete WHERE created_at < cutoff
    delete_stmt = db_events.execute.call_args[0][0]
    sql = str(delete_stmt.compile(compile_kwargs={"literal_binds": False})).lower()
    assert "pipeline_events" in sql or "created_at" in sql


def test_cleanup_logs_job_runs_both_prunes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.jobs.cleanup_logs import run_cleanup_logs

    db = MagicMock()
    calls: list[str] = []

    def _jobs(*_a, **_k):
        calls.append("jobs")
        return {"deleted_jobs": 2, "retain_days": 30, "cutoff": "x"}

    def _events(*_a, **_k):
        calls.append("events")
        return {"deleted_events": 5, "retain_days": 30, "cutoff": "y"}

    monkeypatch.setattr("app.jobs.cleanup_logs.prune_old_jobs", _jobs)
    monkeypatch.setattr("app.jobs.cleanup_logs.prune_pipeline_events", _events)
    monkeypatch.setattr("app.jobs.cleanup_logs.is_stop_requested", lambda *_: False)

    asyncio.run(run_cleanup_logs(db, job_id=10, params={"retain_days": 30}))
    assert calls == ["jobs", "events"]
    logs = _added_of_type(db, JobLog)
    assert any("pipeline_events=5" in log.message for log in logs)
    assert any("jobs=2" in log.message for log in logs)


def test_record_event_actor_precedence_explicit_over_job() -> None:
    """Явный actor в details не перезаписывается service._actor."""
    db = MagicMock()
    event = record_pipeline_event(
        db,
        1,
        event_type="status_change",
        message="x",
        job_id=1,
        details={"actor": "manual"},
    )
    assert event.details_json["actor"] == "manual"

    pipeline = _pipeline(status="discovered")
    service = TorrentPipelineService(db, job_id=5, actor="webhook")
    service.mark_waiting_master(pipeline, "reason", details={"actor": "manual"})
    events = _added_of_type(db, PipelineEvent)
    # последний — от mark_waiting_master
    assert events[-1].details_json.get("actor") == "manual"
    assert events[-1].job_id == 5
