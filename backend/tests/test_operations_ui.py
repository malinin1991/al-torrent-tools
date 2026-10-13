"""Real routes/templates on isolated data; no external delivery or job execution."""
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import rest
from app.api.operations_ui import create_operations_router
from app.db.base import Base
from app.db.models import Job, JobSyncFailure, Setting, TelegramOutbox
from app.db.session import get_db
from app.services.sync_outcomes import SyncOutcome
from app.services.ui_events import channel_token
from app.utils.datetime_fmt import as_utc_iso, utcnow


@compiles(JSONB, "sqlite")
def jsonb_sqlite(*args, **kwargs):
    return "JSON"


@pytest.fixture
def ui(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    db = Session(engine)
    templates = Jinja2Templates(directory=str(Path(__file__).parents[1] / "app/templates"))
    templates.env.filters["as_utc_iso"] = as_utc_iso
    application = FastAPI()
    application.include_router(rest.router)
    application.include_router(create_operations_router(templates, rest.job_runner))
    application.dependency_overrides[get_db] = lambda: db
    accepted = []
    scheduled = []
    def create(_db, job_type, params):
        accepted.append((job_type, params))
        job = Job(type=job_type, status="pending", params_json=params)
        db.add(job)
        db.commit()
        return job
    monkeypatch.setattr(rest.job_runner, "create_job", create)
    monkeypatch.setattr(rest.job_runner, "schedule_job", scheduled.append)
    with TestClient(application) as client:
        yield db, client, templates, accepted, scheduled
    db.close()
    engine.dispose()


def partial(db):
    job = Job(type="ongoing", status="partial", params_json={})
    db.add(job)
    db.commit()
    outcome = SyncOutcome(db, job.id)
    outcome.failure(release_id=42, torrent_id=77, info_hash="a" * 40, source="site",
                    operation="process", message="Проверка <ошибки>")
    return job, db.scalar(select(JobSyncFailure))


def test_retry_uses_only_server_failure_snapshot(ui):
    db, client, _, accepted, scheduled = ui
    parent, failure = partial(db)
    response = client.post(f"/api/jobs/{parent.id}/retry-failures", json={"failure_ids": [failure.id]})
    assert response.status_code == 202
    assert len(accepted) == len(scheduled) == 1
    job_type, params = accepted[0]
    assert job_type == "retry_sync_failures"
    assert params["retry_of"] == parent.id
    assert params["targets"][0]["info_hash"] == "a" * 40
    assert parent.status == "partial" and parent.params_json == {}
    assert response.json()["count"] == 1


def test_generic_job_route_rejects_forged_retry_snapshot(ui):
    _, client, _, accepted, _ = ui
    response = client.post("/api/jobs", json={"type": "retry_sync_failures", "params": {
        "retry_of": 1, "targets": [{"release_id": 999, "force_qb_load": True}],
    }})
    assert response.status_code == 422 and not accepted


def test_failure_pagination_and_ui_escape(ui):
    db, client, _, _, _ = ui
    parent, failure = partial(db)
    data = client.get(f"/api/jobs/{parent.id}/failures?limit=1").json()
    assert data["total"] == 1 and data["items"][0]["id"] == failure.id
    html = client.get(f"/jobs/{parent.id}/failures/live").text
    assert "&lt;ошибки&gt;" in html and "sync-failure-select" in html
    assert "<ошибки>" not in html
    assert client.get("/api/jobs/999999/failures").status_code == 404


def test_active_parent_and_foreign_failure_are_rejected(ui):
    db, client, _, accepted, _ = ui
    parent, failure = partial(db)
    parent.status = "running"
    db.commit()
    assert client.post(f"/api/jobs/{parent.id}/retry-failures", json={"failure_ids": [failure.id]}).status_code == 409
    parent.status = "partial"
    db.commit()
    assert client.post(f"/api/jobs/{parent.id}/retry-failures", json={"failure_ids": [failure.id + 100]}).status_code == 422
    assert not accepted


def test_job_result_is_exposed_without_unbounded_failure_list(ui):
    db, client, _, _, _ = ui
    parent, _ = partial(db)
    response = client.get(f"/api/jobs/{parent.id}")
    assert response.status_code == 200
    assert response.json()["result"]["issue_count"] == 1
    assert "failures" not in response.json()


def test_unknown_outbox_requires_ack_and_keeps_same_record(ui, monkeypatch):
    from app.services import telegram_outbox
    from types import SimpleNamespace
    db, client, _, _, _ = ui
    monkeypatch.setattr(telegram_outbox, "resolve_telegram_bot_settings", lambda *_: SimpleNamespace(enabled=True, token="isolated", bot_key="primary"))
    row = TelegramOutbox(bot_key="primary", chat_id="1", status="failed", attempts=2,
                         last_error_kind="delivery_unknown", dedupe_key="unique", payload_json={"text": "hidden payload"})
    db.add(row)
    db.commit()
    url = f"/api/telegram-outbox/{row.id}/retry"
    assert client.post(url, json={"bot_key": "primary"}).status_code == 422
    assert client.post(url, json={"bot_key": "primary", "acknowledge_unknown": True}).status_code == 200
    db.refresh(row)
    assert row.status == "pending" and row.attempts == 2 and row.dedupe_key == "unique"
    assert client.post(url, json={"acknowledge_unknown": True}).status_code == 409
    assert client.post(url, json={"acknowledge_unknown": "yes"}).status_code == 422


def test_outbox_html_and_api_do_not_expose_payload(ui):
    db, client, _, _, _ = ui
    row = TelegramOutbox(bot_key="hevc", chat_id="-1001", status="failed", attempts=1,
                         last_error="Ошибка <script>alert(1)</script>", last_error_kind="delivery_unknown",
                         payload_json={"text": "private-notification", "token": "should-not-appear"})
    db.add(row)
    db.commit()
    html = client.get("/telegram-outbox?bot_key=hevc").text
    assert "Доставка неизвестна" in html and "&lt;script&gt;" in html
    assert "private-notification" not in html and "should-not-appear" not in html
    data = client.get("/api/telegram-outbox?bot_key=hevc").json()
    assert data["items"][0]["delivery_unknown"] is True and "payload_json" not in data["items"][0]
    assert client.get("/api/telegram-outbox?bot_key=bogus").status_code == 422


def test_outbox_sse_changes_for_attempt_and_hevc_heartbeat(ui, monkeypatch):
    db, _, _, _, _ = ui
    monkeypatch.setattr("app.services.ui_events.time.time", lambda: 1000)
    row = TelegramOutbox(bot_key="hevc", chat_id="-1001", status="pending", attempts=0,
                         next_attempt_at=utcnow() + timedelta(hours=1), payload_json={})
    db.add(row)
    db.commit()
    before = channel_token(db, "telegram-outbox")
    row.attempts = 1
    db.commit()
    assert channel_token(db, "telegram-outbox") != before
    before = channel_token(db, "info")
    db.add(Setting(key="telegram_hevc_bot_heartbeat_at", value="2026-10-13T00:00:00Z"))
    db.commit()
    assert channel_token(db, "info") != before


def test_sync_sse_changes_for_result_without_new_logs(ui):
    db, _, _, _, _ = ui
    job = Job(type="ongoing", status="running", params_json={})
    db.add(job)
    db.commit()
    before = channel_token(db, "jobs")
    outcome = SyncOutcome(db, job.id)
    outcome.release(42, status="processed")
    assert channel_token(db, "jobs") != before
