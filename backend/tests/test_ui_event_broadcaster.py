"""Deterministic fan-out and lifecycle regressions for the shared SSE poller."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from app.services.ui_events import UiEventBroadcaster


@dataclass
class _Read:
    channels: tuple[str, ...]
    result: asyncio.Future[dict[str, str]]
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    def reply(self, tokens: dict[str, str]) -> None:
        self.result.set_result(tokens)

    def fail(self, error: Exception) -> None:
        self.result.set_exception(error)


class _ControlledReader:
    """Hold each read until the test supplies its complete database snapshot."""

    def __init__(self) -> None:
        self.calls: list[_Read] = []
        self.started: asyncio.Queue[_Read] = asyncio.Queue()
        self.closing = False
        self.active = 0
        self.max_active = 0

    async def read(self, _function, channels: list[str]) -> dict[str, str]:
        if self.closing:
            return {}
        call = _Read(tuple(channels), asyncio.get_running_loop().create_future())
        self.calls.append(call)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.put_nowait(call)
        try:
            return await call.result
        finally:
            self.active -= 1
            call.finished.set()

    async def next(self) -> _Read:
        return await asyncio.wait_for(self.started.get(), timeout=1)

    def release_all(self) -> None:
        self.closing = True
        for call in self.calls:
            if not call.result.done():
                call.reply({})


@asynccontextmanager
async def _controlled_broadcaster(
    *, poll_interval: float = 0,
) -> AsyncIterator[tuple[UiEventBroadcaster, _ControlledReader]]:
    reader = _ControlledReader()
    broadcaster = UiEventBroadcaster(poll_interval=poll_interval)
    with patch("app.services.ui_events.run_in_threadpool", side_effect=reader.read):
        try:
            yield broadcaster, reader
        finally:
            reader.release_all()
            await asyncio.wait_for(broadcaster.close(), timeout=1)


def test_broadcaster_reads_channel_union_once_and_fans_out_only_subscriptions() -> None:
    async def run() -> None:
        async with _controlled_broadcaster() as (broadcaster, reader):
            async with broadcaster.subscribe(["jobs", "jobs"]) as jobs:
                async with broadcaster.subscribe(["jobs", "releases"]) as both:
                    baseline = await reader.next()
                    assert set(baseline.channels) == {"jobs", "releases"}
                    assert len(baseline.channels) == 2
                    baseline.reply({"jobs": "j0", "releases": "r0"})
                    changed = await reader.next()
                    assert await jobs.wait(timeout=0) == []
                    assert await both.wait(timeout=0) == []

                    changed.reply({"jobs": "j1", "releases": "r1"})
                    await reader.next()
                    assert await jobs.wait(timeout=0) == ["jobs"]
                    assert set(await both.wait(timeout=0)) == {"jobs", "releases"}
                    assert reader.max_active == 1
                    assert len(reader.calls) == 3

    asyncio.run(run())


def test_slow_subscriber_coalesces_changes_without_blocking_fast_subscriber() -> None:
    async def run() -> None:
        async with _controlled_broadcaster() as (broadcaster, reader):
            async with broadcaster.subscribe(["jobs", "releases"]) as slow:
                async with broadcaster.subscribe(["jobs", "releases"]) as fast:
                    initial = await reader.next()
                    initial.reply({"jobs": "j0", "releases": "r0"})
                    current = await reader.next()
                    for version in range(1, 101):
                        current.reply({
                            "jobs": f"j{version}",
                            "releases": "r0" if version < 50 else "r1",
                        })
                        current = await reader.next()
                        expected = {"jobs", "releases"} if version == 50 else {"jobs"}
                        assert set(await fast.wait(timeout=0)) == expected

                    assert set(await slow.wait(timeout=0)) == {"jobs", "releases"}
                    assert await slow.wait(timeout=0) == []
                    assert reader.max_active == 1

    asyncio.run(run())


def test_late_subscriber_receives_next_change_to_an_existing_channel() -> None:
    async def run() -> None:
        async with _controlled_broadcaster() as (broadcaster, reader):
            async with broadcaster.subscribe(["jobs"]) as first:
                initial = await reader.next()
                initial.reply({"jobs": "j0"})
                changed = await reader.next()
                async with broadcaster.subscribe(["jobs"]) as late:
                    assert await late.wait(timeout=0) == []
                    changed.reply({"jobs": "j1"})
                    await reader.next()
                    assert await first.wait(timeout=0) == ["jobs"]
                    assert await late.wait(timeout=0) == ["jobs"]

    asyncio.run(run())


def test_new_channel_joining_during_read_is_polled_next_without_resetting_existing_baseline() -> None:
    async def run() -> None:
        async with _controlled_broadcaster() as (broadcaster, reader):
            async with broadcaster.subscribe(["jobs"]) as jobs:
                initial = await reader.next()
                initial.reply({"jobs": "j0"})
                in_flight = await reader.next()
                assert in_flight.channels == ("jobs",)
                async with broadcaster.subscribe(["releases"]) as releases:
                    in_flight.reply({"jobs": "j1"})
                    joined = await reader.next()
                    assert set(joined.channels) == {"jobs", "releases"}
                    assert await jobs.wait(timeout=0) == ["jobs"]
                    joined.reply({"jobs": "j1", "releases": "r0"})
                    changed = await reader.next()
                    assert await jobs.wait(timeout=0) == []
                    assert await releases.wait(timeout=0) == []
                    changed.reply({"jobs": "j1", "releases": "r1"})
                    await reader.next()
                    assert await jobs.wait(timeout=0) == []
                    assert await releases.wait(timeout=0) == ["releases"]

    asyncio.run(run())


def test_subscribing_to_active_channel_does_not_trigger_an_extra_database_read() -> None:
    async def run() -> None:
        async with _controlled_broadcaster(poll_interval=60) as (broadcaster, reader):
            async with broadcaster.subscribe(["jobs"]):
                initial = await reader.next()
                initial.reply({"jobs": "j0"})
                await asyncio.wait_for(initial.finished.wait(), timeout=1)
                async with broadcaster.subscribe(["jobs"]) as late:
                    assert await late.wait(timeout=0.01) == []
                    assert len(reader.calls) == 1

    asyncio.run(run())


def test_unsubscribed_channels_are_dropped_and_idle_broadcaster_restarts() -> None:
    async def run() -> None:
        async with _controlled_broadcaster() as (broadcaster, reader):
            async with broadcaster.subscribe(["jobs"]) as jobs:
                async with broadcaster.subscribe(["releases"]) as releases:
                    baseline = await reader.next()
                    baseline.reply({"jobs": "j0", "releases": "r0"})
                    in_flight = await reader.next()
                assert releases.closed
                in_flight.reply({"jobs": "j1", "releases": "r1"})
                jobs_only = await reader.next()
                assert jobs_only.channels == ("jobs",)
                assert await jobs.wait(timeout=0) == ["jobs"]
                assert await releases.wait(timeout=0) == []

            task = broadcaster._task
            jobs_only.reply({"jobs": "j2"})
            if task is not None:
                await asyncio.wait_for(asyncio.shield(task), timeout=1)
            assert broadcaster._task is None
            assert broadcaster._channels == {}
            assert len(reader.calls) == 3

            async with broadcaster.subscribe(["jobs"]) as restarted:
                baseline = await reader.next()
                baseline.reply({"jobs": "j3"})
                changed = await reader.next()
                assert await restarted.wait(timeout=0) == []
                changed.reply({"jobs": "j4"})
                await reader.next()
                assert await restarted.wait(timeout=0) == ["jobs"]

    asyncio.run(run())


def test_polling_recovers_from_read_failures_without_resetting_channel_baselines() -> None:
    async def run() -> None:
        async with _controlled_broadcaster() as (broadcaster, reader):
            async with broadcaster.subscribe(["jobs", "releases"]) as subscription:
                initial = await reader.next()
                initial.reply({"jobs": "j0", "releases": "r0"})
                failing = await reader.next()
                failing.fail(RuntimeError("database temporarily unavailable"))
                partial = await reader.next()
                assert await subscription.wait(timeout=0) == []

                # _read_channel_tokens omits only the channel whose SQL failed.
                partial.reply({"releases": "r1"})
                recovered = await reader.next()
                assert await subscription.wait(timeout=0) == ["releases"]
                recovered.reply({"jobs": "j1", "releases": "r1"})
                await reader.next()
                assert await subscription.wait(timeout=0) == ["jobs"]

    asyncio.run(run())


def test_last_unsubscribe_and_resubscribe_reuses_live_thread_and_discards_old_baseline() -> None:
    async def run() -> None:
        broadcaster = UiEventBroadcaster(poll_interval=0.001)
        loop = asyncio.get_running_loop()
        first_started = asyncio.Event()
        third_started = asyncio.Event()
        release_first = threading.Event()
        lock = threading.Lock()
        calls = 0
        active = 0
        max_active = 0
        current_token = "fresh"
        read_threads: list[int] = []

        def read(channels: list[str]) -> dict[str, str]:
            nonlocal calls, active, max_active
            with lock:
                calls += 1
                number = calls
                active += 1
                max_active = max(max_active, active)
                read_threads.append(threading.get_ident())
            try:
                if number == 1:
                    loop.call_soon_threadsafe(first_started.set)
                    assert release_first.wait(timeout=2)
                    return {"jobs": "stale"}
                if number == 3:
                    loop.call_soon_threadsafe(third_started.set)
                return {channel: current_token for channel in channels}
            finally:
                with lock:
                    active -= 1

        first_context = broadcaster.subscribe(["jobs"])
        with patch("app.services.ui_events._read_channel_tokens", side_effect=read):
            try:
                await first_context.__aenter__()
                await asyncio.wait_for(first_started.wait(), timeout=1)
                original_task = broadcaster._task
                await asyncio.wait_for(first_context.__aexit__(None, None, None), timeout=1)
                async with broadcaster.subscribe(["jobs"]) as replacement:
                    assert broadcaster._task is original_task
                    release_first.set()
                    await asyncio.wait_for(third_started.wait(), timeout=1)
                    assert await replacement.wait(timeout=0) == []
                    current_token = "changed"
                    assert await replacement.wait(timeout=1) == ["jobs"]
                    assert max_active == 1
                    assert read_threads
                    assert all(thread != threading.get_ident() for thread in read_threads)
            finally:
                release_first.set()
                await first_context.__aexit__(None, None, None)
                await asyncio.wait_for(broadcaster.close(), timeout=2)

    asyncio.run(run())


def test_close_wakes_subscribers_and_drains_in_flight_sql_without_cancelling_it() -> None:
    async def run() -> None:
        broadcaster = UiEventBroadcaster(poll_interval=0)
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        finished = threading.Event()
        calls: list[tuple[str, ...]] = []
        close_task: asyncio.Task[None] | None = None

        def read(channels: list[str]) -> dict[str, str]:
            calls.append(tuple(channels))
            loop.call_soon_threadsafe(started.set)
            try:
                assert release.wait(timeout=2)
                return {"jobs": "j0"}
            finally:
                finished.set()

        with patch("app.services.ui_events._read_channel_tokens", side_effect=read):
            try:
                async with broadcaster.subscribe(["jobs"]) as subscription:
                    await asyncio.wait_for(started.wait(), timeout=1)
                    waiting = asyncio.create_task(subscription.wait(timeout=10))
                    close_task = asyncio.create_task(broadcaster.close())
                    assert await asyncio.wait_for(waiting, timeout=1) == []
                    assert subscription.closed
                    assert not close_task.done()
                    assert not finished.is_set()
                    release.set()
                    await asyncio.wait_for(asyncio.shield(close_task), timeout=1)
                    assert finished.is_set()
                    assert calls == [("jobs",)]
                    assert broadcaster._task is None

                await broadcaster.close()
                with pytest.raises(RuntimeError, match="closed"):
                    async with broadcaster.subscribe(["jobs"]):
                        pytest.fail("A closed broadcaster must reject new subscriptions")
            finally:
                release.set()
                if close_task is not None:
                    await asyncio.wait_for(asyncio.shield(close_task), timeout=2)
                await broadcaster.close()

    asyncio.run(run())
