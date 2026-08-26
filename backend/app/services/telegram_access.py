"""ACL второго Telegram-бота: auto-pending и административные решения."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import TelegramBotAccess, TelegramOutbox
from app.services.telegram_notify import OUTBOX_CANCELLED, OUTBOX_PENDING
from app.utils.datetime_fmt import utcnow

HEVC_BOT_KEY = "hevc"

SUBJECT_USER = "user"
SUBJECT_CHAT = "chat"
SUBJECT_TYPES = frozenset({SUBJECT_USER, SUBJECT_CHAT})

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
ACCESS_STATUSES = frozenset({STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED})

_AVAILABLE_COMMANDS = """Доступные команды:
• /overdue — список просроченных релизов
• /overdue <nickname> — просрочки конкретного исполнителя
• /status — релизы, ожидающие HEVC
• /status <release_id> — подробный статус релиза
• /error — расхождения типов AVC/HEVC"""

_USER_WELCOME_TEXT = f"""Привет-привет! 🎀

Ого, а вот и новенький пользователь. Я — Такаги-сан. Да-да, та самая. 😏

Я буду следить за статусами и ходом работы над видео. Ты, главное, не отлынивай, а то я всё вижу и замечаю. Обещаю постараться сделать твою работу чуточку интереснее... Хотя кто знает — может быть, иногда я буду тебя немного поддразнивать?

Ну что, начнём? Покажи-ка, на что ты способен. ✨

{_AVAILABLE_COMMANDS}"""

_CHAT_WELCOME_TEXT = f"""Всем привет! 🎒✨

Я — Такаги-сан и с сегодняшнего дня буду в вашем классе... Ой, то есть в этом чате. Буду следить за порядком, статусами и, конечно, за тем, как продвигается работа над видео.

Надеюсь, вы не против, если я иногда буду отпускать комментарии в своей фирменной манере? 😏 Обещаю: сама скучать не буду и вам не дам.

Ну что, давайте знакомиться и работать весело! 👀

