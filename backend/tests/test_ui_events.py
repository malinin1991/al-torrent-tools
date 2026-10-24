"""Тесты SSE change-token'ов и парсинга каналов UI."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ui_events import UiEventBroadcaster, channel_token, parse_channels, sse_event_stream


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
    request.is_disconnected = AsyncMock(side_effect=[False, True])

    tokens = {"jobs": ["a", "a", "b", "b"]}

    def _token(_db, ch):  # noqa: ANN001
        seq = tokens[ch]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    async def _run() -> str:
        with (
            patch("app.services.ui_events.SessionLocal") as session_cls,
            patch("app.services.ui_events.channel_token", side_effect=_token),
        ):
            session_cls.return_value.__enter__.return_value = MagicMock()
            session_cls.return_value.__exit__.return_value = None
            broadcaster = UiEventBroadcaster(poll_interval=0.001)
            chunks: list[str] = []
            try:
                async with asyncio.timeout(1):
                    async for chunk in sse_event_stream(
                        request, ["jobs"], broadcaster=broadcaster, heartbeat_sec=999,
                    ):
                        chunks.append(chunk)
                assert not broadcaster._subscribers
            finally:
                await broadcaster.close()
            return "".join(chunks)

    joined = asyncio.run(_run())
    assert "event: jobs" in joined
    assert "data: jobs" in joined


def test_sse_empty_channels_comment() -> None:
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=True)

    async def _run() -> str:
        broadcaster = UiEventBroadcaster()
        try:
            parts = []
            async for chunk in sse_event_stream(request, [], broadcaster=broadcaster):
                parts.append(chunk)
            assert broadcaster._task is None
            return "".join(parts)
        finally:
            await broadcaster.close()

    assert "no-channels" in asyncio.run(_run())


def test_sse_stream_sends_connected_immediately() -> None:
    """Первый chunk до poll — иначе OpenResty буферит пустой body (HAR status 0)."""
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=True)

    async def _run() -> list[str]:
        broadcaster = UiEventBroadcaster()
        stream = sse_event_stream(request, ["info"], broadcaster=broadcaster)
        try:
            with patch("app.services.ui_events._read_channel_tokens") as read:
                chunks = [await anext(stream), await anext(stream)]
                read.assert_not_called()
                assert not broadcaster._subscribers
            return chunks
        finally:
            await stream.aclose()
            await broadcaster.close()

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
        ):
            factory.return_value.__enter__.return_value = session
            factory.return_value.__exit__.side_effect = exit_session
            broadcaster = UiEventBroadcaster(poll_interval=0.001)
            stream = sse_event_stream(request, ["jobs"], broadcaster=broadcaster, heartbeat_sec=999)
            try:
                assert "retry:" in await anext(stream)
                assert "connected" in await anext(stream)
                assert "event: jobs" in await asyncio.wait_for(anext(stream), timeout=1)
                assert closed, "A slow SSE client must not hold a DB session"
                assert threads and all(t != loop_thread for t in threads)
            finally:
                await stream.aclose()
                await broadcaster.close()

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


def test_stream_cancellation_unsubscribes_without_cancelling_shared_read() -> None:
    async def run() -> None:
        reading = asyncio.Event()
        release = asyncio.Event()

        async def read(*args):
            reading.set()
            await release.wait()
            return {"jobs": "baseline"}

        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        broadcaster = UiEventBroadcaster()
        stream = sse_event_stream(request, ["jobs"], broadcaster=broadcaster)
        pending = None
        try:
            with patch("app.services.ui_events.run_in_threadpool", side_effect=read):
                await anext(stream)
                await anext(stream)
                pending = asyncio.create_task(anext(stream))
                await asyncio.wait_for(reading.wait(), 1)
                poller = broadcaster._task
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                assert not broadcaster._subscribers
                assert not poller.done()
                release.set()
                await broadcaster.close()
        finally:
            release.set()
            if pending is not None and not pending.done():
                pending.cancel()
            await stream.aclose()
            await broadcaster.close()

    asyncio.run(run())


def test_stream_heartbeat_during_slow_read_and_shutdown() -> None:
    async def run() -> None:
        release = asyncio.Event()

        async def read(*args):
            await release.wait()
            return {"jobs": "baseline"}

        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        broadcaster = UiEventBroadcaster()
        stream = sse_event_stream(request, ["jobs"], broadcaster=broadcaster, heartbeat_sec=0.001)
        try:
            with patch("app.services.ui_events.run_in_threadpool", side_effect=read):
                await anext(stream)
                await anext(stream)
                assert await asyncio.wait_for(anext(stream), 1) == ": ping\n\n"
                release.set()
                await broadcaster.close()
                with pytest.raises(StopAsyncIteration):
                    await anext(stream)
                assert not broadcaster._subscribers
        finally:
            release.set()
            await stream.aclose()
            await broadcaster.close()

    asyncio.run(run())


def test_ui_events_route_uses_application_broadcaster() -> None:
    from app.main import ui_events

    async def run() -> None:
        broadcaster = UiEventBroadcaster()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ui_event_broadcaster=broadcaster)))
        try:
            with patch("app.main.sse_event_stream") as stream:
                response = await ui_events(request, channels="jobs,jobs,pipeline_detail:12")
                stream.assert_called_once_with(request, ["jobs", "pipeline_detail:12"], broadcaster=broadcaster)
                assert response.media_type == "text/event-stream"
                assert response.headers["X-Accel-Buffering"] == "no"
        finally:
            await broadcaster.close()

    asyncio.run(run())


def test_lifespan_closes_broadcaster_on_error_and_recreates_on_restart(tmp_path) -> None:
    from app import main

    application = SimpleNamespace(state=SimpleNamespace())
    broadcasters = []

    async def run() -> None:
        with (
            patch("app.logging_filters.setup_redacted_logging"),
            patch.object(main, "settings", SimpleNamespace(app_env="dev", secret_key="test")),
            patch.object(main, "resolve_torrent_storage_root", return_value=tmp_path),
            patch.object(main, "SessionLocal"),
            patch.object(main, "reclaim_orphan_jobs", return_value=0),
            patch.object(main, "reclaim_stale_jobs", return_value=0),
            patch.object(main, "shutdown_cancel_active_jobs", return_value=0) as shutdown,
        ):
            with pytest.raises(RuntimeError, match="lifespan body failed"):
                async with main.lifespan(application):
                    broadcaster = application.state.ui_event_broadcaster
                    broadcasters.append(broadcaster)
                    assert not broadcaster._closed
                    raise RuntimeError("lifespan body failed")
            assert broadcaster._closed
            shutdown.assert_called_once()

    # Reuse the application in a fresh loop, as consecutive TestClient lifespans do.
    asyncio.run(run())
    asyncio.run(run())
    assert broadcasters[0] is not broadcasters[1]


def test_releases_token_includes_presets_and_encoder_settings() -> None:
    db = MagicMock()
    db.scalar.return_value = 0
    policy = ["same-timestamp", 1, 1]
    settings = [("video_kensetsu_auto_enabled", "false"), ("video_kensetsu_base_url", "https://encoder.example"), ("video_kensetsu_enabled", "true")]

    def execute(statement):
        result = MagicMock()
        sql = str(statement)
        if "release_encoder_policies" in sql:
            result.one.return_value = tuple(policy)
        elif "settings" in sql:
            result.all.return_value = list(settings)
        else:
            result.one.return_value = (0, 0, 0, 0, 0)
        return result

    db.execute.side_effect = execute
    with patch("app.services.ui_events.time.time", return_value=120):
        first = channel_token(db, "releases")
        assert first == channel_token(db, "releases")
        policy[1] += 1
        second = channel_token(db, "releases")
        assert second != first
        settings[0] = ("video_kensetsu_auto_enabled", "true")
        third = channel_token(db, "releases")
        assert third != second
        settings[1] = ("video_kensetsu_base_url", "https://other.example")
        fourth = channel_token(db, "releases")
        assert fourth != third
        settings[2] = ("video_kensetsu_enabled", "false")
        assert channel_token(db, "releases") != fourth
        assert "https://" not in fourth
