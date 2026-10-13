"""Work list uses real SQL and bounded isolated disk fixtures, no external services."""
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import EncoderSubmission, EncoderSubmissionItem, Job, Release, TorrentArchive, TorrentFile
from app.services import hevc_workbench as workbench

NOW = datetime(2026, 10, 13, 12)


@compiles(JSONB, "sqlite")
def jsonb_sqlite(*args, **kwargs):
    return "JSON"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(workbench, "resolve_media_root", lambda: tmp_path.resolve())
    with Session(engine) as session:
        yield session
    engine.dispose()


def source(db, tmp_path, *, rid=42, codec="AVC", tid=None, info_hash=None, relative=None, age=30, rip="BDRip", files=True, **flags):
    info_hash = info_hash or uuid4().hex + "0" * 8
    tid = tid or 100 + rid
    release = db.get(Release, rid)
    if release is None:
        db.add(Release(release_id=rid, title=f"Релиз {rid}", release_alias=f"show-{rid}", source="site"))
    archive = TorrentArchive(info_hash=info_hash, torrent_id=tid, release_id=rid,
        anime_name=f"Релиз {rid}", release_alias=f"show-{rid}", torrent_type=f"{rip} 1080p {codec}",
        torrent_description="1-2", quality_json={"type":{"value":rip}, "quality":{"value":"1080p"}, "codec":{"value":codec}},
        file_path=f"{info_hash}.torrent", api_created_at=NOW - timedelta(hours=age),
        created_at=NOW - timedelta(hours=age), api_present=flags.pop("api_present", True),
        superseded=flags.pop("superseded", False), ignore_hevc=flags.pop("ignore_hevc", False))
    db.add(archive)
    file = None
    if files:
        relative = relative or f"Show-{info_hash}/ep-01.mkv"
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"complete isolated video")
        file = TorrentFile(release_id=rid, torrent_id=tid, info_hash=info_hash, relative_path=relative,
            full_path=str(path), size=path.stat().st_size, ui_status="new", selected=True,
            media_present=True, is_checking=False, updated_at=NOW)
        for key, value in flags.items():
            setattr(file, key, value)
        db.add(file)
    db.commit()
    return file, archive


def receipt(db, file, state, *, expired=False, started=True):
    group = EncoderSubmission(request_id=uuid4().hex, request_digest="a" * 64, base_url="http://encoder.invalid",
        video_id="hevc", audio_id="opus", expires_at=NOW + timedelta(hours=-1 if expired else 1),
        request_started_at=NOW - timedelta(hours=2) if started else None,
        finished_at=None if state == "sending" else NOW,
        items=[EncoderSubmissionItem(file_id=file.id, release_id=file.release_id, info_hash=file.info_hash,
                                    relative_path=file.relative_path, status=state)])
    db.add(group)
    db.commit()
    return group


def listing(db, **params):
    return workbench.list_hevc_workbench(db, now=NOW, **params)


def test_first_new_and_changed_remain_sendable_and_read_only(db, tmp_path):
    a, _ = source(db, tmp_path)
    b, _ = source(db, tmp_path, rid=43, ui_status="changed")
    original = [(file.id, file.ui_status, file.media_present) for file in (a, b)]
    context = listing(db, view="ready")
    assert context["total"] == context["available_count"] == 2
    assert all(row["selectable"] and row["hevc_status"] == "missing" for row in context["rows"])
    assert [(file.id, file.ui_status, file.media_present) for file in (a, b)] == original
    assert not db.dirty and not db.new


@pytest.mark.parametrize("state", ["accepted", "unknown", "sending"])
def test_receipts_block_normal_selection_and_preserve_labels(db, tmp_path, state):
    file, _ = source(db, tmp_path)
    group = receipt(db, file, state)
    context = listing(db)
    row = context["rows"][0]
    assert row["receipt"]["status"] == state and not row["selectable"]
    assert row["receipt"]["history_url"].endswith(str(group.id))
    assert context["counts"]["ready"] == 0


@pytest.mark.parametrize("state", ["not_sent", "rejected"])
def test_definite_failed_reception_can_be_selected(db, tmp_path, state):
    file, _ = source(db, tmp_path)
    receipt(db, file, state)
    assert listing(db, view="ready")["rows"][0]["selectable"]


