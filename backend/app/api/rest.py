from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CleanupRule, ExtraUrl, Job, JobLog, QbClient, Setting, TorrentFile
from app.db.session import get_db
from app.jobs.cleanup import run_cleanup
from app.jobs.cleanup_logs import run_cleanup_logs
from app.jobs.full_sync import run_full_sync
from app.jobs.hash_backfill import run_hash_backfill
from app.jobs.hash_torrent import run_hash_torrent
from app.jobs.mediainfo_sync import run_mediainfo_sync
from app.jobs.meta_sync import run_full_meta_sync, run_meta_sync
from app.jobs.ongoing import run_ongoing
from app.jobs.orphan_cleanup import run_orphan_cleanup
from app.jobs.pipeline_reconcile import load_torrent_bytes_with_fallback, run_pipeline_reconcile
from app.jobs.pipeline_resume_cancelled import run_pipeline_resume_cancelled
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
    probe_torrents_media_files,
    resolve_media_file_for_download,
    torrent_allows_media_download,
)
from app.services.runtime_settings import SECRET_SETTING_KEYS, get_setting_value, mask_settings_dict
from app.services.system_status import collect_system_status
from app.services.torrent_archive import TorrentArchiveService
from app.services.video_kensetsu import (
    ENCODE_BATCH_MAX_FILES,
    VideoKensetsuHttpError,
    coerce_preset_available,
    encode as video_kensetsu_encode,
    encode_timeout_for_paths,
    find_preset,
    is_video_kensetsu_enabled,
    list_presets as video_kensetsu_list_presets,
    resolve_video_kensetsu_base_url,
)
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
job_runner.register("pipeline_resume_cancelled", run_pipeline_resume_cancelled)
job_runner.register("waiting_master_retry", run_waiting_master_retry)
job_runner.register("waiting_slave_retry", run_waiting_slave_retry)
job_runner.register("hash_torrent", run_hash_torrent)
job_runner.register("hash_backfill", run_hash_backfill)
job_runner.register("mediainfo_sync", run_mediainfo_sync)
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


class SendToEncoderIn(BaseModel):
    preset_id: str


class SendToEncoderBatchIn(BaseModel):
    file_ids: list[int] = Field(default_factory=list, max_length=ENCODE_BATCH_MAX_FILES)
    preset_id: str


def _require_video_kensetsu_base_url(db: Session) -> str:
    if not is_video_kensetsu_enabled(db):
        raise HTTPException(status_code=400, detail="Video Kensetsu выключен в настройках")
    base_url = resolve_video_kensetsu_base_url(db)
    if not base_url:
        raise HTTPException(status_code=400, detail="Не задан URL Video Kensetsu")
    return base_url


def _resolve_encode_path_for_file(
    db: Session,
    file_id: int,
) -> tuple[str | None, str | None]:
    """Возвращает (encode_path, error). error заполнен при локальном fail."""
    row = db.get(TorrentFile, file_id)
    if row is None:
        return None, "Файл не найден"
    if not torrent_allows_media_download(db, row.info_hash or ""):
        return None, "Файл недоступен для кодирования"
    resolved = resolve_media_file_for_download(row)
    if resolved is None:
        return None, "Файл недоступен для кодирования"
    encode_path = str(resolved)
    if not encode_path:
        return None, "У файла нет пути для кодировщика"
    return encode_path, None


def _map_encoder_exception(exc: Exception) -> HTTPException:
    """Пробрасывает 400/403/(404) от энкодера; остальное → 502 с текстом."""
    if isinstance(exc, VideoKensetsuHttpError):
        status = exc.status_code if exc.status_code in (400, 403, 404, 409, 422) else 502
        return HTTPException(status_code=status, detail=exc.message)
    return HTTPException(status_code=502, detail=f"Ошибка кодировщика: {exc}")


def _match_encoder_error_file_id(
    err: dict[str, Any],
    paths: list[str],
    path_file_ids: list[int],
) -> int | None:
    marker = str(err.get("path") or err.get("filename") or "").strip()
    if not marker:
        return None
    for idx, path in enumerate(paths):
        if path == marker or path.endswith(marker) or path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] == marker:
            return path_file_ids[idx]
    return None


