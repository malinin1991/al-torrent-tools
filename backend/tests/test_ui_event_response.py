"""Exercise disconnects outside the SSE generator, during ASGI body sends."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import ClientDisconnect

from app.services.ui_events import UiEventBroadcaster, UiEventResponse, sse_event_stream


@asynccontextmanager
async def _response() -> AsyncIterator[tuple[UiEventBroadcaster, UiEventResponse, list[tuple[str, ...]]]]:
    broadcaster = UiEventBroadcaster(poll_interval=0)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    response = UiEventResponse(sse_event_stream(request, ["jobs"], broadcaster=broadcaster))
    calls: list[tuple[str, ...]] = []

    async def read(_function, channels: list[str]) -> dict[str, str]:
        calls.append(tuple(channels))
        return {channel: str(len(calls)) for channel in channels}

    with patch("app.services.ui_events.run_in_threadpool", side_effect=read):
        try:
            yield broadcaster, response, calls
        finally:
            await response.body_iterator.aclose()
            await asyncio.wait_for(broadcaster.close(), timeout=1)


async def _assert_polling_stopped(broadcaster, poller, calls) -> None:
    # Keep the response and body iterator alive in each caller: garbage
    # collection must not be responsible for releasing the subscription.
    assert not broadcaster._subscribers
    assert not broadcaster._channels
    assert poller is not None
    await asyncio.wait_for(asyncio.shield(poller), timeout=1)
    assert broadcaster._task is None
    reads_after_disconnect = len(calls)
    await asyncio.sleep(0)
    assert len(calls) == reads_after_disconnect


def test_asgi_24_send_error_closes_sse_subscription_before_response_is_discarded() -> None:
    async def run() -> None:
        async with _response() as (broadcaster, response, calls):
            body_iterator = response.body_iterator
            poller = None

            async def send(message):
                nonlocal poller
                if message["type"] == "http.response.body" and message["body"].startswith(b"event: jobs\n"):
                    assert broadcaster._subscribers
                    poller = broadcaster._task
                    raise OSError("client disconnected during the body send")

            async def receive():
                pytest.fail("ASGI 2.4 handles a broken send without a receive watcher")

            scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
            with pytest.raises(ClientDisconnect):
                await asyncio.wait_for(response(scope, receive, send), timeout=1)

            assert response.body_iterator is body_iterator
            await _assert_polling_stopped(broadcaster, poller, calls)

    asyncio.run(run())


def test_asgi_23_disconnect_cancels_body_send_and_closes_sse_subscription() -> None:
    async def run() -> None:
        async with _response() as (broadcaster, response, calls):
            body_iterator = response.body_iterator
            sending = asyncio.Event()
            send_cancelled = asyncio.Event()
            never_finish_send = asyncio.Event()
            poller = None

            async def send(message):
                nonlocal poller
                if message["type"] == "http.response.body" and message["body"].startswith(b"event: jobs\n"):
                    assert broadcaster._subscribers
                    poller = broadcaster._task
                    sending.set()
                    try:
                        await never_finish_send.wait()
                    except asyncio.CancelledError:
                        send_cancelled.set()
                        raise

            async def receive():
                await sending.wait()
                return {"type": "http.disconnect"}

            scope = {"type": "http", "asgi": {"spec_version": "2.3"}}
            await asyncio.wait_for(response(scope, receive, send), timeout=1)

            assert send_cancelled.is_set()
            assert response.body_iterator is body_iterator
            await _assert_polling_stopped(broadcaster, poller, calls)

    asyncio.run(run())


def test_cancelling_response_during_body_send_closes_sse_subscription() -> None:
    async def run() -> None:
        async with _response() as (broadcaster, response, calls):
            body_iterator = response.body_iterator
            sending = asyncio.Event()
            never_finish_send = asyncio.Event()
            poller = None

            async def send(message):
                nonlocal poller
                if message["type"] == "http.response.body" and message["body"].startswith(b"event: jobs\n"):
                    assert broadcaster._subscribers
                    poller = broadcaster._task
                    sending.set()
                    await never_finish_send.wait()

            async def receive():
                pytest.fail("ASGI 2.4 must not start a receive watcher")

            scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
            response_task = asyncio.create_task(response(scope, receive, send))
            try:
                await asyncio.wait_for(sending.wait(), timeout=1)
                response_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await response_task

                assert response.body_iterator is body_iterator
                await _assert_polling_stopped(broadcaster, poller, calls)
            finally:
                if not response_task.done():
                    response_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await response_task

    asyncio.run(run())
