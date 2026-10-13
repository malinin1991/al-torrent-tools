"""Real conditional SQL on isolated SQLite; PostgreSQL locks tested separately."""
import asyncio
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.models import Job, JobLog
from app.services import job_runner as module
from app.services.job_runner import (
    JobRunner, JobStopError, JobStopRequested, cancel_jobs_by_ids, cancel_stale_jobs,
    reclaim_orphan_jobs, reclaim_stale_jobs, request_stop,
)
from app.services.sync_outcomes import JobPartialFailure
from app.utils.datetime_fmt import utcnow


@compiles(JSONB, 'sqlite')
def _jsonb_sqlite(*_args, **_kwargs):
    return 'JSON'


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "atomic.sqlite"}')
    Job.__table__.create(engine)
    JobLog.__table__.create(engine)
    monkeypatch.setattr(module, '_hold_run_lock', lambda _id: object())
    monkeypatch.setattr(module, '_release_run_lock', lambda *_args: None)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def job(db, status='pending', **kwargs):
    values = dict(type='demo', status=status, params_json={}, created_at=utcnow())
    values.update(kwargs)
    row = Job(**values)
    db.add(row)
    db.commit()
    return row


def messages(db, job_id):
    return list(db.scalars(select(JobLog.message).where(JobLog.job_id == job_id)).all())


@pytest.mark.parametrize('status', ['running', 'stopping', 'success', 'partial', 'failed', 'cancelled'])
def test_non_pending_does_not_run_or_mutate(db, status):
    called = []
    runner = JobRunner()
    async def handler(*args):
        called.append(True)
    runner.register('demo', handler)
    at = utcnow() - timedelta(hours=1)
    row = job(db, status, started_at=at, finished_at=at, error='preserved', result_json={'version': 1})
    db.add(JobLog(job_id=row.id, message='preserved log'))
    db.commit()
    result = asyncio.run(runner.run_job(db, row.id))
    assert called == []
    assert result.status == status and result.error == 'preserved'
    assert result.started_at == result.finished_at == at
    assert result.result_json == {'version': 1}
    assert messages(db, row.id) == ['preserved log']


def test_stale_identity_map_cannot_resurrect_cancelled_before_claim(db, monkeypatch):
    called, runner = [], JobRunner()
    async def handler(*_args):
        called.append(True)
    runner.register('demo', handler)
    row = job(db)
    def hold(job_id):
        with Session(db.get_bind()) as other:
            cancel_jobs_by_ids(other, [job_id], reason='race cancelled')
        assert row.status == 'pending'  # Cached pre-lock object really is stale.
        return object()
    monkeypatch.setattr(module, '_hold_run_lock', hold)
    result = asyncio.run(runner.run_job(db, row.id))
    assert called == [] and result.status == 'cancelled'
    assert result.error == 'race cancelled'
    assert len(messages(db, row.id)) == 1


def test_duplicate_busy_runner_does_not_remove_active_owner(db, monkeypatch):
    row, runner = job(db), JobRunner()
    locks = iter([object(), None])
    monkeypatch.setattr(module, '_hold_run_lock', lambda _id: next(locks))
    calls = []
    async def handler(_db, job_id, _params):
        calls.append(job_id)
        assert job_id in module.get_active_job_ids()
        with Session(db.get_bind()) as second:
            assert (await runner.run_job(second, job_id)).status == 'running'
        assert job_id in module.get_active_job_ids()
    runner.register('demo', handler)
    assert asyncio.run(runner.run_job(db, row.id)).status == 'success'
    assert calls == [row.id] and row.id not in module.get_active_job_ids()


def test_claim_failure_rolls_back_releases_lock_and_never_calls_handler(db, monkeypatch):
    row, runner, calls = job(db), JobRunner(), []
    async def handler(*_args):
        calls.append('handler')
    runner.register('demo', handler)
    monkeypatch.setattr(module, '_release_run_lock', lambda *_args: calls.append('unlock'))
    def fail(*_args, **_kwargs):
        raise RuntimeError('claim failed')
    monkeypatch.setattr(db, 'scalar', fail)
    with pytest.raises(RuntimeError, match='claim failed'):
        asyncio.run(runner.run_job(db, row.id))
    assert calls == ['unlock'] and row.id not in module.get_active_job_ids()
    assert row.status == 'pending' and messages(db, row.id) == []


