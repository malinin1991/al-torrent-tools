"""Differential guard for workbench preparation changes, using real isolated SQL/files.

The captured service is an independent pre-optimization oracle. Entire contexts
are compared without sorting rows, dropping keys, or normalizing status/history.
Both services use the same fixed wall clock, so even checked_at must match.
"""
from datetime import timedelta
import hashlib
from pathlib import Path

import pytest
from sqlalchemy import create_mock_engine, event, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.db.models import (
    EncoderSubmission, EncoderSubmissionItem, Job, Release, Setting,
    TorrentArchive, TorrentFile, TorrentPipeline,
)
from app.services import hevc_workbench as candidate
from experiments.hevc_workbench_optimization import baseline_service as baseline
from test_hevc_workbench import NOW, db, receipt, source  # noqa: F401 (shared pytest fixture)


BASELINE_SHA256 = "0ed990e00bb6e4e1c2bff88d48b1490a09e926d66f3f147b2929e95a289fb8db"
ENTITIES = (Release, TorrentArchive, TorrentFile, Job, TorrentPipeline, EncoderSubmission, EncoderSubmissionItem, Setting)


def _state(db):
    return {
        model.__tablename__: [tuple(row) for row in db.execute(
            select(model.__table__).order_by(*model.__table__.primary_key.columns)
        )]
        for model in ENTITIES
    }


def _schema(engine):
    inspector = inspect(engine)
    return {
        name: [(column["name"], str(column["type"]), column["nullable"], column["default"])
               for column in inspector.get_columns(name)]
        for name in inspector.get_table_names()
    }


@pytest.fixture
def oracle_db(db, tmp_path, monkeypatch):
    for module in (baseline, candidate):
        monkeypatch.setattr(module, "resolve_media_root", lambda: tmp_path.resolve())
        monkeypatch.setattr(module, "utcnow", lambda: NOW)
    before = _schema(db.get_bind())
    yield db
    assert _schema(db.get_bind()) == before


def equivalent(db, **params):
    """Compare every field and prove neither implementation writes while reading."""
    before = _state(db)
    assert not db.new and not db.dirty and not db.deleted
    statements = []

    def audit(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)
        assert statement.lstrip().upper().startswith(("SELECT", "WITH")), statement

    def no_flush(*_args):
        raise AssertionError("Workbench preparation tried to flush business state")

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", audit)
    event.listen(db, "before_flush", no_flush)
    try:
        expected = baseline.list_hevc_workbench(db, now=NOW, **params)
        assert not db.new and not db.dirty and not db.deleted
        actual = candidate.list_hevc_workbench(db, now=NOW, **params)
        assert not db.new and not db.dirty and not db.deleted
    finally:
        event.remove(engine, "before_cursor_execute", audit)
        event.remove(db, "before_flush", no_flush)
    assert statements
    assert _state(db) == before
    assert actual == expected
    assert list(actual["views"].items()) == list(expected["views"].items())
    assert list(actual["view_urls"].items()) == list(expected["view_urls"].items())
    return actual


