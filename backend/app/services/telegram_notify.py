"""Отслеживание релизов и очередь Telegram-уведомлений."""

from __future__ import annotations

from app.utils.datetime_fmt import utcnow
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import TelegramOutbox, TorrentPipeline, TrackedRelease
from app.services.pipeline import record_pipeline_event
from app.services.runtime_settings import get_setting_value

TG_STATUS_SKIPPED = "skipped"
TG_STATUS_PENDING = "pending"
TG_STATUS_QUEUED = "queued"
TG_STATUS_SENT = "sent"

OUTBOX_PENDING = "pending"
OUTBOX_SENT = "sent"
OUTBOX_FAILED = "failed"
OUTBOX_CANCELLED = "cancelled"

_USER_DM_TERMINAL_MARKERS = (
    "can't initiate conversation",
    "cannot initiate conversation",
    "bot can't initiate conversation with a user",
    "forbidden: bot was blocked by the user",
    "bot was blocked by the user",
    "user is deactivated",
    "chat not found",
    "forbidden: bot was blocked",
)

SOURCE_BOT = "bot"
SOURCE_UI = "ui"

DEFAULT_BOT_API_BASE = "https://api.telegram.org"
PRIMARY_BOT_KEY = "primary"

# Лимиты текста для TG notify (Telegram message ≤ 4096).
_TRACK_NOTIFY_TITLE_MAX = 300
_TELEGRAM_TEXT_MAX = 4096


def truncate_telegram_text(text: str, max_len: int = _TELEGRAM_TEXT_MAX) -> str:
    """Обрезает текст под лимит Telegram (с ellipsis)."""
    value = text or ""
    if len(value) <= max_len:
        return value
    if max_len <= 1:
        return value[:max_len]
    return value[: max_len - 1] + "…"


def _safe_notify_title(title: str) -> str:
    cleaned = (title or "").strip() or "релиз"
    return truncate_telegram_text(cleaned, _TRACK_NOTIFY_TITLE_MAX)


def escape_markdown_v2(text: str) -> str:
    escape_chars = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{ch}" if ch in escape_chars else ch for ch in text)


def normalize_telegram_bot_api_base(base_url: str | None) -> str:
    """Пустой base → api.telegram.org. Не использовать urljoin с токеном (в токене есть ':')."""
    raw = (base_url or "").strip().rstrip("/")
    if not raw:
        return DEFAULT_BOT_API_BASE
    if not raw.lower().startswith(("http://", "https://")):
        raw = f"https://{raw}"
    return raw.rstrip("/")


def resolve_hevc_bot_test_credentials(
    *,
    form_token: str,
    form_base_url: str,
    saved_token: str,
    saved_base_url: str,
) -> tuple[str, str]:
    """Не смешивает сохранённый HEVC-токен с произвольным Bot API URL из запроса.

    Нестандартный URL из формы требует токен в той же форме. Иначе тестируем
    только сохранённую пару URL+token (токен из формы может заменить сохранённый
    при том же URL).
    """
    token_from_form = (form_token or "").strip()
    url_from_form = (form_base_url or "").strip()
    token_saved = (saved_token or "").strip()
    saved_url = normalize_telegram_bot_api_base(saved_base_url)

    if url_from_form:
        request_url = normalize_telegram_bot_api_base(url_from_form)
        if request_url != saved_url and not token_from_form:
            raise ValueError(
                "Для нестандартного Bot API URL укажите HEVC-токен в той же форме"
            )
        return token_from_form or token_saved, request_url
    return token_from_form or token_saved, saved_url


def is_terminal_user_dm_error(error: str, chat_id: str) -> bool:
    """Постоянные 4xx Telegram в ЛС: бот не может первым писать пользователю."""
    raw_id = str(chat_id or "").strip()
    if not raw_id or raw_id.startswith("-"):
        return False
    folded = (error or "").casefold()
    if any(marker in folded for marker in _USER_DM_TERMINAL_MARKERS):
        return True
    if "http 403" in folded or folded.strip() == "403":
        return True
    if "http 401" in folded:
        return True
    return False


