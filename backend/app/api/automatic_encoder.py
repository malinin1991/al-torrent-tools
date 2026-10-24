"""Release policies, bounded queue view and explicit historical selection."""
from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, func, or_, select, update
from sqlalchemy.orm import Session

from app.db.models import (
    EncoderAutoEvent, EncoderAutoItem, ReleaseEncoderPolicy, TorrentArchive, TorrentFile, TrackedRelease,
)
from app.db.session import get_db
from app.services import automatic_encoder as auto
from app.services.encoder_history import safe_json
from app.services.encoder_submission import _require_video_kensetsu_base_url, _resolve_encoder_layers
from app.services.runtime_settings import get_setting_value
from app.services.static_assets import static_asset_url
from app.utils.datetime_fmt import as_utc_iso, utcnow


class PolicyIn(BaseModel):
    revision: int = Field(ge=0)
    video_id: str | None = Field(default=None, max_length=255)
    audio_id: str | None = Field(default=None, max_length=255)
    audio_downmix: str = "none"


class BackfillFile(BaseModel):
    file_id: int = Field(gt=0)
    info_hash: str = Field(max_length=128)
    relative_path: str = Field(max_length=8192)


class BackfillIn(BaseModel):
    revision: int = Field(ge=0)
    files: list[BackfillFile] = Field(min_length=1, max_length=100)


def policy_data(policy, release_id):
    video_id = policy.video_id if policy else None
    audio_id = policy.audio_id if policy else None
    snapshot = policy.presets_json if policy and isinstance(policy.presets_json, dict) else {}
    video = snapshot.get("video") if isinstance(snapshot.get("video"), dict) else {}
    audio = snapshot.get("audio") if isinstance(snapshot.get("audio"), dict) else {}
    return {
        "release_id": release_id, "revision": policy.revision if policy else 0,
        "video_id": video_id, "audio_id": audio_id,
        "video_label": video.get("name") or video_id,
        "audio_label": audio.get("name") or audio_id,
        "mode": "automatic" if not video_id and not audio_id else "custom",
        "audio_downmix": policy.audio_downmix if policy else "none",
        "participating": bool(policy and policy.participating),
    }