def test_exact_hash_and_relative_path_never_basename_match(db, tmp_path):
    a, archive = source(db, tmp_path, relative="A/episode.mkv")
    b = TorrentFile(info_hash=a.info_hash, release_id=a.release_id, torrent_id=a.torrent_id,
        relative_path="B/episode.mkv", full_path=str(tmp_path / "B/episode.mkv"), media_present=True, ui_status="new")
    Path(b.full_path).parent.mkdir()
    Path(b.full_path).write_bytes(b"video")
    db.add(b)
    db.commit()
    c, _ = source(db, tmp_path, rid=43, relative="C/episode.mkv")
    receipt(db, a, "accepted")
    rows = {row["file_id"]:row for row in listing(db)["rows"]}
    assert rows[a.id]["receipt"]["status"] == "accepted"
    assert rows[b.id]["receipt"] is None and rows[c.id]["receipt"] is None
    assert rows[b.id]["selectable"] and rows[c.id]["selectable"]


def test_recreated_exact_source_row_keeps_receipt(db, tmp_path):
    file, _ = source(db, tmp_path)
    receipt(db, file, "accepted")
    old_id = file.id
    attributes = {key:getattr(file, key) for key in ("info_hash", "release_id", "torrent_id", "relative_path", "full_path", "ui_status", "media_present")}
    db.delete(file)
    db.commit()
    replacement = TorrentFile(id=old_id + 100, **attributes)
    db.add(replacement)
    db.commit()
    row = listing(db)["rows"][0]
    assert row["file_id"] == replacement.id and row["receipt"]["status"] == "accepted"
    assert not row["selectable"]


def test_latest_receipt_closes_old_unknown(db, tmp_path):
    file, _ = source(db, tmp_path)
    receipt(db, file, "unknown")
    accepted = receipt(db, file, "accepted")
    assert listing(db, view="unknown")["total"] == 0
    assert listing(db)["rows"][0]["receipt"]["submission_id"] == accepted.id


def test_unknown_survives_hevc_pair_becoming_current(db, tmp_path):
    file, _ = source(db, tmp_path, tid=100)
    receipt(db, file, "unknown")
    source(db, tmp_path, codec="HEVC", tid=101)
    context = listing(db, view="unknown")
    assert context["total"] == 1
    assert context["rows"][0]["hevc_status"] == "" and not context["rows"][0]["selectable"]


@pytest.mark.parametrize("started,expected", [(True, "unknown"), (False, "not_sent")])
def test_expired_receipt_is_derived_without_writes(db, tmp_path, started, expected):
    file, _ = source(db, tmp_path)
    group = receipt(db, file, "sending", expired=True, started=started)
    row = listing(db)["rows"][0]
    assert row["receipt"]["expired"] and row["receipt"]["status"] == expected
    assert group.items[0].status == "sending" and group.finished_at is None and not db.dirty


@pytest.mark.parametrize("flags,reason", [
    ({"selected":False}, "не выбран"), ({"is_checking":True}, "проверяется"),
    ({"media_present":False}, "полного файла"), ({"full_path":None}, "Путь исходника"),
    ({"ui_status":"removed"}, "Исторический статус"),
])
def test_database_blocking_reasons_are_separate_from_sticky(db, tmp_path, flags, reason):
    file, _ = source(db, tmp_path, **flags)
    row = listing(db, view="unavailable")["rows"][0]
    assert any(reason in item for item in row["reasons"])
    assert not row["selectable"] and row["sticky_status"] == file.ui_status


@pytest.mark.parametrize("status", ["pending", "running", "stopping"])
def test_active_hash_blocks_even_first_new_file(db, tmp_path, status):
    file, _ = source(db, tmp_path)
    db.add(Job(type="hash_torrent", status=status, params_json={"info_hash":file.info_hash}))
    db.commit()
    row = listing(db)["rows"][0]
    assert not row["selectable"] and file.ui_status == "new"
    assert "хеширование" in " ".join(row["reasons"])


def test_ready_separates_newly_missing_file_without_claiming_global_live_counts(db, tmp_path):
    file, _ = source(db, tmp_path)
    Path(file.full_path).unlink()
    context = listing(db, view="ready")
    assert context["counts"]["ready"] == context["total"] == 1
    assert context["rows"] == [] and len(context["changed_rows"]) == 1
    assert "отсутствует" in " ".join(context["changed_rows"][0]["reasons"])
    assert file.media_present is True


