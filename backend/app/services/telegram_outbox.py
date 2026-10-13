"""Durable Telegram delivery with per-item leases and fenced outcomes.

No database transaction is held across HTTP. Ambiguous delivery is not retried
automatically: Telegram sendMessage has no client idempotency key.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import math
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.db.models import Release, Setting, TelegramOutbox, TorrentPipeline
from app.logging_filters import redact_secrets
from app.services.pipeline import record_pipeline_event
from app.services.runtime_settings import resolve_telegram_bot_settings
from app.services.telegram_notify import (
    OUTBOX_CANCELLED, OUTBOX_FAILED, OUTBOX_PENDING, OUTBOX_SENT,
    build_telegram_api_url, normalize_telegram_bot_api_base,
)
from app.utils.datetime_fmt import as_utc_iso, utcnow

logger = logging.getLogger(__name__)
OUTBOX_SENDING = "sending"
LEASE_SECONDS = 120
HTTP_TOTAL_SECONDS = 25
BOT_KEYS = frozenset({"primary", "hevc"})
OUTBOX_STATUSES = frozenset({OUTBOX_PENDING, OUTBOX_SENDING, OUTBOX_SENT, OUTBOX_FAILED, OUTBOX_CANCELLED})


@dataclass(frozen=True, slots=True)
class OutboxClaim:
    id: int
    token: str
    bot_key: str
    chat_id: str
    payload: dict[str, Any]
    attempts: int


class TelegramDeliveryError(RuntimeError):
    def __init__(self, message: str, *, kind: str, retry_after: int | None = None,
                 api_code: int | None = None):
        super().__init__(message)
        self.kind, self.retry_after, self.api_code = kind, retry_after, api_code


class OutboxRetryError(ValueError):
    def __init__(self, detail: str, *, status_code: int = 409, code: str = "conflict"):
        super().__init__(detail)
        self.detail, self.status_code, self.code = detail, status_code, code


def _bot_key(value: str) -> str:
    if value not in BOT_KEYS:
        raise ValueError("Неизвестный профиль Telegram")
    return value


def _safe_error(value: Any, token: str = "") -> str:
    text = str(value or "")
    if token:
        text = text.replace(token, "***")
    return redact_secrets(text)[:2000]


def refresh_source_link(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Refresh source links without changing the enqueued event."""
    release_id = payload.get("release_id")
    if type(release_id) is not int or not any(key in payload for key in ("release_link", "torrents", "changes")):
        return payload
    release = db.get(Release, release_id)
    if release is None or release.source not in {"site", "backoffice"}:
        return payload
    offsite = release.source == "backoffice"
    refreshed = dict(payload)
    if "release_link" in payload:
        from types import SimpleNamespace
        from app.services.hevc_bot import _release_link
        row = SimpleNamespace(title=payload.get("title", ""), alias=payload.get("alias", ""), offsite=offsite)
        refreshed["text"] = str(payload.get("text") or "").replace(str(payload["release_link"]), _release_link(row), 1)
    elif "changes" in payload:
        from app.services.telegram_notify import build_file_changes_notification_text
        refreshed["text"] = build_file_changes_notification_text(
            title=payload["title"], alias=payload["alias"], torrent_label=payload["torrent_label"],
            changes=payload["changes"], baseline=bool(payload.get("baseline")), offsite=offsite,
        )
    elif "torrents" in payload:
        from app.services.telegram_notify import build_torrent_notification_text
        refreshed["text"] = build_torrent_notification_text(
            title=payload["title"], alias=payload["alias"], torrents=payload["torrents"], offsite=offsite,
        )
    return refreshed


def _due(now: datetime):
    return or_(TelegramOutbox.next_attempt_at.is_(None), TelegramOutbox.next_attempt_at <= now)


def fetch_pending_outbox(db: Session, *, bot_key: str, limit: int = 20,
                         now: datetime | None = None) -> list[TelegramOutbox]:
    """Read-only due listing. Delivery itself must use claim_next_outbox."""
    return list(db.scalars(select(TelegramOutbox).where(
        TelegramOutbox.bot_key == _bot_key(bot_key), TelegramOutbox.status == OUTBOX_PENDING, _due(now or utcnow()),
    ).order_by(func.coalesce(TelegramOutbox.next_attempt_at, TelegramOutbox.created_at), TelegramOutbox.id)
        .limit(max(0, min(int(limit), 100)))).all())


