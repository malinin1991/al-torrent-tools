"""HTML router factory, independent of app.main to avoid cyclic imports."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.hevc_workbench import list_hevc_workbench


def create_hevc_workbench_router(templates) -> APIRouter:
    router = APIRouter()

    @router.get("/hevc-workbench", response_class=HTMLResponse)
    def page(request: Request, view: str = "all", search: str = "", page: int = 1, per_page: int = 30, db: Session = Depends(get_db)):
        return templates.TemplateResponse(request, "hevc_workbench.html", list_hevc_workbench(db, view=view, search=search, page=page, per_page=per_page))

    @router.get("/hevc-workbench/live", response_class=HTMLResponse)
    def live(request: Request, view: str = "all", search: str = "", page: int = 1, per_page: int = 30, db: Session = Depends(get_db)):
        return templates.TemplateResponse(request, "partials/hevc_workbench_live.html", list_hevc_workbench(db, view=view, search=search, page=page, per_page=per_page))

    return router
