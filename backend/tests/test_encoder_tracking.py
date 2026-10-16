"""Real persistence and mock GET/HEAD; no external encodes or media downloads."""
import asyncio
from datetime import timedelta
import json
import os
from uuid import uuid4

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pathlib import Path
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models import EncoderJob, EncoderSubmission, EncoderSubmissionItem, Setting
from app.services import encoder_history as history
from app.services import encoder_tracking as tracking
from app.services.ui_events import channel_token
from app.utils.datetime_fmt import utcnow


@compiles(JSONB, "sqlite")
def _jsonb(*args, **kwargs):
    return "JSON"


@pytest.fixture(params=["sqlite", "postgres"])
def factory(tmp_path, request):
    admin = None
    if request.param == "postgres":
        url = os.getenv("ALTT_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("ALTT_TEST_POSTGRES_URL отдельной тестовой БД не задан")
        assert url.startswith(("postgresql://", "postgresql+psycopg://"))
        from sqlalchemy import text
        schema="altt_encoder_tracking_"+uuid4().hex
        admin=create_engine(url)
        with admin.begin() as conn: conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine=create_engine(url,connect_args={"options":f"-csearch_path={schema}"})
    else:
        engine = create_engine(f"sqlite:///{tmp_path/'tracking.sqlite'}")
        @event.listens_for(engine, "connect")
        def foreign_keys(conn, record):
            conn.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()
    if admin:
        with admin.begin() as conn: conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def seed(factory, *, external_id="external-1", base_url="http://original-encoder.test", enabled=True):
    with factory() as db:
        db.add(Setting(key="video_kensetsu_enabled", value="true" if enabled else "false"))
        db.add(Setting(key="video_kensetsu_base_url", value="http://new-encoder.test"))
        group = EncoderSubmission(request_id="request", request_digest="digest", base_url=base_url,
            video_id="hevc", audio_id="copy", audio_downmix="none", expires_at=utcnow(),
            request_started_at=utcnow(), finished_at=utcnow(), items=[EncoderSubmissionItem(
                file_id=17, info_hash="a"*40, relative_path="Show/01.mkv", full_path="/anilibria/Show/01.mkv",
                status="accepted", external_job_ids=[external_id], source_json={"title":"Test"})])
        tracking.ensure_item_jobs(group.items[0])
        db.add(group)
        db.commit()
        return group.id, group.items[0].encoding_jobs[0].id


def poll(factory, handler):
    return asyncio.run(tracking.poll_encoder_jobs(session_factory=factory, transport=httpx.MockTransport(handler)))


def remote(status="planned", **fields):
    return {"id":"external-1", "status":status, "progress":0.0,
            "created_at":"2026-10-14T04:25:13Z", "started_at":None,
            "completed_at":None, "error_message":None, **fields}


def make_due(factory, job_id):
    with factory() as db:
        db.get(EncoderJob, job_id).next_poll_at = utcnow() - timedelta(seconds=1)
        db.commit()


def test_queue_encode_done_ready_transitions_preserve_receipt_and_use_original_server(factory):
    group_id, job_id = seed(factory)
    requests = []
    state = {"status":"planned"}
    def handler(request):
        requests.append((request.method, str(request.url)))
        assert request.url.host == "original-encoder.test"
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length":"238014293"})
        assert request.method == "GET"
        return httpx.Response(200, json=remote(state["status"], progress=state.get("progress",0),
            output_filename="finished.mkv", started_at=state.get("started_at"), completed_at=state.get("completed_at")))
    for status, progress in [("planned",0),("in_progress",42.5),("done",100)]:
        state.update(status=status, progress=progress)
        if status != "planned": state["started_at"] = "2026-10-14T11:49:25+07:00"
        if status == "done": state["completed_at"] = "2026-10-14T05:06:23Z"
        make_due(factory,job_id)
        assert poll(factory,handler) == 1
        with factory() as db:
            job = db.get(EncoderJob,job_id)
            assert job.status == status and job.progress == progress
            assert history.get_submission(db,group_id).items[0].status == "accepted"
    with factory() as db:
        group = history.get_submission(db,group_id)
        data = history.serialize(group)
        job = group.items[0].encoding_jobs[0]
        assert str(job.remote_started_at) == "2026-10-14 04:49:25"
        assert job.output_available is True and job.output_size_bytes == 238014293
        assert data["ready_count"] == 1
        assert data["items"][0]["encoding_jobs"][0]["download_url"].endswith("/external-1/download")
        assert job.next_poll_at-job.last_checked_at == timedelta(seconds=300)
        assert not job.claim_token
    assert [method for method,_ in requests] == ["GET","GET","GET","HEAD"]


@pytest.mark.parametrize("status", ["error","cancelled"])
def test_terminal_failure_is_execution_only_and_never_probes_a_file(factory,status):
    group_id,job_id = seed(factory)
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200,json=remote(status,error_message="Encoder failed"))
    assert poll(factory,handler) == 1
    with factory() as db:
        group=history.get_submission(db,group_id)
        assert group.items[0].status == "accepted"
        assert group.items[0].encoding_jobs[0].status == status
        assert history.history_page(db,status="attention")["total"] == 1
        assert history.history_page(db,encoding_state="error")["total"] == 1