def _lock_cooldown(db: Session, bot_key: str) -> Setting:
    """Profile lock is always acquired before an outbox row lock."""
    key = f"telegram_outbox_cooldown_{_bot_key(bot_key)}"
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError("Telegram outbox требует PostgreSQL")
    db.execute(insert(Setting).values(key=key, value="", updated_at=utcnow()).on_conflict_do_nothing(index_elements=[Setting.key]))
    return db.scalar(select(Setting).where(Setting.key == key).with_for_update().execution_options(populate_existing=True))


def _cooldown_until(row: Setting) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(row.value.replace("Z", "+00:00")) if row.value else None
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed and parsed.tzinfo else parsed
    except (ValueError, TypeError):
        return None


def set_bot_cooldown(db: Session, *, bot_key: str, until: datetime) -> None:
    row = _lock_cooldown(db, bot_key)
    previous = _cooldown_until(row)
    if previous is None or previous < until:
        row.value, row.updated_at = as_utc_iso(until), utcnow()
    db.commit()


def claim_next_outbox(db: Session, *, bot_key: str, now: datetime | None = None) -> OutboxClaim | None:
    current = now or utcnow()
    cooldown = _cooldown_until(_lock_cooldown(db, bot_key))
    if cooldown is not None and cooldown > current:
        db.commit()
        return None
    row = db.scalar(select(TelegramOutbox).where(
        TelegramOutbox.bot_key == bot_key, TelegramOutbox.status == OUTBOX_PENDING, _due(current),
    ).order_by(func.coalesce(TelegramOutbox.next_attempt_at, TelegramOutbox.created_at), TelegramOutbox.id)
        .limit(1).with_for_update(skip_locked=True).execution_options(populate_existing=True))
    if row is None:
        db.commit()
        return None
    token = uuid4().hex
    row.status, row.claim_token = OUTBOX_SENDING, token
    row.lease_until = current + timedelta(seconds=LEASE_SECONDS)
    row.last_attempt_at = row.updated_at = current
    row.attempts = int(row.attempts or 0) + 1
    claim = OutboxClaim(row.id, token, row.bot_key, row.chat_id,
                        dict(row.payload_json) if isinstance(row.payload_json, dict) else {}, row.attempts)
    db.commit()
    return claim


def renew_claim(db: Session, claim: OutboxClaim, *, now: datetime | None = None) -> bool:
    current = now or utcnow()
    result = db.execute(update(TelegramOutbox).where(
        TelegramOutbox.id == claim.id, TelegramOutbox.status == OUTBOX_SENDING,
        TelegramOutbox.claim_token == claim.token, TelegramOutbox.lease_until > current,
    ).values(lease_until=current + timedelta(seconds=LEASE_SECONDS), updated_at=current))
    db.commit()
    return result.rowcount == 1


def _audit_outcome(db: Session, row: Any, status: str, *, error_kind: str | None = None) -> None:
    if row.pipeline_id is None:
        return
    pipeline = db.get(TorrentPipeline, row.pipeline_id)
    if pipeline is None:
        return
    if row.bot_key == "primary":
        conditions = [TorrentPipeline.id == row.pipeline_id]
        if status != OUTBOX_SENT:
            conditions.append(TorrentPipeline.tg_status != OUTBOX_SENT)
        db.execute(update(TorrentPipeline).where(*conditions).values(tg_status=status))
        db.refresh(pipeline)
    record_pipeline_event(db, row.pipeline_id, event_type=f"tg_{status}",
        message=f"Telegram {row.bot_key}: {status}", details={
            "actor": "job", "outbox_id": row.id, "chat_id": row.chat_id,
            "bot_key": row.bot_key, "delivery_status": status, "error_kind": error_kind,
            **({"tg_status": pipeline.tg_status} if row.bot_key == "primary" else {}),
        }, commit=False)


