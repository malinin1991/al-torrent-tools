"""Уведомления HEVC-бота: просрочка и первый AVC нового релиза после хеширования."""

from __future__ import annotations

from datetime import datetime, timezone
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import PipelineEvent, TelegramOutbox, TorrentArchive, TorrentFile
from app.services.hevc_pairing import (
    HEVC_SLA_HOURS,
    classify_archive_codec,
    find_unpaired_avc,
    load_file_keys_by_archive_id,
    sync_hevc_pair_events_for_release,
)
from app.services.hevc_bot import (
    TELEGRAM_TEXT_LIMIT,
    format_release_detail,
    query_release_detail,
    telegram_text_length,
)
from app.services.runtime_settings import get_setting_value
from app.services.telegram_access import HEVC_BOT_KEY, list_approved_group_ids
from app.services.telegram_notify import OUTBOX_PENDING
from app.services.torrent_qb_meta import build_release_admin_url
from app.utils.datetime_fmt import utcnow

logger = logging.getLogger(__name__)

# 2026-10-01 00:00 Asia/Novosibirsk; в БД даты naive UTC.
MISSING_RELEASE_ADDED_SINCE = datetime(2026, 9, 30, 17)


def sync_overdue_notifications(db: Session, *, now: datetime | None = None) -> int:
    """Переход SLA по часам, независимо от API checkpoints и открытия UI.

    Pairing считает весь архив вместе с историей; события синхронизируем лишь
    для просроченных релизов. Pure missing уведомляется отдельно после SLA
    для релизов, добавленных с 01.10.2026. Повторы дедуплицируются в outbox.
    Возвращает число событий overdue и поставленных missing-уведомлений.
    """
    current = now or utcnow()
    archives = list(db.scalars(select(TorrentArchive)).all())
    file_keys = load_file_keys_by_archive_id(db, archives)
    unpaired = find_unpaired_avc(
        archives, now=current, file_keys_by_archive_id=file_keys
    )
    release_ids = sorted(
        {item.release_id for item in unpaired if item.status == "overdue"}
    )
    emitted = 0
    for release_id in release_ids:
        try:
            emitted += sync_hevc_pair_events_for_release(db, release_id, now=current)
        except Exception:
            db.rollback()
            logger.exception(
                "Не удалось проверить просрочку HEVC релиза %s", release_id
            )
    # Чистый missing не становится overdue в pairing/UI. Уведомляем отдельно,
    # только для новых релизов, включая историю в определение даты добавления.
    first_added: dict[int, datetime] = {}
    for archive in archives:
        created = archive.created_at
        if created is None:
            continue
        if created.tzinfo is not None:
            created = created.astimezone(timezone.utc).replace(tzinfo=None)
        rid = int(archive.release_id)
        first_added[rid] = min(first_added.get(rid, created), created)
    missing_release_ids = sorted(
        {
            item.release_id
            for item in unpaired
            if item.status == "missing"
            and item.age_hours is not None
            and item.age_hours >= HEVC_SLA_HOURS
            and first_added.get(item.release_id, datetime.min)
            >= MISSING_RELEASE_ADDED_SINCE
        }
    )
    for release_id in missing_release_ids:
        try:
            emitted += _enqueue_release_notifications(
                db,
                release_id=release_id,
                kind="hevc_missing_sla",
                heading="⏰ <b>HEVC не появился за 24 часа</b>\n\n",
            )
        except Exception:
            db.rollback()
            logger.exception(
                "Не удалось поставить уведомление без HEVC релиза %s", release_id
            )
    return emitted


def enqueue_new_release_notification(
    db: Session,
    *,
    release_id: int,
    info_hash: str,
    hashed_files: int,
) -> int:
    """Первый архив релиза — AVC; все выбранные файлы успешно проверены хешером.

    Исторические строки учитываются: снятие с API/republish не делает релиз новым.
    Вызывается после hash_torrent, hashed_files включает проверенные gate-cache файлы.
    """
    first = db.scalar(
        select(TorrentArchive)
        .where(TorrentArchive.release_id == release_id)
        .order_by(TorrentArchive.id.asc())
        .limit(1)
    )
    normalized = info_hash.strip().lower()
    if (
        first is None
        or (first.info_hash or "").strip().lower() != normalized
        or not first.api_present
        or first.superseded
        or classify_archive_codec(
            quality_json=first.quality_json,
            torrent_type=first.torrent_type,
        )
        != "AVC"
    ):
        return 0
    files = list(
        db.scalars(
            select(TorrentFile).where(
                TorrentFile.info_hash == normalized,
                TorrentFile.selected.is_(True),
            )
        ).all()
    )
    if (
        not files
        or hashed_files < len(files)
        or any(f.is_checking or not f.media_present for f in files)
    ):
        return 0
    return _enqueue_release_notifications(
        db,
        release_id=release_id,
        kind="hevc_new_release",
        heading="🆕 <b>Обнаружен новый релиз</b>\nПервый AVC: хеширование файлов завершено.\n\n",
        info_hash=normalized,
    )


