"""Тесты tracked_releases и enqueue telegram_outbox."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.telegram_notify import (
    OUTBOX_PENDING,
    SOURCE_BOT,
    SOURCE_UI,
    TG_STATUS_QUEUED,
    TG_STATUS_SKIPPED,
    build_torrent_notification_text,
    enqueue_pipeline_telegram_notification,
    escape_markdown_v2,
    upsert_tracked_release,
)


def test_escape_markdown_v2() -> None:
    assert escape_markdown_v2("a_b*c") == r"a\_b\*c"


def test_build_torrent_notification_contains_title_and_series() -> None:
    text = build_torrent_notification_text(
        title="Тест",
        alias="test-alias",
        torrents=[
            {
                "label": "WEBRip 1080p",
                "description": "1-12",
                "codec": {"description": "HEVC"},
            }
        ],
    )
    assert "Тест" in text.replace("\\", "")
    assert "1\\-12" in text or "1-12" in text
    assert "HEVC" in text.replace("\\", "")


def test_upsert_tracked_release_creates_and_updates() -> None:
    db = MagicMock()
    db.get.return_value = None

    row = upsert_tracked_release(
        db,
        release_id=42,
        release_alias="show-alias",
        title="Show",
        source=SOURCE_UI,
        enabled=True,
    )

    assert db.add.called
    assert db.commit.called
    added = db.add.call_args[0][0]
    assert added.release_id == 42
    assert added.release_alias == "show-alias"
    assert added.source == SOURCE_UI
    assert added.enabled is True
    assert row is added

    existing = SimpleNamespace(
        release_id=42,
        release_alias="old",
        title="Old",
        enabled=False,
        source=SOURCE_BOT,
    )
    db.get.return_value = existing
    updated = upsert_tracked_release(
        db,
        release_id=42,
        release_alias="new-alias",
        title="New Title",
        source=SOURCE_UI,
        enabled=True,
    )
    assert updated.enabled is True
    assert updated.release_alias == "new-alias"
    assert updated.title == "New Title"
    # UI не затирает source=bot
    assert updated.source == SOURCE_BOT


def test_enqueue_skipped_when_not_tracked() -> None:
    db = MagicMock()
    db.get.return_value = None  # TrackedRelease missing
    pipeline = SimpleNamespace(id=1, release_id=10, torrent_id=100, tg_status="skipped")

    result = enqueue_pipeline_telegram_notification(db, pipeline)
    assert result.tg_status == TG_STATUS_SKIPPED
    assert db.add.call_count == 0


def test_enqueue_creates_outbox_when_tracked_and_enabled() -> None:
    from app.db.models import Setting, TrackedRelease

    tracked = SimpleNamespace(
        release_id=10,
        release_alias="alias",
        title="Title",
        enabled=True,
        source=SOURCE_UI,
    )
    settings = {
        "telegram_enabled": "true",
        "telegram_chat_id": "-100123",
        "telegram_bot_token": "token",
    }

    def _get(model, key):  # noqa: ANN001
        if model is TrackedRelease:
            return tracked
        if model is Setting:
            value = settings.get(key)
            return SimpleNamespace(value=value) if value is not None else None
        return None

    db = MagicMock()
    db.get.side_effect = _get
    pipeline = SimpleNamespace(id=7, release_id=10, torrent_id=55, tg_status="skipped")

    result = enqueue_pipeline_telegram_notification(
        db,
        pipeline,
        release_payload={"alias": "alias", "name": {"main": "Title"}},
        torrent_payload={"label": "BDRip", "description": "1", "codec": {"description": "AVC"}},
    )

    assert result.tg_status == TG_STATUS_QUEUED
    assert db.add.called
    outbox = db.add.call_args[0][0]
    assert outbox.pipeline_id == 7
    assert outbox.chat_id == "-100123"
    assert outbox.status == OUTBOX_PENDING
    assert "Title" in (outbox.payload_json.get("text") or "").replace("\\", "")
