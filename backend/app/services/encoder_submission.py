"""Shared manual/automatic submission service; no dependency on REST or main."""
from __future__ import annotations
import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any
import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from app.db.models import TorrentFile
from app.services import encoder_history
from app.services.releases_view import _info_hashes_with_active_hash_job, resolve_media_file_for_download, torrent_allows_media_download
from app.services.video_kensetsu import (ENCODE_BATCH_MAX_FILES, VideoKensetsuHttpError,
    audio_downmix_forbidden_for_audio, coerce_preset_available, encode as video_kensetsu_encode,
    encode_timeout_for_paths, find_layer_preset, is_video_kensetsu_enabled,
    list_presets as video_kensetsu_list_presets, normalize_audio_downmix, resolve_video_kensetsu_base_url,
    build_payload)

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
    if getattr(row, "is_checking", False) is True:
        return None, "Файл проверяется; дождитесь завершения проверки"
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
        return HTTPException(status_code=status, detail=str(encoder_history.safe_json(exc.message)))
    return HTTPException(status_code=502, detail=f"Ошибка кодировщика: {encoder_history.safe_json(exc)}")


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


def _normalize_created_count(encoder_payload: dict[str, Any], status_code: int) -> int | None:
    """Нормализует ``created`` в int: True→1, False→0; иначе fallback из jobs/job."""
    created = encoder_payload.get("created")
    if isinstance(created, bool):
        return int(created)
    if isinstance(created, int) and not isinstance(created, bool):
        return created
    if status_code not in (200, 207):
        return None
    jobs = encoder_payload.get("jobs")
    if isinstance(jobs, list) and jobs:
        return len(jobs)
    if encoder_payload.get("job") or encoder_payload.get("job_id"):
        return 1
    return 0


