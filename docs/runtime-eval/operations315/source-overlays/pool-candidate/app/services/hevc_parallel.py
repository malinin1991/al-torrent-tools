"""Candidate-only HEVC snapshot/compute boundary for whole-HTTP evaluation.

Compare the original function with workers=0 before interpreting thread scaling.
JSON strings deliberately isolate nested mutable payloads and ORM instances.
Every worker thaws its own dictionaries and calls the unchanged pairing rules.
"""

from __future__ import annotations

import atexit
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
import json
import threading
from typing import Any, Sequence

from app.services import hevc_pairing as _domain_pairing
from app.services.hevc_pairing import HEVC_SLA_HOURS, UnpairedAvc, find_unpaired_avc
from app.utils.datetime_fmt import utcnow


_HTTP_WORKERS = 4
_HTTP_THREAD_PREFIX = "hevc-http-pool"
_pool_lock = threading.Lock()
_pool: ThreadPoolExecutor | None = None
_created_count = 0


def _http_pool() -> ThreadPoolExecutor:
    """Construct inside the first non-empty HTTP evaluation; reuse per process."""
    global _pool, _created_count
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=_HTTP_WORKERS, thread_name_prefix=_HTTP_THREAD_PREFIX)
            _created_count += 1
        return _pool


def pool_state() -> dict[str, int | bool | str]:
    """Harness-only diagnostics, to be read outside the HTTP request timer."""
    with _pool_lock:
        return dict(
            pool_created=_pool is not None,
            created_count=_created_count,
            workers=_HTTP_WORKERS,
            actual_thread_count=sum(thread.name.startswith(_HTTP_THREAD_PREFIX)
                                    for thread in threading.enumerate()),
            chunk_policy="max(1, ceil(release_count / 4))",
        )