@pytest.fixture
def diverse(oracle_db, tmp_path):
    """Overlapping histories, all statuses, exact identities, and disk boundaries."""
    db = oracle_db
    items = {}

    def add(rid, **kwargs):
        file, archive = source(db, tmp_path, rid=rid, info_hash=f"{rid:040x}", **kwargs)
        items[rid] = (file, archive)
        return file, archive

    add(10, relative="shared/episode.mkv")
    add(11, ui_status="changed", relative="other/episode.mkv")
    db.get(Release, 11).source = "backoffice"
    db.get(Release, 11).title = "Ёжик < & offsite"
    add(12, tid=300, age=25)
    source(db, tmp_path, rid=12, codec="HEVC", tid=299, age=60)
    source(db, tmp_path, rid=12, tid=298, age=120, superseded=True, api_present=False, files=False)
    add(13, tid=400, rip="WEBRip", age=100)
    source(db, tmp_path, rid=13, codec="HEVC", tid=401, rip="WEB-DL", age=110)
    paired, _ = add(14, tid=500)
    receipt(db, paired, "unknown")
    source(db, tmp_path, rid=14, codec="HEVC", tid=501)
    add(15, files=False)
    add(16, ignore_hevc=True)
    add(17, api_present=False)
    add(18, superseded=True)
    hashing, _ = add(19)
    db.add(Job(type="hash_torrent", status="running", params_json={"info_hash": f" {hashing.info_hash.upper()} "}))
    db.add(Job(type="hash_torrent", status="done", params_json={"info_hash": items[10][0].info_hash}))
    db.add(Job(type="other", status="running", params_json={"info_hash": items[11][0].info_hash}))
    add(20, selected=False)
    disappeared, _ = add(21)
    Path(disappeared.full_path).unlink()
    add(22, full_path=str(tmp_path / "unfinished.mkv.!qB"))
    partial, _ = add(23)
    Path(partial.full_path).unlink()
    Path(partial.full_path + ".!qB").write_bytes(b"partial")
    add(24, full_path=None)
    add(25, ui_status="removed", media_present=False, is_checking=True)
    add(26, ui_status=" NEW ")
    for rid, status in ((27, "accepted"), (28, "rejected"), (29, "not_sent"), (30, "sending")):
        file, _ = add(rid)
        receipt(db, file, status)
    for rid, started in ((31, True), (32, False)):
        file, _ = add(rid)
        receipt(db, file, "sending", expired=True, started=started)
    latest, _ = add(33)
    receipt(db, latest, "unknown")
    receipt(db, latest, "accepted")

    # One hash can have several archive rows; latest active archive wins while
    # its exact video paths remain separate, even when basenames coincide.
    file, archive = add(34, tid=700, relative="duplicate-a/episode.mkv")
    source(db, tmp_path, rid=34, tid=701, files=False, info_hash=archive.info_hash)
    second_path = tmp_path / "duplicate-b/episode.mkv"
    second_path.parent.mkdir()
    second_path.write_bytes(b"other complete video")
    second = TorrentFile(release_id=34, torrent_id=700, info_hash=archive.info_hash,
        relative_path="duplicate-b/episode.mkv", full_path=str(second_path), size=second_path.stat().st_size,
        ui_status="ok", media_present=True, selected=True, updated_at=NOW)
    junk = TorrentFile(release_id=34, torrent_id=700, info_hash=archive.info_hash,
        relative_path="readme.nfo", full_path="", size=0, ui_status="new", media_present=False)
    db.add_all([second, junk])
    db.commit()
    receipt(db, file, "unknown")
    db.add_all([
        TorrentPipeline(info_hash=archive.info_hash, release_id=34, torrent_id=701, status="done"),
        TorrentPipeline(info_hash=archive.info_hash, release_id=34, torrent_id=701, status="failed", error="newer failure"),
        Setting(key="anilibria_admin_url_template", value="https://admin.invalid/releases/{release_id}"),
    ])
    db.commit()
    return items


def test_oracle_is_the_exact_preoptimization_service():
    assert hashlib.sha256(Path(baseline.__file__).read_bytes()).hexdigest() == BASELINE_SHA256


def test_empty_database_context_matches(oracle_db):
    for view in baseline.VIEWS:
        context = equivalent(oracle_db, view=view, page=999, per_page=1)
        assert context["total"] == 0 and context["page"] == context["total_pages"] == 1


@pytest.mark.parametrize("view", ["all", "ready", "overdue", "unavailable", "unknown"])
@pytest.mark.parametrize("params", [
    {}, {"page": 2, "per_page": 3}, {"page": 999, "per_page": 1},
    {"search": " EPISODE.MKV ", "per_page": 2},
])
def test_entire_mixed_context_matches(oracle_db, diverse, view, params):
    context = equivalent(oracle_db, view=view, **params)
    assert context["checked_at"] == baseline.as_utc_iso(NOW)


@pytest.mark.parametrize("params", [
    {"view": "invalid", "page": -10, "per_page": 0},
    {"search": " ЁЖИК ", "per_page": 10000},
    {"search": "show-34"}, {"search": "701"},
    {"search": "' OR 1=1 --"}, {"search": "x" * 201},
])
def test_search_normalization_and_clamps_match(oracle_db, diverse, params):
    equivalent(oracle_db, **params)


