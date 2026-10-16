"""Generic REST launch must schedule the job on the API event loop."""

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
import httpx
import pytest

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


@pytest.mark.parametrize("job_type, target_role", [("cleanup_master", "slave"), ("cleanup_slave", "master"), ("cleanup", "slave"), ("cleanup_slave", "both")])
def test_generic_cleanup_rejects_role_mismatch_before_creating_job(monkeypatch, job_type, target_role):
    monkeypatch.setattr(rest.job_runner, "create_job", lambda *args: pytest.fail("invalid cleanup must not be persisted"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest.create_job(rest.JobCreateIn(type=job_type, params={"target_role": target_role}), db=None))
    assert exc.value.status_code == 422


@pytest.mark.parametrize("job_type, expected_role", [("cleanup", "master"), ("cleanup_master", "master"), ("cleanup_slave", "slave")])
@pytest.mark.parametrize("supplied_role", [None, "", "  ", "canonical"])
def test_generic_cleanup_normalizes_matching_role(monkeypatch, job_type, expected_role, supplied_role):
    captured = []
    params = {"dry_run": True}
    if supplied_role is not None:
        params["target_role"] = f" {expected_role.upper()} " if supplied_role == "canonical" else supplied_role
    job = SimpleNamespace(id=123, type=f"cleanup_{expected_role}", status="pending")
    monkeypatch.setattr(rest.job_runner, "create_job", lambda db, normalized_type, normalized_params: captured.append((normalized_type, normalized_params)) or job)
    monkeypatch.setattr(rest.job_runner, "schedule_job", lambda job_id: None)
    asyncio.run(rest.create_job(rest.JobCreateIn(type=job_type, params=params), db=None))
    assert captured == [(f"cleanup_{expected_role}", {"dry_run": True, "target_role": expected_role})]
