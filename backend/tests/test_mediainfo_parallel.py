"""Real SQL durability/thread boundaries; native parse is isolated for regressions.

These tests measure no performance. Whole-operation timings use real MediaInfo.
"""

import asyncio
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import Base
from app.db.models import FileMediaInfo, JobLog, TorrentFile
from app.services import mediainfo as mi
from app.services.job_runner import JobStopRequested


@compiles(JSONB, "sqlite")
def jsonb_sqlite(*args, **kwargs):
    return "JSON"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "anilibria_media_root", str(tmp_path))
    engine = create_engine(f"sqlite:///{tmp_path / 'mediainfo.sqlite'}")
    Base.metadata.create_all(engine)
    caller = threading.get_ident()

    class CallerSession(Session):
        commits = 0

        def execute(self, *args, **kwargs):
            assert threading.get_ident() == caller
            return super().execute(*args, **kwargs)

        def add(self, *args, **kwargs):
            assert threading.get_ident() == caller
            return super().add(*args, **kwargs)

        def commit(self):
            assert threading.get_ident() == caller
            self.commits += 1
            return super().commit()

    with CallerSession(engine, autoflush=False) as session:
        yield session
    engine.dispose()


def files(tmp_path, count):
    paths = [tmp_path / f"episode-{i:03d}.mkv" for i in range(count)]
    for path in paths:
        path.write_bytes(b"regression fixture")
    return paths


def parsed(path):
    return ({"format": "Matroska", "fonts": [{"name": "x.ttf", "mime": "font/ttf", "size_bytes": 42}]},
            {"tracks": [{"track_type": "General", "format": "Matroska"}]}, str(path))


def persisted_count(db):
    # A fresh session verifies committed progress, rather than identity-map state.
    with Session(db.get_bind()) as independent:
        return independent.scalar(select(func.count()).select_from(FileMediaInfo))


def test_batch25_uses_one_gate_query_and_commits_last_partial(db, tmp_path, monkeypatch):
    paths = files(tmp_path, 52)
    queries = []
    event.listen(db.get_bind(), "before_cursor_execute", lambda c, cur, sql, p, ctx, many: queries.append(sql))
    monkeypatch.setattr(mi, "parse_media_file", parsed)
    stats = mi.mediainfo_paths_parallel(db, paths, workers=1)
    assert stats == dict(total=52, scanned=52, updated=52, skipped=0, missing=0, errors=0, stopped=0)
    assert db.commits == 3
    assert len([sql for sql in queries if sql.startswith("SELECT") and "file_mediainfo" in sql]) == 1
    assert persisted_count(db) == 52
    row = db.scalar(select(FileMediaInfo).limit(1))
    assert row.attachments_json == [{"name": "x.ttf", "mime": "font/ttf", "size_bytes": 42}]
    assert row.attachments_count == 1 and row.attachments_total_bytes == 42


def test_gate_skips_without_parse_and_changed_file_updates_only_it(db, tmp_path, monkeypatch):
    paths = files(tmp_path, 3)
    calls = []
    monkeypatch.setattr(mi, "parse_media_file", lambda p: (calls.append(p), parsed(p))[1])
    mi.mediainfo_paths_parallel(db, paths, workers=1)
    calls.clear()
    commits = db.commits
    stats = mi.mediainfo_paths_parallel(db, paths, workers=1)
    assert stats["skipped"] == 3 and stats["updated"] == 0 and calls == []
    assert db.commits == commits
    paths[1].write_bytes(b"a new and longer file revision")
    stats = mi.mediainfo_paths_parallel(db, paths, workers=1)
    assert stats["skipped"] == 2 and stats["updated"] == 1 and calls == [paths[1]]
    with Session(db.get_bind()) as check:
        row = check.scalar(select(FileMediaInfo).where(FileMediaInfo.full_path == str(paths[1])))
        assert row.file_size == paths[1].stat().st_size
    assert persisted_count(db) == 3


