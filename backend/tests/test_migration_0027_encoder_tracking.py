"""Real PostgreSQL migration: preserve receipts, repair only proof, seed jobs."""
import json
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url


def test_0027_repairs_exact_single_receipt_and_backfills_tracking(monkeypatch):
    raw=os.getenv("ALTT_TEST_POSTGRES_URL")
    if not raw: pytest.skip("ALTT_TEST_POSTGRES_URL отдельной тестовой БД не задан")
    assert raw.startswith(("postgresql://","postgresql+psycopg://"))
    schema="altt_migration_0027_"+uuid4().hex
    admin=create_engine(raw)
    with admin.begin() as conn: conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    url=make_url(raw).update_query_dict({"options":f"-csearch_path={schema}"}).render_as_string(hide_password=False)
    from app.core.config import settings
    monkeypatch.setattr(settings,"database_url",url)
    config=Config(str(Path(__file__).parents[1]/"alembic.ini"))
    config.set_main_option("script_location",str(Path(__file__).parents[1]/"alembic"))
    engine=create_engine(url)
    try:
        command.upgrade(config,"0026_job_outcomes_outbox")
        with engine.begin() as conn:
            def group(gid,body):
                conn.execute(text("""INSERT INTO encoder_submissions
                    (id,request_id,request_digest,base_url,video_id,audio_id,audio_downmix,presets_json,response_json,
                     created_at,request_started_at,finished_at,updated_at,expires_at,http_status)
                    VALUES (:id,:request,'digest','http://encoder.test','hevc','copy','none','{}',CAST(:body AS jsonb),
                            '2026-10-14 04:25:03','2026-10-14 04:25:03','2026-10-14 04:25:13',
                            '2026-10-14 04:25:13','2026-10-14 04:35:03',200)"""),
                    {"id":gid,"request":f"request-{gid}","body":json.dumps(body)})
            def item(iid,gid,path,status="unknown",ids=None,error="Ответ не подтверждает приём именно этого файла"):
                conn.execute(text("""INSERT INTO encoder_submission_items
                    (id,submission_id,file_id,info_hash,relative_path,full_path,source_json,status,error,external_job_ids)
                    VALUES (:id,:gid,:file,'original-hash',:path,:full,CAST(:source AS jsonb),:status,:error,CAST(:ids AS jsonb))"""),
                    {"id":iid,"gid":gid,"file":100+iid,"path":path,"full":"/anilibria/"+path,
                     "status":status,"error":error,"ids":json.dumps(ids or []),"source":json.dumps({"size":123})})
            group(1,{"created":True,"job":{"id":"legacy-1","filename":"Episode [01] .mkv","status":"planned"}})
            item(1,1,"Show/Episode [01] .mkv")
            group(2,{"created":2,"jobs":[{"id":"a","filename":"episode.mkv"},{"id":"b","filename":"episode.mkv"}]})
            item(2,2,"A/episode.mkv");item(3,2,"B/episode.mkv")
            group(3,{"job":{"id":"contradictory","filename":"ep.mkv"},"errors":[{"path":"/anilibria/Show/ep.mkv","error":"rejected"}]})
            item(4,3,"Show/ep.mkv")
            group(4,{"job":{"id":"timeout","filename":"ep.mkv"}})
            item(5,4,"Show/ep.mkv",error="response lost")
            group(5,{"job":{"id":"confirmed"}})
            item(6,5,"Other/ep.mkv",status="accepted",ids=["confirmed","confirmed"],error=None)
            group(6,{"job":{"id":"wrong-path","filename":"ep.mkv","input_path":"/other/ep.mkv"}})
            item(7,6,"Show/ep.mkv")
        command.upgrade(config,"head")
        with engine.connect() as conn:
            row=conn.execute(text("SELECT status,error,external_job_ids,source_json FROM encoder_submission_items WHERE id=1")).one()
            assert tuple(row[:3]) == ("accepted",None,["legacy-1"])
            assert row.source_json=={"size":123}
            assert conn.scalar(text("SELECT request_id FROM encoder_submissions WHERE id=1"))=="request-1"
            assert str(conn.scalar(text("SELECT finished_at FROM encoder_submissions WHERE id=1")))=="2026-10-14 04:25:13"
            assert conn.execute(text("SELECT id,status FROM encoder_submission_items WHERE id IN (2,3,4,5,7) ORDER BY id")).all()==[(2,"unknown"),(3,"unknown"),(4,"unknown"),(5,"unknown"),(7,"unknown")]
            assert conn.execute(text("SELECT item_id,external_job_id,status FROM encoder_jobs ORDER BY item_id")).all()==[(1,"legacy-1","unknown"),(6,"confirmed","unknown")]
            assert "ix_encoder_jobs_due" in {i["name"] for i in inspect(conn).get_indexes("encoder_jobs")}
            assert conn.scalar(text("SELECT count(*) FROM encoder_jobs WHERE next_poll_at=created_at"))==2
        command.downgrade(config,"0026_job_outcomes_outbox")
        with engine.connect() as conn:
            assert not inspect(conn).has_table("encoder_jobs")
            assert conn.scalar(text("SELECT status FROM encoder_submission_items WHERE id=1"))=="accepted"
        command.upgrade(config,"head")
        with engine.connect() as conn: assert conn.scalar(text("SELECT count(*) FROM encoder_jobs"))==2
    finally:
        engine.dispose()
        with admin.begin() as conn: conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