def test_repeated_calls_follow_database_and_disk_changes_without_cache(oracle_db, tmp_path):
    db = oracle_db
    file, archive = source(db, tmp_path, rid=50, info_hash="f" * 40)

    def check():
        return {view: equivalent(db, view=view) for view in baseline.VIEWS}

    assert check()["ready"]["total"] == 1
    receipt(db, file, "accepted")
    assert check()["ready"]["total"] == 0
    receipt(db, file, "rejected")
    assert check()["ready"]["total"] == 1
    db.get(Release, 50).source = "backoffice"
    db.get(Release, 50).title = "Renamed offsite"
    db.add(Setting(key="anilibria_admin_url_template", value="https://other.invalid/{release_id}"))
    db.commit()
    row = check()["all"]["rows"][0]
    assert row["title"] == "Renamed offsite" and row["release_url"] is None
    Path(file.full_path).unlink()
    changed = check()["ready"]
    assert changed["rows"] == [] and changed["changed_rows"][0]["file_id"] == file.id
    Path(file.full_path).write_bytes(b"restored")
    assert check()["ready"]["rows"][0]["selectable"]
    file.selected = False
    db.commit()
    assert check()["unavailable"]["total"] == 1
    file.selected = True
    archive.ignore_hevc = True
    db.commit()
    assert check()["all"]["total"] == 0
    archive.ignore_hevc = False
    archive.api_present = False
    db.commit()
    assert check()["all"]["total"] == 0
    archive.api_present = True
    db.commit()
    assert check()["ready"]["total"] == 1


def test_hash_case_and_exact_paths_do_not_gain_new_normalization(oracle_db, tmp_path):
    file, _ = source(oracle_db, tmp_path, rid=55, info_hash="A" * 40)
    file.info_hash = "a" * 40
    oracle_db.commit()
    row = equivalent(oracle_db)["rows"][0]
    assert row["file_id"] is None and row["info_hash"] == "A" * 40


def test_bulk_context_crosses_hash_and_release_query_chunk_boundaries(oracle_db, tmp_path):
    """Exercise the 500-item boundary without doing 501 filesystem probes."""
    db = oracle_db
    for index in range(501):
        rid = 1000 + index
        info_hash = f"{rid:040x}"
        db.add_all([
            Release(release_id=rid, title=f"Bulk {rid}", release_alias=f"bulk-{rid}", source="site"),
            TorrentArchive(info_hash=info_hash, torrent_id=rid, release_id=rid,
                anime_name=f"Bulk {rid}", release_alias=f"bulk-{rid}", torrent_type="BDRip 1080p AVC",
                torrent_description="1", quality_json={"type": {"value": "BDRip"}, "quality": {"value": "1080p"}, "codec": {"value": "AVC"}},
                file_path=f"{info_hash}.torrent", api_created_at=NOW - timedelta(hours=30),
                created_at=NOW - timedelta(hours=30), api_present=True, superseded=False),
            TorrentFile(release_id=rid, torrent_id=rid, info_hash=info_hash,
                relative_path=f"bulk-{rid}/episode.mkv", full_path=str(tmp_path / f"bulk-{rid}/episode.mkv"),
                selected=True, ui_status="new", media_present=True, updated_at=NOW),
        ])
    db.commit()
    context = equivalent(db, view="ready", page=501, per_page=1)
    assert context["total"] == context["counts"]["ready"] == 501
    assert context["checked_count"] == 1 and len(context["changed_rows"]) == 1
    assert context["changed_rows"][0]["release_id"] == 1500


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_sla_and_receipt_expiry_boundaries(oracle_db, tmp_path, offset):
    db = oracle_db
    file, archive = source(db, tmp_path, rid=60, tid=600, age=24, relative="avc/ep-12.mkv")
    archive.api_created_at = NOW - timedelta(hours=24, seconds=offset)
    _, pair = source(db, tmp_path, rid=60, codec="HEVC", tid=601, age=1, relative="hevc/ep-11.mkv")
    archive.torrent_description = "1-12"
    pair.torrent_description = "1-11"
    pending = receipt(db, file, "sending", started=True)
    pending.expires_at = NOW - timedelta(seconds=offset)
    db.commit()
    contexts = {view: equivalent(db, view=view) for view in baseline.VIEWS}
    assert contexts["unknown"]["total"] == (1 if offset > 0 else 0)
    assert contexts["overdue"]["total"] == (1 if offset > 0 else 0)