def _enqueue_release_notifications(
    db: Session,
    *,
    release_id: int,
    kind: str,
    heading: str,
    info_hash: str | None = None,
) -> int:
    """Одно сообщение данного типа на релиз и одобренный HEVC-чат."""
    chats = list_approved_group_ids(db, bot_key=HEVC_BOT_KEY)
    pending_chats = []
    for chat_id in chats:
        key = f"{kind}:{release_id}:chat:{chat_id}"
        if (
            db.scalar(
                select(TelegramOutbox.id)
                .where(
                    TelegramOutbox.bot_key == HEVC_BOT_KEY,
                    TelegramOutbox.dedupe_key == key,
                )
                .limit(1)
            )
            is None
        ):
            pending_chats.append((chat_id, key))
    if not pending_chats:
        return 0
    row = query_release_detail(db, release_id)
    if row is None:
        return 0
    text = heading + format_release_detail(
        row, max_len=TELEGRAM_TEXT_LIMIT - telegram_text_length(heading)
    )
    queued = 0
    for chat_id, key in pending_chats:
        try:
            with db.begin_nested():
                db.add(
                    TelegramOutbox(
                        # Уведомление о релизе не меняет tg_status primary-пайплайна.
                        pipeline_id=None,
                        bot_key=HEVC_BOT_KEY,
                        dedupe_key=key,
                        chat_id=str(chat_id),
                        payload_json={
                            "text": text,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True,
                            "kind": kind,
                            "release_id": release_id,
                            "info_hash": info_hash,
                            "reply_markup": _release_reply_markup(db, release_id),
                        },
                        status=OUTBOX_PENDING,
                        attempts=0,
                        created_at=utcnow(),
                    )
                )
                db.flush()
        except IntegrityError:
            continue
        queued += 1
    if queued:
        db.commit()
    return queued


def overdue_transition_dedupe_key(
    *,
    release_id: int,
    info_hash: str,
    prev_event_id: int,
    chat_id: int,
) -> str:
    """Устойчивый ключ входа в overdue: один переход, даже при параллельных event.id."""
    return (
        f"hevc_overdue:{int(release_id)}:{info_hash.strip().lower()}"
        f":after:{int(prev_event_id)}:chat:{int(chat_id)}"
    )


def _release_reply_markup(db: Session, release_id: int) -> dict:
    row = [{"text": "Детали", "callback_data": f"status:{release_id}"}]
    admin_template = get_setting_value(
        db,
        "anilibria_admin_url_template",
        settings.anilibria_admin_url_template,
        allow_empty=True,
    )
    admin_url = build_release_admin_url(release_id, admin_template)
    if admin_url:
        row.append({"text": "Админка", "url": admin_url})
    return {"inline_keyboard": [row]}


def enqueue_overdue_event_notifications(
    db: Session,
    event: PipelineEvent,
    *,
    commit: bool = True,
) -> int:
    """По одной записи на переход overdue+approved chat; повторный sync безопасен."""
    if event.event_type != "hevc_status" or event.to_status != "overdue":
        return 0
    if (event.from_status or "") == "overdue":
        return 0
    details = event.details_json if isinstance(event.details_json, dict) else {}
    release_id = details.get("release_id")
    info_hash = details.get("info_hash")
    if not isinstance(release_id, int):
        return 0
    if not isinstance(info_hash, str) or not info_hash.strip():
        return 0
    prev_event_id = details.get("prev_hevc_status_event_id")
    if not isinstance(prev_event_id, int):
        prev_event_id = 0
    row = query_release_detail(db, release_id)
    if row is None:
        return 0
    heading = "🔔 <b>Новая просрочка HEVC</b>\n\n"
    text = heading + format_release_detail(
        row,
        max_len=TELEGRAM_TEXT_LIMIT - telegram_text_length(heading),
    )
    queued = 0
    for chat_id in list_approved_group_ids(db, bot_key=HEVC_BOT_KEY):
        dedupe_key = overdue_transition_dedupe_key(
            release_id=release_id,
            info_hash=info_hash,
            prev_event_id=prev_event_id,
            chat_id=chat_id,
        )
        exists = db.scalar(
            select(TelegramOutbox.id)
            .where(
                TelegramOutbox.bot_key == HEVC_BOT_KEY,
                TelegramOutbox.dedupe_key == dedupe_key,
            )
            .limit(1)
        )
        if exists is not None:
            continue
        try:
            with db.begin_nested():
                db.add(
                    TelegramOutbox(
                        pipeline_id=event.pipeline_id,
                        bot_key=HEVC_BOT_KEY,
                        dedupe_key=dedupe_key,
                        chat_id=str(chat_id),
                        payload_json={
                            "text": text[:4096],
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True,
                            "kind": "hevc_overdue",
                            "release_id": release_id,
                            "event_id": event.id,
                            "info_hash": info_hash.strip().lower(),
                            "reply_markup": _release_reply_markup(db, release_id),
                        },
                        status=OUTBOX_PENDING,
                        attempts=0,
                        created_at=utcnow(),
                    )
                )
                db.flush()
        except IntegrityError:
            continue
        queued += 1
    if queued:
        if commit:
            db.commit()
        else:
            db.flush()
    return queued
