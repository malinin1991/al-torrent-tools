"""Тесты target_role в TorrentCleanupService и prune cleanup_logs."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.db.models import CleanupRule
from app.jobs.cleanup_logs import KEEP_RUNS_PER_TYPE, prune_old_jobs
from app.services.job_runner import STATUS_RUNNING, STATUS_SUCCESS
from app.services.torrent_cleanup import TorrentCleanupService


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


def test_prune_old_jobs_keeps_last_n_per_type() -> None:
    assert KEEP_RUNS_PER_TYPE == 100

    keep = 2
    # ids 1..5 for ongoing; keep last 2 → delete 1,2,3
    ongoing = [
        SimpleNamespace(id=i, type="ongoing", status=STATUS_SUCCESS) for i in (1, 2, 3, 4, 5)
    ]
    # active job must not be deleted even if outside keep window
    active = SimpleNamespace(id=6, type="ongoing", status=STATUS_RUNNING)
    other = [
        SimpleNamespace(id=i, type="full_sync", status=STATUS_SUCCESS) for i in (10, 11, 12)
    ]

    by_type_all = {
        "ongoing": ongoing + [active],
        "full_sync": other,
    }

    def scalars_side_effect(stmt):
        mock = MagicMock()
        # We don't parse SQLAlchemy stmt; drive via call sequence stored on db.
        idx = db._call_i
        db._call_i += 1
        if idx == 0:
            mock.all.return_value = ["full_sync", "ongoing"]
        elif idx == 1:
            # keep ids full_sync (desc): 12, 11
            mock.all.return_value = [12, 11]
        elif idx == 2:
            # stale full_sync: id 10
            mock.all.return_value = [other[0]]
        elif idx == 3:
            mock.all.return_value = [6, 5]  # keep includes active + newest success
        elif idx == 4:
            # stale ongoing outside keep and not active: 1,2,3,4 but 4 not in keep (keep=6,5)
            # keep ids are 6 and 5; stale = those not in keep and not active → 1,2,3,4
            # but filter status notin active → 1,2,3,4 all success
            mock.all.return_value = [ongoing[0], ongoing[1], ongoing[2], ongoing[3]]
        else:
            mock.all.return_value = []
        return mock

    db = MagicMock()
    db._call_i = 0
    db.scalars.side_effect = scalars_side_effect

    stats = prune_old_jobs(db, keep_per_type=keep, protect_job_id=999)
    assert stats["deleted_jobs"] == 5  # full_sync:1 + ongoing:4
    assert db.delete.call_count == 5
    db.commit.assert_called_once()
    _ = by_type_all  # documented fixture map