@pytest.mark.parametrize('commit_fails', [False, True])
def test_claim_owner_is_visible_to_shutdown_at_start_commit(db, monkeypatch, commit_fails):
    row, runner, calls = job(db), JobRunner(), []
    job_id, original_commit = row.id, db.commit
    async def handler(*_args):
        calls.append('handler')
    runner.register('demo', handler)
    first_commit = True
    def commit():
        nonlocal first_commit
        if first_commit:
            first_commit = False
            assert job_id in module.get_active_job_ids()
            if commit_fails:
                raise RuntimeError('start commit failed')
        return original_commit()
    monkeypatch.setattr(db, 'commit', commit)
    if commit_fails:
        with pytest.raises(RuntimeError, match='start commit failed'):
            asyncio.run(runner.run_job(db, job_id))
        assert row.status == 'pending' and calls == []
        assert messages(db, job_id) == []
    else:
        assert asyncio.run(runner.run_job(db, job_id)).status == 'success'
        assert calls == ['handler']
    assert job_id not in module.get_active_job_ids()


def test_cancelled_error_finalizes_and_releases(db, monkeypatch):
    row, runner, released = job(db), JobRunner(), []
    async def handler(*_args):
        raise asyncio.CancelledError()
    runner.register('demo', handler)
    monkeypatch.setattr(module, '_release_run_lock', lambda *_args: released.append(True))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.run_job(db, row.id))
    assert row.status == 'cancelled' and 'shutdown' in row.error
    assert released == [True] and row.id not in module.get_active_job_ids()


@pytest.mark.parametrize('outcome', ['success', 'error', 'partial', 'cancel'])
def test_shutdown_cancel_reason_and_time_survive_late_handler(db, outcome):
    row, runner = job(db), JobRunner()
    finished = []
    async def handler(session, job_id, _params):
        cancel_jobs_by_ids(session, [job_id], reason='shutdown reason')
        finished.append(session.get(Job, job_id).finished_at)
        if outcome == 'error':
            raise RuntimeError('late error')
        if outcome == 'partial':
            raise JobPartialFailure('late partial')
        if outcome == 'cancel':
            raise JobStopRequested()
    runner.register('demo', handler)
    result = asyncio.run(runner.run_job(db, row.id))
    assert result.status == 'cancelled' and result.error == 'shutdown reason'
    assert result.finished_at == finished[0]
    assert len(messages(db, row.id)) == 2  # start + original cancellation only


@pytest.mark.parametrize('result_status', ['success', 'partial', 'failed'])
def test_stop_wins_atomic_finalization(db, result_status):
    row = job(db, 'running')
    request_stop(db, row.id)
    module._finalize_job(db, row.id, result_status, 'handler outcome')
    db.refresh(row)
    assert row.status == 'cancelled' and row.error == 'Остановлено пользователем'
    assert 'завершен успешно' not in '\n'.join(messages(db, row.id))


def test_stop_stale_running_cannot_change_terminal(db):
    row = job(db, 'running')
    with Session(db.get_bind()) as other:
        module._finalize_job(other, row.id, 'success', None)
    assert row.status == 'running'
    with pytest.raises(JobStopError, match='success'):
        request_stop(db, row.id)
    assert row.status == 'success'
    assert messages(db, row.id) == ['Джоб завершен успешно']


@pytest.mark.parametrize('cancel', [cancel_stale_jobs, lambda db: cancel_jobs_by_ids(db, [1])])
def test_explicit_cancel_uses_current_db_state(db, cancel):
    row = job(db, 'running')
    with Session(db.get_bind()) as other:
        module._finalize_job(other, row.id, 'partial', 'saved partial')
    assert row.status == 'running'
    assert cancel(db) == []
    db.refresh(row)
    assert row.status == 'partial' and row.error == 'saved partial'
    assert len(messages(db, row.id)) == 1


