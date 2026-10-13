"""Equivalent domain results across immutable snapshots and bounded worker counts."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import threading
from types import SimpleNamespace

import pytest

from app.services.hevc_pairing import content_file_keys, find_unpaired_avc
from app.services import hevc_parallel as experiment


def row(archive_id, release_id, codec="AVC", episodes="1-9", *, rip="WEBRip", **kwargs):
    values = dict(
        id=archive_id, release_id=release_id, torrent_id=archive_id,
        info_hash=f"{archive_id:040x}", torrent_type=f"{rip} 1080p {codec}",
        torrent_description=episodes,
        quality_json={"type": {"value": rip}, "quality": {"value": "1080p"}, "codec": {"label": codec}},
        created_at=datetime(2026, 10, 10), api_created_at=None,
        api_present=True, superseded=False, ignore_hevc=False,
    )
    values.update(kwargs)
    return SimpleNamespace(**values)


@pytest.fixture
def catalog():
    now = datetime(2026, 10, 13, 5, 25, tzinfo=timezone(timedelta(hours=7)))
    naive_now = now.astimezone(timezone.utc).replace(tzinfo=None)
    history_at = naive_now - timedelta(hours=50)
    recent = naive_now - timedelta(minutes=2)
    rows = [
        # Interleaved releases ensure completion/ID ordering cannot replace first appearance.
        row(100, 91, "AVC", "1-8", torrent_id=39768, created_at=history_at,
            api_created_at=history_at, api_present=False, superseded=True, ignore_hevc=True),
        row(200, 7, "AVC", "OVA 1-2", ignore_hevc=True),
        row(101, 91, "HEVC", "1-8", torrent_id=39663, created_at=history_at-timedelta(hours=1),
            api_created_at=history_at-timedelta(hours=1)),
        row(300, 55, "AVC", "1-4", rip="WEBRip"),
        row(301, 55, "HEVC", "1-4", rip="WEB-DL"),
        row(102, 91, "AVC", "1-9", torrent_id=39768, created_at=naive_now, api_created_at=recent),
        row(201, 7, "HEVC", "OVA 1", api_present=False),
        row(400, 21, "AVC", "film", api_created_at=naive_now-timedelta(hours=24)),
        row(401, 21, "HEVC", "film", torrent_id=399, api_created_at=naive_now-timedelta(hours=25)),
        row(500, 8, "AVC", "label α", quality_json=None, torrent_type="BDRip 720p AVC"),
    ]
    nine = content_file_keys([f"older/ep_{i:02}.mkv" for i in range(1, 10)])
    keys = {100: nine, 102: nine}
    return rows, keys, now


@pytest.mark.parametrize("workers", [0, 1, 2, 4])
@pytest.mark.parametrize("include_ignored,require_active", [(False, True), (True, True), (False, False), (True, False)])
@pytest.mark.parametrize("releases_per_task", [1, 3, 32])
def test_full_domain_dataclasses_and_order_match_original(catalog, workers, include_ignored, require_active, releases_per_task):
    rows, keys, now = catalog
    options = dict(now=now, include_ignored=include_ignored, require_active=require_active, file_keys_by_archive_id=keys)
    expected = find_unpaired_avc(rows, **options)
    snapshot = experiment.snapshot_archives(rows, keys)
    actual = experiment.evaluate_snapshot(snapshot, now=now, include_ignored=include_ignored,
                                           require_active=require_active, workers=workers,
                                           releases_per_task=releases_per_task)
    assert actual == expected  # Compares every field, including clock, anchor, pair and presence keys.
    assert [r.release_id for r in snapshot.releases] == [91, 7, 55, 21, 8]
    if not include_ignored and require_active:
        rename = next(item for item in actual if item.archive_id == 102)
        assert rename.age_hours == 50
        assert rename.overdue and rename.created_at == rows[0].api_created_at
        assert next(item for item in actual if item.archive_id == 300).type_mismatch


def test_snapshot_copies_nested_json_file_keys_and_orm_values_before_workers(catalog):
    rows, keys, now = catalog
    expected = find_unpaired_avc(rows, now=now, file_keys_by_archive_id=keys)
    snapshot = experiment.snapshot_archives(rows, keys)
    for item in rows:
        item.release_id = -1
        item.torrent_description = "mutated after owning Session closed"
        if isinstance(item.quality_json, dict):
            item.quality_json["codec"]["label"] = "HEVC"
        item.created_at = datetime(2000, 1, 1)
    keys.clear()
    rows.clear()
    assert experiment.evaluate_snapshot(snapshot, now=now, workers=4, releases_per_task=1) == expected
    with pytest.raises(FrozenInstanceError):
        snapshot.releases[0].release_id = -1
    assert isinstance(snapshot.releases[0].archives[0].payload_json, str)
    assert isinstance(snapshot.releases[0].file_keys[0][1], frozenset)


def test_no_orm_access_in_threads_and_caller_executor_remains_usable(catalog):
    rows, keys, now = catalog
    snapshot = experiment.snapshot_archives(rows, keys)
    expected = find_unpaired_avc(rows, now=now, file_keys_by_archive_id=keys)

    class UnavailableOrm:
        def __getattribute__(self, name):
            raise AssertionError("ORM access after snapshot is forbidden")

    rows[:] = [UnavailableOrm()]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert experiment.evaluate_snapshot(snapshot, now=now, workers=2, releases_per_task=1, executor=pool) == expected
        assert pool.submit(lambda: "still open").result() == "still open"


def test_shared_now_is_captured_once_before_dispatch(monkeypatch, catalog):
    rows, keys, now = catalog
    calls = []

    def clock():
        calls.append(threading.get_ident())
        return now

    monkeypatch.setattr(experiment, "utcnow", clock)
    snapshot = experiment.snapshot_archives(rows, keys)
    actual = experiment.evaluate_snapshot(snapshot, workers=4, releases_per_task=1)
    assert actual == find_unpaired_avc(rows, now=now, file_keys_by_archive_id=keys)
    assert calls == [threading.get_ident()]


def test_smaller_worker_limit_bounds_inflight_work_on_larger_external_pool(monkeypatch):
    snapshot = experiment.snapshot_archives([row(i, i) for i in range(1, 9)])
    release = threading.Event()
    first_pair_started = threading.Event()
    lock = threading.Lock()
    active = 0
    maximum = 0
    started = 0
    original = experiment._evaluate_chunk

    def measured(*args, **kwargs):
        nonlocal active, maximum, started
        with lock:
            active += 1
            maximum = max(maximum, active)
            started += 1
            if started == 2:
                first_pair_started.set()
        try:
            assert release.wait(5), "test coordinator failed to release workers"
            return original(*args, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(experiment, "_evaluate_chunk", measured)
    with ThreadPoolExecutor(max_workers=6) as pool, ThreadPoolExecutor(max_workers=1) as coordinator:
        result = coordinator.submit(experiment.evaluate_snapshot, snapshot,
                                    now=datetime(2026, 10, 13), workers=2, releases_per_task=1, executor=pool)
        try:
            assert first_pair_started.wait(5)
            with lock:
                assert started == 2 and active == 2
        finally:
            release.set()
        assert result.result(timeout=5) == find_unpaired_avc([row(i, i) for i in range(1, 9)], now=datetime(2026, 10, 13))
    assert maximum == 2 and started == 8


def test_pool_exception_propagates_without_closing_caller_pool(monkeypatch):
    snapshot = experiment.snapshot_archives([row(i, i) for i in range(1, 5)])
    original = experiment._evaluate_chunk

    def fail_one(releases, **kwargs):
        if releases[0].release_id == 2:
            raise ValueError("bad release snapshot")
        return original(releases, **kwargs)

    monkeypatch.setattr(experiment, "_evaluate_chunk", fail_one)
    with ThreadPoolExecutor(max_workers=4) as pool:
        with pytest.raises(ValueError, match="bad release snapshot"):
            experiment.evaluate_snapshot(snapshot, workers=2, releases_per_task=1, executor=pool)
        assert pool.submit(lambda: 42).result() == 42


@pytest.mark.parametrize("options", [
    {"workers": -1}, {"workers": 9}, {"workers": True}, {"workers": 1.5},
    {"releases_per_task": 0}, {"releases_per_task": -1}, {"releases_per_task": True},
])
def test_invalid_bounds_fail_before_pool_creation(monkeypatch, options):
    monkeypatch.setattr(experiment, "ThreadPoolExecutor", lambda **kwargs: pytest.fail("must validate before pool"))
    with pytest.raises(ValueError):
        experiment.evaluate_snapshot(experiment.HevcSnapshot(()), **options)


def test_malformed_nonjson_metadata_fails_on_owning_thread():
    invalid = row(1, 1)
    invalid.quality_json["unserializable"] = object()
    with pytest.raises(TypeError):
        experiment.snapshot_archives([invalid])


def test_empty_input_returns_without_creating_pool(monkeypatch):
    monkeypatch.setattr(experiment, "ThreadPoolExecutor", lambda **kwargs: pytest.fail("empty input needs no workers"))
    assert experiment.evaluate_snapshot(experiment.snapshot_archives([]), workers=4) == []


def test_http_adapter_is_lazy_reuses_pool_and_preserves_clock_and_domain(catalog, monkeypatch):
    rows, keys, now = catalog
    experiment._shutdown_http_pool()
    before = experiment.pool_state()
    monkeypatch.setattr(experiment._domain_pairing, "utcnow", lambda: now)
    expected = find_unpaired_avc(rows, now=now, file_keys_by_archive_id=keys)
    try:
        assert before["pool_created"] is False
        assert experiment.find_unpaired_avc_parallel(rows, file_keys_by_archive_id=keys) == expected
        first = experiment.pool_state()
        assert first["pool_created"] is True
        assert first["created_count"] == before["created_count"] + 1
        assert 1 <= first["actual_thread_count"] <= 4
        assert experiment.find_unpaired_avc_parallel(rows, file_keys_by_archive_id=keys) == expected
        assert experiment.pool_state()["created_count"] == first["created_count"]
    finally:
        experiment._shutdown_http_pool()


@pytest.mark.parametrize("release_count, expected_chunk_size", [(1, 1), (3, 1), (4, 1), (5, 2), (128, 32), (1024, 256)])
def test_http_adapter_uses_predeclared_four_chunk_policy(monkeypatch, release_count, expected_chunk_size):
    captured = {}
    snapshot = experiment.HevcSnapshot(tuple(
        experiment.ReleaseSnapshot(rid, (), ()) for rid in range(release_count)
    ))
    sentinel = object()
    monkeypatch.setattr(experiment, "snapshot_archives", lambda *_: snapshot)
    monkeypatch.setattr(experiment, "_http_pool", lambda: sentinel)

    def evaluate(value, **options):
        assert value is snapshot
        captured.update(options)
        return []

    monkeypatch.setattr(experiment, "evaluate_snapshot", evaluate)
    assert experiment.find_unpaired_avc_parallel([]) == []
    assert captured["workers"] == 4
    assert captured["releases_per_task"] == expected_chunk_size
    assert captured["executor"] is sentinel


def test_empty_http_adapter_does_not_construct_pool(monkeypatch):
    monkeypatch.setattr(experiment, "_http_pool", lambda: pytest.fail("pool for empty input"))
    assert experiment.find_unpaired_avc_parallel([]) == []