def test_other_session_insert_after_gate_does_not_lose24_prior_reports(db, tmp_path, monkeypatch):
    paths = files(tmp_path, 25)

    def parse(path):
        if path == paths[0]:
            # Gate has already observed no rows. Insert the final path before
            # this caller starts writes; the conflict occurs after24 successes.
            with Session(db.get_bind()) as other:
                other.add(FileMediaInfo(full_path=str(paths[-1]), file_size=0, mtime=0,
                                        summary_json={"other": "stale"}))
                other.commit()
        return parsed(path)

    monkeypatch.setattr(mi, "parse_media_file", parse)
    stats = mi.mediainfo_paths_parallel(db, paths, workers=1)
    assert stats["updated"] == 25 and stats["errors"] == 0
    assert db.commits == 1 and persisted_count(db) == 25
    with Session(db.get_bind()) as check:
        rows = list(check.scalars(select(FileMediaInfo)))
        assert all(row.summary_json["format"] == "Matroska" for row in rows)
        last = next(row for row in rows if row.full_path == str(paths[-1]))
        assert last.file_size == paths[-1].stat().st_size and last.raw_text == str(paths[-1])


def test_force_reparses_unchanged_cache(db, tmp_path, monkeypatch):
    paths = files(tmp_path, 1)
    monkeypatch.setattr(mi, "parse_media_file", parsed)
    mi.mediainfo_paths_parallel(db, paths, workers=1)
    stats = mi.mediainfo_paths_parallel(db, paths, workers=1, force=True)
    assert stats["updated"] == 1 and stats["skipped"] == 0
    assert persisted_count(db) == 1


def test_sequential_stop_preserves_less_than25_completed_reports(db, tmp_path, monkeypatch):
    paths = files(tmp_path, 8)
    completed = []

    def parse(path):
        completed.append(path)
        return parsed(path)

    monkeypatch.setattr(mi, "parse_media_file", parse)
    stats = mi.mediainfo_paths_parallel(db, paths, workers=1, should_stop=lambda: len(completed) >= 3)
    assert stats["stopped"] == 1 and stats["updated"] == 3
    assert len(completed) == 3 and persisted_count(db) == 3
    assert db.commits == 1


def test_parallel_stop_drains_only_bounded_inflight_and_session_stays_on_caller(db, tmp_path, monkeypatch):
    paths = files(tmp_path, 7)
    stop = threading.Event()
    barrier = threading.Barrier(2)
    seen = []
    caller = threading.get_ident()

    def parse(path):
        assert threading.get_ident() != caller
        seen.append(path)
        barrier.wait(timeout=5)
        stop.set()
        return parsed(path)

    logs = []

    def log(message):
        assert threading.get_ident() == caller
        logs.append(message)

    monkeypatch.setattr(mi, "parse_media_file", parse)
    stats = mi.mediainfo_paths_parallel(db, paths, workers=2, should_stop=stop.is_set, log_fn=log)
    assert stats["stopped"] == 1 and stats["updated"] == 2
    assert len(seen) == len(logs) == 2 and persisted_count(db) == 2


def test_reversed_native_completion_uses_sorted_upserts_in_transaction(db, tmp_path, monkeypatch):
    paths = files(tmp_path, 3)
    release_first = threading.Event()
    writes = []
    commits = []

    def capture(connection, cursor, sql, parameters, context, many):
        if sql.startswith("INSERT INTO file_mediainfo"):
            writes.append(context.compiled_parameters[0]["full_path"])

    event.listen(db.get_bind(), "before_cursor_execute", capture)
    event.listen(db, "after_commit", lambda session: commits.append(list(writes)))

    def parse(path):
        if path == paths[0]:
            assert release_first.wait(timeout=5)
        if path == paths[2]:
            return None
        return parsed(path)

    def started(message):
        # The third parse is scheduled only after the second native result
        # reached caller; force B before A without sleeps/timing assumptions.
        if paths[2].name in message:
            release_first.set()

    monkeypatch.setattr(mi, "parse_media_file", parse)
    stats = mi.mediainfo_paths_parallel(db, paths, workers=2, log_fn=started)
    assert stats["updated"] == 2 and stats["errors"] == 1
    assert writes == [str(paths[0]), str(paths[1])]
    assert commits == [writes] and persisted_count(db) == 2