@pytest.mark.parametrize('reclaim', [reclaim_orphan_jobs, lambda db: reclaim_stale_jobs(db, max_age_minutes=30)])
def test_reclaim_refreshes_candidate_after_lock(db, monkeypatch, reclaim):
    old = utcnow() - timedelta(hours=2)
    row = job(db, 'running', started_at=old, created_at=old)
    def hold(job_id):
        with Session(db.get_bind()) as other:
            module._finalize_job(other, job_id, 'success', None)
        assert row.status == 'running'
        return object()
    monkeypatch.setattr(module, '_hold_run_lock', hold)
    assert reclaim(db) == []
    db.refresh(row)
    assert row.status == 'success' and messages(db, row.id) == ['Джоб завершен успешно']


@pytest.mark.parametrize('status', ['pending', 'running', 'stopping'])
def test_reclaim_commit_precedes_unlock_for_every_active_status(db, monkeypatch, status):
    old = utcnow() - timedelta(hours=2)
    row = job(db, status, started_at=old, created_at=old)
    observed = []
    def unlock(_conn, job_id):
        with Session(db.get_bind()) as other:
            fresh = other.get(Job, job_id)
            observed.append((fresh.status, len(messages(other, job_id))))
    monkeypatch.setattr(module, '_release_run_lock', unlock)
    assert reclaim_orphan_jobs(db) == [row.id]
    assert observed == [('cancelled', 1)]


def test_old_pending_with_busy_lock_is_not_reclaimed(db, monkeypatch):
    row = job(db, created_at=utcnow() - timedelta(hours=2))
    monkeypatch.setattr(module, '_hold_run_lock', lambda _id: None)
    assert reclaim_orphan_jobs(db) == []
    assert reclaim_stale_jobs(db, max_age_minutes=30) == []
    assert row.status == 'pending' and messages(db, row.id) == []


def test_failed_flush_transaction_still_finalizes_failed(db):
    row, runner = job(db), JobRunner()
    async def handler(session, job_id, _params):
        session.add(Job(id=job_id, type='duplicate', status='pending', params_json={}))
        session.flush()
    runner.register('demo', handler)
    with pytest.warns(Warning, match='conflicts with persistent instance'):
        result = asyncio.run(runner.run_job(db, row.id))
    assert result.status == 'failed' and 'UNIQUE constraint failed' in result.error
    assert row.id not in module.get_active_job_ids()
    assert len(messages(db, row.id)) == 2


def test_partial_keeps_previously_persisted_summary(db):
    row, runner = job(db), JobRunner()
    async def handler(session, job_id, _params):
        record = session.get(Job, job_id)
        record.result_json = {'version': 1, 'issue_count': 2}
        session.commit()
        raise JobPartialFailure('Завершено с ошибками: 2')
    runner.register('demo', handler)
    result = asyncio.run(runner.run_job(db, row.id))
    assert result.status == 'partial' and result.result_json['issue_count'] == 2
    assert messages(db, row.id)[-1] == 'Завершено с ошибками: 2'


def test_hold_busy_lock_closes_connection(monkeypatch):
    conn = MagicMock()
    conn.execute.return_value.scalar.return_value = False
    monkeypatch.setattr(module.engine, 'connect', lambda: conn)
    assert module._hold_run_lock(9) is None
    conn.commit.assert_called_once()
    conn.close.assert_called_once()


def test_hold_commit_error_invalidates_possible_session_lock(monkeypatch):
    conn = MagicMock()
    conn.commit.side_effect = RuntimeError('connection failed')
    monkeypatch.setattr(module.engine, 'connect', lambda: conn)
    with pytest.raises(RuntimeError):
        module._hold_run_lock(9)
    conn.invalidate.assert_called_once()
    conn.close.assert_called_once()


def test_unlock_error_invalidates_before_returning_to_pool():
    conn = MagicMock()
    conn.closed = False
    conn.execute.side_effect = RuntimeError('connection failed')
    with pytest.raises(RuntimeError):
        module._release_run_lock(conn, 9)
    conn.invalidate.assert_called_once()
    conn.close.assert_called_once()