def finalize_outbox(db: Session, outbox_id: int, claim_token: str | None, *, status: str,
                    error: str | None = None, error_kind: str | None = None,
                    next_attempt_at: datetime | None = None, message_id: int | None = None,
                    now: datetime | None = None, secret_token: str = "") -> bool:
    """CAS outcome and audit atomically; expired owners cannot commit receipts."""
    if status not in OUTBOX_STATUSES - {OUTBOX_SENDING}:
        raise ValueError("Недопустимый результат Telegram")
    if not claim_token:
        return False
    current = now or utcnow()
    try:
        row = db.execute(update(TelegramOutbox).where(
            TelegramOutbox.id == outbox_id, TelegramOutbox.status == OUTBOX_SENDING,
            TelegramOutbox.claim_token == claim_token, TelegramOutbox.lease_until > current,
        ).values(status=status, claim_token=None, lease_until=None, updated_at=current,
                 last_error=_safe_error(error, secret_token) if error else None, last_error_kind=error_kind,
                 next_attempt_at=next_attempt_at if status == OUTBOX_PENDING else None,
                 sent_at=current if status == OUTBOX_SENT else None,
                 telegram_message_id=message_id if status == OUTBOX_SENT else None)
            .returning(TelegramOutbox.id, TelegramOutbox.pipeline_id, TelegramOutbox.bot_key, TelegramOutbox.chat_id)).first()
        if row is not None:
            _audit_outcome(db, row, status, error_kind=error_kind)
        db.commit()
        return row is not None
    except Exception:
        db.rollback()
        raise


def recover_expired_claims(db: Session, *, bot_key: str, now: datetime | None = None) -> int:
    current = now or utcnow()
    try:
        rows = db.execute(update(TelegramOutbox).where(
            TelegramOutbox.bot_key == _bot_key(bot_key), TelegramOutbox.status == OUTBOX_SENDING,
            TelegramOutbox.lease_until <= current,
        ).values(status=OUTBOX_FAILED, claim_token=None, lease_until=None, next_attempt_at=None,
                 last_error_kind="delivery_unknown", last_error="Истёк срок отправки: доставка не подтверждена; повтор может создать дубль",
                 updated_at=current)
            .returning(TelegramOutbox.id, TelegramOutbox.pipeline_id, TelegramOutbox.bot_key, TelegramOutbox.chat_id)).all()
        for row in rows:
            _audit_outcome(db, row, OUTBOX_FAILED, error_kind="delivery_unknown")
        db.commit()
        return len(rows)
    except Exception:
        db.rollback()
        raise