def build_telegram_api_url(base_url: str | None, token: str, method: str) -> str:
    """Собирает URL вида {base}/bot{token}/{method} без urljoin (токен содержит ':')."""
    api_base = normalize_telegram_bot_api_base(base_url)
    cleaned_token = (token or "").strip()
    cleaned_method = (method or "").strip().lstrip("/")
    return f"{api_base}/bot{cleaned_token}/{cleaned_method}"


def resolve_telegram_bot_api_base(db: Session | None) -> str:
    raw = get_setting_value(db, "telegram_bot_api_base_url", "").strip()
    return normalize_telegram_bot_api_base(raw)


def is_telegram_enabled(db: Session | None) -> bool:
    value = get_setting_value(db, "telegram_enabled", "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def get_telegram_chat_id(db: Session | None) -> str:
    return get_setting_value(db, "telegram_chat_id", "").strip()


def get_telegram_bot_token(db: Session | None) -> str:
    return get_setting_value(db, "telegram_bot_token", "").strip()


def is_release_tracked(db: Session, release_id: int) -> bool:
    row = db.get(TrackedRelease, release_id)
    return row is not None and bool(row.enabled)


def list_enabled_tracked_releases(db: Session) -> list[TrackedRelease]:
    return list(
        db.scalars(
            select(TrackedRelease)
            .where(TrackedRelease.enabled.is_(True))
            .order_by(TrackedRelease.title.asc(), TrackedRelease.release_id.asc())
        ).all()
    )


def upsert_tracked_release(
    db: Session,
    *,
    release_id: int,
    release_alias: str,
    title: str = "",
    source: str = SOURCE_UI,
    enabled: bool = True,
    commit: bool = True,
) -> TrackedRelease:
    from app.services.automatic_encoder import lock, tracking_changed
    lock(db)
    alias = (release_alias or "").strip().strip("/")
    title_value = (title or "").strip() or alias or str(release_id)
    title_value = truncate_telegram_text(title_value, 512)
    source_value = source if source in {SOURCE_BOT, SOURCE_UI} else SOURCE_UI
    row = db.get(TrackedRelease, release_id)
    if row is None:
        row = TrackedRelease(
            release_id=release_id,
            release_alias=alias,
            title=title_value,
            enabled=enabled,
            source=source_value,
            created_at=utcnow(),
        )
        db.add(row)
    else:
        row.release_alias = alias or row.release_alias
        if title_value:
            row.title = title_value
        row.enabled = enabled
        # bot имеет приоритет бейджа; ui не затирает bot
        if source_value == SOURCE_BOT or row.source != SOURCE_BOT:
            row.source = source_value
    tracking_changed(db, release_id, bool(enabled))
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def set_tracked_enabled(db: Session, release_id: int, enabled: bool, *, source: str = SOURCE_UI) -> TrackedRelease | None:
    from app.services.automatic_encoder import lock, tracking_changed
    lock(db)
    row = db.get(TrackedRelease, release_id)
    if row is None:
        return None
    row.enabled = enabled
    tracking_changed(db, release_id, bool(enabled))
    if source == SOURCE_BOT or row.source != SOURCE_BOT:
        row.source = source if enabled else row.source
    db.commit()
    db.refresh(row)
    return row


def find_tracked_release(db: Session, alias_or_id: str) -> TrackedRelease | None:
    """Числовые ссылки (включая разобранный Nova URL) адресуют общий release_id."""
    cleaned = (alias_or_id or "").strip().strip("/").lower()
    if not cleaned:
        return None
    if cleaned.isdigit():
        return db.get(TrackedRelease, int(cleaned))
    return db.scalar(
        select(TrackedRelease).where(TrackedRelease.release_alias.ilike(cleaned)).limit(1)
    )


def disable_tracked_by_alias(db: Session, alias: str) -> TrackedRelease | None:
    row = find_tracked_release(db, alias)
    if row is None:
        return None
    from app.services.automatic_encoder import lock, tracking_changed
    lock(db)
    row.enabled = False
    tracking_changed(db, row.release_id, False)
    db.commit()
    db.refresh(row)
    return row


def build_torrent_notification_text(
    *,
    title: str,
    alias: str,
    torrents: list[dict[str, Any]],
    offsite: bool = False,
) -> str:
    """MarkdownV2-шаблон обновления торрентов (жирный — одиночные *, не **)."""
    message = (
        f"🔔 *Обновление для [{escape_markdown_v2(title)}]"
        f"(https://aniliberty\\.top/anime/releases/release/{escape_markdown_v2(alias)})*\n\n"
    )
    if offsite:
        message = f"🔔 *Обновление для {escape_markdown_v2(title)}*\nНет на сайте\n\n"
    if not torrents:
        message += "ℹ️ Торренты AVC/HEVC/AV1 не найдены\\."
        return message
    for torrent in torrents:
        label = str(torrent.get("label") or torrent.get("type") or "торрент")
        codec_obj = torrent.get("codec")
        if isinstance(codec_obj, dict):
            codec = str(codec_obj.get("description") or codec_obj.get("label") or "N/A")
        else:
            codec = str(codec_obj or "N/A")
        description = str(torrent.get("description") or "серии не указаны")
        message += (
            f"▫️ *{escape_markdown_v2(label)}*\n"
            f"    Серии: `{escape_markdown_v2(description)}`\n"
            f"    Кодек: `{escape_markdown_v2(codec)}`\n\n"
        )
    return message


_FILE_KIND_ICONS = {
    "added": "➕",
    "removed": "➖",
    "modified": "✏️",
    "missing": "⚠️",
    "orphan": "🗑",
}


def build_file_changes_notification_text(
    *,
    title: str,
    alias: str,
    torrent_label: str,
    changes: list[dict[str, Any]],
    baseline: bool = False,
    offsite: bool = False,
) -> str:
    """MarkdownV2: изменения файлов для tracked-релиза.

    baseline=True — первый проход hash_torrent: сводка «файлы учтены в базе».
    """
    release_link = (
        f"[{escape_markdown_v2(title)}]"
        f"(https://aniliberty\\.top/anime/releases/release/{escape_markdown_v2(alias)})"
    )
    if offsite:
        release_link = escape_markdown_v2(title) + " · Нет на сайте"
    if baseline:
        message = f"📦 *Файлы торрента добавлены в базу для {release_link}*\n\n"
    else:
        message = f"📁 *Изменения файлов для {release_link}*\n\n"
    label = torrent_label.strip() or "торрент"
    message += f"Torrent: *{escape_markdown_v2(label)}*\n"
    if baseline and changes:
        message += f"Файлов: `{len(changes)}`\n"
    for item in changes:
        kind = str(item.get("kind") or "")
        icon = _FILE_KIND_ICONS.get(kind, "•")
        path = str(item.get("relative_path") or item.get("full_path") or "?")
        suffix = ""
        if kind == "modified":
            suffix = " \\(содержимое\\)"
        elif kind == "missing":
            suffix = " \\(нет на диске\\)"
        elif kind == "orphan":
            suffix = " \\(orphan\\)"
        elif baseline and kind == "added":
            suffix = ""
        message += f"  {icon} `{escape_markdown_v2(path)}`{suffix}\n"
    return message


def enqueue_file_changes_notification(
    db: Session,
    *,
    release_id: int,
    torrent_id: int | None,
    events: list[Any],
    archive: Any | None = None,
    baseline: bool = False,
) -> TelegramOutbox | None:
    """Пишет в outbox уведомление об изменениях файлов (pipeline_id=null)."""
    if not events:
        return None
    if not is_release_tracked(db, release_id):
        return None
    if not is_telegram_enabled(db):
        return None
    chat_id = get_telegram_chat_id(db)
    if not chat_id:
        return None

    tracked = db.get(TrackedRelease, release_id)
    alias = ""
    title = ""
    if tracked is not None:
        alias = tracked.release_alias or ""
        title = tracked.title or alias
    torrent_label = ""
    if archive is not None:
        parts = [
            getattr(archive, "torrent_type", None) or "",
            getattr(archive, "torrent_description", None) or "",
        ]
        torrent_label = " · ".join(p for p in parts if p)
        if not alias and getattr(archive, "release_alias", None):
            alias = str(archive.release_alias)
        if not title and getattr(archive, "anime_name", None):
            title = str(archive.anime_name)
    if not title:
        title = alias or str(release_id)
    if not alias:
        alias = str(release_id)

    changes = [
        {
            "kind": getattr(ev, "kind", None) or (ev.get("kind") if isinstance(ev, dict) else ""),
            "relative_path": getattr(ev, "relative_path", None)
            if not isinstance(ev, dict)
            else ev.get("relative_path"),
            "full_path": getattr(ev, "full_path", None)
            if not isinstance(ev, dict)
            else ev.get("full_path"),
        }
        for ev in events
    ]
    text = build_file_changes_notification_text(
        title=title,
        alias=alias,
        torrent_label=torrent_label or f"torrent_id={torrent_id}",
        changes=changes,
        baseline=baseline,
        offsite=_notification_offsite(db, release_id, archive=archive),
    )
    payload = {
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
        "text": text,
        "kind": "file_changes_baseline" if baseline else "file_changes",
        "release_id": release_id,
        "torrent_id": torrent_id,
        "title": title,
        "alias": alias,
        "baseline": baseline,
        "torrent_label": torrent_label or f"torrent_id={torrent_id}",
        "changes": changes,
    }
    outbox = TelegramOutbox(
        pipeline_id=None,
        bot_key=PRIMARY_BOT_KEY,
        chat_id=chat_id,
        payload_json=payload,
        status=OUTBOX_PENDING,
        attempts=0,
        created_at=utcnow(),
    )
    db.add(outbox)
    now = utcnow()
    for ev in events:
        if hasattr(ev, "notified_at"):
            ev.notified_at = now
    db.commit()
    db.refresh(outbox)
    return outbox


def enqueue_tracking_toggle_notification(
    db: Session,
    *,
    enabled: bool,
    title: str,
    commit: bool = True,
) -> TelegramOutbox | None:
    """Outbox: тексты как у /add|/del. Без parse_mode (plain text)."""
    if not is_telegram_enabled(db):
        return None
    chat_id = get_telegram_chat_id(db)
    if not chat_id:
        return None
    title_value = _safe_notify_title(title)
    prefix = "✅ Добавлен: " if enabled else "❌ Отключен: "
    text = truncate_telegram_text(f"{prefix}{title_value}", _TELEGRAM_TEXT_MAX)
    payload = {
        "disable_web_page_preview": True,
        "text": text,
        "kind": "tracking_toggle",
        "enabled": bool(enabled),
        "title": title_value,
    }
    outbox = TelegramOutbox(
        pipeline_id=None,
        bot_key=PRIMARY_BOT_KEY,
        chat_id=chat_id,
        payload_json=payload,
        status=OUTBOX_PENDING,
        attempts=0,
        created_at=utcnow(),
    )
    db.add(outbox)
    if commit:
        db.commit()
        db.refresh(outbox)
    else:
        db.flush()
    return outbox


_CODEC_FAMILIES = ("AVC", "HEVC", "AV1")


def classify_torrent_codec_family(torrent: dict[str, Any]) -> str | None:
    """AVC / HEVC / AV1 по полям codec/label/type."""
    parts: list[str] = []
    codec = torrent.get("codec")
    if isinstance(codec, dict):
        for key in ("label", "value", "description"):
            val = codec.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val)
    elif isinstance(codec, str) and codec.strip():
        parts.append(codec)
    for key in ("label", "type"):
        val = torrent.get(key)
        if isinstance(val, dict):
            for sub in ("label", "value", "description"):
                text = val.get(sub)
                if isinstance(text, str) and text.strip():
                    parts.append(text)
        elif isinstance(val, str) and val.strip():
            parts.append(val)
    blob = " ".join(parts).casefold()
    if "av1" in blob:
        return "AV1"
    if "hevc" in blob or "x265" in blob or "h.265" in blob or "h265" in blob:
        return "HEVC"
    if "avc" in blob or "x264" in blob or "h.264" in blob or "h264" in blob:
        return "AVC"
    return None