def _shutdown_http_pool() -> None:
    """Only at process exit or between tests when requests have quiesced."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=True, cancel_futures=True)


atexit.register(_shutdown_http_pool)


@dataclass(frozen=True, slots=True)
class ArchiveSnapshot:
    payload_json: str
    created_at: datetime | None
    api_created_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReleaseSnapshot:
    release_id: int
    archives: tuple[ArchiveSnapshot, ...]
    file_keys: tuple[tuple[int, frozenset[str]], ...]


@dataclass(frozen=True, slots=True)
class HevcSnapshot:
    releases: tuple[ReleaseSnapshot, ...]


def _attribute(row: Any, name: str, default: Any = None) -> Any:
    return row.get(name, default) if isinstance(row, dict) else getattr(row, name, default)


def snapshot_archives(
    archives: Sequence[Any],
    file_keys_by_archive_id: dict[int, frozenset[str]] | None = None,
) -> HevcSnapshot:
    """Run on the owning Session thread after loading all required attributes.

    Inputs are the same JSON-compatible archive values used by find_unpaired_avc.
    Snapshot creation may read ORM attributes; evaluation and its pool never do.
    Release order is first occurrence, row order within each release is unchanged.
    """
    by_release: dict[int, list[ArchiveSnapshot]] = {}
    ids_by_release: dict[int, list[int]] = {}
    fields = ("id", "release_id", "torrent_id", "info_hash", "torrent_type", "torrent_description", "quality_json")
    for row in archives:
        release_id = int(_attribute(row, "release_id"))
        values = {name: _attribute(row, name) for name in fields}
        values.update(
            api_present=bool(_attribute(row, "api_present", True)),
            superseded=bool(_attribute(row, "superseded", False)),
            ignore_hevc=bool(_attribute(row, "ignore_hevc", False)),
        )
        if not isinstance(values["quality_json"], dict):
            values["quality_json"] = None
        snapshot = ArchiveSnapshot(
            payload_json=json.dumps(values, ensure_ascii=False, separators=(",", ":")),
            created_at=_attribute(row, "created_at"),
            api_created_at=_attribute(row, "api_created_at"),
        )
        by_release.setdefault(release_id, []).append(snapshot)
        ids_by_release.setdefault(release_id, []).append(int(values["id"] or 0))
    file_keys = file_keys_by_archive_id or {}
    return HevcSnapshot(tuple(
        ReleaseSnapshot(
            release_id=release_id,
            archives=tuple(rows),
            file_keys=tuple((archive_id, frozenset(file_keys[archive_id]))
                            for archive_id in ids_by_release[release_id] if archive_id in file_keys),
        )
        for release_id, rows in by_release.items()
    ))


def _evaluate_chunk(
    releases: tuple[ReleaseSnapshot, ...], *, now: datetime, sla_hours: float,
    require_active: bool, include_ignored: bool,
) -> list[UnpairedAvc]:
    # These dictionaries belong to this task only. No Session/ORM/shared JSON.
    rows = []
    file_keys = {}
    for release in releases:
        for archive in release.archives:
            row = json.loads(archive.payload_json)
            row.update(created_at=archive.created_at, api_created_at=archive.api_created_at)
            rows.append(row)
        file_keys.update(release.file_keys)
    return find_unpaired_avc(
        rows, now=now, sla_hours=sla_hours, require_active=require_active,
        include_ignored=include_ignored, file_keys_by_archive_id=file_keys,
    )


def _evaluate_pool(pool: ThreadPoolExecutor, chunks: tuple[tuple[ReleaseSnapshot, ...], ...],
                   *, workers: int, options: dict) -> list[UnpairedAvc]:
    iterator = iter(enumerate(chunks))
    pending = {}
    results: list[list[UnpairedAvc] | None] = [None] * len(chunks)

    def submit_next() -> bool:
        try:
            index, chunk = next(iterator)
        except StopIteration:
            return False
        pending[pool.submit(_evaluate_chunk, chunk, **options)] = index
        return True

    for _ in range(workers):
        if not submit_next():
            break
    try:
        while pending:
            done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
            for future in done:
                results[pending.pop(future)] = future.result()
                submit_next()
    except BaseException:
        # Cancel this invocation's queued futures only; external pool remains owned by caller.
        for future in pending:
            future.cancel()
        wait(tuple(pending))
        raise
    return [item for chunk in results if chunk is not None for item in chunk]


def evaluate_snapshot(
    snapshot: HevcSnapshot, *, now: datetime | None = None,
    sla_hours: float = HEVC_SLA_HOURS, require_active: bool = True,
    include_ignored: bool = False, workers: int = 0, releases_per_task: int = 32,
    executor: ThreadPoolExecutor | None = None,
) -> list[UnpairedAvc]:
    """Same refactor sequentially (0), with cold pool (1..8), or a caller-owned pool.

    At most `workers` futures of this invocation are in flight, even if a supplied
    executor is larger. Completion order cannot change the returned row order.
    One fixed UTC clock is shared by all tasks; no task performs writes or I/O.
    Timing must report snapshot cost and cold/hot pool cost separately.
    """
    if isinstance(workers, bool) or not isinstance(workers, int) or not 0 <= workers <= 8:
        raise ValueError("workers must be an integer in 0..8; 0 means sequential")
    if isinstance(releases_per_task, bool) or not isinstance(releases_per_task, int) or releases_per_task < 1:
        raise ValueError("releases_per_task must be a positive integer")
    if workers == 0 and executor is not None:
        raise ValueError("sequential evaluation does not use an executor")
    current = now if now is not None else utcnow()
    options = dict(now=current, sla_hours=sla_hours, require_active=require_active, include_ignored=include_ignored)
    chunks = tuple(snapshot.releases[index:index + releases_per_task]
                   for index in range(0, len(snapshot.releases), releases_per_task))
    if not chunks:
        return []
    if workers == 0:
        return [item for chunk in chunks for item in _evaluate_chunk(chunk, **options)]
    if executor is not None:
        return _evaluate_pool(executor, chunks, workers=workers, options=options)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hevc-experiment") as pool:
        return _evaluate_pool(pool, chunks, workers=workers, options=options)


def find_unpaired_avc_parallel(
    archives: Sequence[Any], *, now: datetime | None = None,
    sla_hours: float = HEVC_SLA_HOURS, require_active: bool = True,
    include_ignored: bool = False,
    file_keys_by_archive_id: dict[int, frozenset[str]] | None = None,
) -> list[UnpairedAvc]:
    """Candidate-only drop-in full-filter evaluation, including snapshot costs.

    The whole HTTP timer includes owner-thread JSON snapshot construction, lazy
    pool construction/first submissions, evaluation and ordered merge. SQL and
    badge calculation remain in releases_view. Workers receive immutable DTOs.
    Exactly the same predeclared granularity is used on regular/t-on/t-off:
    up to four contiguous chunks, keeping each release's whole history together.
    """
    current = now if now is not None else _domain_pairing.utcnow()
    snapshot = snapshot_archives(archives, file_keys_by_archive_id)
    if not snapshot.releases:
        return []
    releases_per_task = max(1, (len(snapshot.releases) + _HTTP_WORKERS - 1) // _HTTP_WORKERS)
    return evaluate_snapshot(
        snapshot, now=current, sla_hours=sla_hours, require_active=require_active,
        include_ignored=include_ignored, workers=_HTTP_WORKERS,
        releases_per_task=releases_per_task, executor=_http_pool(),
    )