def test_partial_only_and_complete_plus_partial_follow_existing_guard(db, tmp_path):
    file, _ = source(db, tmp_path)
    path = Path(file.full_path)
    partial = Path(str(path) + ".!qB")
    partial.write_bytes(b"partial")
    assert listing(db)["rows"][0]["selectable"]
    path.unlink()
    row = listing(db)["rows"][0]
    assert not row["selectable"] and "неполный" in " ".join(row["reasons"])


def test_symlink_escape_is_not_sendable(db, tmp_path):
    file, _ = source(db, tmp_path)
    outside = tmp_path.parent / (uuid4().hex + ".mkv")
    outside.write_bytes(b"outside")
    try:
        Path(file.full_path).unlink()
        Path(file.full_path).symlink_to(outside)
        row = listing(db)["rows"][0]
        assert not row["selectable"] and "вне каталога" in " ".join(row["reasons"])
    finally:
        outside.unlink()


def test_probe_permission_error_fails_closed(db, tmp_path, monkeypatch):
    source(db, tmp_path)
    monkeypatch.setattr(workbench.os, "scandir", lambda _: (_ for _ in ()).throw(PermissionError()))
    assert "прочитать каталог" in " ".join(listing(db)["rows"][0]["reasons"])


def test_no_file_placeholder_and_inactive_ignored_are_not_candidates(db, tmp_path):
    source(db, tmp_path, files=False)
    source(db, tmp_path, rid=43, superseded=True)
    source(db, tmp_path, rid=44, api_present=False)
    source(db, tmp_path, rid=45, ignore_hevc=True)
    context = listing(db)
    assert context["total"] == 1 and context["rows"][0]["file_id"] is None
    assert not context["rows"][0]["selectable"]


def test_missing_never_becomes_overdue_and_type_mismatch_is_separate(db, tmp_path):
    source(db, tmp_path, age=100)
    source(db, tmp_path, rid=43, tid=200, rip="WEBRip", age=100)
    source(db, tmp_path, rid=43, codec="HEVC", tid=201, rip="WEB-DL", age=110)
    source(db, tmp_path, rid=44, tid=300, age=100)
    source(db, tmp_path, rid=44, codec="HEVC", tid=299, age=120)
    context = listing(db)
    assert {row["hevc_status"] for row in context["rows"]} == {"missing", "overdue", "type_mismatch"}
    overdue = listing(db, view="overdue")["rows"]
    assert len(overdue) == 1 and overdue[0]["release_id"] == 44


def test_pairing_once_and_probe_only_visible_page_and_bulk_queries(db, tmp_path, monkeypatch):
    for index in range(8):
        source(db, tmp_path, rid=42 + index)
    pairing = workbench.find_unpaired_avc
    calls = []
    monkeypatch.setattr(workbench, "find_unpaired_avc", lambda *args, **kwargs: (calls.append(1), pairing(*args, **kwargs))[1])
    probe = workbench._probe_visible_files
    checked = []
    monkeypatch.setattr(workbench, "_probe_visible_files", lambda rows, root: (checked.extend(row["file_id"] for row in rows), probe(rows, root))[1])
    statements = []
    def query(*args):
        statements.append(args[2])
    event.listen(db.get_bind(), "before_cursor_execute", query)
    try:
        context = listing(db, page=2, per_page=3)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", query)
    assert len(calls) == 1 and len(checked) == 3
    assert checked == [row["file_id"] for row in context["rows"]]
    assert len(statements) < 12 and all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert context["total"] == 8 and context["page"] == 2


def test_search_ids_paths_clamping_and_stable_paging(db, tmp_path):
    first, _ = source(db, tmp_path, relative="A/specific.mkv")
    source(db, tmp_path, rid=43)
    assert listing(db, search="specific")["rows"][0]["file_id"] == first.id
    assert listing(db, search="142")["total"] == 1
    assert listing(db, search="show-43")["rows"][0]["release_id"] == 43
    assert listing(db, search="' OR 1=1 --")["total"] == 0
    context = listing(db, page=999, per_page=1, view="invalid")
    assert context["view"] == "all" and context["page"] == 2 and context["rows"][0]["release_id"] == 43
    assert listing(db, per_page=10000)["per_page"] == 100


def test_backoffice_gets_admin_and_no_public_link(db, tmp_path):
    source(db, tmp_path)
    db.get(Release, 42).source = "backoffice"
    db.commit()
    row = listing(db)["rows"][0]
    assert row["release_url"] is None and row["source"] == "backoffice"
