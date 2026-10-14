"""Durable encoder receipts: real SQL transactions, no external service calls."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape
import pytest
from sqlalchemy import create_engine, delete, event, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.api import rest
from app.db.base import Base
from app.db.models import DiskFileHash, EncoderSubmission, EncoderSubmissionItem, Release, TorrentArchive, TorrentFile
from app.services import encoder_history as history
from app.services.db_maintenance import reset_full, reset_operational_state
from app.services.video_kensetsu import VideoKensetsuHttpError
from app.utils.datetime_fmt import utcnow


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(*args, **kwargs):
    return "JSON"


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'encoder.sqlite'}")

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def source(db, tmp_path, *, release_id=42, info_hash=None, relative_path="Show/episode-01.mkv", title="Тестовый релиз"):
    info_hash = info_hash or uuid4().hex + "0" * 8
    if db.get(Release, release_id) is None:
        db.add(Release(release_id=release_id, title=title))
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"complete video fixture")
    archive = TorrentArchive(info_hash=info_hash, torrent_id=100 + release_id, release_id=release_id,
                             anime_name=title, file_path=f"{info_hash}.torrent")
    file = TorrentFile(info_hash=info_hash, torrent_id=archive.torrent_id, release_id=release_id,
                       relative_path=relative_path, full_path=str(path), size=path.stat().st_size,
                       ui_status="new", media_present=True)
    db.add_all([archive, file])
    db.commit()
    return file, archive


def claim(db, files, *, request_id=None, video="hevc", audio="opus", downmix="none", **options):
    ids = [f.id for f in files]
    return history.claim(
        db, file_ids=ids, request_id=request_id or uuid4().hex,
        digest=history.request_digest(ids, video, audio, downmix, options.get("retry_of")),
        base_url="http://encoder.test", video_id=video, audio_id=audio, downmix=downmix, **options,
    )


def accepted(db, file, **options):
    group, owner = claim(db, [file], **options)
    assert owner
    history.capture_path(db, group.items[0], file.full_path)
    group.request_started_at = utcnow()
    history.settle_response(group, 200, {"jobs": [{"path": file.full_path, "id": "job-confirmed"}]})
    db.commit()
    return group


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(rest, "_require_video_kensetsu_base_url", lambda db: "http://encoder.test")

    async def presets(base_url):
        return {
            "videos": [{"id": "hevc", "name": "HEVC snapshot", "crf": 23}],
            "audios": [{"id": "opus", "name": "Opus snapshot", "available": True}],
        }

    def path(db, file_id):
        row = db.get(TorrentFile, file_id)
        return (row.full_path, None) if row else (None, "Файл не найден")

    monkeypatch.setattr(rest, "video_kensetsu_list_presets", presets)
    monkeypatch.setattr(rest, "_resolve_encode_path_for_file", path)
    return rest


def submit(api, db, files, *, request_id=None, **options):
    body = api.SendToEncoderIn(video_id="hevc", audio_id="opus", request_id=request_id or uuid4().hex, **options)
    return asyncio.run(api._submit_encoder_files([f.id for f in files], body, db))


def payload(response):
    return json.loads(response.body)


@pytest.mark.parametrize("state", ["pending", "running", "stopping"])
def test_active_hash_job_prevents_encoder_post(api, db, tmp_path, monkeypatch, state):
    from app.db.models import Job
    file, _ = source(db, tmp_path)
    db.add(Job(type="hash_torrent", status=state, params_json={"info_hash": file.info_hash}))
    db.commit()
    called = []
    async def encode(*args, **kwargs):
        called.append(True)
        raise AssertionError("Checking source must not be sent")
    monkeypatch.setattr(api, "video_kensetsu_encode", encode)
    with pytest.raises(HTTPException):
        submit(api, db, [file])
    assert not called
    assert db.scalar(select(EncoderSubmissionItem.status)) == "not_sent"


def test_hash_job_started_during_presets_prevents_encoder_post(api, db, tmp_path, monkeypatch):
    from app.db.models import Job
    file, _ = source(db, tmp_path)
    original_presets = api.video_kensetsu_list_presets
    async def presets(base_url):
        db.add(Job(type="hash_torrent", status="running", params_json={"info_hash": file.info_hash}))
        db.commit()
        return await original_presets(base_url)
    async def encode(*args, **kwargs):
        raise AssertionError("Hash started during preflight; source must not be sent")
    monkeypatch.setattr(api, "video_kensetsu_list_presets", presets)
    monkeypatch.setattr(api, "video_kensetsu_encode", encode)
    with pytest.raises(HTTPException) as error:
        submit(api, db, [file])
    assert error.value.status_code == 409
    assert db.scalar(select(EncoderSubmissionItem.status)) == "not_sent"


def test_checking_file_is_rejected_before_path_access(db, tmp_path, monkeypatch):
    file, _ = source(db, tmp_path)
    file.is_checking = True
    db.commit()
    def forbidden(*args, **kwargs):
        raise AssertionError("No path lookup while source is checking")
    monkeypatch.setattr(rest, "resolve_media_file_for_download", forbidden)
    path, error = rest._resolve_encode_path_for_file(db, file.id)
    assert path is None and "проверяется" in error


def test_api_commits_identity_and_presets_before_post_and_replays_without_preflight(api, monkeypatch, db, tmp_path):
    file, _ = source(db, tmp_path)
    requests = []

    async def encode(base_url, **kwargs):
        requests.append(kwargs)
        with Session(db.get_bind()) as observer:
            group = observer.scalar(select(EncoderSubmission))
            assert group.request_started_at is not None
            assert group.finished_at is None
            assert group.presets_json["video"]["name"] == "HEVC snapshot"
            assert group.items[0].source_json["mtime_ns"] == Path(file.full_path).stat().st_mtime_ns
            assert group.items[0].status == "sending"
        return {"status_code": 200, "body": {"created": 1, "job_id": "ext-123"}}

    monkeypatch.setattr(api, "video_kensetsu_encode", encode)
    request_id = uuid4().hex
    first = submit(api, db, [file], request_id=request_id)
    assert first.status_code == 200
    assert payload(first)["items"][0]["external_job_ids"] == ["ext-123"]
    monkeypatch.setattr(api, "_require_video_kensetsu_base_url", lambda db: pytest.fail("replay should not need live settings"))
    second = submit(api, db, [file], request_id=request_id)
    assert second.status_code == 200
    assert payload(second)["submission_id"] == payload(first)["submission_id"]
    assert len(requests) == 1
    assert db.scalar(select(func.count()).select_from(EncoderSubmission)) == 1


def test_request_id_cannot_be_reused_for_different_parameters(api, monkeypatch, db, tmp_path):
    file, _ = source(db, tmp_path)
    calls = []

    async def encode(base_url, **kwargs):
        calls.append(kwargs)
        return {"status_code": 200, "body": {"created": 1}}

    monkeypatch.setattr(api, "video_kensetsu_encode", encode)
    key = uuid4().hex
    submit(api, db, [file], request_id=key)
    with pytest.raises(HTTPException) as error:
        submit(api, db, [file], request_id=key, audio_downmix="stereo")
    assert error.value.status_code == 409
    assert len(calls) == 1


def test_reordering_a_batch_replays_same_user_action(db, tmp_path):
    a, _ = source(db, tmp_path, relative_path="Show/a.mkv")
    b, _ = source(db, tmp_path, relative_path="Show/b.mkv")
    key = uuid4().hex
    first, first_owner = claim(db, [a, b], request_id=key)
    second, second_owner = claim(db, [b, a], request_id=key)
    assert first_owner and not second_owner
    assert second.id == first.id


def test_timeout_is_durable_unknown_and_replaying_does_not_post_again(api, monkeypatch, db, tmp_path):
    file, _ = source(db, tmp_path)
    calls = []

    async def encode(base_url, **kwargs):
        calls.append(kwargs)
        raise httpx.ReadTimeout("response lost")

    monkeypatch.setattr(api, "video_kensetsu_encode", encode)
    key = uuid4().hex
    with pytest.raises(HTTPException) as error:
        submit(api, db, [file], request_id=key)
    assert error.value.status_code == 502
    sid = int(error.value.headers["X-Encoder-Submission-Id"])
    db.expire_all()
    group = history.get_submission(db, sid)
    assert group.items[0].status == "unknown"
    assert group.request_started_at is not None and group.finished_at is not None
    assert submit(api, db, [file], request_id=key).status_code == 207
    with pytest.raises(HTTPException) as conflict:
        submit(api, db, [file])
    assert conflict.value.status_code == 409
    assert len(calls) == 1


@pytest.mark.parametrize("started, expected", [(False, "not_sent"), (True, "unknown")])
def test_expired_claim_recovery_preserves_uncertainty_of_started_request(db, tmp_path, started, expected):
    file, _ = source(db, tmp_path)
    group, _ = claim(db, [file])
    group.expires_at = utcnow() - timedelta(seconds=1)
    if started:
        group.request_started_at = utcnow() - timedelta(minutes=11)
    db.commit()
    history.recover_expired(db)
    db.expire_all()
    restored = history.get_submission(db, group.id)
    assert restored.finished_at is not None
    assert restored.items[0].status == expected


@pytest.mark.parametrize("exception_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_request_not_connected_is_not_sent(api, monkeypatch, db, tmp_path, exception_type):
    file, _ = source(db, tmp_path)

    async def encode(base_url, **kwargs):
        raise exception_type("no connection")

    monkeypatch.setattr(api, "video_kensetsu_encode", encode)
    with pytest.raises(HTTPException):
        submit(api, db, [file])
    assert db.scalar(select(EncoderSubmissionItem.status)) == "not_sent"


def test_retry_links_new_receipt_without_erasing_first_failure(api, monkeypatch, db, tmp_path):
    file, _ = source(db, tmp_path)
    first, _ = claim(db, [file])
    history.finish_failure(db, first, "file unavailable", started=False)

    async def encode(base_url, **kwargs):
        return {"status_code": 200, "body": {"created": 1, "job_id": "retry-job"}}

    monkeypatch.setattr(api, "video_kensetsu_encode", encode)
    response = submit(api, db, [file], retry_of=first.id)
    assert response.status_code == 200
    second = payload(response)
    assert second["retry_of_id"] == first.id
    assert second["submission_id"] != first.id
    db.expire_all()
    assert history.get_submission(db, first.id).items[0].status == "not_sent"
    assert history.get_submission(db, first.id).error == "file unavailable"


def test_changed_version_has_no_old_receipt_and_cannot_retry_old_snapshot(api, monkeypatch, db, tmp_path):
    file, archive = source(db, tmp_path)
    old = accepted(db, file)
    original_hash = file.info_hash
    archive.superseded = True
    file.info_hash = "f" * 40
    db.commit()
    assert history.latest_receipts(db, [file.id]) == {}
    with pytest.raises(HTTPException) as error:
        submit(api, db, [file], retry_of=old.id, allow_repeat=True)
    assert error.value.status_code == 409
    assert history.serialize(history.get_submission(db, old.id))["items"][0]["info_hash"] == original_hash


@pytest.mark.parametrize("reset", ["operational", "full"])
def test_receipt_snapshots_survive_reset_and_source_deletion(db, tmp_path, reset):
    file, archive = source(db, tmp_path)
    stat = Path(file.full_path).stat()
    db.add(DiskFileHash(full_path=file.full_path, size=stat.st_size, mtime=stat.st_mtime,
                        content_hash="b" * 64, hash_algo="blake3", last_hashed_at=utcnow()))
    db.commit()
    group = accepted(db, file)
    before = history.serialize(group)
    storage = tmp_path / "torrents"
    storage.mkdir()
    (storage / archive.file_path).write_bytes(b"torrent archive")
    if reset == "full":
        reset_full(db, storage_root=storage)
    else:
        reset_operational_state(db)
    db.execute(delete(TorrentFile))
    db.execute(delete(Release))
    db.commit()
    db.expire_all()
    restored = history.serialize(history.get_submission(db, group.id))
    assert restored["items"] == before["items"]
    assert restored["items"][0]["source"]["title"] == "Тестовый релиз"
    assert restored["items"][0]["source"]["content_hash"] == "b" * 64
    assert restored["counts"] == {"accepted": 1}


def test_partial_response_accepts_exact_evidence_and_keeps_unmatched_error_unknown(db, tmp_path):
    a, _ = source(db, tmp_path, relative_path="Show/a.mkv")
    b, _ = source(db, tmp_path, relative_path="Show/b.mkv")
    group, _ = claim(db, [a, b])
    for item, file in zip(group.items, [a, b]):
        history.capture_path(db, item, file.full_path)
    history.settle_response(group, 207, {"created": 1, "jobs": [{"path": a.full_path, "id": "a-job"}],
                                      "errors": [{"filename": "unrelated.mkv", "error": "failed"}]})
    db.commit()
    states = {i.file_id: i.status for i in history.get_submission(db, group.id).items}
    assert states == {a.id: "accepted", b.id: "unknown"}


def test_kensetsu_planned_single_filename_receipt_is_accepted(api, db, tmp_path, monkeypatch):
    file, _ = source(db, tmp_path, relative_path="Hotaru no Yomeiri/Hotaru_no_Yomeiri_[01] .mkv")
    external_id = "2ce50252-10ed-4382-839b-5a3e96b3df16"

    async def encode(*args, **kwargs):
        return {"status_code": 200, "body": {
            "created": True, "queued": True, "encoding_started": False,
            "job": {"id": external_id, "filename": "Hotaru_no_Yomeiri_[01] .mkv",
                    "status": "planned", "progress": 0.0, "completed_at": None},
        }}

    monkeypatch.setattr(api, "video_kensetsu_encode", encode)
    response = submit(api, db, [file])
    data = payload(response)
    assert response.status_code == 200
    assert data["counts"] == {"accepted": 1}
    assert data["items"][0]["external_job_ids"] == [external_id]
    assert data["items"][0]["error"] is None
    assert data["result"]["job"]["status"] == "planned"


@pytest.mark.parametrize("filename", ["other.mkv", "episode [01] .mkv", "Episode [01].mkv", "Other/Episode [01] .mkv"])
def test_single_filename_receipt_requires_exact_filename(db, tmp_path, filename):
    file, _ = source(db, tmp_path, relative_path="Show/Episode [01] .mkv")
    group, _ = claim(db, [file])
    history.settle_response(group, 200, {"created": True, "job": {"id": "job", "filename": filename}})
    assert group.items[0].status == "unknown"
    assert group.items[0].external_job_ids == []


@pytest.mark.parametrize("key", ["path", "input_path"])
def test_single_filename_does_not_override_conflicting_full_path(db, tmp_path, key):
    file, _ = source(db, tmp_path)
    group, _ = claim(db, [file])
    history.settle_response(group, 200, {"created": True, "job": {
        "id": "job", "filename": Path(file.full_path).name, key: "/different/episode-01.mkv",
    }})
    assert group.items[0].status == "unknown"
    assert group.items[0].external_job_ids == []


def test_unique_batch_filenames_still_require_exact_paths(db, tmp_path):
    a, _ = source(db, tmp_path, relative_path="A/a.mkv")
    b, _ = source(db, tmp_path, relative_path="B/b.mkv")
    group, _ = claim(db, [a, b])
    history.settle_response(group, 200, {"created": 2, "jobs": [
        {"filename": "a.mkv", "id": "job-a"}, {"filename": "b.mkv", "id": "job-b"},
    ]})
    assert [i.status for i in group.items] == ["unknown", "unknown"]
    assert all(not i.external_job_ids for i in group.items)


def test_single_actual_post_with_local_unsent_item_accepts_filename(db, tmp_path):
    a, _ = source(db, tmp_path, relative_path="A/episode.mkv")
    b, _ = source(db, tmp_path, relative_path="B/episode.mkv")
    group, _ = claim(db, [a, b])
    group.items[1].status = "not_sent"
    history.settle_response(group, 200, {"created": True, "job": {"filename": "episode.mkv", "id": "job-a"}})
    assert [i.status for i in group.items] == ["accepted", "not_sent"]
    assert group.items[0].external_job_ids == ["job-a"]


def _saved_unconfirmed_receipt(db, files, response):
    group, _ = claim(db, files)
    group.request_started_at = utcnow()
    group.http_status = 200
    group.response_json = response
    group.finished_at = utcnow()
    group.updated_at = group.finished_at
    for item in group.items:
        item.status = "unknown"
        item.error = "Ответ не подтверждает приём именно этого файла"
    db.commit()
    return group


def test_saved_single_receipt_can_be_repaired_without_source_or_resend(db, tmp_path):
    file, _ = source(db, tmp_path)
    body = {"created": True, "job": {"id": "saved-job", "filename": Path(file.full_path).name, "status": "planned"}}
    group = _saved_unconfirmed_receipt(db, [file], body)
    original_finished = group.finished_at
    original_request = group.request_id
    original_source = dict(group.items[0].source_json)
    Path(file.full_path).unlink()
    assert history.repair_saved_receipt(group) == [file.id]
    db.commit()
    assert group.items[0].status == "accepted"
    assert group.items[0].external_job_ids == ["saved-job"]
    assert group.items[0].error is None
    assert group.finished_at == original_finished
    assert group.request_id == original_request
    assert group.response_json == body
    assert group.items[0].source_json == original_source
    assert db.scalar(select(func.count()).select_from(EncoderSubmission)) == 1
    assert history.repair_saved_receipt(group) == []


@pytest.mark.parametrize("error", ["response lost", "Противоречивый ответ: для файла получены и задание, и ошибка"])
def test_saved_receipt_repair_does_not_change_other_unknown_causes(db, tmp_path, error):
    file, _ = source(db, tmp_path)
    group = _saved_unconfirmed_receipt(db, [file], {"created": True, "job": {"id": "job", "filename": Path(file.full_path).name}})
    group.items[0].error = error
    assert history.repair_saved_receipt(group) == []
    assert group.items[0].status == "unknown"


@pytest.mark.parametrize("status", [None, 500])
def test_saved_receipt_repair_requires_successful_http_response(db, tmp_path, status):
    file, _ = source(db, tmp_path)
    group = _saved_unconfirmed_receipt(db, [file], {"created": True, "job": {"id": "job", "filename": Path(file.full_path).name}})
    group.http_status = status
    assert history.repair_saved_receipt(group) == []
    assert group.items[0].status == "unknown"


def test_saved_partial_batch_repair_preserves_original_posted_scope(db, tmp_path):
    a, _ = source(db, tmp_path, relative_path="A/episode.mkv")
    b, _ = source(db, tmp_path, relative_path="B/episode.mkv")
    group = _saved_unconfirmed_receipt(db, [a, b], {"created": 2, "jobs": [
        {"id": "job-a", "path": a.full_path}, {"id": "job-b", "filename": "episode.mkv"},
    ]})
    group.items[0].status = "accepted"
    group.items[0].error = None
    group.items[0].external_job_ids = ["job-a"]
    db.commit()
    assert history.repair_saved_receipt(group) == []
    assert [i.status for i in group.items] == ["accepted", "unknown"]
    assert group.items[1].external_job_ids == []


def test_receipt_repair_cli_is_dry_run_by_default_and_apply_is_idempotent(db, tmp_path, capsys):
    from scripts.repair_encoder_receipts import main
    from sqlalchemy.orm import sessionmaker

    file, _ = source(db, tmp_path)
    group = _saved_unconfirmed_receipt(db, [file], {"created": True, "job": {"id": "saved-job", "filename": Path(file.full_path).name}})
    factory = sessionmaker(bind=db.get_bind())
    arguments = ["--submission-id", str(group.id)]
    assert main(arguments, session_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    db.expire_all()
    assert group.items[0].status == "unknown"
    assert main([*arguments, "--apply"], session_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)["repaired_file_ids"] == [file.id]
    db.expire_all()
    assert group.items[0].status == "accepted"
    assert main([*arguments, "--apply"], session_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)["repaired_file_ids"] == []


def test_same_basename_does_not_attach_error_to_arbitrary_source(db, tmp_path):
    a, _ = source(db, tmp_path, relative_path="A/episode.mkv")
    b, _ = source(db, tmp_path, relative_path="B/episode.mkv")
    group, _ = claim(db, [a, b])
    for item, file in zip(group.items, [a, b]):
        history.capture_path(db, item, file.full_path)
    history.settle_response(group, 207, {"created": 1, "errors": [{"filename": "episode.mkv", "error": "failed"}]})
    db.commit()
    assert [i.status for i in group.items] == ["unknown", "unknown"]


def test_same_basename_job_markers_cannot_prove_reception_of_both_sources(db, tmp_path):
    a, _ = source(db, tmp_path, relative_path="A/episode.mkv")
    b, _ = source(db, tmp_path, relative_path="B/episode.mkv")
    group, _ = claim(db, [a, b])
    for item, file in zip(group.items, [a, b]):
        history.capture_path(db, item, file.full_path)
    history.settle_response(group, 207, {"jobs": [{"filename": "episode.mkv", "id": "job-a"},
                                              {"filename": "episode.mkv", "id": "job-b"}]})
    db.commit()
    assert [i.status for i in group.items] == ["unknown", "unknown"]
    assert all(not i.external_job_ids for i in group.items)


def test_conflicting_error_remains_unknown_when_multiple_jobs_are_returned(db, tmp_path):
    file, _ = source(db, tmp_path)
    group, _ = claim(db, [file])
    history.capture_path(db, group.items[0], file.full_path)
    history.settle_response(group, 207, {"errors": [{"path": file.full_path, "error": "refused"}],
                                      "jobs": [{"path": file.full_path, "id": "job-a"},
                                               {"path": file.full_path, "id": "job-b"}]})
    db.commit()
    assert group.items[0].status == "unknown"


def test_history_filters_same_item_and_escapes_snapshots_when_rendered(db, tmp_path):
    a, _ = source(db, tmp_path, title='<script>alert("title")</script>', relative_path="Show/<input>.mkv")
    b, _ = source(db, tmp_path, release_id=43, relative_path="Other/other.mkv")
    good = accepted(db, a)
    bad, _ = claim(db, [b])
    history.finish_failure(db, bad, '<img src=x onerror="alert(1)">', started=False)
    assert history.history_page(db, status="attention")["total"] == 1
    assert history.history_page(db, release_id=42, status="attention")["total"] == 0
    assert history.history_page(db, search="job-confirmed")["submissions"][0]["id"] == good.id
    assert history.history_page(db, search="Other/other")["submissions"][0]["id"] == bad.id
    page = history.history_page(db, page=999)
    assert page["page"] == 1 and page["total_pages"] == 1
    environment = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / "app/templates"),
                              autoescape=select_autoescape(["html"]))
    html = environment.get_template("partials/encoder_history_live.html").render(**page)
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "&lt;input&gt;.mkv" in html
    assert "&lt;img src=x" in html and '<img src=x onerror="alert(1)">' not in html
    assert "Принят Kensetsu" in html and "Не отправлен" in html
    assert "Повторить неотправленные / отклонённые" in html


def test_recreated_row_for_same_source_cannot_bypass_duplicate_protection(db, tmp_path):
    file, _ = source(db, tmp_path)
    first = accepted(db, file)
    identity = dict(info_hash=file.info_hash, relative_path=file.relative_path, full_path=file.full_path,
                    torrent_id=file.torrent_id, release_id=file.release_id, size=file.size)
    new_id = file.id + 100
    db.delete(file)
    db.commit()
    replacement = TorrentFile(id=new_id, **identity)
    db.add(replacement)
    db.commit()
    with pytest.raises(history.SubmissionConflict) as error:
        claim(db, [replacement])
    assert error.value.previous[0]["submission_id"] == first.id


def test_equivalent_downmix_spelling_cannot_bypass_duplicate_protection(api, monkeypatch, db, tmp_path):
    file, _ = source(db, tmp_path)
    calls = []

    async def encode(base_url, **kwargs):
        calls.append(kwargs)
        return {"status_code": 200, "body": {"created": 1}}

    monkeypatch.setattr(api, "video_kensetsu_encode", encode)
    submit(api, db, [file], audio_downmix="none")
    with pytest.raises(HTTPException) as error:
        submit(api, db, [file], audio_downmix=" NONE ")
    assert error.value.status_code == 409
    assert len(calls) == 1


def test_rejected_exception_diagnostics_are_redacted_before_persistence(api, monkeypatch, db, tmp_path):
    snapshot = history.safe_json({"api_key": "private-key", "nested": {"X-Api-Key": "private-header"}})
    assert snapshot == {"api_key": "***", "nested": {"X-Api-Key": "***"}}
    file, _ = source(db, tmp_path)

    async def encode(base_url, **kwargs):
        raise VideoKensetsuHttpError("refused http://operator:private-password@encoder.test/?passkey=private-passkey", status_code=422)

    monkeypatch.setattr(api, "video_kensetsu_encode", encode)
    with pytest.raises(HTTPException):
        submit(api, db, [file])
    group = db.scalar(select(EncoderSubmission))
    stored = json.dumps(history.serialize(group), ensure_ascii=False)
    assert "private-password" not in stored
    assert "private-passkey" not in stored
    assert group.items[0].status == "rejected"


def test_failed_receipt_commit_rolls_back_before_reloading_durable_intent(api, monkeypatch, db, tmp_path):
    """A real failed flush expires ORM fields and requires rollback before any load."""
    file, _ = source(db, tmp_path)
    calls = []
    fail_once = {"enabled": True}

    async def encode(base_url, **kwargs):
        calls.append(kwargs)
        return {"status_code": 200, "body": {"created": 1, "job_id": "external-already-created"}}

    def fail_final_flush(session, flush_context, instances):
        for row in list(session.dirty):
            if isinstance(row, EncoderSubmissionItem) and row.status == "accepted" and fail_once["enabled"]:
                fail_once["enabled"] = False
                # Uses the real DB CHECK constraint, putting Session into failed-flush state.
                row.status = "invalid_status"

    monkeypatch.setattr(api, "video_kensetsu_encode", encode)
    event.listen(db, "before_flush", fail_final_flush)
    key = uuid4().hex
    try:
        with pytest.raises(HTTPException) as error:
            submit(api, db, [file], request_id=key)
    finally:
        event.remove(db, "before_flush", fail_final_flush)
    assert error.value.status_code == 502
    sid = int(error.value.headers["X-Encoder-Submission-Id"])
    db.expire_all()
    receipt = history.get_submission(db, sid)
    assert receipt.items[0].status == "unknown"
    assert receipt.request_started_at is not None and receipt.finished_at is not None
    assert submit(api, db, [file], request_id=key).status_code == 207
    assert len(calls) == 1


def test_source_changed_during_preset_preflight_is_not_dispatched(api, monkeypatch, db, tmp_path):
    file, _ = source(db, tmp_path)
    original_presets = api.video_kensetsu_list_presets
    calls = []

    async def changing_presets(base_url):
        Path(file.full_path).write_bytes(b"a different rebuilt source during await")
        return await original_presets(base_url)

    async def encode(*args, **kwargs):
        calls.append(kwargs)
        return {"status_code": 200, "body": {"created": True, "job_id": "must-not-exist"}}

    monkeypatch.setattr(api, "video_kensetsu_list_presets", changing_presets)
    monkeypatch.setattr(api, "video_kensetsu_encode", encode)
    with pytest.raises(HTTPException) as exc:
        submit(api, db, [file])
    assert exc.value.status_code == 409
    assert calls == []
    group = history.get_submission(db, int(exc.value.headers["X-Encoder-Submission-Id"]))
    assert group.request_started_at is None
    assert group.items[0].status == "not_sent"
    assert "изменился" in group.items[0].error
