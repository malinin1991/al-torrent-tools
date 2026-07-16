from pathlib import Path

from fastapi import Depends, FastAPI, Form, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.api.rest import job_runner, router as api_router
from app.core.config import settings
from app.db.models import ExtraUrl, Job, JobLog, QbClient, Setting, TorrentArchive, TorrentPipeline
from app.db.session import SessionLocal, get_db
from app.services.job_runner import JobAlreadyRunningError, UnknownJobTypeError, reclaim_stale_jobs
from app.services.anilibria_auth import login_and_store_token, resolve_anilibria_password
from app.services.db_maintenance import reset_full, reset_operational_state
from app.services.pipeline import TorrentPipelineService
from app.services.qbittorrent import test_qb_connection
from app.services.runtime_settings import SECRET_SETTING_KEYS, build_anilibria_client, get_setting_value
from app.services.releases_view import list_release_groups
from app.services.system_status import collect_system_status
from app.services.torrent_archive import resolve_torrent_storage_root
from app.utils.datetime_fmt import as_utc_iso


app = FastAPI(title=settings.app_name)
app.include_router(api_router)

base_path = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(base_path / "templates"))
templates.env.filters["as_utc_iso"] = as_utc_iso
app.mount("/static", StaticFiles(directory=str(base_path / "static")), name="static")


def _parse_port(raw: str, default: int = 8080) -> int:
    text = raw.strip()
    if text.isdigit():
        return int(text)
    return default


def _upsert_qb_client(
    db: Session,
    *,
    role: str,
    host: str,
    port: str,
    username: str,
    password: str,
) -> None:
    host_value = host.strip()
    username_value = username.strip()
    port_value = _parse_port(port)
    client = db.scalar(select(QbClient).where(QbClient.role == role).limit(1))
    if client is None:
        password_to_store = password
        if not password_to_store.strip():
            # Первичная синхронизация: пароль мог уже лежать только в settings.
            settings_row = db.get(Setting, f"qb_{role}_password")
            if settings_row is not None:
                password_to_store = settings_row.value
        db.add(
            QbClient(
                name=role,
                role=role,
                host=host_value,
                port=port_value,
                username=username_value,
                password_encrypted=password_to_store,
                enabled=True,
            )
        )
        return

    client.name = role
    client.host = host_value
    client.port = port_value
    client.username = username_value
    client.enabled = True
    if password.strip():
        client.password_encrypted = password


def _resolve_qb_password(db: Session, role: str, form_password: str) -> str:
    if form_password.strip():
        return form_password
    from_settings = get_setting_value(db, f"qb_{role}_password", "")
    if from_settings.strip():
        return from_settings
    client = db.scalar(select(QbClient).where(QbClient.role == role).limit(1))
    if client is not None and client.password_encrypted:
        return client.password_encrypted
    return ""


def _qb_test_message(role: str, result: dict) -> str:
    return (
        f"qBittorrent {role}: OK — {result['host']}:{result['port']}, "
        f"version={result['version']}, webapi={result['webapi']}, "
        f"торрентов={result['torrents']}"
    )


@app.on_event("startup")
async def validate_runtime_settings() -> None:
    if settings.app_env.lower() != "dev" and settings.secret_key == "change-me":
        raise RuntimeError("Для окружения вне dev требуется задать SECRET_KEY")
    resolve_torrent_storage_root().mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        # Только по порогу бездействия — не трогаем джобы, которые крутит worker.
        cancelled = reclaim_stale_jobs(
            db,
            reason="API перезапущен — джобы без активности помечены как cancelled",
        )
        if cancelled:
            import logging

            logging.getLogger(__name__).warning(
                "При старте api помечены cancelled зависшие джобы: %s",
                cancelled,
            )


@app.get("/health")
async def health(db: Session = Depends(get_db)) -> dict:
    client = build_anilibria_client(db)
    return {"status": "ok", "anilibria": await client.health()}


@app.get("/info", response_class=HTMLResponse)
async def info_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    status = await collect_system_status(db)
    return templates.TemplateResponse(request, "info.html", {"status": status})


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    jobs = db.scalars(select(Job).order_by(Job.id.desc()).limit(20)).all()
    return templates.TemplateResponse(request, "dashboard.html", {"jobs": jobs})


@app.get("/dashboard/jobs-live", response_class=HTMLResponse)
def dashboard_jobs_live(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    jobs = db.scalars(select(Job).order_by(Job.id.desc()).limit(20)).all()
    return templates.TemplateResponse(request, "partials/dashboard_jobs.html", {"jobs": jobs})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    rows = db.scalars(select(Setting)).all()
    settings_map = {row.key: row.value for row in rows}
    has_anilibria_token = bool((settings_map.get("anilibria_bearer_token") or "").strip())
    has_anilibria_passkey = bool((settings_map.get("anilibria_passkey") or "").strip())
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"settings_map": settings_map, "has_anilibria_token": has_anilibria_token, "has_anilibria_passkey": has_anilibria_passkey},
    )