def test_parse_exception_and_none_keep_other_committed_results(db, tmp_path, monkeypatch):
    paths = files(tmp_path, 4)

    def parse(path):
        if path == paths[1]:
            raise OSError("isolated file error")
        if path == paths[2]:
            return None
        return parsed(path)

    monkeypatch.setattr(mi, "parse_media_file", parse)
    stats = mi.mediainfo_paths_parallel(db, paths, workers=2)
    assert stats["errors"] == 2 and stats["updated"] == 2
    assert persisted_count(db) == 2


def test_replacement_during_parse_keeps_previous_cache(db, tmp_path, monkeypatch):
    path = files(tmp_path, 1)[0]
    monkeypatch.setattr(mi, "parse_media_file", parsed)
    mi.mediainfo_paths_parallel(db, [path], workers=1)

    def changed(path):
        path.write_bytes(b"replaced media content with different size")
        return ({"invalid": "stale parsed report"}, {}, "stale")

    monkeypatch.setattr(mi, "parse_media_file", changed)
    stats = mi.mediainfo_paths_parallel(db, [path], workers=1, force=True)
    assert stats["errors"] == 1 and stats["updated"] == 0
    with Session(db.get_bind()) as check:
        row = check.scalar(select(FileMediaInfo))
        assert row.summary_json["format"] == "Matroska" and row.raw_text != "stale"


def test_atomic_replace_with_identical_size_mtime_is_not_saved(db, tmp_path, monkeypatch):
    path = files(tmp_path, 1)[0]

    def replace(path):
        original = path.stat()
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(b"x" * original.st_size)
        os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(replacement, path)
        return parsed(path)

    monkeypatch.setattr(mi, "parse_media_file", replace)
    stats = mi.mediainfo_paths_parallel(db, [path], workers=1)
    assert stats["errors"] == 1 and stats["updated"] == 0
    assert persisted_count(db) == 0


def test_single_parse_preserves_identical_native_options_without_can_parse(db, tmp_path, monkeypatch):
    import sys

    path = files(tmp_path, 1)[0].with_suffix(".mp4")
    path.write_bytes(b"regression")
    calls = []

    class Native:
        @staticmethod
        def parse(filename, **kwargs):
            calls.append((filename, kwargs))
            return SimpleNamespace(to_data=lambda: {"tracks": []})

        @staticmethod
        def can_parse():
            pytest.fail("parse already loads the library; duplicate can_parse must not run")

    monkeypatch.setitem(sys.modules, "pymediainfo", SimpleNamespace(MediaInfo=Native))
    assert mi.parse_media_file(path) is not None
    assert calls == [(str(path), {"full": True})]


def test_partial_paths_deduplicate_complete_neighbor_and_reject_outside_symlink(db, tmp_path, monkeypatch):
    path = files(tmp_path, 1)[0]
    incomplete = tmp_path / "incomplete.mkv.!qB"
    incomplete.write_bytes(b"partial")
    neighbor = Path(str(path) + ".!qB")
    neighbor.write_bytes(b"leftover")
    outside = tmp_path.parent / (tmp_path.name + "-outside.mkv")
    outside.write_bytes(b"outside")
    alias = tmp_path / "outside.mkv"
    alias.symlink_to(outside)
    calls = []
    monkeypatch.setattr(mi, "parse_media_file", lambda p: (calls.append(p), parsed(p))[1])
    try:
        stats = mi.mediainfo_paths_parallel(db, [path, neighbor, incomplete, alias], workers=1)
        assert stats["updated"] == 1 and stats["errors"] == 1
        assert calls == [path] and persisted_count(db) == 1
    finally:
        outside.unlink()


def test_stop_during_gate_starts_no_native_parse(db, tmp_path, monkeypatch):
    paths = files(tmp_path, 30)
    calls = 0

    def stop():
        nonlocal calls
        calls += 1
        return calls == 3

    monkeypatch.setattr(mi, "parse_media_file", lambda p: pytest.fail("parse after stop"))
    stats = mi.mediainfo_paths_parallel(db, paths, should_stop=stop)
    assert stats["stopped"] == 1 and persisted_count(db) == 0


