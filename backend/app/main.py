from pathlib import Path
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import Depends, FastAPI, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.api.rest import job_runner, router as api_router
from app.core.config import settings
from app.db.models import (
    ExtraUrl,
    Job,
    JobLog,
    PipelineEvent,
    QbClient,
    Setting,
    TorrentArchive,
    TorrentPipeline,
    TrackedRelease,
)
from app.db.session import SessionLocal, get_db
from app.services.job_catalog import load_job_catalog
from app.services.job_runner import (
    JobAlreadyRunningError,
    UnknownJobTypeError,
    reclaim_orphan_jobs,
    reclaim_stale_jobs,
    shutdown_cancel_active_jobs,
)
from app.services.anilibria_auth import login_and_store_token, resolve_anilibria_password
from app.services.db_maintenance import reset_full, reset_operational_state
from app.services.pipeline import TorrentPipelineService
from app.services.qbittorrent import test_qb_connection
from app.services.runtime_settings import SECRET_SETTING_KEYS, build_anilibria_client, get_setting_value
from app.services.file_hasher import normalize_file_hash_workers_setting
from app.services.hevc_pairing import (
    age_hours,
    classify_archive_codec,
    find_unpaired_avc,
    overdue_hours_past_sla,
    sla_age_source,
    sync_hevc_pair_events_for_release,
)
from app.services.releases_view import (
    build_archive_page_rows,
    format_torrent_files_summary,
    list_release_groups,
)
from app.services.system_status import collect_system_status
from app.services.ui_events import parse_channels, sse_event_stream
from app.services.telegram_notify import (
    SOURCE_UI,
    enqueue_tracking_toggle_notification,
    normalize_telegram_bot_api_base,
    resolve_telegram_bot_api_base,
    test_telegram_get_me,
    upsert_tracked_release,
)
from app.services.torrent_archive import resolve_torrent_storage_root
from app.utils.datetime_fmt import as_utc_iso, utcnow


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from app.logging_filters import setup_redacted_logging

    setup_redacted_logging(level=logging.INFO)
    if settings.app_env.lower() != "dev" and settings.secret_key == "change-me":
        raise RuntimeError("Для окружения вне dev требуется задать SECRET_KEY")
    resolve_torrent_storage_root().mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        # Сироты после kill: lock свободен → сразу cancelled (не ждём JOB_STALE_MINUTES).
        cancelled = reclaim_orphan_jobs(
            db,
            reason="API перезапущен — джоб-сирота помечен как cancelled",
        )
        if cancelled:
            logger.warning("При старте api помечены cancelled джобы-сироты: %s", cancelled)
        stale = reclaim_stale_jobs(
            db,
            reason="API перезапущен — джобы без активности помечены как cancelled",
        )
        if stale:
            logger.warning("При старте api помечены cancelled зависшие джобы: %s", stale)
    yield
    cancelled = shutdown_cancel_active_jobs(reason="API остановлен — джоб помечен как cancelled")
    if cancelled:
        logger.warning("При остановке api помечены cancelled активные джобы: %s", cancelled)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(api_router)

base_path = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(base_path / "templates"))
templates.env.filters["as_utc_iso"] = as_utc_iso
templates.env.filters["torrent_files_summary"] = format_torrent_files_summary
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


@app.get("/health/live")
async def health_live(db: Session = Depends(get_db)) -> dict:
    """Лёгкий probe для Docker healthcheck (без AniLibria)."""
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/health")
async def health(db: Session = Depends(get_db)) -> dict:
    client = build_anilibria_client(db)
    return {"status": "ok", "anilibria": await client.health()}