def test_done_without_file_and_later_restored_file_are_detected(factory):
    group_id,job_id=seed(factory)
    available={"value":False}
    def handler(request):
        return httpx.Response(200,json=remote("done",progress=100)) if request.method=="GET" else httpx.Response(200 if available["value"] else 404)
    poll(factory,handler)
    with factory() as db:
        job=db.get(EncoderJob,job_id)
        assert job.status=="done" and job.output_available is False
        assert history.history_page(db,encoding_state="unavailable")["total"]==1
        assert history.serialize(history.get_submission(db,group_id))["ready_count"]==0
    available["value"]=True
    make_due(factory,job_id)
    poll(factory,handler)
    with factory() as db:
        assert db.get(EncoderJob,job_id).output_available is True
        assert history.history_page(db,encoding_state="ready")["total"]==1


@pytest.mark.parametrize("failure", ["connection", "missing", "malformed", "wrong_id", "unknown_status", "bad_progress", "bad_date", "server"])
def test_poll_errors_preserve_last_status_and_backoff(factory,failure):
    group_id,job_id=seed(factory)
    poll(factory,lambda request:httpx.Response(200,json=remote("in_progress",progress=51)))
    make_due(factory,job_id)
    def handler(request):
        if failure=="connection": raise httpx.ConnectError("http://user:private-password@host?token=private-token",request=request)
        if failure=="missing": return httpx.Response(404)
        if failure=="server": return httpx.Response(503,text="private-token")
        if failure=="malformed": return httpx.Response(200,text="not-json")
        fields={"wrong_id":{"id":"other"}, "unknown_status":{"status":"mystery"},
                "bad_progress":{"progress":float("nan")}, "bad_date":{"started_at":"broken"}}[failure]
        # NaN JSON is deliberately emitted as raw text to test the decoder.
        return httpx.Response(200,content=json.dumps(remote(**fields)).encode(),headers={"Content-Type":"application/json"})
    assert poll(factory,handler)==1
    with factory() as db:
        group=history.get_submission(db,group_id)
        job=group.items[0].encoding_jobs[0]
        assert group.items[0].status=="accepted"
        assert job.status=="in_progress" and job.progress==51
        assert job.poll_error and job.poll_failures==1
        assert "private" not in job.poll_error
        assert job.last_success_at < job.last_checked_at
        assert job.next_poll_at-job.last_checked_at == timedelta(seconds=60)


def test_head_failure_keeps_done_but_does_not_claim_output_ready(factory):
    group_id,job_id=seed(factory)
    def handler(request):
        if request.method=="HEAD": raise httpx.ReadTimeout("lost response",request=request)
        return httpx.Response(200,json=remote("done",progress=100))
    poll(factory,handler)
    with factory() as db:
        job=db.get(EncoderJob,job_id)
        assert job.status=="done" and job.output_available is None
        assert job.poll_error_kind=="output_probe_failed"
        assert history.serialize(history.get_submission(db,group_id))["ready_count"]==0


