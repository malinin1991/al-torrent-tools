"""Generic REST launch must schedule the job on the API event loop."""

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
import httpx

from app.api import rest
from app.db.session import get_db


def test_generic_job_launch_has_running_event_loop(monkeypatch):
    app = FastAPI()
    app.include_router(rest.router)
    app.dependency_overrides[get_db] = lambda: None
    scheduled = []
    job = SimpleNamespace(id=123, type="backoffice_sync", status="pending")
    monkeypatch.setattr(rest.job_runner, "create_job", lambda db, job_type, params: job)
    def schedule(job_id):
        asyncio.get_running_loop()  # The real schedule_job uses asyncio.create_task.
        scheduled.append(job_id)
    monkeypatch.setattr(rest.job_runner, "schedule_job", schedule)
    async def launch():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/api/jobs", json={"type":"backoffice_sync", "params":{}})
    response = asyncio.run(launch())
    assert response.status_code == 200
    assert response.json() == {"id":123,"type":"backoffice_sync","status":"pending","queued":True}
    assert scheduled == [123]