def _enrich_encoder_errors(
    raw_errors: Any,
    paths: list[str],
    path_file_ids: list[int],
) -> tuple[list[dict[str, Any]] | None, set[int]]:
    if not isinstance(raw_errors, list) or not raw_errors:
        return None, set()
    enriched: list[dict[str, Any]] = []
    failed: set[int] = set()
    for item in raw_errors:
        if not isinstance(item, dict):
            enriched.append({"error": str(item)})
            continue
        row = dict(item)
        fid = _match_encoder_error_file_id(row, paths, path_file_ids)
        if fid is not None:
            row["file_id"] = fid
            failed.add(fid)
        enriched.append(row)
    return enriched, failed


async def _resolve_encoder_preset(base_url: str, preset_id: str) -> str:
    cleaned = (preset_id or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Не передан preset_id")
    try:
        presets = await video_kensetsu_list_presets(base_url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Video Kensetsu недоступен: {exc}") from exc
    preset = find_preset(presets, cleaned)
    if preset is None:
        raise HTTPException(status_code=404, detail=f"Пресет не найден: {cleaned}")
    if not coerce_preset_available(preset.get("available"), default=True):
        raise HTTPException(
            status_code=400,
            detail=f"Пресет недоступен на кодировщике: {cleaned}",
        )
    return cleaned


@router.get("/video-kensetsu/presets")
async def video_kensetsu_presets(db: Session = Depends(get_db)) -> dict:
    """Прокси списка пресетов Video Kensetsu (без CORS с браузера)."""
    base_url = _require_video_kensetsu_base_url(db)
    try:
        presets = await video_kensetsu_list_presets(base_url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Video Kensetsu недоступен: {exc}") from exc
    return {
        "base_url": base_url,
        "presets": [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or item.get("id") or ""),
                "description": item.get("description"),
                "group": item.get("group"),
                "available": coerce_preset_available(item.get("available"), default=True),
            }
            for item in presets
            if item.get("id")
        ],
    }


@router.post("/torrent-files/send-to-encoder")
async def send_torrent_files_to_encoder_batch(
    body: SendToEncoderBatchIn,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Пакетная отправка media-файлов в Video Kensetsu."""
    base_url = _require_video_kensetsu_base_url(db)
    preset_id = await _resolve_encoder_preset(base_url, body.preset_id)

    file_ids = list(dict.fromkeys(int(fid) for fid in (body.file_ids or [])))
    if not file_ids:
        raise HTTPException(status_code=400, detail="Не переданы file_ids")
    if len(file_ids) > ENCODE_BATCH_MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Слишком много файлов за раз (макс. {ENCODE_BATCH_MAX_FILES})",
        )

    paths: list[str] = []
    path_file_ids: list[int] = []
    local_errors: list[dict[str, Any]] = []
    for file_id in file_ids:
        encode_path, error = _resolve_encode_path_for_file(db, file_id)
        if error or not encode_path:
            local_errors.append({"file_id": file_id, "error": error or "Файл недоступен"})
            continue
        paths.append(encode_path)
        path_file_ids.append(file_id)

    if not paths:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Нет файлов для отправки в кодировщик",
                "local_errors": local_errors,
            },
        )

    try:
        encoded = await video_kensetsu_encode(
            base_url,
            paths=paths,
            preset_id=preset_id,
            timeout_sec=encode_timeout_for_paths(len(paths)),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _map_encoder_exception(exc) from exc

    status_code = int(encoded.get("status_code") or 200)
    encoder_body = encoded.get("body")
    encoder_payload = encoder_body if isinstance(encoder_body, dict) else {"result": encoder_body}
    created = encoder_payload.get("created")
    if created is None and status_code in (200, 207):
        created = len(encoder_payload.get("jobs") or []) or (1 if encoder_payload.get("job") else 0)

    enriched_errors, failed_ids = _enrich_encoder_errors(
        encoder_payload.get("errors"),
        paths,
        path_file_ids,
    )
    accepted_file_ids = [fid for fid in path_file_ids if fid not in failed_ids]
    partial = status_code == 207 or bool(local_errors) or bool(enriched_errors)

    response_body: dict[str, Any] = {
        "ok": True,
        "partial": partial,
        "preset_id": preset_id,
        "created": created,
        "file_ids": accepted_file_ids,
        "jobs": encoder_payload.get("jobs"),
        "job": encoder_payload.get("job"),
        "errors": enriched_errors,
        "local_errors": local_errors or None,
        "result": encoder_payload,
    }
    http_status = 207 if partial else 200
    return JSONResponse(content=response_body, status_code=http_status)


@router.post("/torrent-files/{file_id}/send-to-encoder")
async def send_torrent_file_to_encoder(
    file_id: int,
    body: SendToEncoderIn,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Отправить media-файл в Video Kensetsu с выбранным пресетом."""
    base_url = _require_video_kensetsu_base_url(db)

    encode_path, error = _resolve_encode_path_for_file(db, file_id)
    if error or not encode_path:
        raise HTTPException(status_code=404, detail=error or "Файл недоступен для кодирования")

    preset_id = await _resolve_encoder_preset(base_url, body.preset_id)

    try:
        encoded = await video_kensetsu_encode(
            base_url,
            paths=[encode_path],
            preset_id=preset_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _map_encoder_exception(exc) from exc

    status_code = int(encoded.get("status_code") or 200)
    encoder_body = encoded.get("body")
    encoder_payload = encoder_body if isinstance(encoder_body, dict) else {"result": encoder_body}
    errors = encoder_payload.get("errors") if isinstance(encoder_payload, dict) else None
    if isinstance(errors, list) and not errors:
        errors = None
    partial = status_code == 207 or bool(errors)
    created = encoder_payload.get("created") if isinstance(encoder_payload, dict) else None
    if created is None and status_code in (200, 207):
        created = len(encoder_payload.get("jobs") or []) or (1 if encoder_payload.get("job") else 0)

    result: dict[str, Any] = {
        "ok": not partial,
        "partial": partial,
        "file_id": file_id,
        "path": encode_path,
        "preset_id": preset_id,
        "created": created,
        "result": encoder_body,
        "errors": errors,
        "job": encoder_payload.get("job") if isinstance(encoder_payload, dict) else None,
        "jobs": encoder_payload.get("jobs") if isinstance(encoder_payload, dict) else None,
        "status_code": status_code,
    }
    return JSONResponse(content=result, status_code=207 if partial else 200)


@router.get("/torrent-files/{file_id}/mediainfo")
def get_torrent_file_mediainfo(
    file_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Получить MediaInfo для файла торрента (с on-demand парсингом, если в БД пусто)."""
    from app.services.mediainfo import get_or_extract_mediainfo

    result = get_or_extract_mediainfo(db, file_id, force=False)
    if not result.get("ok") and result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail=result.get("error", "Файл не найден"))
    return result


@router.post("/torrent-files/{file_id}/mediainfo/refresh")
def refresh_torrent_file_mediainfo(
    file_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Принудительно перечитать MediaInfo для файла торрента с диска."""
    from app.services.mediainfo import get_or_extract_mediainfo

    result = get_or_extract_mediainfo(db, file_id, force=True)
    if not result.get("ok") and result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail=result.get("error", "Файл не найден"))
    return result


@router.get("/torrents/{info_hash}/downloadable-files")
def list_torrent_downloadable_files(
    info_hash: str,
    refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> dict:
    """Подгрузка: кнопки скачивания + overlay «проверка» из БД.

    refresh=1 — обновить media_present/is_checking с диска и master.
    """
    normalized = sanitize_info_hash(info_hash) or (info_hash or "").strip().lower()
    probe = probe_torrent_media_files(db, normalized, refresh=refresh)
    return {
        "info_hash": normalized,
        "file_ids": probe.downloadable_ids,
        "checking_ids": probe.checking_ids,
    }


class DownloadableFilesBatchIn(BaseModel):
    hashes: list[str] = []
    refresh: bool = False


@router.post("/torrents/downloadable-files")
def list_torrents_downloadable_files_batch(
    body: DownloadableFilesBatchIn,
    db: Session = Depends(get_db),
) -> dict:
    """Batch: один ответ на список info_hash. По умолчанию только БД."""
    raw_hashes = body.hashes or []
    if len(raw_hashes) > 100:
        raise HTTPException(status_code=400, detail="Слишком много hashes (макс. 100)")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_hashes:
        key = sanitize_info_hash(item) or (item or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    probes = probe_torrents_media_files(db, normalized, refresh=bool(body.refresh))
    items = []
    for key in normalized:
        probe = probes.get(key)
        items.append(
            {
                "info_hash": key,
                "file_ids": list(probe.downloadable_ids) if probe else [],
                "checking_ids": list(probe.checking_ids) if probe else [],
            }
        )
    return {"items": items}



@router.api_route("/webhooks/qb/complete", methods=["GET", "POST"])
async def qb_complete_webhook(
    request: Request,
    hash_query: str | None = Query(default=None, alias="hash"),
    role_query: str | None = Query(default=None, alias="role"),
    db: Session = Depends(get_db),
) -> dict:
    """Webhook от qBittorrent: Run external program on torrent finished.

    Обязательные query: `?hash=%I&role=master|slave` (или JSON `{"hash","role"}` на POST).
    - role=master → process_completion (досылка на slave → slave_added)
    - role=slave → process_slave_completion (slave seeding → done)
    """
    info_hash = (hash_query or "").strip().lower()
    role = (role_query or "").strip().lower()
    body: dict = {}
    if request.method == "POST":
        content_type = (request.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            try:
                raw_payload = await request.json()
            except Exception:
                raw_payload = {}
            if isinstance(raw_payload, dict):
                body = raw_payload
                if not info_hash:
                    info_hash = str(body.get("hash") or "").strip().lower()
                if not role:
                    role = str(body.get("role") or "").strip().lower()

    if not info_hash:
        raise HTTPException(status_code=400, detail="Не передан hash (ожидается %I из qBittorrent)")
    if role not in {"master", "slave"}:
        raise HTTPException(
            status_code=400,
            detail="Не передан role (ожидается role=master или role=slave)",
        )

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

    if role == "slave":
        return await _qb_complete_slave_role(pipeline_service, pipeline)

    return await _qb_complete_master_role(db, pipeline_service, pipeline)


async def _qb_complete_slave_role(
    pipeline_service: TorrentPipelineService,
    pipeline,
) -> dict:
    """role=slave: slave_added → проверить раздачу → done."""
    if pipeline.status in {
        TorrentPipelineService.STATUS_DISCOVERED,
        TorrentPipelineService.STATUS_WAITING_MASTER,
        TorrentPipelineService.STATUS_MASTER_ADDED,
        TorrentPipelineService.STATUS_MASTER_COMPLETE,
        TorrentPipelineService.STATUS_WAITING_SLAVE,
    }:
        return {
            "ok": False,
            "status": pipeline.status,
            "pipeline_id": pipeline.id,
            "message": "Рано: slave ещё не в пайплайне (ожидается slave_added)",
        }
    if pipeline.status != TorrentPipelineService.STATUS_SLAVE_ADDED:
        return {
            "ok": False,
            "status": pipeline.status,
            "pipeline_id": pipeline.id,
            "message": f"Неожиданный статус pipeline для role=slave: {pipeline.status}",
        }

    try:
        slave_state = pipeline_service.classify_slave_torrent(pipeline)
    except Exception as exc:
        if should_wait_for_qb(exc):
            return {
                "ok": False,
                "status": pipeline.status,
                "pipeline_id": pipeline.id,
                "message": qb_client_wait_message("slave", exc),
            }
        return {
            "ok": False,
            "status": pipeline.status,
            "pipeline_id": pipeline.id,
            "message": f"Не удалось проверить slave: {exc}",
        }

    if slave_state == "in_progress":
        return {
            "ok": False,
            "status": pipeline.status,
            "pipeline_id": pipeline.id,
            "message": "Торрент на slave ещё не завершён — done отклонён",
        }
    if slave_state == "missing":
        # Не cancelled: webhook «finished» при lag API / гонке; cancel — через aged poll.
        return {
            "ok": False,
            "status": pipeline.status,
            "pipeline_id": pipeline.id,
            "message": "Торрент на slave не найден — done отклонён (ждём poll)",
        }

    # complete: mark_done без повторного classify (TOCTOU missing→cancel).
    try:
        updated = pipeline_service.mark_done(
            pipeline,
            details={"qb_role": "slave", "slave_state": "complete", "actor": "webhook"},
        )
    except Exception as exc:
        if should_wait_for_qb(exc):
            return {
                "ok": False,
                "status": pipeline.status,
                "pipeline_id": pipeline.id,
                "message": qb_client_wait_message("slave", exc),
            }
        pipeline_service.mark_failed(pipeline, str(exc))
        raise HTTPException(status_code=500, detail=f"Ошибка обработки pipeline: {exc}") from exc

    if updated.status == TorrentPipelineService.STATUS_DONE:
        return {"ok": True, "status": updated.status, "pipeline_id": updated.id}
    return {
        "ok": False,
        "status": updated.status,
        "pipeline_id": updated.id,
        "message": updated.error or f"Ожидался done, получен {updated.status}",
    }


async def _qb_complete_master_role(
    db: Session,
    pipeline_service: TorrentPipelineService,
    pipeline,
) -> dict:
    """role=master: master_* / waiting_slave → досылка на slave."""
    if pipeline.status in TorrentPipelineService._SLAVE_REACHED:
        return {
            "ok": True,
            "status": pipeline.status,
            "pipeline_id": pipeline.id,
            "message": "Pipeline уже после master (на slave или done)",
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
