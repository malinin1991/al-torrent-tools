"""Тесты tracked_releases и enqueue telegram_outbox."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.telegram_notify import (
    OUTBOX_PENDING,
    SOURCE_BOT,
    SOURCE_UI,
    TG_STATUS_QUEUED,
    TG_STATUS_SKIPPED,
    build_telegram_api_url,
    build_torrent_notification_text,
    enqueue_pipeline_telegram_notification,
    enqueue_tracking_toggle_notification,
    escape_markdown_v2,
    normalize_telegram_bot_api_base,
    upsert_tracked_release,
)


def test_escape_markdown_v2() -> None:
    assert escape_markdown_v2("a_b*c") == r"a\_b\*c"


def test_build_telegram_api_url_with_colon_token() -> None:
    """urljoin ломается на токене с ':' — собираем URL вручную."""
    token = "7123456789:AAH-real-looking-token"
    url = build_telegram_api_url("", token, "getMe")
    assert url == f"https://api.telegram.org/bot{token}/getMe"
    assert url.startswith("https://")


def test_ptb_bot_api_base_url_no_trailing_slash() -> None:
    """PTB: base + token → .../botTOKEN, не .../bot/TOKEN."""
    from app.telegram_bot.__main__ import ptb_bot_api_base_url

    assert ptb_bot_api_base_url("") == "https://api.telegram.org/bot"
    assert ptb_bot_api_base_url("https://api.telegram.org") == "https://api.telegram.org/bot"
    assert ptb_bot_api_base_url("https://api.telegram.org/") == "https://api.telegram.org/bot"
    assert ptb_bot_api_base_url("https://api.telegram.org/bot") == "https://api.telegram.org/bot"
    assert ptb_bot_api_base_url("https://api.telegram.org/bot/") == "https://api.telegram.org/bot"
    assert ptb_bot_api_base_url("https://proxy.example") == "https://proxy.example/bot"

def test_normalize_telegram_bot_api_base_empty_and_no_scheme() -> None:
    assert normalize_telegram_bot_api_base("") == "https://api.telegram.org"
    assert normalize_telegram_bot_api_base("   ") == "https://api.telegram.org"
    assert normalize_telegram_bot_api_base("proxy.example:8081") == "https://proxy.example:8081"
    assert normalize_telegram_bot_api_base("http://proxy.example") == "http://proxy.example"


def test_classify_and_pick_latest_codec_torrents() -> None:
    from app.services.telegram_notify import classify_torrent_codec_family, pick_latest_codec_torrents

    torrents = [
        {
            "id": 1,
            "label": "WEBRip 1080p",
            "description": "1-10",
            "codec": {"label": "AVC", "value": "x264/AVC"},
            "updated_at": "2024-01-01T00:00:00Z",
        },
        {
            "id": 2,
            "label": "WEBRip 1080p",
            "description": "1-12",
            "codec": {"label": "AVC", "value": "x264/AVC"},
            "updated_at": "2024-06-01T00:00:00Z",
        },
        {
            "id": 3,
            "label": "WEBRip 1080p HEVC",
            "description": "1-12",
            "codec": {"label": "HEVC", "value": "x265/HEVC"},
            "updated_at": "2024-06-01T00:00:00Z",
        },
        {
            "id": 4,
            "label": "WEBRip 1080p AV1",
            "description": "1-8",
            "codec": {"label": "AV1", "value": "AV1"},
            "updated_at": "2024-05-01T00:00:00Z",
        },
    ]
    assert classify_torrent_codec_family(torrents[2]) == "HEVC"
    picked = pick_latest_codec_torrents(torrents)
    assert [t["id"] for t in picked] == [2, 3, 4]


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
    # MarkdownV2: жирный — одиночные *, не **
    assert "**" not in text
    assert text.startswith("🔔 *Обновление для")


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


def test_enqueue_noop_when_already_queued() -> None:
    db = MagicMock()
    pipeline = SimpleNamespace(id=1, release_id=10, torrent_id=100, tg_status=TG_STATUS_QUEUED)
    result = enqueue_pipeline_telegram_notification(db, pipeline)
    assert result.tg_status == TG_STATUS_QUEUED
    assert db.add.call_count == 0
    assert db.commit.call_count == 0


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


def test_enqueue_tracking_toggle_notification_add_and_del() -> None:
    from app.db.models import Setting

    settings = {
        "telegram_enabled": "true",
        "telegram_chat_id": "-100123",
    }

    def _get(model, key):  # noqa: ANN001
        if model is Setting:
            value = settings.get(key)
            return SimpleNamespace(value=value) if value is not None else None
        return None

    db = MagicMock()
    db.get.side_effect = _get

    added = enqueue_tracking_toggle_notification(db, enabled=True, title="Show!")
    assert added is not None
    assert db.add.called
    assert db.commit.called
    outbox = db.add.call_args[0][0]
    assert outbox.chat_id == "-100123"
    assert outbox.status == OUTBOX_PENDING
    assert outbox.payload_json["text"] == "✅ Добавлен: Show!"
    assert outbox.payload_json["kind"] == "tracking_toggle"
    assert "parse_mode" not in outbox.payload_json

    db.add.reset_mock()
    db.commit.reset_mock()
    disabled = enqueue_tracking_toggle_notification(db, enabled=False, title="Show!", commit=False)
    assert disabled is not None
    outbox2 = db.add.call_args[0][0]
    assert outbox2.payload_json["text"] == "❌ Отключен: Show!"
    assert "parse_mode" not in outbox2.payload_json
    assert db.commit.call_count == 0
    assert db.flush.called


def test_enqueue_tracking_toggle_truncates_long_title() -> None:
    from app.db.models import Setting
    from app.services.telegram_notify import _TELEGRAM_TEXT_MAX, _TRACK_NOTIFY_TITLE_MAX

    settings = {
        "telegram_enabled": "true",
        "telegram_chat_id": "-100123",
    }

    def _get(model, key):  # noqa: ANN001
        if model is Setting:
            value = settings.get(key)
            return SimpleNamespace(value=value) if value is not None else None
        return None

    db = MagicMock()
    db.get.side_effect = _get
    huge = "X" * 10_000
    outbox = enqueue_tracking_toggle_notification(db, enabled=True, title=huge)
    assert outbox is not None
    text = outbox.payload_json["text"]
    title = outbox.payload_json["title"]
    assert len(title) <= _TRACK_NOTIFY_TITLE_MAX
    assert len(text) <= _TELEGRAM_TEXT_MAX
    assert text.startswith("✅ Добавлен: ")
    assert text.endswith("…")


def test_enqueue_tracking_toggle_skipped_when_tg_disabled() -> None:
    from app.db.models import Setting

    def _get(model, key):  # noqa: ANN001
        if model is Setting and key == "telegram_enabled":
            return SimpleNamespace(value="false")
        return None

    db = MagicMock()
    db.get.side_effect = _get
    assert enqueue_tracking_toggle_notification(db, enabled=True, title="X") is None
    assert db.add.call_count == 0


def test_upsert_tracked_release_commit_false_flushes_only() -> None:
    db = MagicMock()
    db.get.return_value = None
    row = upsert_tracked_release(
        db,
        release_id=7,
        release_alias="alias",
        title="T",
        commit=False,
    )
    assert row.release_id == 7
    assert db.add.called
    assert db.flush.called
    assert db.commit.call_count == 0


def test_send_outbox_message_omits_parse_mode_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    import httpx

    from app.services.telegram_outbox import send_outbox_message

    captured: dict = {}

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"ok": True}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    asyncio.run(
        send_outbox_message(
            token="t",
            base_url="https://api.telegram.org",
            chat_id="-1",
            payload={"text": "✅ Добавлен: Show!", "disable_web_page_preview": True},
        )
    )
    assert "parse_mode" not in captured["json"]
    assert captured["json"]["text"] == "✅ Добавлен: Show!"

    asyncio.run(
        send_outbox_message(
            token="t",
            base_url="https://api.telegram.org",
            chat_id="-1",
            payload={"text": "x", "parse_mode": "MarkdownV2"},
        )
    )
    assert captured["json"]["parse_mode"] == "MarkdownV2"


def test_toggle_release_tracking_notifies_only_on_enabled_change(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import toggle_release_tracking

    calls: list[dict] = []
    upsert_kwargs: list[dict] = []

    def _enqueue(db, *, enabled, title, commit=True):  # noqa: ANN001
        calls.append({"enabled": enabled, "title": title, "commit": commit})
        return SimpleNamespace()

    def _upsert(db, **kwargs):  # noqa: ANN001
        upsert_kwargs.append(kwargs)
        return SimpleNamespace(
            enabled=kwargs["enabled"],
            source="ui",
            title="DB Title",
            release_alias=kwargs.get("release_alias") or "a",
        )

    monkeypatch.setattr("app.main.enqueue_tracking_toggle_notification", _enqueue)
    monkeypatch.setattr("app.main.upsert_tracked_release", _upsert)
    monkeypatch.setattr(
        "app.main.templates.TemplateResponse",
        lambda request, name, ctx: ctx,
    )

    request = MagicMock()

    # no-op: uncheck без записи
    db = MagicMock()
    db.get.return_value = None
    calls.clear()
    upsert_kwargs.clear()
    toggle_release_tracking(
        request, release_id=1, enabled=None, release_alias="a", title="T", db=db
    )
    assert calls == []
    assert upsert_kwargs == []

    # enable новой записи — title из DB row, один commit в эндпоинте
    db = MagicMock()
    db.get.return_value = None
    calls.clear()
    upsert_kwargs.clear()
    toggle_release_tracking(
        request, release_id=1, enabled="on", release_alias="a", title="Show!", db=db
    )
    assert upsert_kwargs == [
        {
            "release_id": 1,
            "release_alias": "a",
            "title": "Show!",
            "source": "ui",
            "enabled": True,
            "commit": False,
        }
    ]
    assert calls == [{"enabled": True, "title": "DB Title", "commit": False}]
    assert db.commit.called

    # повторный enable — без notify, но commit upsert
    db = MagicMock()
    db.get.return_value = SimpleNamespace(enabled=True, source="ui")
    calls.clear()
    toggle_release_tracking(
        request, release_id=1, enabled="on", release_alias="a", title="Show!", db=db
    )
    assert calls == []
    assert db.commit.called

    # disable — title из DB row после upsert, не сырой Form
    db = MagicMock()
    db.get.return_value = SimpleNamespace(enabled=True, source="ui")
    calls.clear()
    toggle_release_tracking(
        request, release_id=1, enabled=None, release_alias="a", title="FormSpam", db=db
    )
    assert calls == [{"enabled": False, "title": "DB Title", "commit": False}]
