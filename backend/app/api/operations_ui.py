"""UI/API for persisted sync failures and Telegram delivery diagnostics."""

from typing import Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy.orm import Session

from app.db.models import Job
from app.db.session import get_db
from app.services.job_runner import JobAlreadyRunningError, JobRunner
from app.services.sync_outcomes import build_retry_params, get_failure_page


class SyncRetryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    failure_ids: list[StrictInt] | None = Field(default=None, min_length=1, max_length=100)


class TelegramRetryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bot_key: Literal["primary", "hevc"] | None = None
    acknowledge_unknown: StrictBool = False


def create_operations_router(templates: Jinja2Templates, runner: JobRunner) -> APIRouter:
    router = APIRouter()

    def require_job(db: Session, job_id: int) -> Job:
        job = db.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Джоб не найден")
        return job

    @router.get("/api/jobs/{job_id}/failures")
    def job_failures(job_id: int, after_id: int = Query(default=0, ge=0),
                     limit: int = Query(default=50, ge=1, le=100), db: Session = Depends(get_db)):
        require_job(db, job_id)
        return get_failure_page(db, job_id, after_id=after_id, limit=limit)

    @router.post("/api/jobs/{job_id}/retry-failures", status_code=202)
    async def retry_failures(job_id: int, payload: SyncRetryIn, db: Session = Depends(get_db)):
        parent = require_job(db, job_id)
        if parent.status in {"pending", "running", "stopping"}:
            raise HTTPException(status_code=409, detail="Дождитесь завершения исходного джоба")
        try:
            params = build_retry_params(db, job_id, payload.failure_ids)
            job = runner.create_job(db, "retry_sync_failures", params)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except JobAlreadyRunningError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        runner.schedule_job(job.id)
        return {"id": job.id, "type": job.type, "status": job.status, "retry_of": job_id,
                "count": len(params["targets"]), "queued": True, "url": f"/jobs?job_id={job.id}"}

    @router.get("/jobs/{job_id}/failures/live", response_class=HTMLResponse)
    def failure_partial(request: Request, job_id: int, after_id: int = Query(default=0, ge=0),
                        db: Session = Depends(get_db)):
        job = require_job(db, job_id)
        return templates.TemplateResponse(request, "partials/job_failures_live.html", {
            "failure_page": get_failure_page(db, job_id, after_id=after_id),
            "failure_after_id": after_id, "selected_job": job,
        })

    def outbox_context(db: Session, bot_key: str | None, status: str | None, page: int):
        from app.services.telegram_outbox import list_outbox_page
        try:
            return list_outbox_page(db, bot_key=bot_key or None, status=status or None, page=page)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/api/telegram-outbox")
    def outbox_api(bot_key: str | None = None, status: str | None = None,
                   page: int = Query(default=1, ge=1), db: Session = Depends(get_db)):
        return outbox_context(db, bot_key, status, page)

    @router.get("/telegram-outbox", response_class=HTMLResponse)
    def outbox_page(request: Request, bot_key: str | None = None, status: str | None = None,
                    page: int = Query(default=1, ge=1), db: Session = Depends(get_db)):
        return templates.TemplateResponse(request, "telegram_outbox.html", outbox_context(db, bot_key, status, page))

    @router.get("/telegram-outbox/live", response_class=HTMLResponse)
    def outbox_live(request: Request, bot_key: str | None = None, status: str | None = None,
                    page: int = Query(default=1, ge=1), db: Session = Depends(get_db)):
        return templates.TemplateResponse(request, "partials/telegram_outbox_live.html", outbox_context(db, bot_key, status, page))

    def retry_outbox(db: Session, outbox_id: int, payload: TelegramRetryIn):
        from app.services.telegram_outbox import OutboxRetryError, retry_failed_outbox
        try:
            return retry_failed_outbox(db, outbox_id, bot_key=payload.bot_key,
                                       acknowledge_unknown=payload.acknowledge_unknown)
        except OutboxRetryError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @router.post("/api/telegram-outbox/{outbox_id}/retry")
    def outbox_retry_api(outbox_id: int, payload: TelegramRetryIn, db: Session = Depends(get_db)):
        return retry_outbox(db, outbox_id, payload)

    @router.post("/telegram-outbox/{outbox_id}/retry", response_class=HTMLResponse)
    def outbox_retry_html(request: Request, outbox_id: int,
                          bot_key: Literal["primary", "hevc"] = Form(),
                          acknowledge_unknown: bool = Form(default=False), db: Session = Depends(get_db)):
        retry_outbox(db, outbox_id, TelegramRetryIn(bot_key=bot_key, acknowledge_unknown=acknowledge_unknown))
        return templates.TemplateResponse(request, "partials/action_result.html", {
            "message": "Повтор поставлен в очередь. Доставку выполнит процесс Telegram-бота.",
        }, headers={"HX-Trigger": "telegram-outbox-updated"})

    return router