def context(db, *, page=1, search="", release_id=None, preview_page=1):
    condition = [TrackedRelease.enabled.is_(True)]
    if search:
        term = "%" + search[:200] + "%"
        condition.append(or_(TrackedRelease.title.ilike(term), TrackedRelease.release_alias.ilike(term),
                             cast(TrackedRelease.release_id, String).ilike(term)))
    if release_id:
        condition.append(TrackedRelease.release_id == release_id)
    total = db.scalar(select(func.count()).select_from(TrackedRelease).where(*condition)) or 0
    pages = max(1, (total + 19) // 20)
    page = max(1, min(page, pages))
    rows = db.execute(select(TrackedRelease, ReleaseEncoderPolicy).outerjoin(
        ReleaseEncoderPolicy, ReleaseEncoderPolicy.release_id == TrackedRelease.release_id
    ).where(*condition).order_by(TrackedRelease.title, TrackedRelease.release_id).offset((page-1)*20).limit(20)).all()
    releases = [{"title": t.title, **policy_data(p, t.release_id)} for t, p in rows]
    queue_query = select(EncoderAutoItem)
    if release_id:
        queue_query = queue_query.where(EncoderAutoItem.release_id == release_id)
    counts = dict(db.execute(select(EncoderAutoItem.state, func.count()).group_by(EncoderAutoItem.state)).all())
    oldest = db.scalar(select(func.min(EncoderAutoItem.created_at)).where(EncoderAutoItem.state == "ready"))
    queue = db.scalars(queue_query.order_by(EncoderAutoItem.updated_at.desc(), EncoderAutoItem.id.desc()).limit(50)).all()
    previews = []
    preview_pages = 1
    if release_id:
        # One bounded page; exact identity accompanies every checkbox.
        preview_query = select(TorrentFile, TorrentArchive).join(
            TorrentArchive, TorrentArchive.info_hash == TorrentFile.info_hash).where(
            TorrentFile.release_id == release_id, TorrentArchive.api_present.is_(True),
            TorrentArchive.superseded.is_(False), TorrentFile.ui_status.in_(("new", "changed"))
        )
        preview_count = db.scalar(select(func.count()).select_from(preview_query.subquery())) or 0
        preview_pages = max(1, (preview_count+29)//30)
        preview_page = min(max(1, preview_page), preview_pages)
        files = db.execute(preview_query.order_by(TorrentFile.id).limit(30).offset((preview_page-1)*30)).all()
        from app.services.hevc_pairing import classify_archive_codec
        for file, archive in files:
            if auto.is_video_file(file.relative_path) and classify_archive_codec(
                    quality_json=archive.quality_json, torrent_type=archive.torrent_type) == "AVC":
                prior = auto.previous_receipt(db, file)
                previews.append({"blocked": prior[0].status if prior else None, "file_id": file.id, "info_hash": file.info_hash,
                                 "relative_path": file.relative_path, "selected": file.selected,
                                 "verified": auto.readiness_matches(file, auto.source_readiness(db, file))})
    params = {"page": page, "search": search[:200], "preview_page": preview_page}
    if release_id:
        params["release_id"] = release_id
    heartbeat = get_setting_value(db, "encoder_auto_heartbeat_at", "")
    try:
        from datetime import datetime
        late = (utcnow() - datetime.fromisoformat(heartbeat.replace("Z", "+00:00")).replace(tzinfo=None)).total_seconds() > 120
    except ValueError:
        late = True
    return {"releases": releases, "queue": queue, "counts": counts, "oldest": oldest,
            "auto_enabled": auto.enabled(db), "heartbeat": heartbeat, "worker_late": late,
            "reason_labels": auto.REASONS, "search": search[:200], "release_id": release_id,
            "page": page, "total_pages": pages, "total": total, "previews": previews,
            "preview_page": preview_page, "preview_pages": preview_pages,
            "live_query": urlencode(params)}


def create_router(templates):
    templates.env.globals["static_asset_url"] = static_asset_url
    router = APIRouter()

    @router.get("/automatic-encoder")
    def page(request: Request, page: int = Query(1, ge=1), search: str = "", release_id: int | None = Query(None, gt=0), preview_page: int = Query(1, ge=1),
             db: Session = Depends(get_db)):
        return templates.TemplateResponse(request, "automatic_encoder.html", context(db, page=page, search=search, release_id=release_id, preview_page=preview_page))

    @router.get("/automatic-encoder/live")
    def live(request: Request, page: int = Query(1, ge=1), search: str = "", release_id: int | None = Query(None, gt=0), preview_page: int = Query(1, ge=1),
             db: Session = Depends(get_db)):
        return templates.TemplateResponse(request, "partials/automatic_encoder_live.html", context(db, page=page, search=search, release_id=release_id, preview_page=preview_page))

    @router.get("/api/automatic-encoder/releases/{release_id}")
    def get_policy(release_id: int, db: Session = Depends(get_db)):
        return policy_data(db.get(ReleaseEncoderPolicy, release_id), release_id)

    @router.put("/api/automatic-encoder/releases/{release_id}")
    async def save_policy(release_id: int, body: PolicyIn, db: Session = Depends(get_db)):
        if not 0 < release_id <= 2_147_483_647:
            raise HTTPException(422, "Некорректный release_id")
        video_id = (body.video_id or "").strip() or None
        audio_id = (body.audio_id or "").strip() or None
        if bool(video_id) != bool(audio_id):
            raise HTTPException(422, "Выберите оба пресета")
        snapshot = {}
        base_url = None
        if video_id:
            base_url = _require_video_kensetsu_base_url(db)
            db.commit()
            layers = await _resolve_encoder_layers(base_url, video_id=video_id, audio_id=audio_id,
                                                    audio_downmix=body.audio_downmix)
            snapshot = safe_json(layers["preset_snapshot"])
        auto.lock(db)
        if base_url and _require_video_kensetsu_base_url(db) != base_url:
            raise HTTPException(409, "Сервер изменился во время проверки пресетов")
        policy = auto.policy_for(db, release_id)
        if policy.revision != body.revision:
            raise HTTPException(409, "Настройки уже изменены в другой вкладке; откройте форму заново")
        policy.video_id, policy.audio_id = video_id, audio_id
        policy.audio_downmix = layers["audio_downmix"] if video_id else "none"
        policy.presets_json, policy.checked_at = snapshot, utcnow() if video_id else None
        policy.revision += 1
        auto.audit(db, "policy_saved", release_id=release_id, revision=policy.revision,
                   video_id=policy.video_id, audio_id=policy.audio_id, downmix=policy.audio_downmix)
        for item in db.scalars(select(EncoderAutoItem).where(
            EncoderAutoItem.release_id == release_id,
            EncoderAutoItem.state.in_(("waiting", "needs_attention")),
            EncoderAutoItem.block_reason.in_(("preset_changed", "preset")))).all():
            auto.transition(db, item, "waiting", "settings")
            item.next_attempt_at = utcnow()
        db.commit()
        return policy_data(policy, release_id)

    @router.post("/api/automatic-encoder/releases/{release_id}/backfill")
    def backfill(release_id: int, body: BackfillIn, db: Session = Depends(get_db)):
        auto.lock(db)
        policy = db.get(ReleaseEncoderPolicy, release_id)
        tracked = db.get(TrackedRelease, release_id)
        if not policy or not policy.participating or not tracked or not tracked.enabled:
            raise HTTPException(409, "Включите автоматический кодировщик и отслеживание релиза")
        if policy.revision != body.revision:
            raise HTTPException(409, "Параметры изменились; обновите предпросмотр")
        ids = []
        from app.services.hevc_pairing import classify_archive_codec
        # Validate the entire selection before registering any candidate.
        selected = []
        for source in body.files:
            file = db.get(TorrentFile, source.file_id)
            archive = db.scalar(select(TorrentArchive).where(
                TorrentArchive.info_hash == source.info_hash, TorrentArchive.release_id == release_id,
                TorrentArchive.api_present.is_(True), TorrentArchive.superseded.is_(False)))
            if (not file or file.release_id != release_id or not archive
                    or (file.info_hash, file.relative_path) != (source.info_hash, source.relative_path)
                    or file.ui_status not in {"new", "changed"} or not file.selected
                    or not auto.is_video_file(file.relative_path)
                    or classify_archive_codec(quality_json=archive.quality_json, torrent_type=archive.torrent_type) != "AVC"):
                raise HTTPException(409, "Выбранная версия больше недоступна; обновите предпросмотр")
            if auto.previous_receipt(db, file):
                raise HTTPException(409, "Источник уже отправлен или приём требует проверки; откройте историю")
            selected.append(file)
        for file in selected:
            item = auto.register_file(db, file, explicit=True)
            if item:
                ids.append(item.id)
        db.commit()
        return {"queued": len(set(ids)), "item_ids": ids}

    return router
