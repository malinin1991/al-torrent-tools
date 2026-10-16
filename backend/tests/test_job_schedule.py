import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
import threading
import time

import pytest

from app.services import job_runner as job_runner_module
from app.services.job_runner import JobRunner


def test_schedule_job_starts_and_completes_background_task() -> None:
    runner = JobRunner()
    called: list[int] = []
    request_context = ContextVar("test_request_context", default="missing")

    async def fake_run(db: object, job_id: int) -> object:
        assert request_context.get() == "request"
        called.append(job_id)
        return None

    runner.run_job = fake_run  # type: ignore[method-assign]

    async def _exercise() -> None:
        # Не ходим в реальную БД: подменим SessionLocal на dummy context manager.
        class DummySession:
            def __enter__(self) -> object:
                return object()

            def __exit__(self, *args: object) -> None:
                return None

        original = job_runner_module.SessionLocal
        job_runner_module.SessionLocal = DummySession  # type: ignore[misc, assignment]
        try:
            request_context.set("request")
            task = runner.schedule_job(7)
            assert task.get_name() == "job-7"
            await task
        finally:
            job_runner_module.SessionLocal = original  # type: ignore[misc]

        assert called == [7]

    asyncio.run(_exercise())


@pytest.mark.parametrize("nested", [False, True])
def test_dispatched_jobs_survive_wrapper_cancellation_and_nested_loop_shutdown(monkeypatch, nested):
    runner = JobRunner()
    called = []

    class DummySession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    async def run(db, job_id):
        called.append(job_id)
        if nested and job_id == 1:
            # Pipeline handlers schedule hash jobs from their private loop.
            # At loop shutdown, some executor jobs may still be queued.
            for child in range(2, 6):
                runner.schedule_job(child)
        else:
            time.sleep(0.02)

    monkeypatch.setattr(runner, "run_job", run)
    monkeypatch.setattr(job_runner_module, "SessionLocal", DummySession)

    async def exercise():
        task = runner.schedule_job(1)
        if nested:
            await task
        else:
            # Cancellation can occur before the wrapper's first event-loop tick.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(job_runner_module, "_job_executor", executor, raising=False)
        asyncio.run(exercise())
    assert sorted(called) == ([1, 2, 3, 4, 5] if nested else [1])


def test_parent_finishes_while_nested_hash_is_still_running(monkeypatch):
    runner = JobRunner()
    child_started, child_release, child_done = threading.Event(), threading.Event(), threading.Event()
    parent_returned_before_child = []

    class DummySession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    async def run(db, job_id):
        if job_id == 1:
            runner.schedule_job(2)
            return
        child_started.set()
        assert child_release.wait(timeout=5)
        child_done.set()

    monkeypatch.setattr(runner, "run_job", run)
    monkeypatch.setattr(job_runner_module, "SessionLocal", DummySession)

    async def exercise():
        task = runner.schedule_job(1)
        try:
            async with asyncio.timeout(2):
                while not child_started.is_set():
                    await asyncio.sleep(0)
                await asyncio.shield(task)
            parent_returned_before_child.append(not child_done.is_set())
        finally:
            child_release.set()
            await task
            assert await asyncio.to_thread(child_done.wait, 2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        monkeypatch.setattr(job_runner_module, "_job_executor", executor, raising=False)
        asyncio.run(exercise())
    assert parent_returned_before_child == [True]
    assert not job_runner_module._background_tasks


def test_running_job_can_await_a_nested_job(monkeypatch):
    runner = JobRunner()
    called = []

    class DummySession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    async def run(db, job_id):
        called.append(job_id)
        if job_id == 1:
            await runner.schedule_job(2)
            called.append("parent-finished")

    monkeypatch.setattr(runner, "run_job", run)
    monkeypatch.setattr(job_runner_module, "SessionLocal", DummySession)

    async def exercise():
        async with asyncio.timeout(2):
            await runner.schedule_job(1)

    with ThreadPoolExecutor(max_workers=2) as executor:
        monkeypatch.setattr(job_runner_module, "_job_executor", executor, raising=False)
        asyncio.run(exercise())
    assert called == [1, 2, "parent-finished"]
    assert not job_runner_module._background_tasks