@app.get("/ui/events")
async def ui_events(
    request: Request,
    channels: str = Query(default=""),
) -> StreamingResponse:
    """SSE: именованные события каналов при смене change-token (без HTML в payload)."""
    channel_list = parse_channels(channels)
    return StreamingResponse(
        sse_event_stream(request, channel_list),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/info", response_class=HTMLResponse)
async def info_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    status = await collect_system_status(db)
    return templates.TemplateResponse(request, "info.html", {"status": status})


@app.get("/info/live", response_class=HTMLResponse)
async def info_live(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    status = await collect_system_status(db)
    return templates.TemplateResponse(request, "partials/info_live.html", {"status": status})


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
    has_telegram_token = bool((settings_map.get("telegram_bot_token") or "").strip())
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings_map": settings_map,
            "has_anilibria_token": has_anilibria_token,
            "has_anilibria_passkey": has_anilibria_passkey,
            "has_telegram_token": has_telegram_token,
        },
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
    telegram_bot_token: str = Form(default=""),
    telegram_chat_id: str = Form(default=""),
    telegram_bot_api_base_url: str = Form(default=""),
    telegram_enabled: str | None = Form(default=None),
    scrape_pause_every: str = Form(default=str(settings.scrape_pause_every)),
    scrape_pause_sec: str = Form(default=str(settings.scrape_pause_sec)),
    ongoing_interval_sec: str = Form(default=str(settings.ongoing_interval_sec)),
    cleanup_interval_sec: str = Form(default=str(settings.cleanup_interval_sec)),
    pipeline_master_min_age_min: str = Form(default=str(settings.pipeline_master_min_age_min)),
    pipeline_reconcile_interval_sec: str = Form(
        default=str(settings.pipeline_reconcile_interval_sec)
    ),
    file_hash_workers: str = Form(default=str(settings.file_hash_workers)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    workers_clamped = normalize_file_hash_workers_setting(file_hash_workers)
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
        "telegram_bot_token": telegram_bot_token,
        "telegram_chat_id": telegram_chat_id,
        "telegram_bot_api_base_url": telegram_bot_api_base_url,
        "telegram_enabled": "true" if telegram_enabled == "on" else "false",
        "scrape_pause_every": scrape_pause_every,
        "scrape_pause_sec": scrape_pause_sec,
        "ongoing_interval_sec": ongoing_interval_sec,
        "cleanup_interval_sec": cleanup_interval_sec,
        "pipeline_master_min_age_min": pipeline_master_min_age_min,
        "pipeline_reconcile_interval_sec": pipeline_reconcile_interval_sec,
        "file_hash_workers": workers_clamped,
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
async def qb_test_connection_settings(
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
        result = await asyncio.to_thread(
            test_qb_connection,
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


@app.post("/settings/telegram-test", response_class=HTMLResponse)
async def telegram_test_settings(
    request: Request,
    telegram_bot_token: str = Form(default=""),
    telegram_bot_api_base_url: str = Form(default=""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    token = telegram_bot_token.strip() or get_setting_value(db, "telegram_bot_token", "")
    # Пустое поле формы → дефолт api.telegram.org (не сырой пустой URL).
    base_url = normalize_telegram_bot_api_base(
        telegram_bot_api_base_url.strip() or resolve_telegram_bot_api_base(db)
    )
    try:
        me = await test_telegram_get_me(token=token, base_url=base_url)
        username = me.get("username") or me.get("first_name") or me.get("id")
        message = f"Telegram: OK — @{username} (id={me.get('id')}), base={base_url}"
        ok = True
    except Exception as exc:
        message = f"Telegram: ошибка — {exc}"
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


@app.get("/jobs/catalog/live", response_class=HTMLResponse)
def jobs_catalog_live(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/jobs_catalog.html",
        {"job_catalog": load_job_catalog(db)},
    )


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


@app.get("/jobs/list/live", response_class=HTMLResponse)
def jobs_list_live(
    request: Request,
    job_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    job_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _jobs_page_context(db, job_type=job_type, status=status, job_id=job_id)
    return templates.TemplateResponse(request, "partials/jobs_list_live.html", context)


@app.get("/jobs/detail/live", response_class=HTMLResponse)
def jobs_detail_live(
    request: Request,
    job_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    job_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _jobs_page_context(db, job_type=job_type, status=status, job_id=job_id)
    return templates.TemplateResponse(request, "partials/job_detail_live.html", context)


@app.get("/jobs/select/live", response_class=HTMLResponse)
def jobs_select_live(
    request: Request,
    job_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    job_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Выбор джоба без перезагрузки: детали + OOB-обновление списка."""
    context = _jobs_page_context(db, job_type=job_type, status=status, job_id=job_id)
    return templates.TemplateResponse(request, "partials/jobs_select_live.html", context)


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
        "job_catalog": load_job_catalog(db),
    }


@app.get("/pipeline", response_class=HTMLResponse)
async def pipeline_page(
    request: Request,
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = await _pipeline_page_context_async(db, status=status)
    return templates.TemplateResponse(request, "pipeline.html", context)


@app.get("/pipeline/live", response_class=HTMLResponse)
async def pipeline_live(
    request: Request,
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = await _pipeline_page_context_async(db, status=status)
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


@app.get("/pipeline/{pipeline_id}", response_class=HTMLResponse)
async def pipeline_detail_page(
    request: Request,
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = await _pipeline_detail_context_async(db, pipeline_id)
    return templates.TemplateResponse(request, "pipeline_detail.html", context)


@app.get("/pipeline/{pipeline_id}/live", response_class=HTMLResponse)
async def pipeline_detail_live(
    request: Request,
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = await _pipeline_detail_context_async(db, pipeline_id)
    return templates.TemplateResponse(request, "partials/pipeline_detail_live.html", context)


async def _pipeline_detail_context_async(db: Session, pipeline_id: int) -> dict:
    pipeline = db.get(TorrentPipeline, pipeline_id)
    if pipeline is None:
        raise HTTPException(status_code=404, detail=f"Pipeline {pipeline_id} не найден")

    events = list(
        db.scalars(
            select(PipelineEvent)
            .where(PipelineEvent.pipeline_id == pipeline_id)
            .order_by(PipelineEvent.id.asc())
        ).all()
    )
    job_ids = sorted({int(e.job_id) for e in events if e.job_id is not None})
    existing_jobs: set[int] = set()
    if job_ids:
        existing_jobs = set(
            db.scalars(select(Job.id).where(Job.id.in_(job_ids))).all()
        )

    # Сначала точный info_hash пайплайна; torrent_id — только fallback.
    archive = db.scalar(
        select(TorrentArchive)
        .where(TorrentArchive.info_hash == pipeline.info_hash)
        .order_by(TorrentArchive.id.desc())
        .limit(1)
    )
    if archive is None:
        archive = db.scalar(
            select(TorrentArchive)
            .where(TorrentArchive.torrent_id == pipeline.torrent_id)
            .order_by(TorrentArchive.id.desc())
            .limit(1)
        )
    release_name = None
    torrent_label = None
    if archive is not None:
        release_name = archive.anime_name or archive.release_alias
        parts = [p for p in (archive.torrent_type, archive.torrent_description) if p]
        torrent_label = " · ".join(parts) if parts else None

    timeline = []
    life_path_chunks: list[str] = []
    for event in events:
        job_exists = event.job_id is not None and event.job_id in existing_jobs
        details = event.details_json if isinstance(event.details_json, dict) else {}
        details_pretty = ""
        if details:
            details_pretty = json.dumps(details, ensure_ascii=False, indent=2, sort_keys=True)
        actor = details.get("actor")
        timeline.append(
            {
                "event": event,
                "job_exists": job_exists,
                "job_link": (
                    f"/jobs?job_id={event.job_id}" if job_exists else None
                ),
                "actor": actor,
                "details_pretty": details_pretty,
            }
        )
        status_part = ""
        if event.from_status or event.to_status:
            status_part = f" {event.from_status or '—'} → {event.to_status or '—'}"
        actor_part = f" actor={actor}" if actor else ""
        job_part = f" job=#{event.job_id}" if event.job_id is not None else ""
        chunk = (
            f"{event.created_at} [{event.event_type}]{status_part}{actor_part}{job_part}\n"
            f"{event.message or ''}"
        )
        if details_pretty:
            chunk += f"\nдетали:\n{details_pretty}"
        life_path_chunks.append(chunk)

    master_states: dict = {}
    try:
        master_states = await asyncio.to_thread(
            _load_master_ui_states, [pipeline.info_hash]
        )
    except Exception:
        master_states = {}

    return {
        "pipeline": pipeline,
        "timeline": timeline,
        "life_path_text": "\n\n".join(life_path_chunks),
        "release_name": release_name or f"Release #{pipeline.release_id}",
        "torrent_label": torrent_label or f"Torrent #{pipeline.torrent_id}",
        "master_state": master_states.get((pipeline.info_hash or "").lower(), {}),
    }


def _load_master_ui_states(info_hashes: list[str]) -> dict:
    """Опрос master в отдельном потоке (своя DB-сессия)."""
    with SessionLocal() as thread_db:
        try:
            return TorrentPipelineService(thread_db).get_master_ui_states(info_hashes)
        except Exception:
            return {}


async def _pipeline_page_context_async(db: Session, *, status: str | None) -> dict:
    context = _pipeline_page_context(db, status=status, master_states={})
    hashes = [row.info_hash for row in context["rows"]]
    context["master_states"] = await asyncio.to_thread(_load_master_ui_states, hashes)
    return context


def _pipeline_page_context(
    db: Session,
    *,
    status: str | None,
    master_states: dict | None = None,
) -> dict:
    query = select(TorrentPipeline)
    if status:
        query = query.where(TorrentPipeline.status == status)
    rows = list(db.scalars(query.order_by(TorrentPipeline.id.desc()).limit(300)).all())
    if master_states is None:
        try:
            master_states = TorrentPipelineService(db).get_master_ui_states(
                [row.info_hash for row in rows]
            )
        except Exception:
            master_states = {}

    archive_meta: dict[tuple[int, int], dict] = {}
    if rows:
        release_ids = {row.release_id for row in rows}
        torrent_ids = {row.torrent_id for row in rows}
        archives = db.scalars(
            select(TorrentArchive).where(
                TorrentArchive.release_id.in_(release_ids),
                TorrentArchive.torrent_id.in_(torrent_ids),
            )
        ).all()
        for archive in archives:
            key = (archive.release_id, archive.torrent_id)
            # Берём самую свежую запись
            prev = archive_meta.get(key)
            if prev is None or (archive.id or 0) > (prev.get("archive_id") or 0):
                archive_meta[key] = {
                    "archive_id": archive.id,
                    "anime_name": archive.anime_name,
                    "release_alias": archive.release_alias,
                    "torrent_description": archive.torrent_description,
                    "torrent_type": archive.torrent_type,
                }

    display_rows = []
    for row in rows:
        meta = archive_meta.get((row.release_id, row.torrent_id), {})
        release_name = meta.get("anime_name") or meta.get("release_alias") or f"Release #{row.release_id}"
        torrent_parts = [p for p in (meta.get("torrent_type"), meta.get("torrent_description")) if p]
        torrent_label = " · ".join(torrent_parts) if torrent_parts else f"Torrent #{row.torrent_id}"
        display_rows.append(
            {
                "row": row,
                "release_name": release_name,
                "torrent_label": torrent_label,
                "ids_title": f"release_id={row.release_id} torrent_id={row.torrent_id}",
            }
        )

    return {
        "rows": rows,
        "display_rows": display_rows,
        "master_states": master_states,
        "status": status or "",
    }


@app.get("/releases", response_class=HTMLResponse)
def releases_page(
    request: Request,
    search: str | None = Query(default=None),
    tracked_only: str | None = Query(default=None),
    hevc_filter: str | None = Query(default=None),
    show_hidden: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    only_tracked = tracked_only == "on"
    context = list_release_groups(
        db,
        search=search,
        tracked_only=only_tracked,
        hevc_filter=hevc_filter,
        show_hidden=show_hidden == "on",
        page=page,
        per_page=30,
    )
    return templates.TemplateResponse(request, "releases.html", context)


@app.get("/releases/live", response_class=HTMLResponse)
def releases_live(
    request: Request,
    search: str | None = Query(default=None),
    tracked_only: str | None = Query(default=None),
    hevc_filter: str | None = Query(default=None),
    show_hidden: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    only_tracked = tracked_only == "on"
    context = list_release_groups(
        db,
        search=search,
        tracked_only=only_tracked,
        hevc_filter=hevc_filter,
        show_hidden=show_hidden == "on",
        page=page,
        per_page=30,
    )
    return templates.TemplateResponse(request, "partials/releases_live.html", context)


@app.post("/releases/{release_id}/track", response_class=HTMLResponse)
def toggle_release_tracking(
    request: Request,
    release_id: int,
    enabled: str | None = Form(default=None),
    release_alias: str = Form(default=""),
    title: str = Form(default=""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    enabled_value = enabled == "on"
    alias = (release_alias or "").strip()
    title_value = (title or "").strip()
    if not alias or not title_value:
        archive = db.scalar(
            select(TorrentArchive)
            .where(TorrentArchive.release_id == release_id)
            .order_by(TorrentArchive.id.desc())
            .limit(1)
        )
        if archive is not None:
            alias = alias or (archive.release_alias or "")
            title_value = title_value or (archive.anime_name or alias or str(release_id))
    if not alias:
        alias = str(release_id)
    if not title_value:
        title_value = alias

    existing = db.get(TrackedRelease, release_id)
    was_enabled = bool(existing is not None and existing.enabled)
    if existing is None and not enabled_value:
        tracked = False
        source = None
    else:
        row = upsert_tracked_release(
            db,
            release_id=release_id,
            release_alias=alias,
            title=title_value,
            source=SOURCE_UI,
            enabled=enabled_value,
            commit=False,
        )
        tracked = bool(row.enabled)
        source = row.source
        # Title/alias для TG — из DB после upsert, не сырой Form.
        if tracked != was_enabled:
            enqueue_tracking_toggle_notification(
                db,
                enabled=tracked,
                title=(row.title or row.release_alias or str(release_id)),
                commit=False,
            )
        db.commit()

    return templates.TemplateResponse(
        request,
        "partials/release_track_toggle.html",
        {
            "release_id": release_id,
            "release_alias": alias,
            "anime_name": title_value,
            "tracked": tracked,
            "track_source": source,
        },
    )


@app.post("/releases/archive/{archive_id}/ignore-hevc", response_class=HTMLResponse)
def toggle_ignore_hevc(
    request: Request,
    archive_id: int,
    enabled: str | None = Form(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX: «Игнорировать HEVC» на AVC-строке /releases."""
    archive = db.get(TorrentArchive, archive_id)
    if archive is None:
        raise HTTPException(status_code=404, detail="Архив не найден")
    if bool(getattr(archive, "superseded", False)) or not bool(
        getattr(archive, "api_present", True)
    ):
        raise HTTPException(status_code=400, detail="Только для актуального торрента")
    qj = archive.quality_json if isinstance(archive.quality_json, dict) else None
    codec = classify_archive_codec(quality_json=qj, torrent_type=archive.torrent_type)
    if codec != "AVC":
        raise HTTPException(status_code=400, detail="Игнор HEVC только для AVC")

    archive.ignore_hevc = enabled == "on"
    db.commit()
    try:
        sync_hevc_pair_events_for_release(db, int(archive.release_id))
    except Exception:
        logger.exception(
            "hevc_status sync после ignore_hevc archive_id=%s", archive_id
        )

    # Пересчитаем бейдж для HTMX-ячейки (без полного list_release_groups).
    # Все строки релиза: superseded нужны для якоря overdue у преемника.
    siblings = list(
        db.scalars(
            select(TorrentArchive).where(
                TorrentArchive.release_id == archive.release_id,
            )
        ).all()
    )
    unpaired = {
        item.archive_id: item for item in find_unpaired_avc(siblings, now=utcnow())
    }.get(int(archive.id))
    if unpaired is not None:
        age_past = overdue_hours_past_sla(unpaired.age_hours)
        age_from_api = bool(unpaired.age_from_api)
    else:
        clock_at, age_from_api = sla_age_source(archive)
        age_past = overdue_hours_past_sla(age_hours(clock_at, now=utcnow()))
    row = SimpleNamespace(
        torrent_type=archive.torrent_type,
        hevc_pair_status=unpaired.status if unpaired else None,
        hevc_pair_age_hours=age_past,
        hevc_overdue_age_from_api=age_from_api,
        codec_family=codec,
        archive_id=archive.id,
        ignore_hevc=bool(archive.ignore_hevc),
    )
    return templates.TemplateResponse(
        request,
        "partials/release_torrent_type_cell.html",
        {"t": row},
    )


@app.get("/archive", response_class=HTMLResponse)
def archive_page(
    request: Request,
    search: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _archive_page_context(db, search=search, page=page)
    return templates.TemplateResponse(request, "archive.html", context)


@app.get("/archive/live", response_class=HTMLResponse)
def archive_live(
    request: Request,
    search: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _archive_page_context(db, search=search, page=page)
    return templates.TemplateResponse(request, "partials/archive_live.html", context)


def _archive_page_context(
    db: Session,
    *,
    search: str | None,
    page: int,
) -> dict:
    per_page = 20
    query = select(TorrentArchive)
    count_query = select(TorrentArchive)
    if search:
        query = query.where(TorrentArchive.anime_name.ilike(f"%{search}%"))
        count_query = count_query.where(TorrentArchive.anime_name.ilike(f"%{search}%"))

    archives = db.scalars(
        query.order_by(TorrentArchive.id.desc()).offset((page - 1) * per_page).limit(per_page)
    ).all()
    total = db.scalar(select(func.count()).select_from(count_query.subquery())) or 0
    total_pages = max(1, (total + per_page - 1) // per_page)
    rows = build_archive_page_rows(db, list(archives))
    return {
        "rows": rows,
        "search": search or "",
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


@app.post("/actions/run/{job_type}", response_class=HTMLResponse)
async def run_job_action(
    request: Request,
    job_type: str,
    dry_run: bool = Form(default=True),
    apply: bool = Form(default=False),
    force_qb_load: bool = Form(default=False),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if job_type == "orphan_cleanup":
        params: dict = {"dry_run": dry_run, "apply": apply}
    elif job_type in ("cleanup_master", "cleanup_slave"):
        role = "master" if job_type == "cleanup_master" else "slave"
        params = {"dry_run": dry_run, "target_role": role}
    elif job_type == "cleanup":
        # Legacy URL: направляем на master.
        params = {"dry_run": dry_run, "target_role": "master"}
        job_type = "cleanup_master"
    elif job_type == "full_sync":
        params = {"force_qb_load": force_qb_load}
    else:
        params = {}
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
    message = f"Джоб {job.type} запущен в фоне (job_id={job.id})"
    return templates.TemplateResponse(
        request,
        "partials/action_result.html",
        {
            "message": message,
            "job_link": f"/jobs?job_type={job.type}&job_id={job.id}",
        },
    )


@app.post("/actions/stop/{job_type}", response_class=HTMLResponse)
async def stop_job_action(
    request: Request,
    job_type: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    from app.services.job_runner import JobStopError, request_stop_latest_running

    try:
        job = request_stop_latest_running(db, job_type)
    except JobStopError as exc:
        return templates.TemplateResponse(
            request, "partials/action_result.html", {"message": str(exc)}
        )
    message = f"Остановка джоба {job.type} запрошена (job_id={job.id}, status=stopping)"
    return templates.TemplateResponse(
        request,
        "partials/action_result.html",
        {
            "message": message,
            "job_link": f"/jobs?job_type={job.type}&job_id={job.id}",
        },
    )
