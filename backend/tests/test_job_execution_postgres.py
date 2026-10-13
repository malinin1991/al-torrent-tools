"""Opt-in real PostgreSQL races; private schema and explicit test database only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Job, JobLog
from app.services import job_runner as module
from app.services.job_runner import JobRunner, JobStopError, cancel_jobs_by_ids, reclaim_orphan_jobs, reclaim_stale_jobs, request_stop
from app.utils.datetime_fmt import utcnow


@pytest.fixture
def pg(monkeypatch):
    url = os.getenv('ALTT_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('ALTT_TEST_POSTGRES_URL отдельной тестовой БД не задан')
    schema = 'job_execution_' + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, pool_size=8, max_overflow=8, connect_args={
        'options': f'-csearch_path={schema} -clock_timeout=3000 -cstatement_timeout=10000',
        'prepare_threshold': None,
    })
    factory = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        Job.__table__.create(engine)
        JobLog.__table__.create(engine)
        monkeypatch.setattr(module, 'engine', engine)
        monkeypatch.setattr(module, 'SessionLocal', factory)
        yield factory
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def make_job(pg, status='pending', *, old=False):
    now = utcnow() - timedelta(hours=2) if old else utcnow()
    # Advisory locks are database-wide, so ids also differ across private schemas.
    job_id = 1_000_000_000 + uuid.uuid4().int % 1_000_000_000
    with pg() as db:
        db.add(Job(id=job_id, type='demo', status=status, params_json={}, created_at=now,
                   started_at=now if status != 'pending' else None))
        db.commit()
    return job_id


def run(pg, runner, job_id):
    with pg() as db:
        return asyncio.run(runner.run_job(db, job_id)).status


def snapshot(pg, job_id):
    with pg() as db:
        row = db.get(Job, job_id)
        logs = list(db.scalars(select(JobLog.message).where(JobLog.job_id == job_id)).all())
        return row.status, row.error, row.finished_at, logs


def test_two_runners_execute_same_pending_only_once(pg):
    job_id, runner = make_job(pg), JobRunner()
    entered, finish, calls = threading.Event(), threading.Event(), []
    async def handler(*_args):
        calls.append(True)
        entered.set()
        assert finish.wait(10), 'test failed to release handler'
    runner.register('demo', handler)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, pg, runner, job_id)
        try:
            assert entered.wait(5)
            second = pool.submit(run, pg, runner, job_id)
            assert second.result(timeout=5) == 'running'
            assert calls == [True]
            assert job_id in module.get_active_job_ids()
        finally:
            finish.set()
        assert first.result(timeout=5) == 'success'
    assert job_id not in module.get_active_job_ids()
    assert len(snapshot(pg, job_id)[3]) == 2
    assert run(pg, runner, job_id) == 'success'
    assert calls == [True]


def test_cancellation_committed_after_read_beats_claim(pg):
    job_id, runner, calls = make_job(pg), JobRunner(), []
    async def handler(*_args):
        calls.append(True)
    runner.register('demo', handler)
    with pg() as cached:
        row = cached.get(Job, job_id)
        with pg() as other:
            assert cancel_jobs_by_ids(other, [job_id], reason='won before claim') == [job_id]
        assert row.status == 'pending'
        assert asyncio.run(runner.run_job(cached, job_id)).status == 'cancelled'
    assert calls == []
    status, error, _, logs = snapshot(pg, job_id)
    assert status == 'cancelled' and error == 'won before claim' and len(logs) == 1


def test_duplicate_schedule_tasks_keep_only_one_running_owner(pg):
    job_id, runner = make_job(pg), JobRunner()
    entered, finish, calls = threading.Event(), threading.Event(), []
    async def handler(*_args):
        calls.append(True)
        entered.set()
        assert finish.wait(10)
    runner.register('demo', handler)
    async def exercise():
        first = runner.schedule_job(job_id)
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            second = runner.schedule_job(job_id)
            await asyncio.wait_for(second, timeout=5)
            assert snapshot(pg, job_id)[0] == 'running'
            assert calls == [True] and job_id in module.get_active_job_ids()
        finally:
            finish.set()
        await asyncio.wait_for(first, timeout=5)
    asyncio.run(exercise())
    assert calls == [True] and job_id not in module.get_active_job_ids()
    assert snapshot(pg, job_id)[0] == 'success'


def test_runner_lock_protects_old_pending_before_claim(pg, monkeypatch):
    job_id, runner = make_job(pg, old=True), JobRunner()
    locked, continue_claim, calls = threading.Event(), threading.Event(), []
    real_hold = module._hold_run_lock
    def paused_hold(current_id):
        conn = real_hold(current_id)
        if threading.current_thread().name.startswith('runner') and conn is not None:
            locked.set()
            assert continue_claim.wait(10)
        return conn
    monkeypatch.setattr(module, '_hold_run_lock', paused_hold)
    async def handler(*_args):
        calls.append(True)
    runner.register('demo', handler)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='runner') as pool:
        future = pool.submit(run, pg, runner, job_id)
        try:
            assert locked.wait(5)
            with pg() as reclaimer:
                assert reclaim_orphan_jobs(reclaimer) == []
                assert reclaim_stale_jobs(reclaimer, max_age_minutes=30) == []
            assert snapshot(pg, job_id)[0] == 'pending'
        finally:
            continue_claim.set()
        assert future.result(timeout=5) == 'success'
    assert calls == [True]


def test_reclaim_lock_and_committed_cancel_beat_runner(pg, monkeypatch):
    job_id, runner = make_job(pg, old=True), JobRunner()
    locked, continue_cancel, calls = threading.Event(), threading.Event(), []
    real_hold = module._hold_run_lock
    def paused_hold(current_id):
        conn = real_hold(current_id)
        if threading.current_thread().name.startswith('reclaim') and conn is not None:
            locked.set()
            assert continue_cancel.wait(10)
        return conn
    monkeypatch.setattr(module, '_hold_run_lock', paused_hold)
    async def handler(*_args):
        calls.append(True)
    runner.register('demo', handler)
    def reclaim():
        with pg() as db:
            return reclaim_orphan_jobs(db)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='reclaim') as pool:
        future = pool.submit(reclaim)
        try:
            assert locked.wait(5)
            assert run(pg, runner, job_id) == 'pending'
        finally:
            continue_cancel.set()
        assert future.result(timeout=5) == [job_id]
    assert run(pg, runner, job_id) == 'cancelled' and calls == []


@pytest.mark.parametrize('terminal', ['success', 'partial', 'failed'])
def test_reclaim_rechecks_terminal_after_stale_candidate_read(pg, monkeypatch, terminal):
    job_id = make_job(pg, 'running', old=True)
    real_hold, transitioned = module._hold_run_lock, []
    def finish_before_lock(current_id):
        if not transitioned:
            with pg() as finisher:
                module._finalize_job(finisher, current_id, terminal, 'original outcome')
            transitioned.append(True)
        return real_hold(current_id)
    monkeypatch.setattr(module, '_hold_run_lock', finish_before_lock)
    with pg() as db:
        stale = db.get(Job, job_id)
        assert stale.status == 'running'
        assert reclaim_orphan_jobs(db) == []
        assert stale.status == terminal
    assert snapshot(pg, job_id)[0:2] == (terminal, 'original outcome')
    assert len(snapshot(pg, job_id)[3]) == 1


@pytest.mark.parametrize('status', ['pending', 'running', 'stopping'])
def test_reclaim_commit_visible_while_lock_still_held(pg, monkeypatch, status):
    job_id = make_job(pg, status, old=True)
    real_release, observations = module._release_run_lock, []
    def release_after_observation(conn, current_id):
        if conn is not None:
            # Separate SQL session observes the durable cancellation, while another
            # dedicated connection still cannot acquire the runner's ownership lock.
            observations.append(snapshot(pg, current_id))
            contender = module._hold_run_lock(current_id)
            assert contender is None
        real_release(conn, current_id)
    monkeypatch.setattr(module, '_release_run_lock', release_after_observation)
    with pg() as db:
        assert reclaim_orphan_jobs(db) == [job_id]
    assert observations[0][0] == 'cancelled' and len(observations[0][3]) == 1
    # Once cancellation commits and unlock completes, lock can be acquired again.
    conn = module._hold_run_lock(job_id)
    assert conn is not None
    real_release(conn, job_id)


@pytest.mark.parametrize('stop_first', [True, False])
def test_stop_and_finalization_use_postgres_row_lock_order(pg, stop_first):
    job_id = make_job(pg, 'running')
    entered, contender_pids = threading.Event(), []
    with pg() as first:
        # Keep the winning state change uncommitted so the other UPDATE must
        # contend for the same PostgreSQL row and then re-evaluate its WHERE.
        if stop_first:
            first.execute(text("UPDATE jobs SET status='stopping' WHERE id=:id"), {'id': job_id})
            def competing():
                with pg() as db:
                    contender_pids.append(db.scalar(text('SELECT pg_backend_pid()')))
                    entered.set()
                    module._finalize_job(db, job_id, 'success', None)
                return 'finalized'
        else:
            first.execute(text("UPDATE jobs SET status='success', finished_at=:at WHERE id=:id"), {'at': utcnow(), 'id': job_id})
            def competing():
                with pg() as db:
                    contender_pids.append(db.scalar(text('SELECT pg_backend_pid()')))
                    entered.set()
                    with pytest.raises(JobStopError, match='success'):
                        request_stop(db, job_id)
                return 'stop rejected'
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(competing)
            try:
                assert entered.wait(5)
                deadline, blocked = time.monotonic() + 2, False
                with pg() as observer:
                    while time.monotonic() < deadline:
                        blocked = bool(observer.scalar(text('SELECT cardinality(pg_blocking_pids(:pid)) > 0'), {'pid': contender_pids[0]}))
                        if blocked:
                            break
                        time.sleep(0.01)
                assert blocked, 'second UPDATE never contended for the PostgreSQL row'
                assert not future.done()
            finally:
                first.commit()
            future.result(timeout=5)
    assert snapshot(pg, job_id)[0] == ('cancelled' if stop_first else 'success')


def test_run_lock_survives_orm_commit_and_connection_reconnect(pg):
    job_id = make_job(pg, 'running')
    conn = module._hold_run_lock(job_id)
    assert conn is not None
    try:
        with pg() as db:
            db.get(Job, job_id)
            db.commit()
            db.close()  # Working ORM connection returned/reopened independently.
            assert reclaim_orphan_jobs(db) == []
    finally:
        module._release_run_lock(conn, job_id)
    with pg() as db:
        assert reclaim_orphan_jobs(db) == [job_id]


def test_invalidated_owner_connection_allows_orphan_reclaim(pg):
    job_id = make_job(pg, 'running')
    conn = module._hold_run_lock(job_id)
    assert conn is not None
    conn.invalidate()  # Simulate dead owner's DB connection without protocol unlock.
    conn.close()
    with pg() as db:
        assert reclaim_orphan_jobs(db) == [job_id]
    assert snapshot(pg, job_id)[0] == 'cancelled'
