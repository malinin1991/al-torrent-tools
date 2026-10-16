"""Scheduled handlers share the manual runner's thread-owned Session boundary."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

import worker
from app.services import job_runner as runner_module
from app.services.job_runner import JobAlreadyRunningError, JobRunner


@pytest.mark.parametrize("callback,job_type,params", [
    (lambda: worker._run_by_type("ongoing"), "ongoing", {}),
    (lambda: worker._run_by_type("cleanup_master"), "cleanup_master", {"dry_run": True, "target_role": "master"}),
    (lambda: worker._run_by_type("cleanup_slave"), "cleanup_slave", {"dry_run": True, "target_role": "slave"}),
    (worker._pipeline_reconcile, "pipeline_reconcile", {}),
    (worker._retry_waiting_master, "waiting_master_retry", {}),
    (worker._retry_waiting_slave, "waiting_slave_retry", {}),
])
def test_blocking_scheduled_handler_keeps_scheduler_responsive(monkeypatch, callback, job_type, params):
    entered, release = threading.Event(), threading.Event()
    creation_sessions, execution_threads = [], []
    main_thread = threading.get_ident()

    class Session:
        def __enter__(self):
            self.thread = threading.get_ident()
            self.closed = False
            return self

        def __exit__(self, *args):
            self.closed = True

    runner = JobRunner()

    def create(db, actual_type, actual_params):
        assert (actual_type, actual_params) == (job_type, params)
        creation_sessions.append(db)
        return SimpleNamespace(id=17)

    async def run(db, job_id):
        execution_threads.append(threading.get_ident())
        assert job_id == 17 and db.thread == threading.get_ident()
        assert db is not creation_sessions[0] and creation_sessions[0].closed
        entered.set()
        # Represents blocking qB or filesystem I/O in an async job handler.
        assert release.wait(timeout=5)

    monkeypatch.setattr(runner, "create_job", create)
    monkeypatch.setattr(runner, "run_job", run)
    monkeypatch.setattr(worker, "job_runner", runner)
    monkeypatch.setattr(worker, "SessionLocal", Session)
    monkeypatch.setattr(runner_module, "SessionLocal", Session)

    async def exercise():
        task = asyncio.create_task(callback())
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0)
            # The event loop can continue while the handler is still blocked.
            assert not task.done()
            assert len(execution_threads) == 1
            assert execution_threads[0] != main_thread
        finally:
            release.set()
            await task

    asyncio.run(exercise())


def test_duplicate_scheduled_job_never_dispatches(monkeypatch):
    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def create(*args):
        raise JobAlreadyRunningError("ongoing", 3)

    def dispatch(*args):
        pytest.fail("duplicate job must not be dispatched")

    monkeypatch.setattr(worker, "SessionLocal", Session)
    monkeypatch.setattr(worker, "job_runner", SimpleNamespace(create_job=create, schedule_job=dispatch))
    asyncio.run(worker._run_by_type("ongoing"))


@pytest.mark.parametrize("callback", [worker._poll_master_pipeline, worker._poll_slave_pipeline])
def test_blocking_pipeline_poll_owns_session_in_thread_and_dispatches_nested_hash(monkeypatch, callback):
    from concurrent.futures import ThreadPoolExecutor
    import time

    entered, release = threading.Event(), threading.Event()
    hashes_done = threading.Event()
    main_thread = threading.get_ident()
    sessions, hashed = [], []
    runner = JobRunner()

    class Session:
        def __enter__(self):
            self.thread = threading.get_ident()
            sessions.append(self)
            return self

        def __exit__(self, *args):
            pass

    async def run_hash(db, job_id):
        assert db.thread == threading.get_ident()
        hashed.append(job_id)
        time.sleep(0.02)
        if len(hashed) == 4:
            hashes_done.set()

    class PipelineService:
        def __init__(self, db, **kwargs):
            assert db.thread == threading.get_ident() != main_thread

        def get_master_added_older_than(self, age):
            return [SimpleNamespace(id=1)]

        get_slave_added_older_than = get_master_added_older_than

        def classify_master_torrent(self, pipeline):
            self.process_slave_completion(pipeline)
            return "in_progress"

        def process_slave_completion(self, pipeline):
            entered.set()
            assert release.wait(timeout=5)
            # Closing a polling loop must preserve all queued hash dispatches.
            for job_id in range(2, 6):
                runner.schedule_job(job_id)

    monkeypatch.setattr(runner, "run_job", run_hash)
    monkeypatch.setattr(runner_module, "SessionLocal", Session)
    monkeypatch.setattr(worker, "SessionLocal", Session)
    monkeypatch.setattr(worker, "TorrentPipelineService", PipelineService)
    monkeypatch.setattr(worker, "_setting_int", lambda *args: 0)

    async def exercise():
        task = asyncio.create_task(callback())
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
            await task
            # Hashes continue after the poll's private event loop has closed.
            assert await asyncio.to_thread(hashes_done.wait, 2)

    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(runner_module, "_job_executor", executor, raising=False)
        asyncio.run(exercise())
    assert sorted(hashed) == [2, 3, 4, 5]
    assert all(session.thread != main_thread for session in sessions)
