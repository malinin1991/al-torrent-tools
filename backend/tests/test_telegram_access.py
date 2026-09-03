"""Узкие тесты ACL второго Telegram-бота."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.models import Setting, TelegramBotAccess, TelegramOutbox
from app.services.telegram_access import (
    HEVC_BOT_KEY,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    SUBJECT_CHAT,
    SUBJECT_USER,
    cancel_pending_hevc_outbox,
    check_access,
    hevc_recipient_is_approved,
    list_approved_group_ids,
    register_access_attempt,
    set_access_status,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type: JSONB, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Setting.__table__.create(engine)
    TelegramBotAccess.__table__.create(engine)
    TelegramOutbox.__table__.create(engine)
    return Session(engine)


def test_private_attempt_stays_pending_until_approved() -> None:
    with _session() as db:
        first = check_access(
            db,
            bot_key=HEVC_BOT_KEY,
            user_id=100,
            username="first_name",
            is_private=True,
        )
        assert first.allowed is False
        assert first.waiting_for_approval is True
        assert first.user is not None
        assert first.user.status == STATUS_PENDING

        set_access_status(
            db,
            first.user.id,
            STATUS_APPROVED,
            bot_key=HEVC_BOT_KEY,
        )
        second = check_access(
            db,
            bot_key=HEVC_BOT_KEY,
            user_id=100,
            username="renamed",
            is_private=True,
        )
        assert second.allowed is True
        assert second.user is not None
        assert second.user.username == "renamed"


def test_group_requires_approved_user_and_chat() -> None:
    with _session() as db:
        decision = check_access(
            db,
            bot_key=HEVC_BOT_KEY,
            user_id=101,
            username="member",
            chat_id=-200,
            chat_title="Encoding room",
            is_private=False,
        )
        assert decision.allowed is False
        assert decision.user is not None
        assert decision.chat is not None

        set_access_status(db, decision.chat.id, STATUS_APPROVED, bot_key=HEVC_BOT_KEY)
        only_chat = check_access(
            db,
            bot_key=HEVC_BOT_KEY,
            user_id=101,
            chat_id=-200,
            is_private=False,
        )
        assert only_chat.allowed is False

        set_access_status(db, decision.user.id, STATUS_APPROVED, bot_key=HEVC_BOT_KEY)
        both = check_access(
            db,
            bot_key=HEVC_BOT_KEY,
            user_id=101,
            chat_id=-200,
            is_private=False,
        )
        assert both.allowed is True
        assert list_approved_group_ids(db) == [-200]


def test_rejected_attempt_is_not_reset_to_pending() -> None:
    with _session() as db:
        row = register_access_attempt(
            db,
            bot_key=HEVC_BOT_KEY,
            subject_type=SUBJECT_USER,
            telegram_id=102,
        )
        set_access_status(db, row.id, STATUS_REJECTED, bot_key=HEVC_BOT_KEY)

        repeated = register_access_attempt(
            db,
            bot_key=HEVC_BOT_KEY,
            subject_type=SUBJECT_USER,
            telegram_id=102,
            username="returned",
        )
        assert repeated.id == row.id
        assert repeated.status == STATUS_REJECTED
        assert repeated.username == "returned"


def test_approved_groups_are_isolated_by_bot_and_subject() -> None:
    with _session() as db:
        for bot_key, subject_type, telegram_id in (
            (HEVC_BOT_KEY, SUBJECT_CHAT, -10),
            (HEVC_BOT_KEY, SUBJECT_USER, 10),
            ("primary", SUBJECT_CHAT, -20),
        ):
            row = register_access_attempt(
                db,
                bot_key=bot_key,
                subject_type=subject_type,
                telegram_id=telegram_id,
            )
            set_access_status(db, row.id, STATUS_APPROVED, bot_key=bot_key)

        assert list_approved_group_ids(db, bot_key=HEVC_BOT_KEY) == [-10]


def test_user_approval_enqueues_plain_text_welcome_once() -> None:
    with _session() as db:
        row = register_access_attempt(
            db,
            bot_key=HEVC_BOT_KEY,
            subject_type=SUBJECT_USER,
            telegram_id=777,
        )

        set_access_status(db, row.id, STATUS_APPROVED, bot_key=HEVC_BOT_KEY)
        set_access_status(db, row.id, STATUS_APPROVED, bot_key=HEVC_BOT_KEY)

        messages = list(db.scalars(select(TelegramOutbox)).all())
        assert len(messages) == 1
        message = messages[0]
        assert message.bot_key == HEVC_BOT_KEY
        assert message.chat_id == "777"
        assert message.dedupe_key == f"hevc_access_welcome:user:{row.id}"
        assert "parse_mode" not in message.payload_json
        assert "Привет-привет! 🎀" in message.payload_json["text"]
        assert message.payload_json["text"].splitlines()[-6:] == [
            "Доступные команды:",
            "• /overdue — список просроченных релизов",
            "• /overdue <nickname> — просрочки конкретного исполнителя",
            "• /status — релизы, ожидающие HEVC",
            "• /status <release_id> — подробный статус релиза",
            "• /error — расхождения типов AVC/HEVC",
        ]


def test_chat_approval_enqueues_group_welcome_once() -> None:
    with _session() as db:
        row = register_access_attempt(
            db,
            bot_key=HEVC_BOT_KEY,
            subject_type=SUBJECT_CHAT,
            telegram_id=-100500,
        )

        set_access_status(db, row.id, STATUS_APPROVED, bot_key=HEVC_BOT_KEY)
        db.refresh(row)
        set_access_status(db, row.id, STATUS_APPROVED, bot_key=HEVC_BOT_KEY)

        messages = list(db.scalars(select(TelegramOutbox)).all())
        assert len(messages) == 1
        message = messages[0]
        assert message.bot_key == HEVC_BOT_KEY
        assert message.chat_id == "-100500"
        assert message.dedupe_key == f"hevc_access_welcome:chat:{row.id}"
        assert "parse_mode" not in message.payload_json
        assert "Всем привет! 🎒✨" in message.payload_json["text"]
        assert "\n• /overdue\n" not in message.payload_json["text"]
        assert "\n• /overdue — список просроченных релизов\n" in message.payload_json["text"]
        assert message.payload_json["text"].splitlines()[-1] == (
            "• /error — расхождения типов AVC/HEVC"
        )


def test_existing_welcome_dedupe_does_not_break_approval() -> None:
    with _session() as db:
        row = register_access_attempt(
            db,
            bot_key=HEVC_BOT_KEY,
            subject_type=SUBJECT_USER,
            telegram_id=778,
        )
        db.add(
            TelegramOutbox(
                bot_key=HEVC_BOT_KEY,
                dedupe_key=f"hevc_access_welcome:user:{row.id}",
                chat_id="778",
                payload_json={"text": "already queued"},
                status="pending",
                attempts=0,
            )
        )
        db.commit()

        approved = set_access_status(
            db,
            row.id,
            STATUS_APPROVED,
            bot_key=HEVC_BOT_KEY,
        )

        assert approved is not None
        assert approved.status == STATUS_APPROVED
        assert len(list(db.scalars(select(TelegramOutbox)).all())) == 1


def test_register_access_attempt_retries_after_unique_race() -> None:
    with _session() as db:
        existing = register_access_attempt(
            db,
            bot_key=HEVC_BOT_KEY,
            subject_type=SUBJECT_USER,
            telegram_id=321,
        )
        existing_id = existing.id
        original_scalar = db.scalar
        missed = {"first": True}

        def scalar_miss_once(statement, *args, **kwargs):  # noqa: ANN001
            if missed["first"]:
                missed["first"] = False
                return None
            return original_scalar(statement, *args, **kwargs)

        db.scalar = scalar_miss_once  # type: ignore[method-assign]
        result = register_access_attempt(
            db,
            bot_key=HEVC_BOT_KEY,
            subject_type=SUBJECT_USER,
            telegram_id=321,
            username="racer",
        )
        assert result.id == existing_id
        assert result.username == "racer"
        assert result.status == STATUS_PENDING
        assert len(list(db.scalars(select(TelegramBotAccess)).all())) == 1


def test_reject_cancels_pending_hevc_outbox_for_that_chat() -> None:
    with _session() as db:
        user = register_access_attempt(
            db,
            bot_key=HEVC_BOT_KEY,
            subject_type=SUBJECT_USER,
            telegram_id=900,
        )
        db.add(
            TelegramOutbox(
                bot_key=HEVC_BOT_KEY,
                dedupe_key="hevc_access_welcome:user:pending",
                chat_id="900",
                payload_json={"kind": "hevc_access_welcome", "text": "hi"},
                status="pending",
                attempts=0,
            )
        )
        db.add(
            TelegramOutbox(
                bot_key="primary",
                chat_id="900",
                payload_json={"kind": "tracking_toggle", "text": "keep"},
                status="pending",
                attempts=0,
            )
        )
        db.commit()

        set_access_status(db, user.id, STATUS_REJECTED, bot_key=HEVC_BOT_KEY)
        rows = {row.bot_key: row for row in db.scalars(select(TelegramOutbox)).all()}
        assert rows[HEVC_BOT_KEY].status == "cancelled"
        assert rows[HEVC_BOT_KEY].last_error == "acl_rejected"
        assert rows["primary"].status == "pending"


def test_hevc_recipient_is_approved_only_for_approved_subjects() -> None:
    with _session() as db:
        row = register_access_attempt(
            db,
            bot_key=HEVC_BOT_KEY,
            subject_type=SUBJECT_CHAT,
            telegram_id=-1001,
        )
        assert hevc_recipient_is_approved(db, "-1001") is False
        set_access_status(db, row.id, STATUS_APPROVED, bot_key=HEVC_BOT_KEY)
        assert hevc_recipient_is_approved(db, "-1001") is True
        set_access_status(db, row.id, STATUS_REJECTED, bot_key=HEVC_BOT_KEY)
        assert hevc_recipient_is_approved(db, "-1001") is False
        assert hevc_recipient_is_approved(db, "not-a-chat") is False


def test_cancel_pending_hevc_outbox_is_scoped_to_hevc_and_chat() -> None:
    with _session() as db:
        db.add(
            TelegramOutbox(
                bot_key=HEVC_BOT_KEY,
                dedupe_key="overdue-a",
                chat_id="-1001",
                payload_json={"kind": "hevc_overdue"},
                status="pending",
                attempts=0,
            )
        )
        db.add(
            TelegramOutbox(
                bot_key=HEVC_BOT_KEY,
                dedupe_key="overdue-b",
                chat_id="-1002",
                payload_json={"kind": "hevc_overdue"},
                status="pending",
                attempts=0,
            )
        )
        db.commit()
        assert cancel_pending_hevc_outbox(db, -1001) == 1
        db.commit()
        rows = list(db.scalars(select(TelegramOutbox)).all())
        by_chat = {row.chat_id: row.status for row in rows}
        assert by_chat == {"-1001": "cancelled", "-1002": "pending"}


def _hevc_settings():
    return SimpleNamespace(token="hevc-token", api_base_url="", bot_key=HEVC_BOT_KEY)


def test_hevc_drain_skips_rejected_recipient_and_sends_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import telegram_outbox

    sent: list[str] = []

    async def fake_send(*, token, base_url, chat_id, payload):  # noqa: ANN001
        sent.append(chat_id)

    monkeypatch.setattr(telegram_outbox, "send_outbox_message", fake_send)
    monkeypatch.setattr(
        telegram_outbox,
        "resolve_telegram_bot_settings",
        lambda db, bot_key: _hevc_settings(),
    )

    with _session() as db:
        approved = register_access_attempt(
            db,
            bot_key=HEVC_BOT_KEY,
            subject_type=SUBJECT_CHAT,
            telegram_id=-1001,
        )
        rejected = register_access_attempt(
            db,
            bot_key=HEVC_BOT_KEY,
            subject_type=SUBJECT_CHAT,
            telegram_id=-1002,
        )
        set_access_status(db, approved.id, STATUS_APPROVED, bot_key=HEVC_BOT_KEY)
        set_access_status(db, rejected.id, STATUS_REJECTED, bot_key=HEVC_BOT_KEY)
        for row in list(db.scalars(select(TelegramOutbox)).all()):
            db.delete(row)
        db.commit()
        db.add(
            TelegramOutbox(
                bot_key=HEVC_BOT_KEY,
                dedupe_key="overdue-ok",
                chat_id="-1001",
                payload_json={"kind": "hevc_overdue", "text": "ok"},
                status="pending",
                attempts=0,
            )
        )
        db.add(
            TelegramOutbox(
                bot_key=HEVC_BOT_KEY,
                dedupe_key="overdue-no",
                chat_id="-1002",
                payload_json={"kind": "hevc_overdue", "text": "no"},
                status="pending",
                attempts=0,
            )
        )
        db.commit()

        stats = asyncio.run(telegram_outbox.drain_outbox(db, bot_key=HEVC_BOT_KEY))
        assert stats["sent"] == 1
        assert stats["skipped"] == 1
        assert sent == ["-1001"]
        rows = {row.chat_id: row.status for row in db.scalars(select(TelegramOutbox)).all()}
        assert rows["-1001"] == "sent"
        assert rows["-1002"] == "cancelled"


def test_primary_drain_does_not_use_hevc_acl(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import telegram_outbox

    sent: list[str] = []

    async def fake_send(*, token, base_url, chat_id, payload):  # noqa: ANN001
        sent.append(chat_id)

    monkeypatch.setattr(telegram_outbox, "send_outbox_message", fake_send)
    monkeypatch.setattr(
        telegram_outbox,
        "resolve_telegram_bot_settings",
        lambda db, bot_key: SimpleNamespace(
            token="primary-token", api_base_url="", bot_key="primary"
        ),
    )

    with _session() as db:
        db.add(
            TelegramOutbox(
                bot_key="primary",
                chat_id="900",
                payload_json={"text": "primary"},
                status="pending",
                attempts=0,
            )
        )
        db.commit()
        stats = asyncio.run(telegram_outbox.drain_outbox(db, bot_key="primary"))
        assert stats == {"sent": 1, "failed": 0, "skipped": 0}
        assert sent == ["900"]


def test_hevc_user_welcome_dead_letters_forbidden_dm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import telegram_outbox

    async def fake_send(*, token, base_url, chat_id, payload):  # noqa: ANN001
        raise RuntimeError("Forbidden: bot can't initiate conversation with a user")

    monkeypatch.setattr(telegram_outbox, "send_outbox_message", fake_send)
    monkeypatch.setattr(
        telegram_outbox,
        "resolve_telegram_bot_settings",
        lambda db, bot_key: _hevc_settings(),
    )

    with _session() as db:
        user = register_access_attempt(
            db,
            bot_key=HEVC_BOT_KEY,
            subject_type=SUBJECT_USER,
            telegram_id=777,
        )
        set_access_status(db, user.id, STATUS_APPROVED, bot_key=HEVC_BOT_KEY)
        welcome = next(iter(db.scalars(select(TelegramOutbox)).all()))
        assert welcome.chat_id == "777"
        assert welcome.status == "pending"

        stats = asyncio.run(telegram_outbox.drain_outbox(db, bot_key=HEVC_BOT_KEY))
        db.refresh(welcome)
        assert stats["failed"] == 1
        assert stats["sent"] == 0
        assert welcome.status == "failed"
        assert "initiate conversation" in (welcome.last_error or "")


def test_hevc_group_welcome_retries_forbidden_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import telegram_outbox

    async def fake_send(*, token, base_url, chat_id, payload):  # noqa: ANN001
        raise RuntimeError("Forbidden: bot was kicked from the group chat")

    monkeypatch.setattr(telegram_outbox, "send_outbox_message", fake_send)
    monkeypatch.setattr(
        telegram_outbox,
        "resolve_telegram_bot_settings",
        lambda db, bot_key: _hevc_settings(),
    )

    with _session() as db:
        chat = register_access_attempt(
            db,
            bot_key=HEVC_BOT_KEY,
            subject_type=SUBJECT_CHAT,
            telegram_id=-100500,
        )
        set_access_status(db, chat.id, STATUS_APPROVED, bot_key=HEVC_BOT_KEY)
        welcome = next(iter(db.scalars(select(TelegramOutbox)).all()))

        stats = asyncio.run(telegram_outbox.drain_outbox(db, bot_key=HEVC_BOT_KEY))
        db.refresh(welcome)
        assert stats["failed"] == 1
        assert welcome.status == "pending"
        assert welcome.attempts == 1


def test_overdue_enqueue_survives_dedupe_integrity_error(monkeypatch) -> None:
    from app.services.hevc_bot import HevcReleaseStatus
    from app.services.hevc_notifications import (
        enqueue_overdue_event_notifications,
        overdue_transition_dedupe_key,
    )

    monkeypatch.setattr(
        "app.services.hevc_notifications.list_approved_group_ids",
        lambda db, bot_key: [-1001],
    )
    monkeypatch.setattr(
        "app.services.hevc_notifications.query_release_detail",
        lambda db, release_id: HevcReleaseStatus(
            release_id=9,
            alias="show",
            title="Show",
            original_title=None,
            executors=[],
        ),
    )
    key = overdue_transition_dedupe_key(
        release_id=9, info_hash="ff" * 20, prev_event_id=3, chat_id=-1001
    )
    with _session() as db:
        db.add(
            TelegramOutbox(
                bot_key=HEVC_BOT_KEY,
                dedupe_key=key,
                chat_id="-1001",
                payload_json={"kind": "hevc_overdue"},
                status="pending",
                attempts=0,
            )
        )
        db.commit()
        original_scalar = db.scalar
        missed = {"first": True}

        def scalar_miss_once(statement, *args, **kwargs):  # noqa: ANN001
            if missed["first"]:
                missed["first"] = False
                return None
            return original_scalar(statement, *args, **kwargs)

        db.scalar = scalar_miss_once  # type: ignore[method-assign]
        event = SimpleNamespace(
            id=99,
            pipeline_id=3,
            event_type="hevc_status",
            from_status="ok",
            to_status="overdue",
            details_json={
                "release_id": 9,
                "info_hash": "ff" * 20,
                "prev_hevc_status_event_id": 3,
            },
        )
        assert enqueue_overdue_event_notifications(db, event) == 0
        assert len(list(db.scalars(select(TelegramOutbox)).all())) == 1