def test_claim_lease_recovery_and_old_response_fencing(factory):
    group_id,job_id=seed(factory)
    now=utcnow()
    with factory() as db: old=tracking.claim_due(db,now=now)[0]
    with factory() as db: assert tracking.claim_due(db,now=now)==[]
    later=now+timedelta(seconds=121)
    with factory() as db: current=tracking.claim_due(db,now=later)[0]
    with factory() as db:
        assert not tracking.save_observation(db,old,{"status":"error"},None,None,now=later)
        assert tracking.save_observation(db,current,{"status":"planned"},None,None,now=later)
        assert db.get(EncoderJob,job_id).status=="planned"


def test_disabled_encoder_does_not_issue_requests_or_claim_jobs(factory):
    _,job_id=seed(factory,enabled=False)
    def handler(request): raise AssertionError("No network expected")
    assert poll(factory,handler)==0
    with factory() as db: assert db.get(EncoderJob,job_id).claim_token is None


def test_tracking_changes_sse_token_and_history_renders_ready_link_safely(factory):
    group_id,job_id=seed(factory)
    with factory() as db: old=channel_token(db,"encoder")
    def handler(request):
        return httpx.Response(200,json=remote("done",progress=100,output_filename="<script>unsafe</script>.mkv")) if request.method=="GET" else httpx.Response(200,headers={"Content-Length":"17"})
    poll(factory,handler)
    with factory() as db:
        assert channel_token(db,"encoder")!=old
        context=history.history_page(db,encoding_state="ready")
    environment=Environment(loader=FileSystemLoader(Path(__file__).parents[1]/"app/templates"),autoescape=select_autoescape())
    rendered=environment.get_template("partials/encoder_history_live.html").render(**context)
    assert "Скачать готовый файл" in rendered
    assert "Кодирование завершено" in rendered
    assert "<script>unsafe" not in rendered
    assert "&lt;script&gt;unsafe" in rendered
    assert "17 байт" in rendered


def test_persisted_snapshot_remains_readable_after_job_get_404(factory):
    group_id,job_id=seed(factory)
    poll(factory,lambda request:httpx.Response(200,json=remote("done",progress=100)) if request.method=="GET" else httpx.Response(200))
    make_due(factory,job_id)
    poll(factory,lambda request:httpx.Response(404))
    with factory() as db:
        data=history.serialize(history.get_submission(db,group_id))
        job=data["items"][0]["encoding_jobs"][0]
        assert job["status"]=="done" and job["poll_error_kind"]=="not_found"
        assert job["download_url"] is None and data["ready_count"]==0


def test_missing_job_then_connection_error_cannot_restore_stale_download_readiness(factory):
    group_id, job_id = seed(factory)
    ready = lambda request: httpx.Response(200, json=remote(
        "done", progress=100, output_available=True, output_size_bytes=123))
    poll(factory, ready)
    make_due(factory, job_id)
    poll(factory, lambda request: httpx.Response(404))
    make_due(factory, job_id)

    def disconnected(request):
        raise httpx.ConnectError("offline", request=request)

    poll(factory, disconnected)
    with factory() as db:
        job = db.get(EncoderJob, job_id)
        data = history.serialize(history.get_submission(db, group_id))
        assert job.status == "done" and job.poll_error_kind == "connection"
        assert data["ready_count"] == 0
        assert data["items"][0]["encoding_jobs"][0]["download_url"] is None
        assert history.history_page(db, encoding_state="ready")["total"] == 0
    make_due(factory, job_id)
    poll(factory, ready)
    with factory() as db:
        assert history.serialize(history.get_submission(db, group_id))["ready_count"] == 1