def retry_delay_seconds(attempts: int, outbox_id: int = 0) -> int:
    base = min(3600, 30 * 2 ** min(max(0, attempts - 1), 7))
    return min(3600, base + (outbox_id * 17 + attempts * 7) % max(1, base // 5))


def _retry_after(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 31_536_000 else None


async def send_outbox_message(*, token: str, base_url: str, chat_id: str,
                              payload: dict[str, Any]) -> int:
    body: dict[str, Any] = {"chat_id": chat_id, "text": payload.get("text") or "",
                            "disable_web_page_preview": bool(payload.get("disable_web_page_preview", True))}
    if isinstance(payload.get("parse_mode"), str) and payload["parse_mode"].strip():
        body["parse_mode"] = payload["parse_mode"].strip()
    if isinstance(payload.get("reply_markup"), dict):
        body["reply_markup"] = payload["reply_markup"]
    try:
        async with asyncio.timeout(HTTP_TOTAL_SECONDS):
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(build_telegram_api_url(base_url, token, "sendMessage"), json=body)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        raise TelegramDeliveryError(_safe_error(exc, token), kind="temporary") from None
    except (httpx.HTTPError, TimeoutError) as exc:
        raise TelegramDeliveryError(_safe_error(exc, token), kind="delivery_unknown") from None
    try:
        data = response.json()
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict) and data.get("ok") is True:
        result = data.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if response.status_code == 200 and type(message_id) is int:
            return message_id
    if isinstance(data, dict) and data.get("ok") is False and type(data.get("error_code")) is int:
        code = data["error_code"]
        parameters = data.get("parameters")
        retry_after = _retry_after(parameters.get("retry_after")) if isinstance(parameters, dict) else None
        kind = "rate_limited" if code == 429 else "configuration" if code == 401 else "temporary" if code >= 500 else "permanent"
        description = data.get("description") or f"Telegram API {code}"
        if isinstance(parameters, dict) and type(parameters.get("migrate_to_chat_id")) is int:
            description = f"{description}; migrate_to_chat_id={parameters['migrate_to_chat_id']} (проверьте ACL и адресата)"
        raise TelegramDeliveryError(_safe_error(description, token), kind=kind, retry_after=retry_after, api_code=code)
    raise TelegramDeliveryError(f"HTTP {response.status_code}: результат доставки не подтверждён", kind="delivery_unknown")


async def drain_outbox(db: Session, *, bot_key: str = "primary", limit: int = 20) -> dict[str, int]:
    _bot_key(bot_key)
    recover_expired_claims(db, bot_key=bot_key)
    stats = {"sent": 0, "failed": 0, "skipped": 0}
    for _ in range(max(0, min(int(limit), 100))):
        config = resolve_telegram_bot_settings(db, bot_key)
        if not config.enabled or not config.token:
            db.commit()
            break
        claim = claim_next_outbox(db, bot_key=bot_key)
        if claim is None:
            break
        try:
            from app.services.telegram_access import hevc_recipient_is_approved
            if bot_key == "hevc" and not hevc_recipient_is_approved(db, claim.chat_id):
                if finalize_outbox(db, claim.id, claim.token, status=OUTBOX_CANCELLED, error="acl_rejected", error_kind="acl_rejected"):
                    stats["skipped"] += 1
                continue
            payload = refresh_source_link(db, claim.payload)
            if not isinstance(payload.get("text"), str) or not payload["text"].strip():
                raise ValueError("Пустой или некорректный текст уведомления")
        except Exception as exc:
            if finalize_outbox(db, claim.id, claim.token, status=OUTBOX_FAILED, error=str(exc), error_kind="permanent", secret_token=config.token):
                stats["failed"] += 1
            continue
        # End source/ACL read transactions before awaiting HTTP.
        if not renew_claim(db, claim):
            continue
        delivery_error: TelegramDeliveryError | None = None
        try:
            message_id = await send_outbox_message(token=config.token,
                base_url=normalize_telegram_bot_api_base(config.api_base_url), chat_id=claim.chat_id, payload=payload)
        except TelegramDeliveryError as exc:
            delivery_error = exc
        except Exception as exc:
            delivery_error = TelegramDeliveryError(_safe_error(exc, config.token), kind="delivery_unknown")
        if delivery_error is None:
            if finalize_outbox(db, claim.id, claim.token, status=OUTBOX_SENT, message_id=message_id):
                stats["sent"] += 1
            continue
        current = utcnow()
        retryable = delivery_error.kind in {"temporary", "rate_limited"}
        delay = max(retry_delay_seconds(claim.attempts, claim.id), delivery_error.retry_after or 0)
        deadline = current + timedelta(seconds=delay)
        # Cooldown lock precedes finalization; never acquire it while holding an item lock.
        if delivery_error.kind in {"rate_limited", "configuration"}:
            set_bot_cooldown(db, bot_key=bot_key, until=deadline if retryable else current + timedelta(seconds=60))
        recorded = finalize_outbox(db, claim.id, claim.token,
            status=OUTBOX_PENDING if retryable else OUTBOX_FAILED, error=str(delivery_error), error_kind=delivery_error.kind,
            next_attempt_at=deadline if retryable else None, secret_token=config.token)
        if recorded:
            stats["failed"] += 1
        logger.warning("Outbox %s: %s: %s", claim.id, delivery_error.kind, _safe_error(delivery_error, config.token))
        if delivery_error.kind in {"rate_limited", "configuration"}:
            break
    return stats


def outbox_summary(db: Session, *, bot_key: str | None = None, now: datetime | None = None) -> dict[str, int]:
    current = now or utcnow()
    query = select(TelegramOutbox.status, func.count()).group_by(TelegramOutbox.status)
    if bot_key is not None:
        query = query.where(TelegramOutbox.bot_key == _bot_key(bot_key))
    counts = dict(db.execute(query).all())
    base = [TelegramOutbox.bot_key == bot_key] if bot_key is not None else []
    ready = db.scalar(select(func.count()).select_from(TelegramOutbox).where(*base, TelegramOutbox.status == OUTBOX_PENDING, _due(current))) or 0
    for key in ([bot_key] if bot_key else sorted(BOT_KEYS)):
        setting = db.get(Setting, f"telegram_outbox_cooldown_{key}")
        cooldown = _cooldown_until(setting) if setting is not None else None
        if cooldown and cooldown > current:
            ready -= db.scalar(select(func.count()).select_from(TelegramOutbox).where(
                TelegramOutbox.bot_key == key, TelegramOutbox.status == OUTBOX_PENDING, _due(current))) or 0
    unknown = db.scalar(select(func.count()).select_from(TelegramOutbox).where(*base,
        TelegramOutbox.status == OUTBOX_FAILED, TelegramOutbox.last_error_kind == "delivery_unknown")) or 0
    return {"total": sum(counts.values()), **{status: int(counts.get(status, 0)) for status in sorted(OUTBOX_STATUSES)},
            "ready": int(ready), "deferred": int(counts.get(OUTBOX_PENDING, 0) - ready), "unknown": int(unknown)}


def serialize_outbox(row: TelegramOutbox) -> dict[str, Any]:
    unknown = row.status == OUTBOX_FAILED and row.last_error_kind == "delivery_unknown"
    labels = {OUTBOX_PENDING: "Ожидает отправки", OUTBOX_SENDING: "Отправляется", OUTBOX_SENT: "Отправлено",
              OUTBOX_FAILED: "Ошибка", OUTBOX_CANCELLED: "Отменено"}
    payload = row.payload_json if isinstance(row.payload_json, dict) else {}
    release_id, kind = payload.get("release_id"), payload.get("kind")
    return {"id": row.id, "bot_key": row.bot_key, "pipeline_id": row.pipeline_id, "chat_id": row.chat_id,
            "event_kind": str(kind)[:80] if isinstance(kind, str) else "notification",
            "release_id": release_id if type(release_id) is int else None,
            "status": row.status, "status_label": "Доставка неизвестна" if unknown else labels.get(row.status, row.status),
            "attempts": row.attempts, "last_error": _safe_error(row.last_error) if row.last_error else None,
            "last_error_kind": row.last_error_kind, "telegram_message_id": row.telegram_message_id,
            "delivery_unknown": unknown, "can_retry": row.status == OUTBOX_FAILED,
            **{key: as_utc_iso(getattr(row, key)) for key in ("created_at", "updated_at", "last_attempt_at", "next_attempt_at", "lease_until", "sent_at")}}


def list_outbox_page(db: Session, *, bot_key: str | None = None, status: str | None = None,
                     page: int = 1, page_size: int = 50, now: datetime | None = None) -> dict[str, Any]:
    filters = []
    if bot_key:
        filters.append(TelegramOutbox.bot_key == _bot_key(bot_key))
    if status:
        if status not in OUTBOX_STATUSES:
            raise ValueError("Неизвестный статус Telegram")
        filters.append(TelegramOutbox.status == status)
    page, page_size = max(1, int(page)), max(1, min(int(page_size), 100))
    total = db.scalar(select(func.count()).select_from(TelegramOutbox).where(*filters)) or 0
    pages = max(1, math.ceil(total / page_size))
    page = min(page, pages)
    rows = db.scalars(select(TelegramOutbox).where(*filters).order_by(TelegramOutbox.id.desc())
        .offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [serialize_outbox(row) for row in rows], "total": total, "page": page,
            "page_size": page_size, "pages": pages, "bot_key": bot_key or "", "status": status or "",
            "summary": outbox_summary(db, bot_key=bot_key or None, now=now)}


def retry_failed_outbox(db: Session, outbox_id: int, *, bot_key: str | None = None,
                        acknowledge_unknown: bool = False, now: datetime | None = None) -> dict[str, Any]:
    current = now or utcnow()
    try:
        if bot_key is not None:
            _bot_key(bot_key)
        profile = db.scalar(select(TelegramOutbox.bot_key).where(TelegramOutbox.id == outbox_id))
        if profile is None or (bot_key is not None and profile != bot_key):
            raise OutboxRetryError("Сообщение не найдено", status_code=404, code="missing_or_profile")
        _lock_cooldown(db, profile)
        row = db.scalar(select(TelegramOutbox).where(TelegramOutbox.id == outbox_id)
            .with_for_update().execution_options(populate_existing=True))
        if row is None or row.status != OUTBOX_FAILED:
            raise OutboxRetryError("Повтор доступен только для сообщения с ошибкой", code="state")
        if row.last_error_kind == "delivery_unknown" and acknowledge_unknown is not True:
            raise OutboxRetryError("Доставка неизвестна: подтвердите возможную повторную доставку", status_code=422, code="acknowledge_unknown_required")
        config = resolve_telegram_bot_settings(db, profile)
        if not config.enabled or not config.token:
            raise OutboxRetryError("Профиль выключен или токен не задан", code="disabled")
        if profile == "hevc":
            from app.services.telegram_access import hevc_recipient_is_approved
            if not hevc_recipient_is_approved(db, row.chat_id):
                raise OutboxRetryError("Адресат HEVC не одобрен", code="acl")
        row.status, row.next_attempt_at, row.updated_at = OUTBOX_PENDING, current, current
        row.claim_token = row.lease_until = None
        _audit_outcome(db, row, OUTBOX_PENDING, error_kind=row.last_error_kind)
        db.flush()
        result = serialize_outbox(row)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