def _torrent_sort_key(torrent: dict[str, Any]) -> tuple[Any, ...]:
    """Новее выше: updated_at / created_at / id."""
    return (
        str(torrent.get("updated_at") or ""),
        str(torrent.get("created_at") or ""),
        int(torrent["id"]) if isinstance(torrent.get("id"), int) else 0,
    )


def pick_latest_codec_torrents(torrents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """По одному последнему торренту на семейство кодека: AVC, HEVC, AV1 (если есть)."""
    best: dict[str, dict[str, Any]] = {}
    for item in torrents:
        if not isinstance(item, dict):
            continue
        family = classify_torrent_codec_family(item)
        if family is None:
            continue
        prev = best.get(family)
        if prev is None or _torrent_sort_key(item) > _torrent_sort_key(prev):
            best[family] = item
    return [best[name] for name in _CODEC_FAMILIES if name in best]


def normalize_torrents_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "list", "items", "torrents"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []


def _notification_offsite(db, release_id: int, *, payload=None, archive=None) -> bool:
    known = isinstance(payload, dict) and payload.get("_source") in {"site", "backoffice"}
    quality = getattr(archive, "quality_json", None)
    known = known or (isinstance(quality, dict) and quality.get("_source") in {"site", "backoffice"})
    if known:
        from app.services.release_source import release_is_offsite
        return release_is_offsite(db, release_id)
    return False


def enqueue_pipeline_telegram_notification(
    db: Session,
    pipeline: TorrentPipeline,
    *,
    release_payload: dict[str, Any] | None = None,
    torrent_payload: dict[str, Any] | None = None,
) -> TorrentPipeline:
    """One primary event per pipeline, including failed/cancelled history.

    A normal processor call must never bypass acknowledgement of unknown
    delivery. Only explicit retry of the existing failed row can do that.
    Prevent query autoflush from locking a dirty pipeline before outbox insert.
    """
    with db.no_autoflush:
        return _enqueue_pipeline_notification(db, pipeline, release_payload=release_payload,
                                              torrent_payload=torrent_payload)


def _enqueue_pipeline_notification(
    db: Session, pipeline: TorrentPipeline, *, release_payload: dict[str, Any] | None,
    torrent_payload: dict[str, Any] | None,
) -> TorrentPipeline:
    if pipeline.tg_status in {TG_STATUS_QUEUED, TG_STATUS_SENT, TG_STATUS_PENDING, OUTBOX_FAILED, OUTBOX_CANCELLED}:
        return pipeline
    # Legacy events have NULL dedupe_key. A stale pipeline object or changed chat
    # does not make them new events; all historical delivery states count.
    existing = db.scalar(select(TelegramOutbox.id).where(
        TelegramOutbox.bot_key == PRIMARY_BOT_KEY, TelegramOutbox.pipeline_id == pipeline.id,
    ).limit(1))
    if existing is not None:
        return pipeline

    if not is_release_tracked(db, pipeline.release_id):
        pipeline.tg_status = TG_STATUS_SKIPPED
        db.commit()
        db.refresh(pipeline)
        return pipeline

    if not is_telegram_enabled(db):
        pipeline.tg_status = TG_STATUS_SKIPPED
        db.commit()
        db.refresh(pipeline)
        return pipeline

    chat_id = get_telegram_chat_id(db)
    if not chat_id:
        pipeline.tg_status = TG_STATUS_SKIPPED
        db.commit()
        db.refresh(pipeline)
        return pipeline

    tracked = db.get(TrackedRelease, pipeline.release_id)
    alias = ""
    title = ""
    if tracked is not None:
        alias = tracked.release_alias or ""
        title = tracked.title or alias
    if release_payload:
        raw_alias = release_payload.get("alias")
        if isinstance(raw_alias, str) and raw_alias.strip():
            alias = raw_alias.strip()
        name = release_payload.get("name")
        if isinstance(name, dict):
            main = name.get("main")
            if isinstance(main, str) and main.strip():
                title = main.strip()
        elif isinstance(name, str) and name.strip():
            title = name.strip()
    if not title:
        title = alias or str(pipeline.release_id)
    if not alias:
        alias = str(pipeline.release_id)

    torrents = [torrent_payload] if isinstance(torrent_payload, dict) else []
    text = build_torrent_notification_text(title=title, alias=alias, torrents=torrents,
        offsite=_notification_offsite(db, pipeline.release_id, payload=release_payload))
    payload = {
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
        "text": text,
        "release_id": pipeline.release_id,
        "torrent_id": pipeline.torrent_id,
        "title": title,
        "alias": alias,
        "torrents": torrents,
    }
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError("Telegram outbox требует PostgreSQL")
    try:
        outbox_id = db.execute(insert(TelegramOutbox).values(
            pipeline_id=pipeline.id, bot_key=PRIMARY_BOT_KEY,
            dedupe_key=f"primary_pipeline:{pipeline.id}", chat_id=chat_id,
            payload_json=payload, status=OUTBOX_PENDING, attempts=0,
            created_at=utcnow(), updated_at=utcnow(),
        ).on_conflict_do_nothing(index_elements=[TelegramOutbox.bot_key, TelegramOutbox.dedupe_key])
            .returning(TelegramOutbox.id)).scalar_one_or_none()
        if outbox_id is not None:
            pipeline.tg_status = TG_STATUS_QUEUED
            record_pipeline_event(db, pipeline.id, event_type="tg_queued",
                message=f"Pipeline {pipeline.id}: Telegram уведомление в очереди (chat_id={chat_id})",
                details={"actor": "job", "chat_id": chat_id, "tg_status": TG_STATUS_QUEUED,
                         "bot_key": PRIMARY_BOT_KEY, "outbox_id": outbox_id}, commit=False)
        db.commit()
        db.refresh(pipeline)
    except Exception:
        db.rollback()
        raise
    return pipeline


def mark_outbox_sent(db: Session, outbox: TelegramOutbox, *, claim_token: str | None = None,
                     message_id: int | None = None) -> bool:
    """Only the current lease owner may acknowledge delivery."""
    from app.services.telegram_outbox import finalize_outbox
    return finalize_outbox(db, outbox.id, claim_token,
                           status=OUTBOX_SENT, message_id=message_id)


def mark_outbox_attempt_failed(db: Session, outbox: TelegramOutbox, error: str,
                               *, claim_token: str | None = None) -> bool:
    from datetime import timedelta
    from app.services.telegram_outbox import finalize_outbox, retry_delay_seconds
    return finalize_outbox(db, outbox.id, claim_token,
        status=OUTBOX_PENDING, error=error, error_kind="temporary",
        next_attempt_at=utcnow() + timedelta(seconds=retry_delay_seconds(int(outbox.attempts or 0), outbox.id)))


def mark_outbox_failed(db: Session, outbox: TelegramOutbox, error: str,
                       *, claim_token: str | None = None, error_kind: str = "permanent") -> bool:
    from app.services.telegram_outbox import finalize_outbox
    return finalize_outbox(db, outbox.id, claim_token,
                           status=OUTBOX_FAILED, error=error, error_kind=error_kind)


def mark_outbox_cancelled(db: Session, outbox: TelegramOutbox, reason: str,
                          *, claim_token: str | None = None) -> bool:
    from app.services.telegram_outbox import finalize_outbox
    return finalize_outbox(db, outbox.id, claim_token,
                           status=OUTBOX_CANCELLED, error=reason, error_kind="acl_rejected")


async def test_telegram_get_me(
    *,
    token: str,
    base_url: str = DEFAULT_BOT_API_BASE,
) -> dict[str, Any]:
    """Проверка токена через Bot API getMe."""
    cleaned_token = (token or "").strip()
    if not cleaned_token:
        raise ValueError("Не задан токен Telegram-бота")
    url = build_telegram_api_url(base_url, cleaned_token, "getMe")
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url)
    data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if response.status_code != 200 or not data.get("ok"):
        description = data.get("description") if isinstance(data, dict) else None
        raise RuntimeError(description or f"HTTP {response.status_code}")
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError("Некорректный ответ getMe")
    return result