@pytest.mark.parametrize("available", [True,False])
def test_current_api_output_metadata_avoids_head_and_ignores_untrusted_download_url(factory,available):
    group_id,job_id=seed(factory)
    def handler(request):
        assert request.method=="GET"
        return httpx.Response(200,json=remote("done",progress=100,input_path="/anilibria/Show/01.mkv",
            output_available=available,output_size_bytes=123 if available else None,
            download_url="https://unrelated.invalid/file" if available else None))
    poll(factory,handler)
    with factory() as db:
        job=db.get(EncoderJob,job_id)
        assert job.output_available is available
        result=history.serialize(history.get_submission(db,group_id))["items"][0]["encoding_jobs"][0]
        assert result["download_url"] == ("http://original-encoder.test/api/jobs/external-1/download" if available else None)


def test_current_api_conflicting_input_path_does_not_attach_another_source(factory):
    _,job_id=seed(factory)
    poll(factory,lambda request:httpx.Response(200,json=remote("done",output_available=True,input_path="/another/source.mkv")))
    with factory() as db:
        job=db.get(EncoderJob,job_id)
        assert job.status=="unknown" and job.poll_error_kind=="invalid_response"
        assert job.output_available is None


@pytest.mark.parametrize("fields", [{"output_available":"false"},{"output_available":True,"output_size_bytes":-1},
                                    {"output_available":True,"output_size_bytes":True}])
def test_invalid_native_ready_metadata_does_not_prove_availability(factory,fields):
    _,job_id=seed(factory)
    poll(factory,lambda request:httpx.Response(200,json=remote("done",**fields)))
    with factory() as db:
        job=db.get(EncoderJob,job_id)
        assert job.status=="done" and job.output_available is None
        assert job.poll_error_kind=="invalid_response"


def test_legacy_download_head_html_200_is_not_a_ready_file(factory):
    _,job_id=seed(factory)
    poll(factory,lambda request:httpx.Response(200,json=remote("done")) if request.method=="GET" else
         httpx.Response(200,headers={"Content-Type":"text/html"}))
    with factory() as db:
        job=db.get(EncoderJob,job_id)
        assert job.status=="done" and job.output_available is None
        assert job.poll_error_kind=="output_probe_failed"


def test_concurrent_claims_have_one_owner(factory):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    _,job_id=seed(factory)
    barrier=threading.Barrier(2)
    now=utcnow()
    def claim(_):
        with factory() as db:
            barrier.wait(timeout=10)
            return tracking.claim_due(db,now=now)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims=list(pool.map(claim,range(2)))
    assert sum(len(rows) for rows in claims)==1


def test_history_routes_keep_execution_filter_and_ready_data(factory,monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    from app.db.session import get_db
    group_id,job_id=seed(factory)
    poll(factory,lambda request:httpx.Response(200,json=remote("done",progress=100,output_available=True,output_size_bytes=123)))
    def database():
        with factory() as db: yield db
    monkeypatch.setattr(main,"SessionLocal",factory)
    main.app.dependency_overrides[get_db]=database
    try:
        client=TestClient(main.app)
        page=client.get("/encoder-history?encoding_state=ready")
        assert page.status_code==200
        assert 'encoding_state=ready' in page.text
        assert 'Скачать готовый файл' in page.text
        partial=client.get("/encoder-history/live?encoding_state=ready")
        assert partial.status_code==200 and 'Скачать готовый файл' in partial.text
        api=client.get("/api/encoder-submissions?encoding_state=ready")
        assert api.status_code==200 and api.json()["submissions"][0]["ready_count"]==1
        detail=client.get(f"/api/encoder-submissions/{group_id}")
        assert detail.status_code==200 and detail.json()["items"][0]["encoding_jobs"][0]["status"]=="done"
    finally:
        main.app.dependency_overrides.pop(get_db,None)


def test_no_database_connection_is_held_during_remote_requests(factory):
    _,job_id=seed(factory)
    with factory() as db: engine=db.get_bind()
    def handler(request):
        assert engine.pool.checkedout()==0
        return httpx.Response(200,json=remote("planned"))
    poll(factory,handler)
