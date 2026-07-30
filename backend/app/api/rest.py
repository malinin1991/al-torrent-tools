from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CleanupRule, ExtraUrl, Job, JobLog, QbClient, Setting, TorrentFile
from app.db.session import get_db
from app.jobs.cleanup import run_cleanup
from app.jobs.cleanup_logs import run_cleanup_logs
from app.jobs.full_sync import run_full_sync
from app.jobs.hash_backfill import run_hash_backfill
from app.jobs.hash_torrent import run_hash_torrent
from app.jobs.meta_sync import run_full_meta_sync, run_meta_sync
from app.jobs.ongoing import run_ongoing
from app.jobs.orphan_cleanup import run_orphan_cleanup
from app.jobs.pipeline_reconcile import load_torrent_bytes_with_fallback, run_pipeline_reconcile
from app.jobs.waiting_master_retry import run_waiting_master_retry
from app.jobs.waiting_slave_retry import run_waiting_slave_retry
from app.services.anilibria_auth import login_and_store_token
from app.services.db_maintenance import purge_false_orphan_events, reset_full, reset_operational_state
from app.services.file_hasher import normalize_file_hash_workers_setting
from app.services.job_runner import JobAlreadyRunningError, JobRunner, UnknownJobTypeError
from app.services.pipeline import TorrentPipelineService
from app.services.qbittorrent import qb_client_wait_message, sanitize_info_hash, should_wait_for_qb, test_qb_connection
from app.services.releases_view import (
    probe_torrent_media_files,
    resolve_media_file_for_download,
    torrent_allows_media_download,
)
from app.services.runtime_settings import SECRET_SETTING_KEYS, get_setting_value, mask_settings_dict
from app.services.system_status import collect_system_status
from app.services.torrent_archive import TorrentArchiveService
from app.utils.datetime_fmt import as_utc_iso


router = APIRouter(prefix="/api")
job_runner = JobRunner()
job_runner.register("ongoing", run_ongoing)
job_runner.register("full_sync", run_full_sync)
job_runner.register("meta_sync", run_meta_sync)
job_runner.register("full_meta_sync", run_full_meta_sync)
job_runner.register("cleanup_master", run_cleanup)
job_runner.register("cleanup_slave", run_cleanup)
job_runner.register("cleanup_logs", run_cleanup_logs)
job_runner.register("pipeline_reconcile", run_pipeline_reconcile)
job_runner.register("waiting_master_retry", run_waiting_master_retry)
job_runner.register("waiting_slave_retry", run_waiting_slave_retry)
job_runner.register("hash_torrent", run_hash_torrent)
job_runner.register("hash_backfill", run_hash_backfill)
job_runner.register("orphan_cleanup", run_orphan_cleanup)


class JobCreateIn(BaseModel):
    type: str
    params: dict = {}


class SettingsIn(BaseModel):
    key: str
    value: str


class ExtraUrlIn(BaseModel):
    release_alias: str
    release_id: int | None = None
    note: str = ""
    enabled: bool = True


class CleanupRuleIn(BaseModel):
    name: str
    tracker_host: str
    message_contains: str
    include_errored: bool = True
    delete_files: bool = False
    target_client: str = "both"
    enabled: bool = True


class AniLibriaLoginIn(BaseModel):
    login: str
    password: str


class QbTestIn(BaseModel):
    host: str = ""
    port: int = 8080
    username: str = ""
    password: str = ""


class ArchiveListOut(BaseModel):
    items: list[dict]
    page: int
    per_page: int
    total: int


