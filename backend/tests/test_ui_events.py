"""Тесты SSE change-token'ов и парсинга каналов UI."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.ui_events import channel_token, parse_channels, sse_event_stream


def test_parse_channels_filters_and_dedupes() -> None:
    assert parse_channels("") == []
    assert parse_channels(None) == []
    assert parse_channels("jobs,pipeline,jobs,bogus") == ["jobs", "pipeline"]
    assert parse_channels("pipeline_detail:12,pipeline_detail:x,info") == [
        "pipeline_detail:12",
        "info",
    ]


def _jobs_db(*, max_job: int, max_log: int, active: int = 0) -> MagicMock:
    db = MagicMock()
    scalars = iter([max_job, max_log, active])

    def _scalar(_stmt):  # noqa: ANN001
        return next(scalars)

    db.scalar.side_effect = _scalar
    db.execute.return_value.all.return_value = [
        SimpleNamespace(
            id=max_job,
            status="success",
            finished_at="t1",
            started_at="t0",
            error=None,
        )
    ]
    return db


def test_jobs_token_changes_when_max_log_changes() -> None:
    t1 = channel_token(_jobs_db(max_job=10, max_log=1), "jobs")
    t2 = channel_token(_jobs_db(max_job=10, max_log=2), "jobs")
    assert t1 != t2
    assert channel_token(_jobs_db(max_job=10, max_log=2), "dashboard") == t2


def test_pipeline_detail_token_missing() -> None:
    db = MagicMock()
    db.get.return_value = None
    assert channel_token(db, "pipeline_detail:99") == "pd:99:missing"


def test_pipeline_detail_token_includes_status() -> None:
    db = MagicMock()
    db.get.return_value = SimpleNamespace(
        status="done",
        tg_status="skipped",
        error=None,
        master_added_at="t1",
        slave_added_at="t2",
    )
    db.scalar.return_value = 5
    token = channel_token(db, "pipeline_detail:7")
    assert "pd:7:done:skipped" in token
    assert token.endswith(":5") or ":5" in token


def test_sse_stream_emits_on_token_change() -> None:
    request = MagicMock()
    # цикл: init token → same → change (emit) → disconnect
    request.is_disconnected = AsyncMock(side_effect=[False, False, False, True])

    tokens = {"jobs": ["a", "a", "b", "b"]}

    def _token(_db, ch):  # noqa: ANN001
        seq = tokens[ch]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    async def _run() -> str:
        with (
            patch("app.services.ui_events.SessionLocal") as session_cls,
            patch("app.services.ui_events.channel_token", side_effect=_token),
            patch("app.services.ui_events.asyncio.sleep", new_callable=AsyncMock),
        ):
            session_cls.return_value.__enter__.return_value = MagicMock()
            session_cls.return_value.__exit__.return_value = None
            chunks: list[str] = []
            async for chunk in sse_event_stream(
                request,
                ["jobs"],
                poll_interval=0.01,
                heartbeat_sec=999,
            ):
                chunks.append(chunk)
            return "".join(chunks)

    joined = asyncio.run(_run())
    assert "event: jobs" in joined
    assert "data: jobs" in joined


def test_sse_empty_channels_comment() -> None:
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=True)

    async def _run() -> str:
        parts = []
        async for chunk in sse_event_stream(request, []):
            parts.append(chunk)
        return "".join(parts)

    assert "no-channels" in asyncio.run(_run())


def test_sse_stream_sends_connected_immediately() -> None:
    """Первый chunk до poll — иначе OpenResty буферит пустой body (HAR status 0)."""
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=True)

    async def _run() -> list[str]:
        with (
            patch("app.services.ui_events.SessionLocal") as session_cls,
            patch("app.services.ui_events.channel_token", return_value="t0"),
            patch("app.services.ui_events.asyncio.sleep", new_callable=AsyncMock),
        ):
            session_cls.return_value.__enter__.return_value = MagicMock()
            session_cls.return_value.__exit__.return_value = None
            chunks: list[str] = []
            async for chunk in sse_event_stream(
                request,
                ["info"],
                poll_interval=0.01,
                heartbeat_sec=999,
            ):
                chunks.append(chunk)
                if len(chunks) >= 2:
                    break
            return chunks

    chunks = asyncio.run(_run())
    assert any("retry:" in c for c in chunks)
    assert any("connected" in c for c in chunks)