def test_file_projection_keeps_one_snapshot_of_all_composition_paths(oracle_db, tmp_path):
    """Historical/HEVC paths survive without loading their readiness metadata."""
    db = oracle_db
    active, _ = source(db, tmp_path, rid=70, info_hash="a" * 40)
    ignored, _ = source(db, tmp_path, rid=71, info_hash="b" * 40, ignore_hevc=True)
    receipt(db, ignored, "unknown")
    source(db, tmp_path, rid=71, tid=900, codec="HEVC", info_hash="c" * 40)
    source(db, tmp_path, rid=72, info_hash="d" * 40, superseded=True)
    source(db, tmp_path, rid=73, info_hash="e" * 40, api_present=False)
    hashes = [active.info_hash, ignored.info_hash]
    original = list(db.execute(select(TorrentFile.__table__)).mappings())
    statements = []

    def audit(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", audit)
    try:
        projected = list(db.execute(select(*candidate._file_projection(db, hashes))).mappings())
    finally:
        event.remove(engine, "before_cursor_execute", audit)
    assert len(statements) == 1 and statements[0].lstrip().upper().startswith("SELECT")
    assert "torrent_files" in statements[0].lower()
    originals = {(row["info_hash"], row["relative_path"]): row for row in original}
    assert {(row["info_hash"], row["relative_path"]) for row in projected} == set(originals)
    assert len(projected) == len(originals) == 5
    for row in projected:
        expected = originals[(row["info_hash"], row["relative_path"])]
        metadata = set(row) - {"info_hash", "relative_path"}
        if row["info_hash"] in hashes:
            assert {key: row[key] for key in metadata} == {key: expected[key] for key in metadata}
        else:
            assert all(row[key] is None for key in metadata)


@pytest.mark.parametrize("ignored", [False, True])
def test_unknown_receipt_keeps_full_source_after_pair_is_closed(oracle_db, tmp_path, ignored):
    """Unknown receipt remains visible even when pairing produces no need."""
    db = oracle_db
    file, _ = source(db, tmp_path, rid=75, tid=750, ignore_hevc=ignored,
                     ui_status="changed", size=4096)
    receipt(db, file, "unknown")
    source(db, tmp_path, rid=75, tid=751, codec="HEVC")
    context = equivalent(db, view="unknown")
    assert context["total"] == context["counts"]["unknown"] == 1
    row = context["rows"][0]
    assert row["hevc_status"] == "" and row["receipt"]["status"] == "unknown"
    assert row["file_id"] == file.id and row["full_path"] == file.full_path
    assert row["relative_path"] == file.relative_path and row["size_label"] == baseline.format_bytes(4096)
    assert row["sticky_status"] == "changed" and row["snapshot_at"] == baseline.as_utc_iso(NOW)
    assert row["db_ready"] and row["fs_available"] and not row["selectable"]


@pytest.mark.parametrize("same_files", [False, True])
def test_inactive_composition_preserves_label_rename_sla(oracle_db, tmp_path, same_files):
    """Inactive AVC paths distinguish an old debt from a newly added episode."""
    db = oracle_db
    _, historical = source(db, tmp_path, rid=80, tid=800, age=50,
        relative="old-batch/ep-09.mkv", api_present=False, superseded=True, ignore_hevc=True)
    _, hevc = source(db, tmp_path, rid=80, tid=799, age=51, codec="HEVC",
        relative="hevc/ep-08.mkv")
    current_file, current = source(db, tmp_path, rid=80, tid=800, age=1,
        relative="new-batch/ep-09.mkv" if same_files else "new-batch/ep-10.mkv")
    historical.torrent_description = hevc.torrent_description = "1-8"
    current.torrent_description = "1-9"
    db.commit()
    context = equivalent(db, view="overdue")
    if same_files:
        assert context["total"] == 1
        row = context["rows"][0]
        assert row["file_id"] == current_file.id and row["hevc_status"] == "overdue"
        assert row["age_hours"] == 50.0 and row["overdue_hours"] == 26.0
        assert row["anchor_at"] == baseline.as_utc_iso(NOW - timedelta(hours=50))
    else:
        assert context["total"] == 0


def test_postgresql_projection_binds_one_array_for_any_catalog_size():
    """Large catalogs and hostile raw hashes never expand/interpolate into SQL."""
    def no_execute(*_args, **_kwargs):
        raise AssertionError("Compilation test must not contact a database")

    engine = create_mock_engine("postgresql://", no_execute)
    malicious = "x') OR TRUE; --"
    sql_shapes = []
    with Session(bind=engine) as db:
        for hashes in ([], [malicious], [f"{index:040x}" for index in range(10000)] + [malicious]):
            compiled = select(*candidate._file_projection(db, hashes)).compile(dialect=postgresql.dialect())
            sql = str(compiled)
            assert "ANY (" in sql and "VARCHAR[]" in sql
            assert malicious not in sql and not compiled.post_compile_params
            assert compiled.params == {"workbench_active_hashes": hashes}
            binding = compiled.binds["workbench_active_hashes"]
            assert isinstance(binding.type, postgresql.ARRAY) and not binding.expanding
            sql_shapes.append(sql)
    assert sql_shapes[0] == sql_shapes[1] == sql_shapes[2]