@router.get("/jobs")
def list_jobs(
    job_type: str | None = Query(default=None, alias="type"),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[dict]:
    query = select(Job)
    if job_type:
        query = query.where(Job.type == job_type)
    if status:
        query = query.where(Job.status == status)
    jobs = db.scalars(query.order_by(Job.id.desc()).limit(200)).all()
    return [{"id": item.id, "type": item.type, "status": item.status, "error": item.error} for item in jobs]


@router.post("/jobs")
def create_job(payload: JobCreateIn, db: Session = Depends(get_db)) -> dict:
    job_type = payload.type
    params = dict(payload.params or {})
    # Legacy: type=cleanup → cleanup_master.
    if job_type == "cleanup":
        job_type = "cleanup_master"
        params.setdefault("target_role", "master")
    elif job_type == "cleanup_master":
        params.setdefault("target_role", "master")
    elif job_type == "cleanup_slave":
        params.setdefault("target_role", "slave")
    try:
        job = job_runner.create_job(db, job_type, params)
    except UnknownJobTypeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Неизвестный тип джоба: {exc.job_type}. Допустимы: {sorted(job_runner.known_types())}",
        ) from exc
    except JobAlreadyRunningError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Джоб типа {exc.job_type} уже выполняется (id={exc.running_job_id})",
        ) from exc
    job_runner.schedule_job(job.id)
    return {"id": job.id, "type": job.type, "status": job.status, "queued": True}


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db)) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Джоб не найден")
    logs = db.scalars(select(JobLog).where(JobLog.job_id == job.id).order_by(JobLog.id.asc())).all()
    return {
        "id": job.id,
        "type": job.type,
        "status": job.status,
        "error": job.error,
        "logs": [
            {
                "level": line.level,
                "message": line.message,
                "created_at": as_utc_iso(line.created_at),
            }
            for line in logs
        ],
    }


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(Setting)).all()
    return mask_settings_dict({item.key: item.value for item in rows})


@router.get("/info")
async def api_system_info(db: Session = Depends(get_db)) -> dict:
    """Краткий статус API, токенов, qB master/slave и версий библиотек."""
    return await collect_system_status(db)


@router.put("/settings")
def put_settings(items: list[SettingsIn], db: Session = Depends(get_db)) -> dict:
    for item in items:
        row = db.get(Setting, item.key)
        value_to_store = item.value
        if item.key in SECRET_SETTING_KEYS and not item.value.strip() and row is not None:
            value_to_store = row.value
        if item.key == "file_hash_workers":
            value_to_store = normalize_file_hash_workers_setting(value_to_store)
        if row is None:
            row = Setting(key=item.key, value=value_to_store)
            db.add(row)
        else:
            row.value = value_to_store
    db.commit()
    return {"updated": len(items)}