def test_sync_debug_logs_do_not_turn_batch_into_per_file_commits(db, tmp_path, monkeypatch):
    from app.jobs import mediainfo_sync as job

    paths = files(tmp_path, 26)
    for index, path in enumerate(paths):
        db.add(TorrentFile(info_hash="a" * 40, torrent_id=1, release_id=2,
                           relative_path=path.name, full_path=str(path), ui_status="new"))
    db.commit()
    db.commits = 0
    monkeypatch.setattr(mi, "parse_media_file", parsed)
    monkeypatch.setattr(job, "is_stop_requested", lambda *_: False)
    asyncio.run(job.run_mediainfo_sync(db, 10, {"mode": "full"}))
    assert db.commits == 4  # start + batch25 + final partial1 + completion log
    assert persisted_count(db) == 26
    assert db.scalar(select(func.count()).select_from(JobLog).where(JobLog.level == "debug")) == 26
    assert set(db.scalars(select(TorrentFile.ui_status))) == {"new"}


def test_hash_followup_only_added_modified_and_stop_prevents_done_notification(db, tmp_path, monkeypatch):
    from app.jobs import hash_torrent as job

    paths = files(tmp_path, 3)
    tracker = SimpleNamespace(track_torrent=lambda **kwargs: SimpleNamespace(
        skipped_reason=None, changes=[SimpleNamespace(kind=kind, full_path=str(path))
                                       for kind, path in zip(("added", "modified", "removed"), paths)],
        files_upserted=3, hashed=3, gated=0, errors=0))
    monkeypatch.setattr(job, "FileTrackerService", lambda *_a, **_kw: tracker)
    monkeypatch.setattr(job, "_pipeline_for_hash", lambda *_: None)
    monkeypatch.setattr(job, "is_stop_requested", lambda *_: False)
    notifications = []
    monkeypatch.setattr(job, "enqueue_new_release_notification", lambda *_a, **_kw: notifications.append(True))
    monkeypatch.setattr(mi, "parse_media_file", parsed)
    asyncio.run(job.run_hash_torrent(db, 10, {"info_hash": "a" * 40, "torrent_id": 1, "release_id": 2}))
    assert persisted_count(db) == 2 and notifications == [True]
    monkeypatch.setattr(mi, "mediainfo_paths_parallel", lambda *_a, **_kw: dict(stopped=1))
    with pytest.raises(JobStopRequested):
        asyncio.run(job.run_hash_torrent(db, 10, {"info_hash": "a" * 40, "torrent_id": 1, "release_id": 2}))
    assert notifications == [True]


def test_postgres_overlapping_native_batches_atomic_and_deadlock_free(tmp_path, monkeypatch):
    """Two real PG sessions, only in a unique disposable test schema."""
    from concurrent.futures import ThreadPoolExecutor

    url = os.environ.get("ALTT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("ALTT_TEST_POSTGRES_URL отдельной тестовой БД не задан")
    if not url.startswith(("postgresql://", "postgresql+psycopg://")):
        pytest.fail("An isolated PostgreSQL test DB is required")
    schema = "altt_mediainfo_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=10000"})
    monkeypatch.setattr(settings, "anilibria_media_root", str(tmp_path))
    paths = files(tmp_path, 2)
    native_barrier = threading.Barrier(4)

    def parse(path):
        native_barrier.wait(timeout=10)
        return parsed(path)

    monkeypatch.setattr(mi, "parse_media_file", parse)

    def operation(reverse):
        with Session(engine, autoflush=False) as session:
            return mi.mediainfo_paths_parallel(session, list(reversed(paths)) if reverse else paths, workers=2)

    try:
        FileMediaInfo.__table__.create(engine)
        with ThreadPoolExecutor(max_workers=2) as operations:
            results = list(operations.map(operation, (False, True)))
        assert all(result["updated"] == 2 and result["errors"] == 0 for result in results)
        with Session(engine) as session:
            rows = list(session.scalars(select(FileMediaInfo)))
            assert len(rows) == 2 and all(row.summary_json["format"] == "Matroska" for row in rows)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
