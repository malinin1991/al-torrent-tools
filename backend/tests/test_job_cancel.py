import asyncio
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.db.models import Job, JobLog
from app.services import job_runner as job_runner_module
from app.services.job_runner import (
    STATUS_CANCELLED, STATUS_PENDING, STATUS_RUNNING, STATUS_STOPPING, STATUS_SUCCESS,
    JobRunner, JobStopError, JobStopRequested, UnknownJobTypeError,
    cancel_jobs_by_ids, cancel_stale_jobs, is_stop_requested, reclaim_orphan_jobs,
    reclaim_stale_jobs, request_stop,
)
from app.utils.datetime_fmt import utcnow


@compiles(JSONB, 'sqlite')
def _jsonb_sqlite(*_args, **_kwargs):
    return 'JSON'


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "jobs.sqlite"}')
    Job.__table__.create(engine)
    JobLog.__table__.create(engine)
    monkeypatch.setattr(job_runner_module, '_hold_run_lock', lambda _id: object())
    monkeypatch.setattr(job_runner_module, '_release_run_lock', lambda *_args: None)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def _job(db, status=STATUS_PENDING, **kwargs):
    values = dict(type='demo', status=status, params_json={}, created_at=utcnow())
    values.update(kwargs)
    job = Job(**values)
    db.add(job)
    db.commit()
    return job


def _logs(db, job_id):
    return list(db.scalars(select(JobLog).where(JobLog.job_id == job_id)).all())


def test_cancel_stale_jobs_marks_running_and_pending(db):
    running, pending, success = [_job(db, status) for status in (STATUS_RUNNING, STATUS_PENDING, STATUS_SUCCESS)]
    assert cancel_stale_jobs(db, reason='тест') == [running.id, pending.id]
    assert running.status == pending.status == STATUS_CANCELLED
    assert running.error == 'тест' and pending.finished_at is not None
    assert success.status == STATUS_SUCCESS
    assert len(_logs(db, running.id)) == len(_logs(db, pending.id)) == 1
    assert _logs(db, success.id) == []


def test_cancel_stale_jobs_noop_when_empty(db):
    assert cancel_stale_jobs(db) == []
    assert list(db.scalars(select(JobLog)).all()) == []


def test_cancel_jobs_by_ids(db):
    running, other = _job(db, STATUS_RUNNING), _job(db, STATUS_RUNNING)
    assert cancel_jobs_by_ids(db, [running.id], reason='shutdown') == [running.id]
    assert running.status == STATUS_CANCELLED and running.error == 'shutdown'
    assert other.status == STATUS_RUNNING


def test_reclaim_stale_jobs_by_age(db):
    old, fresh = utcnow() - timedelta(minutes=60), utcnow() - timedelta(minutes=1)
    stale = _job(db, STATUS_RUNNING, created_at=old, started_at=old)
    pending = _job(db, created_at=fresh)
    assert reclaim_stale_jobs(db, max_age_minutes=30) == [stale.id]
    assert stale.status == STATUS_CANCELLED and pending.status == STATUS_PENDING


def test_reclaim_keeps_long_running_job_with_recent_logs(db):
    old = utcnow() - timedelta(minutes=120)
    job = _job(db, STATUS_RUNNING, created_at=old, started_at=old)
    db.add(JobLog(job_id=job.id, message='alive', created_at=utcnow() - timedelta(minutes=2)))
    db.commit()
    assert reclaim_stale_jobs(db, max_age_minutes=30) == []
    assert job.status == STATUS_RUNNING


def test_reclaim_orphan_running_when_lock_free(db):
    job = _job(db, STATUS_RUNNING)
    assert reclaim_orphan_jobs(db, reason='сирота') == [job.id]
    assert job.status == STATUS_CANCELLED and job.error == 'сирота'


def test_reclaim_orphan_skips_running_when_lock_held(db, monkeypatch):
    job = _job(db, STATUS_RUNNING)
    monkeypatch.setattr(job_runner_module, '_hold_run_lock', lambda _id: None)
    assert reclaim_orphan_jobs(db) == []
    assert job.status == STATUS_RUNNING


