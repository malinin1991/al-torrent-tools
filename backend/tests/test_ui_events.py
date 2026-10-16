"""Тесты SSE change-token'ов и парсинга каналов UI."""

from __future__ import annotations

import asyncio
import threading
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


def test_parse_channels_limits_pipeline_ids_and_normalizes_duplicates() -> None:
    assert parse_channels(
        "pipeline_detail:²,pipeline_detail:١,pipeline_detail:0,"
        "pipeline_detail:9223372036854775808,pipeline_detail:" + "9" * 5000
    ) == []
    assert parse_channels(
        "pipeline_detail:00012,pipeline_detail:12,pipeline_detail: 12,"
        "pipeline_detail:9223372036854775807"
    ) == ["pipeline_detail:12", "pipeline_detail:9223372036854775807"]


def _jobs_db(*, max_job: int, max_log: int, active: int = 0, result_updated=None) -> MagicMock:
    db = MagicMock()
    scalars = iter([max_job, max_log, active, result_updated])

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


def test_releases_token_refreshes_hevc_sla_without_database_changes() -> None:
    db = MagicMock()
    db.scalar.return_value = 0
    db.execute.return_value.one.return_value = (0, 0, 0, 0, 0)
    with patch("app.services.ui_events.time.time", return_value=120):
        first = channel_token(db, "releases")
    with patch("app.services.ui_events.time.time", return_value=179):
        assert channel_token(db, "releases") == first
    with patch("app.services.ui_events.time.time", return_value=180):
        assert channel_token(db, "releases") != first


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
        slave_completed_at="t3",
    )
    db.scalar.return_value = 5
    token = channel_token(db, "pipeline_detail:7")
    assert "pd:7:done:skipped" in token
    assert "t3" in token
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


def test_sse_db_poll_runs_off_event_loop_and_closes_before_yield() -> None:
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)

    async def run() -> None:
        loop_thread = threading.get_ident()
        threads = []
        session = MagicMock()
        closed = False

        def exit_session(*args):
            nonlocal closed
            closed = True

        def token(db, channel):
            nonlocal closed
            closed = False
            threads.append(threading.get_ident())
            return str(len(threads))

        with (
            patch("app.services.ui_events.SessionLocal") as factory,
            patch("app.services.ui_events.channel_token", side_effect=token),
            patch("app.services.ui_events.asyncio.sleep", new_callable=AsyncMock),
        ):
            factory.return_value.__enter__.return_value = session
            factory.return_value.__exit__.side_effect = exit_session
            stream = sse_event_stream(request, ["jobs"], heartbeat_sec=999)
            try:
                assert "retry:" in await anext(stream)
                assert "connected" in await anext(stream)
                assert "event: jobs" in await anext(stream)
                assert closed, "A slow SSE client must not hold a DB session"
                assert threads and all(t != loop_thread for t in threads)
            finally:
                await stream.aclose()

    asyncio.run(run())


def test_failed_channel_rolls_back_before_reading_next_channel() -> None:
    from app.services.ui_events import _read_channel_tokens

    db = MagicMock()

    def token(session, channel):
        if channel == "jobs":
            raise RuntimeError("failed statement")
        session.rollback.assert_called_once_with()
        return "valid"

    with (
        patch("app.services.ui_events.SessionLocal") as factory,
        patch("app.services.ui_events.channel_token", side_effect=token),
    ):
        factory.return_value.__enter__.return_value = db
        assert _read_channel_tokens(["jobs", "releases"]) == {"releases": "valid"}