{_AVAILABLE_COMMANDS}"""


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    user: TelegramBotAccess | None
    chat: TelegramBotAccess | None

    @property
    def waiting_for_approval(self) -> bool:
        rows = [row for row in (self.user, self.chat) if row is not None]
        return bool(rows) and any(row.status == STATUS_PENDING for row in rows)


def _clean_metadata(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned[:255] or None


def _load_access_row(
    db: Session,
    *,
    bot_key: str,
    subject_type: str,
    telegram_id: int,
) -> TelegramBotAccess | None:
    return db.scalar(
        select(TelegramBotAccess)
        .where(
            TelegramBotAccess.bot_key == bot_key,
            TelegramBotAccess.subject_type == subject_type,
            TelegramBotAccess.telegram_id == int(telegram_id),
        )
        .limit(1)
    )


def register_access_attempt(
    db: Session,
    *,
    bot_key: str,
    subject_type: str,
    telegram_id: int,
    username: str | None = None,
    title: str | None = None,
    commit: bool = True,
) -> TelegramBotAccess:
    """Создаёт pending при первой попытке, не сбрасывая последующее решение."""
    normalized_type = subject_type.strip().lower()
    if normalized_type not in SUBJECT_TYPES:
        raise ValueError(f"Неизвестный тип Telegram-субъекта: {subject_type}")
    normalized_bot = bot_key.strip()
    if not normalized_bot:
        raise ValueError("bot_key не может быть пустым")

    row = _load_access_row(
        db,
        bot_key=normalized_bot,
        subject_type=normalized_type,
        telegram_id=int(telegram_id),
    )
    now = utcnow()
    if row is None:
        candidate = TelegramBotAccess(
            bot_key=normalized_bot,
            subject_type=normalized_type,
            telegram_id=int(telegram_id),
            status=STATUS_PENDING,
            created_at=now,
            updated_at=now,
        )
        try:
            with db.begin_nested():
                db.add(candidate)
                db.flush()
            row = candidate
        except IntegrityError:
            if candidate in db:
                db.expunge(candidate)
            row = _load_access_row(
                db,
                bot_key=normalized_bot,
                subject_type=normalized_type,
                telegram_id=int(telegram_id),
            )
            if row is None:
                raise
    row.username = _clean_metadata(username)
    row.title = _clean_metadata(title)
    row.updated_at = now

    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def check_access(
    db: Session,
    *,
    bot_key: str,
    user_id: int,
    username: str | None = None,
    user_title: str | None = None,
    chat_id: int | None = None,
    chat_title: str | None = None,
    is_private: bool,
    commit: bool = True,
) -> AccessDecision:
    """Регистрирует попытку и применяет правило ЛС либо user+chat для группы."""
    user = register_access_attempt(
        db,
        bot_key=bot_key,
        subject_type=SUBJECT_USER,
        telegram_id=user_id,
        username=username,
        title=user_title,
        commit=False,
    )
    chat = None
    if not is_private:
        if chat_id is None:
            raise ValueError("Для групповой проверки требуется chat_id")
        chat = register_access_attempt(
            db,
            bot_key=bot_key,
            subject_type=SUBJECT_CHAT,
            telegram_id=chat_id,
            title=chat_title,
            commit=False,
        )

    if commit:
        db.commit()
        db.refresh(user)
        if chat is not None:
            db.refresh(chat)

    allowed = user.status == STATUS_APPROVED and (
        is_private or (chat is not None and chat.status == STATUS_APPROVED)
    )
    return AccessDecision(allowed=allowed, user=user, chat=chat)


def _enqueue_approval_welcome(db: Session, row: TelegramBotAccess) -> bool:
    """Ставит одно приветствие в HEVC outbox, не пробрасывая dedupe-конфликт."""
    dedupe_key = f"hevc_access_welcome:{row.subject_type}:{row.id}"
    exists = db.scalar(
        select(TelegramOutbox.id)
        .where(
            TelegramOutbox.bot_key == HEVC_BOT_KEY,
            TelegramOutbox.dedupe_key == dedupe_key,
        )
        .limit(1)
    )
    if exists is not None:
        return False

    text = _USER_WELCOME_TEXT if row.subject_type == SUBJECT_USER else _CHAT_WELCOME_TEXT
    try:
        with db.begin_nested():
            db.add(
                TelegramOutbox(
                    bot_key=HEVC_BOT_KEY,
                    dedupe_key=dedupe_key,
                    chat_id=str(row.telegram_id),
                    payload_json={
                        "text": text,
                        "disable_web_page_preview": True,
                        "kind": "hevc_access_welcome",
                        "subject_type": row.subject_type,
                        "access_id": row.id,
                    },
                    status=OUTBOX_PENDING,
                    attempts=0,
                    created_at=utcnow(),
                )
            )
            db.flush()
    except IntegrityError:
        return False
    return True


def cancel_pending_hevc_outbox(db: Session, telegram_id: int) -> int:
    """Снимает pending HEVC-сообщения этому telegram_id / chat_id."""
    rows = list(
        db.scalars(
            select(TelegramOutbox).where(
                TelegramOutbox.bot_key == HEVC_BOT_KEY,
                TelegramOutbox.chat_id == str(int(telegram_id)),
                TelegramOutbox.status == OUTBOX_PENDING,
            )
        ).all()
    )
    for row in rows:
        row.status = OUTBOX_CANCELLED
        row.last_error = "acl_rejected"
    return len(rows)


def hevc_recipient_is_approved(db: Session, chat_id: str) -> bool:
    """Drain HEVC: отправлять только approved user/chat с этим telegram_id."""
    try:
        telegram_id = int(str(chat_id).strip())
    except (TypeError, ValueError):
        return False
    status = db.scalar(
        select(TelegramBotAccess.status)
        .where(
            TelegramBotAccess.bot_key == HEVC_BOT_KEY,
            TelegramBotAccess.telegram_id == telegram_id,
            TelegramBotAccess.status == STATUS_APPROVED,
        )
        .limit(1)
    )
    return status == STATUS_APPROVED


def set_access_status(
    db: Session,
    access_id: int,
    status: str,
    *,
    bot_key: str | None = None,
    commit: bool = True,
) -> TelegramBotAccess | None:
    normalized_status = status.strip().lower()
    if normalized_status not in {STATUS_APPROVED, STATUS_REJECTED}:
        raise ValueError(f"Недопустимое решение ACL: {status}")
    row = db.get(TelegramBotAccess, int(access_id))
    if row is None or (bot_key is not None and row.bot_key != bot_key):
        return None
    became_approved = row.status != STATUS_APPROVED and normalized_status == STATUS_APPROVED
    became_rejected = row.status != STATUS_REJECTED and normalized_status == STATUS_REJECTED
    now = utcnow()
    row.status = normalized_status
    row.decided_at = now
    row.updated_at = now
    if became_approved and row.bot_key == HEVC_BOT_KEY:
        _enqueue_approval_welcome(db, row)
    if became_rejected and row.bot_key == HEVC_BOT_KEY:
        cancel_pending_hevc_outbox(db, row.telegram_id)
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def list_access_rows(
    db: Session,
    *,
    bot_key: str = HEVC_BOT_KEY,
    subject_type: str,
    status: str | None = None,
) -> list[TelegramBotAccess]:
    normalized_type = subject_type.strip().lower()
    if normalized_type not in SUBJECT_TYPES:
        raise ValueError(f"Неизвестный тип Telegram-субъекта: {subject_type}")
    query = select(TelegramBotAccess).where(
        TelegramBotAccess.bot_key == bot_key,
        TelegramBotAccess.subject_type == normalized_type,
    )
    normalized_status = (status or "").strip().lower()
    if normalized_status:
        if normalized_status not in ACCESS_STATUSES:
            raise ValueError(f"Недопустимый статус ACL: {status}")
        query = query.where(TelegramBotAccess.status == normalized_status)
    return list(
        db.scalars(
            query.order_by(
                TelegramBotAccess.created_at.desc(),
                TelegramBotAccess.id.desc(),
            )
        ).all()
    )


def list_approved_group_ids(
    db: Session,
    *,
    bot_key: str = HEVC_BOT_KEY,
) -> list[int]:
    return [
        int(value)
        for value in db.scalars(
            select(TelegramBotAccess.telegram_id)
            .where(
                TelegramBotAccess.bot_key == bot_key,
                TelegramBotAccess.subject_type == SUBJECT_CHAT,
                TelegramBotAccess.status == STATUS_APPROVED,
            )
            .order_by(TelegramBotAccess.telegram_id.asc())
        ).all()
    ]