def test_reclaim_orphan_pending_older_than_grace(db):
    job = _job(db, created_at=utcnow() - timedelta(seconds=120))
    assert reclaim_orphan_jobs(db, pending_grace_sec=60) == [job.id]
    assert job.status == STATUS_CANCELLED


def test_create_job_rejects_unknown_type():
    with pytest.raises(UnknownJobTypeError):
        JobRunner().create_job(MagicMock(), 'not_a_real_job', {})


def test_request_stop_running_to_stopping(db):
    job = _job(db, STATUS_RUNNING)
    assert request_stop(db, job.id) is job
    assert job.status == STATUS_STOPPING
    assert len(_logs(db, job.id)) == 1


def test_request_stop_rejects_non_running(db):
    job = _job(db, STATUS_SUCCESS)
    with pytest.raises(JobStopError):
        request_stop(db, job.id)
    assert job.status == STATUS_SUCCESS
    assert _logs(db, job.id) == []


def test_is_stop_requested(db):
    job = _job(db, STATUS_STOPPING)
    assert is_stop_requested(db, job.id)
    job.status = STATUS_RUNNING
    db.commit()
    assert not is_stop_requested(db, job.id)
    job.status = STATUS_CANCELLED
    db.commit()
    assert is_stop_requested(db, job.id)


def test_run_job_holds_lock_on_separate_connection(db, monkeypatch):
    runner, calls, connection = JobRunner(), [], object()
    async def handler(_db, _id, _params):
        calls.append('handler')
    runner.register('demo', handler)
    job = _job(db)
    monkeypatch.setattr(job_runner_module, '_hold_run_lock', lambda job_id: calls.append(('hold', job_id)) or connection)
    monkeypatch.setattr(job_runner_module, '_release_run_lock', lambda conn, job_id: calls.append(('release', conn, job_id)))
    # ORM executes conditional SQL, but never takes advisory locks itself.
    original = db.execute
    def execute(stmt, *args, **kwargs):
        assert 'advisory' not in str(stmt)
        return original(stmt, *args, **kwargs)
    monkeypatch.setattr(db, 'execute', execute)
    result = asyncio.run(runner.run_job(db, job.id))
    assert result.status == STATUS_SUCCESS
    assert calls == [('hold', job.id), 'handler', ('release', connection, job.id)]


def test_hold_run_lock_uses_engine_not_session(monkeypatch):
    conn = MagicMock()
    conn.execute.return_value.scalar.return_value = True
    monkeypatch.setattr(job_runner_module.engine, 'connect', lambda: conn)
    assert job_runner_module._hold_run_lock(42) is conn
    assert 'pg_try_advisory_lock' in str(conn.execute.call_args.args[0])
    conn.commit.assert_called_once()
    conn.close.assert_not_called()


def test_run_job_finalizes_stop_requested_as_cancelled(db):
    runner = JobRunner()
    async def handler(_db, _id, _params):
        raise JobStopRequested()
    runner.register('demo', handler)
    job = _job(db)
    result = asyncio.run(runner.run_job(db, job.id))
    assert result.status == STATUS_CANCELLED
    assert result.error == 'Остановлено пользователем'


def test_run_job_cancelled_mid_run_via_is_stop_requested(db):
    runner = JobRunner()
    async def handler(session, job_id, _params):
        cancel_jobs_by_ids(session, [job_id], reason='сирота')
        if is_stop_requested(session, job_id):
            raise JobStopRequested()
    runner.register('demo', handler)
    job = _job(db)
    result = asyncio.run(runner.run_job(db, job.id))
    assert result.status == STATUS_CANCELLED and result.error == 'сирота'


def test_run_job_no_success_log_when_already_cancelled(db):
    runner = JobRunner()
    async def handler(session, job_id, _params):
        cancel_jobs_by_ids(session, [job_id], reason='сирота')
    runner.register('demo', handler)
    job = _job(db)
    result = asyncio.run(runner.run_job(db, job.id))
    assert result.status == STATUS_CANCELLED and result.error == 'сирота'
    assert not any('завершен успешно' in row.message for row in _logs(db, job.id))