@router.post("/settings/anilibria/login")
async def api_anilibria_login(payload: AniLibriaLoginIn, db: Session = Depends(get_db)) -> dict:
    try:
        await login_and_store_token(db, payload.login, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {"ok": True, "message": "Bearer token сохранён"}


@router.post("/settings/qb/{role}/test")
def api_qb_test_connection(role: str, payload: QbTestIn | None = None, db: Session = Depends(get_db)) -> dict:
    role_value = role.strip().lower()
    if role_value not in {"master", "slave"}:
        raise HTTPException(status_code=400, detail=f"Неизвестная роль: {role}")

    data = payload or QbTestIn()
    host = data.host.strip() or get_setting_value(db, f"qb_{role_value}_host", "")
    port = data.port if data.port else int(get_setting_value(db, f"qb_{role_value}_port", "8080") or "8080")
    username = data.username.strip() or get_setting_value(db, f"qb_{role_value}_username", "")
    password = data.password
    if not password.strip():
        password = get_setting_value(db, f"qb_{role_value}_password", "")
        if not password.strip():
            client = db.scalar(select(QbClient).where(QbClient.role == role_value).limit(1))
            password = client.password_encrypted if client is not None else ""

    try:
        return test_qb_connection(host=host, port=port, username=username, password=password)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/maintenance/reset-state")
def api_reset_state(db: Session = Depends(get_db)) -> dict:
    return reset_operational_state(db)


@router.post("/maintenance/reset-full")
def api_reset_full(db: Session = Depends(get_db)) -> dict:
    return reset_full(db)


@router.post("/maintenance/purge-false-orphans")
def api_purge_false_orphans(db: Session = Depends(get_db)) -> dict:
    """Удалить ложные orphan-события (чужие тайтлы после скана save_path года)."""
    return purge_false_orphan_events(db)


@router.get("/extra-urls")
def list_extra_urls(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(ExtraUrl).order_by(ExtraUrl.id.desc())).all()
    return [
        {
            "id": item.id,
            "release_alias": item.release_alias,
            "release_id": item.release_id,
            "note": item.note,
            "enabled": item.enabled,
        }
        for item in rows
    ]


@router.post("/extra-urls")
def create_extra_url(payload: ExtraUrlIn, db: Session = Depends(get_db)) -> dict:
    row = ExtraUrl(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id}


@router.put("/extra-urls/{row_id}")
def update_extra_url(row_id: int, payload: ExtraUrlIn, db: Session = Depends(get_db)) -> dict:
    row = db.get(ExtraUrl, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.commit()
    return {"ok": True}


@router.delete("/extra-urls/{row_id}")
def delete_extra_url(row_id: int, db: Session = Depends(get_db)) -> dict:
    row = db.get(ExtraUrl, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.get("/cleanup-rules")
def list_cleanup_rules(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(CleanupRule).order_by(CleanupRule.id.desc())).all()
    return [
        {
            "id": row.id,
            "name": row.name,
            "tracker_host": row.tracker_host,
            "message_contains": row.message_contains,
            "include_errored": row.include_errored,
            "delete_files": row.delete_files,
            "target_client": row.target_client,
            "enabled": row.enabled,
        }
        for row in rows
    ]


@router.post("/cleanup-rules")
def create_cleanup_rule(payload: CleanupRuleIn, db: Session = Depends(get_db)) -> dict:
    row = CleanupRule(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id}


@router.put("/cleanup-rules/{row_id}")
def update_cleanup_rule(row_id: int, payload: CleanupRuleIn, db: Session = Depends(get_db)) -> dict:
    row = db.get(CleanupRule, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Правило не найдено")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.commit()
    return {"ok": True}


@router.delete("/cleanup-rules/{row_id}")
def delete_cleanup_rule(row_id: int, db: Session = Depends(get_db)) -> dict:
    row = db.get(CleanupRule, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Правило не найдено")
    db.delete(row)
    db.commit()
    return {"ok": True}


def _create_and_run_job(db: Session, job_type: str, params: dict) -> Job:
    try:
        job = job_runner.create_job(db, job_type, params)
    except UnknownJobTypeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Неизвестный тип джоба: {exc.job_type}. Допустимы: {sorted(job_runner.known_types())}",
        ) from exc
    except JobAlreadyRunningError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Джоб типа {exc.job_type} уже выполняется (id={exc.running_job_id})",
        ) from exc
    return job


@router.post("/jobs/ongoing/run")
async def run_ongoing_job(db: Session = Depends(get_db)) -> dict:
    job = _create_and_run_job(db, "ongoing", {})
    job_runner.schedule_job(job.id)
    return {"id": job.id, "type": job.type, "status": job.status, "error": job.error, "queued": True}


@router.post("/jobs/full-sync/run")
async def run_full_sync_job(
    force_qb_load: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> dict:
    job = _create_and_run_job(db, "full_sync", {"force_qb_load": force_qb_load})
    job_runner.schedule_job(job.id)
    return {"id": job.id, "type": job.type, "status": job.status, "error": job.error, "queued": True}


@router.post("/jobs/meta-sync/run")
async def run_meta_sync_job(
    release_id: int | None = Query(default=None),
    torrent_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict:
    params: dict = {}
    if release_id is not None:
        params["release_id"] = release_id
    if torrent_id is not None:
        params["torrent_id"] = torrent_id
    if not params:
        raise HTTPException(
            status_code=400,
            detail="meta_sync: укажите release_id и/или torrent_id",
        )
    job = _create_and_run_job(db, "meta_sync", params)
    job_runner.schedule_job(job.id)
    return {"id": job.id, "type": job.type, "status": job.status, "error": job.error, "queued": True}


@router.post("/jobs/full-meta-sync/run")
async def run_full_meta_sync_job(db: Session = Depends(get_db)) -> dict:
    job = _create_and_run_job(db, "full_meta_sync", {})
    job_runner.schedule_job(job.id)
    return {"id": job.id, "type": job.type, "status": job.status, "error": job.error, "queued": True}


@router.post("/jobs/cleanup/run")
async def run_cleanup_job(
    dry_run: bool = True,
    target: str = Query(default="master"),
    db: Session = Depends(get_db),
) -> dict:
    # Удаление только при CLEANUP_ALLOW_DELETE=true и ?dry_run=false; иначе только отчёт.
    role = (target or "master").strip().lower()
    if role not in {"master", "slave"}:
        raise HTTPException(status_code=400, detail="target должен быть master или slave")
    job_type = f"cleanup_{role}"
    job = _create_and_run_job(db, job_type, {"dry_run": dry_run, "target_role": role})
    job_runner.schedule_job(job.id)
    return {"id": job.id, "type": job.type, "status": job.status, "error": job.error, "queued": True}


@router.post("/jobs/cleanup-logs/run")
async def run_cleanup_logs_job(db: Session = Depends(get_db)) -> dict:
    job = _create_and_run_job(db, "cleanup_logs", {})
    job_runner.schedule_job(job.id)
    return {"id": job.id, "type": job.type, "status": job.status, "error": job.error, "queued": True}


@router.post("/jobs/hash-backfill/run")
async def run_hash_backfill_job(
    reset_checkpoint: bool = False,
    db: Session = Depends(get_db),
) -> dict:
    job = _create_and_run_job(db, "hash_backfill", {"reset_checkpoint": reset_checkpoint})
    job_runner.schedule_job(job.id)
    return {"id": job.id, "type": job.type, "status": job.status, "error": job.error, "queued": True}


@router.post("/jobs/orphan-cleanup/run")
async def run_orphan_cleanup_job(
    dry_run: bool = True,
    apply: bool = False,
    db: Session = Depends(get_db),
) -> dict:
    job = _create_and_run_job(db, "orphan_cleanup", {"dry_run": dry_run, "apply": apply})
    job_runner.schedule_job(job.id)
    return {"id": job.id, "type": job.type, "status": job.status, "error": job.error, "queued": True}


@router.get("/archive")
def list_archive(
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> ArchiveListOut:
    archive_service = TorrentArchiveService(db)
    return ArchiveListOut(**archive_service.list_archive(page=page, per_page=per_page, search=search))


@router.get("/archive/{archive_id}")
def get_archive(archive_id: int, db: Session = Depends(get_db)) -> dict:
    archive_service = TorrentArchiveService(db)
    archive = archive_service.get_archive(archive_id)
    if archive is None:
        raise HTTPException(status_code=404, detail="Запись архива не найдена")
    return archive_service.serialize_item(archive)


@router.get("/archive/{archive_id}/download")
def download_archive_file(archive_id: int, db: Session = Depends(get_db)) -> FileResponse:
    archive_service = TorrentArchiveService(db)
    archive = archive_service.get_archive(archive_id)
    if archive is None:
        raise HTTPException(status_code=404, detail="Запись архива не найдена")

    file_path = archive_service.resolve_file_path(archive)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Файл торрента не найден")

    return FileResponse(path=file_path, media_type="application/x-bittorrent", filename=file_path.name)


@router.get("/torrent-files/{file_id}/download")
def download_torrent_media_file(file_id: int, db: Session = Depends(get_db)) -> FileResponse:
    """Скачать media-файл с диска (только актуальный торрент + файл на диске)."""
    row = db.get(TorrentFile, file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Файл не найден")
    if not torrent_allows_media_download(db, row.info_hash or ""):
        raise HTTPException(status_code=404, detail="Файл недоступен для скачивания")
    path = resolve_media_file_for_download(row)
    if path is None:
        raise HTTPException(status_code=404, detail="Файл недоступен для скачивания")
    return FileResponse(path=path, filename=path.name, media_type="application/octet-stream")


@router.get("/torrents/{info_hash}/downloadable-files")
def list_torrent_downloadable_files(info_hash: str, db: Session = Depends(get_db)) -> dict:
    """Фоновая подгрузка: кнопки скачивания + оверлей «проверка» для .!qB."""
    normalized = sanitize_info_hash(info_hash) or (info_hash or "").strip().lower()
    probe = probe_torrent_media_files(db, normalized)
    return {
        "info_hash": normalized,
        "file_ids": probe.downloadable_ids,
        "checking_ids": probe.checking_ids,
    }


@router.api_route("/webhooks/qb/complete", methods=["GET", "POST"])
async def qb_complete_webhook(
    request: Request,
    hash_query: str | None = Query(default=None, alias="hash"),
    db: Session = Depends(get_db),
) -> dict:
    """Webhook от qBittorrent master: Run external program on torrent finished.

    Ожидает info hash v1 (`%I`) в query `?hash=` или JSON `{"hash":"..."}` (POST).
    Если pipeline с таким hash есть и статус `master_added` — добавляет торрент на slave.
    """
    info_hash = (hash_query or "").strip().lower()
    if not info_hash and request.method == "POST":
        content_type = (request.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            try:
                raw_payload = await request.json()
            except Exception:
                raw_payload = {}
            if isinstance(raw_payload, dict):
                info_hash = str(raw_payload.get("hash") or "").strip().lower()
    if not info_hash:
        raise HTTPException(status_code=400, detail="Не передан hash (ожидается %I из qBittorrent)")

    try:
        info_hash = sanitize_info_hash(info_hash)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    pipeline_service = TorrentPipelineService(db, actor="webhook")
    pipeline = pipeline_service.get_latest_by_hash(info_hash)
    if pipeline is None:
        raise HTTPException(
            status_code=404,
            detail=f"Pipeline с hash={info_hash} не найден (торрент не из AL Torrent Tools?)",
        )

    if pipeline.status == TorrentPipelineService.STATUS_DONE:
        return {"ok": True, "status": pipeline.status, "message": "Pipeline уже завершен"}
    if pipeline.status == TorrentPipelineService.STATUS_SLAVE_ADDED:
        return {"ok": True, "status": pipeline.status, "message": "Pipeline уже добавлен на slave"}
    if pipeline.status == TorrentPipelineService.STATUS_FAILED:
        return {
            "ok": False,
            "status": pipeline.status,
            "pipeline_id": pipeline.id,
            "message": pipeline.error or "Pipeline в статусе failed",
        }
    if pipeline.status == TorrentPipelineService.STATUS_CANCELLED:
        return {
            "ok": False,
            "status": pipeline.status,
            "pipeline_id": pipeline.id,
            "message": pipeline.error or "Pipeline отменён",
        }
    if pipeline.status == TorrentPipelineService.STATUS_DISCOVERED:
        return {
            "ok": False,
            "status": pipeline.status,
            "pipeline_id": pipeline.id,
            "message": "Торрент ещё не добавлен на master",
        }
    if pipeline.status == TorrentPipelineService.STATUS_WAITING_MASTER:
        return {
            "ok": False,
            "status": pipeline.status,
            "pipeline_id": pipeline.id,
            "message": "Ожидание доступности master — торрент ещё не добавлен",
        }
    if pipeline.status not in {
        TorrentPipelineService.STATUS_MASTER_ADDED,
        TorrentPipelineService.STATUS_MASTER_COMPLETE,
        TorrentPipelineService.STATUS_WAITING_SLAVE,
    }:
        return {
            "ok": False,
            "status": pipeline.status,
            "pipeline_id": pipeline.id,
            "message": f"Неожиданный статус pipeline: {pipeline.status}",
        }

    # Не досылать на slave, пока торрент на master ещё качается (ложный/ранний webhook).
    if pipeline.status == TorrentPipelineService.STATUS_MASTER_ADDED:
        try:
            master_state = pipeline_service.classify_master_torrent(pipeline)
        except Exception as exc:
            return {
                "ok": False,
                "status": pipeline.status,
                "pipeline_id": pipeline.id,
                "message": f"Не удалось проверить master: {exc}",
            }
        if master_state == "in_progress":
            return {
                "ok": False,
                "status": pipeline.status,
                "pipeline_id": pipeline.id,
                "message": "Торрент на master ещё не завершён — досылка на slave отклонена",
            }
        if master_state == "missing":
            pipeline_service.mark_cancelled(
                pipeline,
                "Webhook: торрент отсутствует на master — pipeline cancelled",
            )
            return {
                "ok": False,
                "status": pipeline.status,
                "pipeline_id": pipeline.id,
                "message": pipeline.error or "Торрент отсутствует на master",
            }

    try:
        torrent_file = await load_torrent_bytes_with_fallback(db, pipeline_service, pipeline)
        if torrent_file is None:
            raise RuntimeError("Нет .torrent в архиве и не удалось загрузить файл")
        updated = pipeline_service.process_completion(pipeline, torrent_file)
        if updated.status == TorrentPipelineService.STATUS_WAITING_SLAVE:
            return {
                "ok": False,
                "status": updated.status,
                "pipeline_id": updated.id,
                "message": updated.error or "Slave недоступен — pipeline в waiting_slave",
            }
        return {"ok": True, "status": updated.status, "pipeline_id": updated.id}
    except HTTPException:
        raise
    except Exception as exc:
        if should_wait_for_qb(exc):
            return {
                "ok": False,
                "status": pipeline.status,
                "pipeline_id": pipeline.id,
                "message": qb_client_wait_message("master", exc),
            }
        pipeline_service.mark_failed(pipeline, str(exc))
        raise HTTPException(status_code=500, detail=f"Ошибка обработки pipeline: {exc}") from exc