@app.post("/settings", response_class=HTMLResponse)
def update_settings(
    request: Request,
    anilibria_base_url: str = Form(default=settings.anilibria_base_url),
    anilibria_fallback_base_url: str = Form(default=settings.anilibria_fallback_base_url),
    anilibria_bearer_token: str = Form(default=""),
    anilibria_login: str = Form(default=""),
    anilibria_password: str = Form(default=""),
    qb_master_host: str = Form(default=""),
    qb_master_port: str = Form(default="8080"),
    qb_master_username: str = Form(default=""),
    qb_master_password: str = Form(default=""),
    qb_slave_host: str = Form(default=""),
    qb_slave_port: str = Form(default="8080"),
    qb_slave_username: str = Form(default=""),
    qb_slave_password: str = Form(default=""),
    scrape_pause_every: str = Form(default=str(settings.scrape_pause_every)),
    scrape_pause_sec: str = Form(default=str(settings.scrape_pause_sec)),
    ongoing_interval_sec: str = Form(default=str(settings.ongoing_interval_sec)),
    cleanup_interval_sec: str = Form(default=str(settings.cleanup_interval_sec)),
    pipeline_master_min_age_min: str = Form(default=str(settings.pipeline_master_min_age_min)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form_data = {
        "anilibria_base_url": anilibria_base_url,
        "anilibria_fallback_base_url": anilibria_fallback_base_url,
        "anilibria_login": anilibria_login,
        "anilibria_bearer_token": anilibria_bearer_token,
        "qb_master_host": qb_master_host,
        "qb_master_port": qb_master_port,
        "qb_master_username": qb_master_username,
        "qb_master_password": qb_master_password,
        "qb_slave_host": qb_slave_host,
        "qb_slave_port": qb_slave_port,
        "qb_slave_username": qb_slave_username,
        "qb_slave_password": qb_slave_password,
        "scrape_pause_every": scrape_pause_every,
        "scrape_pause_sec": scrape_pause_sec,
        "ongoing_interval_sec": ongoing_interval_sec,
        "cleanup_interval_sec": cleanup_interval_sec,
        "pipeline_master_min_age_min": pipeline_master_min_age_min,
    }
    for key, value in form_data.items():
        row = db.get(Setting, key)
        value_to_store = value
        if key in SECRET_SETTING_KEYS and not value.strip() and row is not None:
            value_to_store = row.value
        if row is None:
            row = Setting(key=key, value=value_to_store)
            db.add(row)
        else:
            row.value = value_to_store

    if anilibria_password.strip():
        password_row = db.get(Setting, "anilibria_password")
        if password_row is None:
            db.add(Setting(key="anilibria_password", value=anilibria_password))
        else:
            password_row.value = anilibria_password

    _upsert_qb_client(
        db,
        role="master",
        host=qb_master_host,
        port=qb_master_port,
        username=qb_master_username,
        password=qb_master_password,
    )
    _upsert_qb_client(
        db,
        role="slave",
        host=qb_slave_host,
        port=qb_slave_port,
        username=qb_slave_username,
        password=qb_slave_password,
    )
    db.commit()
    return templates.TemplateResponse(request, "partials/settings_result.html", {"message": "Настройки сохранены", "ok": True})


@app.post("/settings/qb-test/{role}", response_class=HTMLResponse)
def qb_test_connection_settings(
    request: Request,
    role: str,
    qb_master_host: str = Form(default=""),
    qb_master_port: str = Form(default="8080"),
    qb_master_username: str = Form(default=""),
    qb_master_password: str = Form(default=""),
    qb_slave_host: str = Form(default=""),
    qb_slave_port: str = Form(default="8080"),
    qb_slave_username: str = Form(default=""),
    qb_slave_password: str = Form(default=""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    role_value = role.strip().lower()
    if role_value not in {"master", "slave"}:
        return templates.TemplateResponse(
            request,
            "partials/settings_result.html",
            {"message": f"Неизвестная роль qBittorrent: {role}", "ok": False},
        )

    if role_value == "master":
        host = qb_master_host or get_setting_value(db, "qb_master_host", "")
        port_raw = qb_master_port or get_setting_value(db, "qb_master_port", "8080")
        username = qb_master_username or get_setting_value(db, "qb_master_username", "")
        password = _resolve_qb_password(db, "master", qb_master_password)
    else:
        host = qb_slave_host or get_setting_value(db, "qb_slave_host", "")
        port_raw = qb_slave_port or get_setting_value(db, "qb_slave_port", "8080")
        username = qb_slave_username or get_setting_value(db, "qb_slave_username", "")
        password = _resolve_qb_password(db, "slave", qb_slave_password)

    try:
        result = test_qb_connection(
            host=host,
            port=_parse_port(port_raw),
            username=username,
            password=password,
        )
        message = _qb_test_message(role_value, result)
        ok = True
    except Exception as exc:
        message = f"qBittorrent {role_value}: ошибка — {exc}"
        ok = False
    return templates.TemplateResponse(
        request,
        "partials/settings_result.html",
        {"message": message, "ok": ok},
    )


@app.post("/settings/anilibria-login", response_class=HTMLResponse)
async def anilibria_login_settings(
    request: Request,
    anilibria_login: str = Form(default=""),
    anilibria_password: str = Form(default=""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    password = resolve_anilibria_password(db, anilibria_password)
    try:
        await login_and_store_token(db, anilibria_login, password)
        message = "Вход выполнен: bearer token и passkey сохранены"
    except Exception as exc:
        message = f"Ошибка входа в AniLibria API: {exc}"
    return templates.TemplateResponse(request, "partials/settings_result.html", {"message": message})


@app.post("/settings/reset-state", response_class=HTMLResponse)
def reset_state_settings(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    stats = reset_operational_state(db)
    message = (
        f"Состояние сброшено: jobs={stats['jobs_removed']}, logs={stats['job_logs_removed']}, "
        f"seen={stats['seen_torrents_removed']}, pipeline={stats['pipeline_removed']}, "
        f"checkpoints={stats.get('checkpoints_removed', 0)}. "
        f"Архив сохранён: {stats['archive_kept']} записей."
    )
    return templates.TemplateResponse(request, "partials/settings_result.html", {"message": message})


@app.post("/settings/reset-full", response_class=HTMLResponse)
def reset_full_settings(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    stats = reset_full(db)
    message = (
        f"Полная очистка: archive={stats['archive_removed']}, files={stats['torrent_files_removed']}, "
        f"jobs={stats['jobs_removed']}, seen={stats['seen_torrents_removed']}, pipeline={stats['pipeline_removed']}."
    )
    return templates.TemplateResponse(request, "partials/settings_result.html", {"message": message})


@app.get("/extra-urls", response_class=HTMLResponse)
def extra_urls_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    rows = db.scalars(select(ExtraUrl).order_by(ExtraUrl.id.desc())).all()
    return templates.TemplateResponse(request, "extra_urls.html", {"rows": rows})


@app.get("/partials/extra-urls-table", response_class=HTMLResponse)
def extra_urls_table(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    rows = db.scalars(select(ExtraUrl).order_by(ExtraUrl.id.desc())).all()
    return templates.TemplateResponse(request, "partials/extra_urls_table.html", {"rows": rows})


@app.post("/extra-urls/save", response_class=HTMLResponse)
def save_extra_url(
    request: Request,
    row_id: int | None = Form(default=None),
    release_alias: str = Form(default=""),
    release_id: str = Form(default=""),
    note: str = Form(default=""),
    enabled: str | None = Form(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    release_id_value = int(release_id) if release_id.strip().isdigit() else None
    enabled_value = enabled == "on"
    if row_id:
        row = db.get(ExtraUrl, row_id)
        if row is not None:
            row.release_alias = release_alias.strip()
            row.release_id = release_id_value
            row.note = note
            row.enabled = enabled_value
    else:
        db.add(
            ExtraUrl(
                release_alias=release_alias.strip(),
                release_id=release_id_value,
                note=note,
                enabled=enabled_value,
            )
        )
    db.commit()
    rows = db.scalars(select(ExtraUrl).order_by(ExtraUrl.id.desc())).all()
    return templates.TemplateResponse(request, "partials/extra_urls_table.html", {"rows": rows})


@app.post("/extra-urls/{row_id}/delete", response_class=HTMLResponse)
def remove_extra_url(request: Request, row_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    row = db.get(ExtraUrl, row_id)
    if row is not None:
        db.delete(row)
        db.commit()
    rows = db.scalars(select(ExtraUrl).order_by(ExtraUrl.id.desc())).all()
    return templates.TemplateResponse(request, "partials/extra_urls_table.html", {"rows": rows})


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request,
    job_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    job_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _jobs_page_context(db, job_type=job_type, status=status, job_id=job_id)
    return templates.TemplateResponse(request, "jobs.html", context)


@app.get("/jobs/live", response_class=HTMLResponse)
def jobs_live(
    request: Request,
    job_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    job_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _jobs_page_context(db, job_type=job_type, status=status, job_id=job_id)
    return templates.TemplateResponse(request, "partials/jobs_live.html", context)


def _jobs_page_context(
    db: Session,
    *,
    job_type: str | None,
    status: str | None,
    job_id: int | None,
) -> dict:
    query = select(Job)
    if job_type:
        query = query.where(Job.type == job_type)
    if status:
        query = query.where(Job.status == status)
    jobs = db.scalars(query.order_by(Job.id.desc()).limit(200)).all()
    selected_job = db.get(Job, job_id) if job_id else (jobs[0] if jobs else None)
    logs: list = []
    if selected_job is not None:
        # В UI по умолчанию без debug — иначе шум от «уже обработан, пропуск».
        logs = list(
            db.scalars(
                select(JobLog)
                .where(JobLog.job_id == selected_job.id, JobLog.level != "debug")
                .order_by(JobLog.id.desc())
                .limit(500)
            ).all()
        )
    return {
        "jobs": jobs,
        "logs": logs,
        "selected_job": selected_job,
        "job_type": job_type or "",
        "status": status or "",
    }


@app.get("/pipeline", response_class=HTMLResponse)
def pipeline_page(
    request: Request,
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _pipeline_page_context(db, status=status)
    return templates.TemplateResponse(request, "pipeline.html", context)


@app.get("/pipeline/live", response_class=HTMLResponse)
def pipeline_live(
    request: Request,
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _pipeline_page_context(db, status=status)
    return templates.TemplateResponse(request, "partials/pipeline_live.html", context)


@app.post("/pipeline/reconcile", response_class=HTMLResponse)
async def pipeline_reconcile_action(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Ручная сверка pipeline без slave с master (альтернатива пропущенному webhook)."""
    try:
        job = job_runner.create_job(db, "pipeline_reconcile", {})
    except JobAlreadyRunningError as exc:
        message = f"Сверка уже выполняется (job_id={exc.running_job_id})"
        return templates.TemplateResponse(request, "partials/action_result.html", {"message": message})
    job_runner.schedule_job(job.id)
    message = f"Сверка с master запущена в фоне (job_id={job.id}) — смотри статус в списке джобов"
    return templates.TemplateResponse(request, "partials/action_result.html", {"message": message})


def _pipeline_page_context(db: Session, *, status: str | None) -> dict:
    query = select(TorrentPipeline)
    if status:
        query = query.where(TorrentPipeline.status == status)
    rows = list(db.scalars(query.order_by(TorrentPipeline.id.desc()).limit(300)).all())
    master_states: dict = {}
    try:
        master_states = TorrentPipelineService(db).get_master_ui_states([row.info_hash for row in rows])
    except Exception:
        master_states = {}
    return {
        "rows": rows,
        "master_states": master_states,
        "status": status or "",
    }


@app.get("/releases", response_class=HTMLResponse)
def releases_page(
    request: Request,
    search: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = list_release_groups(db, search=search, page=page, per_page=30)
    return templates.TemplateResponse(request, "releases.html", context)


@app.get("/archive", response_class=HTMLResponse)
def archive_page(
    request: Request,
    search: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    per_page = 20
    query = select(TorrentArchive)
    count_query = select(TorrentArchive)
    if search:
        query = query.where(TorrentArchive.anime_name.ilike(f"%{search}%"))
        count_query = count_query.where(TorrentArchive.anime_name.ilike(f"%{search}%"))

    rows = db.scalars(query.order_by(TorrentArchive.id.desc()).offset((page - 1) * per_page).limit(per_page)).all()
    total = db.scalar(select(func.count()).select_from(count_query.subquery())) or 0
    total_pages = max(1, (total + per_page - 1) // per_page)
    return templates.TemplateResponse(
        request,
        "archive.html",
        {
            "rows": rows,
            "search": search or "",
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        },
    )


@app.post("/actions/run/{job_type}", response_class=HTMLResponse)
async def run_job_action(
    request: Request,
    job_type: str,
    dry_run: bool = Form(default=True),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    params = {"dry_run": dry_run} if job_type == "cleanup" else {}
    try:
        job = job_runner.create_job(db, job_type, params)
    except UnknownJobTypeError as exc:
        message = (
            f"Неизвестный тип джоба: {exc.job_type}. "
            f"Допустимы: {', '.join(sorted(job_runner.known_types()))}"
        )
        return templates.TemplateResponse(request, "partials/action_result.html", {"message": message})
    except JobAlreadyRunningError as exc:
        message = f"Джоб типа {exc.job_type} уже выполняется (id={exc.running_job_id})"
        return templates.TemplateResponse(request, "partials/action_result.html", {"message": message})
    job_runner.schedule_job(job.id)
    message = f"Джоб {job.type} запущен в фоне (job_id={job.id}) — прогресс в списке ниже"
    return templates.TemplateResponse(request, "partials/action_result.html", {"message": message})