async def _resolve_encoder_layers(
    base_url: str,
    *,
    video_id: str | None,
    audio_id: str | None,
    audio_downmix: str = "none",
    use_defaults: bool = False,
) -> dict[str, Any]:
    cleaned_video = (video_id or "").strip()
    cleaned_audio = (audio_id or "").strip()
    if not cleaned_video and not use_defaults:
        raise HTTPException(status_code=400, detail="Не передан video_id")
    if not cleaned_audio and not use_defaults:
        raise HTTPException(status_code=400, detail="Не передан audio_id")
    if not use_defaults:
        try:
            cleaned_downmix = normalize_audio_downmix(audio_downmix, default="none")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if audio_downmix_forbidden_for_audio(cleaned_audio, cleaned_downmix):
            raise HTTPException(status_code=400, detail="audio_downmix=stereo недоступен для audio_id copy/none")
    try:
        layers = await video_kensetsu_list_presets(base_url, strict_defaults=True) if use_defaults else await video_kensetsu_list_presets(base_url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Video Kensetsu недоступен: {exc}") from exc
    videos = layers.get("videos") if isinstance(layers, dict) else None
    audios = layers.get("audios") if isinstance(layers, dict) else None
    if not isinstance(videos, list) or not isinstance(audios, list):
        raise HTTPException(status_code=502, detail="Некорректный ответ пресетов Video Kensetsu")
    if use_defaults:
        defaults = layers.get("defaults")
        if (not isinstance(defaults, dict)
                or any(not isinstance(defaults.get(key), str) or not defaults[key].strip()
                       for key in ("video_id", "audio_id", "audio_downmix"))):
            raise HTTPException(status_code=502, detail="Kensetsu не передал действующий пресет по умолчанию")
        cleaned_video, cleaned_audio = defaults["video_id"].strip(), defaults["audio_id"].strip()
        audio_downmix = defaults["audio_downmix"]
        try:
            cleaned_downmix = normalize_audio_downmix(audio_downmix, default="none")
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if audio_downmix_forbidden_for_audio(cleaned_audio, cleaned_downmix):
            raise HTTPException(status_code=502, detail="audio_downmix=stereo недоступен для audio_id copy/none")
    video = find_layer_preset(videos, cleaned_video)
    if video is None:
        raise HTTPException(status_code=400, detail=f"Неизвестный video_id: {cleaned_video}")
    audio = find_layer_preset(audios, cleaned_audio)
    if audio is None:
        raise HTTPException(status_code=400, detail=f"Неизвестный audio_id: {cleaned_audio}")
    if not coerce_preset_available(audio.get("available"), default=True):
        raise HTTPException(
            status_code=400,
            detail=f"Аудио-пресет недоступен на кодировщике: {cleaned_audio}",
        )
    return {
        "video_id": cleaned_video,
        "audio_id": cleaned_audio,
        "audio_downmix": cleaned_downmix,
        "preset_snapshot": {"video": video, "audio": audio},
    }


def _encoder_receipt_response(group) -> JSONResponse:
    result = encoder_history.serialize(group)
    recovery = group.recovery_json or {}
    display_body = recovery.get("body") if isinstance(recovery.get("body"), dict) else (group.response_json or {})
    result["created"] = _normalize_created_count(display_body, recovery.get("status_code") or group.http_status or 200)
    code = 202 if group.finished_at is None else 200 if result["ok"] else 207
    return JSONResponse(content=result, status_code=code)



def record_pending(group, body):
    group.http_status = 202
    group.response_json = encoder_history.safe_json(body if isinstance(body, dict) else {})
    group.updated_at = encoder_history.utcnow()
    group.receipt_next_at = group.updated_at + timedelta(seconds=2)


def check_source_conflicts(group):
    for item in group.items:
        if item.status == "not_sent":
            continue
        try:
            stat = Path(item.full_path).stat()
            same = (stat.st_size == item.source_json.get("size") and stat.st_mtime_ns == item.source_json.get("mtime_ns")
                    and stat.st_ino == item.source_json.get("inode", stat.st_ino)
                    and stat.st_dev == item.source_json.get("device", stat.st_dev))
        except OSError:
            same = False
        if not same:
            item.source_json = {**item.source_json, "source_conflict": True}
            item.error = "Исходник изменился во время приёма; проверьте задание Kensetsu"
        item.source_json = {**item.source_json, "source_checked_after_post": encoder_history.as_utc_iso(encoder_history.utcnow())}

async def submit_files(file_ids: list[int], body: Any, db: Session, *, single: bool = False, hooks=None, auto_item_id=None, claim_token=None) -> JSONResponse:
    if hooks is not None:
        require_url = hooks._require_video_kensetsu_base_url
        resolve_path = hooks._resolve_encode_path_for_file
        resolve_layers = hooks._resolve_encoder_layers
        active_hashes_for = hooks._info_hashes_with_active_hash_job
        encode_files = hooks.video_kensetsu_encode
    else:
        require_url, resolve_path, resolve_layers = _require_video_kensetsu_base_url, _resolve_encode_path_for_file, _resolve_encoder_layers
        active_hashes_for, encode_files = _info_hashes_with_active_hash_job, video_kensetsu_encode
    ids = list(dict.fromkeys(file_ids))
    if not ids or any(fid <= 0 or fid > 2_147_483_647 for fid in ids):
        raise HTTPException(status_code=400, detail="Не переданы корректные file_ids")
    if len(ids) > ENCODE_BATCH_MAX_FILES:
        raise HTTPException(status_code=400, detail="Слишком много файлов")
    use_defaults = bool(auto_item_id) and not body.video_id and not body.audio_id
    requested_video, requested_audio = body.video_id or "", body.audio_id or ""
    try:
        downmix = normalize_audio_downmix("none" if use_defaults else body.audio_downmix)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    digest = encoder_history.request_digest(ids, requested_video, requested_audio, downmix, body.retry_of)
    layers = None
    try:
        if body.request_id:
            encoder_history.recover_expired(db)
            old = encoder_history.replay(db, body.request_id, digest)
            if old is not None:
                return _encoder_receipt_response(old)
        base_url = require_url(db)
        if use_defaults:
            # Replay above remains independent of later changes in defaults.
            # Resolve before claim so the receipt stores the actual parameters.
            db.commit()
            layers = await resolve_layers(base_url, video_id=None, audio_id=None, use_defaults=True)
            effective_video, effective_audio, downmix = layers["video_id"], layers["audio_id"], layers["audio_downmix"]
        else:
            effective_video, effective_audio = requested_video.strip(), requested_audio.strip()
        if body.retry_of is not None:
            parent = encoder_history.get_submission(db, body.retry_of)
            if parent is None:
                raise HTTPException(status_code=404, detail="Предыдущая отправка не найдена")
            originals = {i.file_id: i for i in parent.items}
            for fid in ids:
                original = originals.get(fid)
                current = db.get(TorrentFile, fid)
                if original is None or current is None or (current.info_hash, current.relative_path) != (original.info_hash, original.relative_path):
                    raise HTTPException(status_code=409, detail="Исходная версия файла больше недоступна; выберите текущую версию в релизе")
        group, owner = encoder_history.claim(
            db, file_ids=ids, request_id=body.request_id, digest=digest, base_url=base_url,
            video_id=effective_video, audio_id=effective_audio, downmix=downmix,
            retry_of=body.retry_of, allow_repeat=body.allow_repeat, acknowledge_unknown=body.acknowledge_unknown,
            origin="auto" if auto_item_id else "manual", auto_item_id=auto_item_id, claim_token=claim_token,
        )
    except encoder_history.SubmissionConflict as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "previous": exc.previous}) from exc
    if not owner:
        return _encoder_receipt_response(group)
    submission_id = group.id
    started = False
    try:
        paths = []
        active_hashes = active_hashes_for(db, [item.info_hash for item in group.items])
        for item in group.items:
            if item.status == "not_sent":
                continue
            path, error = resolve_path(db, item.file_id)
            if item.info_hash.lower() in active_hashes:
                error = "Идёт проверка хешей исходника; дождитесь завершения"
            if not error and path in paths:
                error = "Этот путь уже включён в отправку другим выбранным файлом"
            if error or not path:
                item.status = "not_sent"
                item.error = error or "Файл недоступен"
                continue
            try:
                encoder_history.capture_path(db, item, path)
            except OSError:
                item.status = "not_sent"
                item.error = "Файл исчез или недоступен перед отправкой"
                continue
            paths.append(path)
        if not paths:
            if single:
                raise HTTPException(status_code=404, detail=group.items[0].error or "Файл недоступен для кодирования")
            raise HTTPException(status_code=400, detail={"message": "Нет файлов для отправки в кодировщик", "local_errors": [{"file_id": i.file_id, "error": i.error} for i in group.items]})
        db.commit()
        if layers is None:
            layers = await resolve_layers(base_url, video_id=body.video_id, audio_id=body.audio_id, audio_downmix=downmix)
        group.presets_json = encoder_history.safe_json(layers.get("preset_snapshot", {}))
        db.commit()  # expire cached source rows after the asynchronous preflight
        paths = []
        active_hashes = active_hashes_for(db, [item.info_hash for item in group.items])
        for item in group.items:
            if item.status != "sending":
                continue
            current = db.get(TorrentFile, item.file_id)
            path, error = resolve_path(db, item.file_id)
            if item.info_hash.lower() in active_hashes:
                error = "Идёт проверка хешей исходника; дождитесь завершения"
            try:
                stat = Path(path).stat() if path else None
            except OSError:
                stat = None
            if (error or current is None or path != item.full_path or stat is None
                    or current.info_hash != item.info_hash
                    or (getattr(current, "relative_path", "") or "") != item.relative_path
                    or stat.st_size != item.source_json.get("size")
                    or stat.st_mtime_ns != item.source_json.get("mtime_ns")
                    or stat.st_ino != item.source_json.get("inode", stat.st_ino)
                    or stat.st_dev != item.source_json.get("device", stat.st_dev)):
                item.status = "not_sent"
                item.error = "Исходник изменился или стал недоступен во время подготовки; выберите актуальную версию"
            else:
                paths.append(path)
        if not paths:
            raise HTTPException(status_code=409, detail="Исходники изменились во время подготовки; отправка не выполнялась")
        from app.services.automatic_encoder import lock, validate_intent
        lock(db)
        if require_url(db) != base_url:
            raise HTTPException(status_code=409, detail="Сервер кодировщика изменился во время подготовки")
        if auto_item_id:
            validate_intent(db, auto_item_id, claim_token, group.presets_json)
        group.payload_json = build_payload(paths, layers["video_id"], layers["audio_id"], layers["audio_downmix"], group.request_id)
        group.request_started_at = encoder_history.utcnow()
        group.receipt_next_at = group.request_started_at + timedelta(seconds=600)
        group.updated_at = group.request_started_at
        db.commit()  # durable intent before an uncertain external side effect
        started = True
        request_id = group.request_id
        db.commit()
        encoded = await encode_files(
            base_url, paths=paths, video_id=layers["video_id"], audio_id=layers["audio_id"],
            audio_downmix=layers["audio_downmix"], timeout_sec=encode_timeout_for_paths(len(paths)),
            request_id=request_id,
        )
        status_code = int(encoded.get("status_code") or 200)
        if status_code == 202:
            record_pending(group, encoded.get("body"))
        else:
            encoder_history.settle_response(group, status_code, encoded.get("body"))
            group.receipt_next_at = encoder_history.utcnow() if any(i.status == "unknown" for i in group.items) else None
        check_source_conflicts(group)
        db.commit()
        return _encoder_receipt_response(group)
    except asyncio.CancelledError:
        if started:
            check_source_conflicts(group)
        encoder_history.finish_failure(db, group, "Отправка прервана; проверьте историю Kensetsu", started=started)
        raise
    except Exception as exc:
        if isinstance(exc, SQLAlchemyError):
            db.rollback()
            group = encoder_history.get_submission(db, submission_id)
            if group is None:
                raise
            started = started or group.request_started_at is not None
        status = exc.status_code if isinstance(exc, VideoKensetsuHttpError) else None
        group.http_status = status
        # A documented validation refusal is different from a 5xx/timeout.
        if started and status in (400, 403, 404, 422):
            for item in group.items:
                if item.status == "sending":
                    item.status = "rejected"
                    item.error = str(encoder_history.safe_json(exc))
        definitely_unsent = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
        if started and not definitely_unsent:
            check_source_conflicts(group)
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        encoder_history.finish_failure(db, group, detail, started=started and not definitely_unsent)
        if started and not definitely_unsent and status not in (400, 403, 404, 422):
            group.receipt_next_at = encoder_history.utcnow()
            db.commit()
        error = exc if isinstance(exc, HTTPException) else _map_encoder_exception(exc)
        error.headers = {**(error.headers or {}), "X-Encoder-Submission-Id": str(group.id)}
        raise error

