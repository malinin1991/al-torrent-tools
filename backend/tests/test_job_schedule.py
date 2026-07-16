import asyncio

from app.services import job_runner as job_runner_module
from app.services.job_runner import JobRunner


def test_schedule_job_starts_and_completes_background_task() -> None:
    runner = JobRunner()
    called: list[int] = []

    async def fake_run(db: object, job_id: int) -> object:
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
            task = runner.schedule_job(7)
            assert task.get_name() == "job-7"
            await task
        finally:
            job_runner_module.SessionLocal = original  # type: ignore[misc]

        assert called == [7]

    asyncio.run(_exercise())
