"""Тесты target_role в TorrentCleanupService и prune cleanup_logs."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.db.models import CleanupRule, PipelineEvent
from app.jobs.cleanup_logs import RETAIN_DAYS, prune_old_jobs, prune_pipeline_events
from app.services.job_runner import STATUS_RUNNING, STATUS_SUCCESS
from app.services.torrent_cleanup import TorrentCleanupService
from app.utils.datetime_fmt import utcnow


def _rule(*, target_client: str, rule_id: int = 1) -> CleanupRule:
    return CleanupRule(
        id=rule_id,
        name=f"rule-{rule_id}",
        tracker_host="tr.libria.fun",
        message_contains="не зарегистрирован",
        include_errored=False,
        delete_files=False,
        target_client=target_client,
        enabled=True,
    )


def test_cleanup_service_filters_rules_by_target_role() -> None:
    master_rule = _rule(target_client="master", rule_id=1)
    slave_rule = _rule(target_client="slave", rule_id=2)
    both_rule = _rule(target_client="both", rule_id=3)

    db = MagicMock()
    db.scalars.return_value.all.return_value = [master_rule, slave_rule, both_rule]

    service = TorrentCleanupService(db=db, job_id=1)
    seen_targets: list[str] = []

    def fake_clients(target: str):
        seen_targets.append(target)
        return []

    service._get_clients_by_target = fake_clients  # type: ignore[method-assign]
    service._check_stop = lambda: None  # type: ignore[method-assign]
    service._add_log = lambda *a, **k: None  # type: ignore[method-assign]

    service.run(dry_run=True, target_role="master")

    # slave-only правило пропущено; master и both → клиенты только master.
    assert seen_targets == ["master", "master"]


def test_cleanup_slave_warns_when_only_master_rules() -> None:
    master_rule = _rule(target_client="master", rule_id=1)
    db = MagicMock()
    db.scalars.return_value.all.return_value = [master_rule]

    service = TorrentCleanupService(db=db, job_id=1)
    logs: list[str] = []
    service._get_clients_by_target = lambda _t: []  # type: ignore[method-assign]
    service._check_stop = lambda: None  # type: ignore[method-assign]
    service._add_log = lambda msg, level="info": logs.append(msg)  # type: ignore[method-assign]

    stats = service.run(dry_run=True, target_role="slave")
    assert stats == {"checked": 0, "matched": 0, "deleted": 0}
    assert any("нет применимых правил" in msg and "slave" in msg for msg in logs)


def test_prune_old_jobs_retains_by_age() -> None:
    assert RETAIN_DAYS == 30
    now = utcnow()
    old = SimpleNamespace(
        id=1,
        type="ongoing",
        status=STATUS_SUCCESS,
        finished_at=now - timedelta(days=40),
        created_at=now - timedelta(days=41),
    )
    recent = SimpleNamespace(
        id=2,
        type="ongoing",
        status=STATUS_SUCCESS,
        finished_at=now - timedelta(days=5),
        created_at=now - timedelta(days=6),
    )
    active = SimpleNamespace(
        id=3,
        type="ongoing",
        status=STATUS_RUNNING,
        finished_at=None,
        created_at=now - timedelta(days=50),
    )
    # SQLAlchemy filter not executed on MagicMock — emulate filtered stale list.
    db = MagicMock()
    db.scalars.return_value.all.return_value = [old]

    stats = prune_old_jobs(db, retain_days=30, protect_job_id=999)
    assert stats["deleted_jobs"] == 1
    assert stats["retain_days"] == 30
    db.delete.assert_called_once_with(old)
    db.commit.assert_called_once()
    _ = (recent, active)


def test_prune_pipeline_events_by_age() -> None:
    db = MagicMock()
    result = MagicMock()
    result.rowcount = 7
    db.execute.return_value = result

    stats = prune_pipeline_events(db, retain_days=30)
    assert stats["deleted_events"] == 7
    assert stats["retain_days"] == 30
    db.execute.assert_called_once()
    db.commit.assert_called_once()
    assert PipelineEvent.__tablename__ == "pipeline_events"
