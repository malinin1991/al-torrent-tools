"""Тесты audit trail PipelineEvent."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.db.models import JobLog, PipelineEvent
from app.services.pipeline import TorrentPipelineService, record_pipeline_event


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
    )


def _added_of_type(db: MagicMock, model_type: type) -> list:
    return [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], model_type)]


def test_record_pipeline_event_without_job_id() -> None:
    db = MagicMock()
    event = record_pipeline_event(
        db,
        pipeline_id=42,
        event_type="created",
        message="создан",
        details={"actor": "webhook"},
    )
    assert isinstance(event, PipelineEvent)
    assert event.pipeline_id == 42
    assert event.job_id is None
    assert event.details_json["actor"] == "webhook"
    db.add.assert_called_once()
    db.commit.assert_called_once()


def test_create_discovered_writes_event_without_job() -> None:
    db = MagicMock()

    def refresh(obj):
        obj.id = 9

    db.refresh.side_effect = refresh
    # add(pipeline) then later add(event)
    added: list = []

    def add_side_effect(obj):
        added.append(obj)
        if hasattr(obj, "info_hash") and not isinstance(obj, PipelineEvent):
            obj.id = 9
            obj.status = "discovered"

    db.add.side_effect = add_side_effect

    service = TorrentPipelineService(db)  # без job_id
    result = service.create_discovered("AB" * 20, release_id=1, torrent_id=2)
    assert result.id == 9
    events = [o for o in added if isinstance(o, PipelineEvent)]
    assert len(events) == 1
    assert events[0].event_type == "created"
    assert events[0].to_status == "discovered"
    assert events[0].job_id is None
    logs = [o for o in added if isinstance(o, JobLog)]
    assert logs == []


def test_mark_failed_writes_event_and_job_log() -> None:
    db = MagicMock()
    pipeline = _pipeline(status="master_added", pipeline_id=3)
    service = TorrentPipelineService(db, job_id=77)
    service.mark_failed(pipeline, "boom")

    events = _added_of_type(db, PipelineEvent)
    logs = _added_of_type(db, JobLog)
    assert len(events) == 1
    assert events[0].event_type == "failed"
    assert events[0].from_status == "master_added"
    assert events[0].to_status == "failed"
    assert events[0].job_id == 77
    assert events[0].details_json.get("actor") == "job"
    assert len(logs) == 1
    assert "boom" in logs[0].message


def test_mark_cancelled_actor_webhook() -> None:
    db = MagicMock()
    pipeline = _pipeline(status="master_added")
    service = TorrentPipelineService(db, actor="webhook")
    service.mark_cancelled(pipeline, "нет на master")

    events = _added_of_type(db, PipelineEvent)
    assert len(events) == 1
    assert events[0].event_type == "cancelled"
    assert events[0].details_json.get("actor") == "webhook"
    assert events[0].job_id is None


def test_mark_master_added_records_master_add(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    pipeline = _pipeline(status="discovered")
    service = TorrentPipelineService(db, actor="poll")
    service._sync_composition_best_effort = MagicMock()  # type: ignore[method-assign]
    service.mark_master_added(pipeline, details={"added_new": True})

    events = _added_of_type(db, PipelineEvent)
    assert len(events) == 1
    assert events[0].event_type == "master_add"
    assert events[0].to_status == "master_added"
    assert events[0].details_json.get("actor") == "poll"
    assert events[0].details_json.get("added_new") is True
    service._sync_composition_best_effort.assert_called_once()


def test_mark_waiting_master_noop_when_already_waiting() -> None:
    db = MagicMock()
    pipeline = _pipeline(status="waiting_master", pipeline_id=4)
    pipeline.error = "old"
    service = TorrentPipelineService(db, job_id=1)
    service.mark_waiting_master(pipeline, "still down")

    events = _added_of_type(db, PipelineEvent)
    assert events == []
    assert pipeline.error == "still down"
    db.commit.assert_called()


def test_mark_waiting_slave_noop_when_already_waiting() -> None:
    db = MagicMock()
    pipeline = _pipeline(status="waiting_slave", pipeline_id=5)
    pipeline.error = "old"
    service = TorrentPipelineService(db)
    result = service.mark_waiting_slave(pipeline, "old")

    events = _added_of_type(db, PipelineEvent)
    assert events == []
    assert result is pipeline
    # reason не менялся — без лишнего commit error-update
    db.commit.assert_not_called()


def test_mark_waiting_slave_writes_event_on_first_transition() -> None:
    db = MagicMock()
    pipeline = _pipeline(status="master_complete", pipeline_id=6)
    service = TorrentPipelineService(db)
    service.mark_waiting_slave(pipeline, "slave down")

    events = _added_of_type(db, PipelineEvent)
    assert len(events) == 1
    assert events[0].event_type == "status_change"
    assert events[0].from_status == "master_complete"
    assert events[0].to_status == "waiting_slave"
