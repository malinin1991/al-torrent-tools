"""Real PostgreSQL migration, activation boundary and competing submissions."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from threading import Barrier
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import EncoderSubmission, TorrentArchive, TorrentFile
from app.services import encoder_history as history


@pytest.fixture
def pg(monkeypatch):
    raw=os.getenv("ALTT_TEST_POSTGRES_URL")
    if not raw:
        pytest.skip("ALTT_TEST_POSTGRES_URL отдельной тестовой БД не задан")
    schema="altt_auto_"+uuid4().hex
    admin=create_engine(raw)
    with admin.begin() as c:c.execute(text('CREATE SCHEMA "'+schema+'"'))
    url=make_url(raw).update_query_dict({"options":"-csearch_path="+schema}).render_as_string(hide_password=False)
    engine=create_engine(url)
    try:
        yield engine,url
    finally:
        engine.dispose()
        with admin.begin() as c:c.execute(text('DROP SCHEMA "'+schema+'" CASCADE'))
        admin.dispose()


def test_0028_upgrade_downgrade_reupgrade_preserves_receipts(pg,monkeypatch):
    engine,url=pg
    from app.core.config import settings
    monkeypatch.setattr(settings,"database_url",url)
    config=Config(str(Path(__file__).parents[1]/"alembic.ini"))
    config.set_main_option("script_location",str(Path(__file__).parents[1]/"alembic"))
    command.upgrade(config,"0027_encoder_job_tracking")
    with engine.begin() as c:
        c.execute(text("""INSERT INTO encoder_submissions
            (request_id,request_digest,base_url,video_id,audio_id,audio_downmix,presets_json,response_json,
             created_at,updated_at,expires_at)
            VALUES ('kept-request','digest','http://encoder.test','hevc','copy','none','{}','{}',
                    timezone('UTC',now()),timezone('UTC',now()),timezone('UTC',now()))"""))
    command.upgrade(config,"head")
    with engine.connect() as c:
        assert c.scalar(text("SELECT origin FROM encoder_submissions WHERE request_id='kept-request'"))=="manual"
        assert c.scalar(text("SELECT value FROM settings WHERE key='video_kensetsu_auto_enabled'"))=="false"
        assert set(("encoder_auto_sources","encoder_file_readiness","encoder_auto_items","encoder_auto_events","release_encoder_policies")).issubset(inspect(c).get_table_names())
        assert not c.scalar(text("SELECT count(*) FROM encoder_auto_items"))
    command.downgrade(config,"0027_encoder_job_tracking")
    with engine.connect() as c:
        assert not inspect(c).has_table("encoder_auto_items")
        assert c.scalar(text("SELECT request_id FROM encoder_submissions"))=="kept-request"
    command.upgrade(config,"head")
    with engine.connect() as c:
        assert c.scalar(text("SELECT request_id FROM encoder_submissions"))=="kept-request"


def test_manual_auto_different_parameters_serialize_under_real_sessions(pg,tmp_path):
    engine,_=pg
    Base.metadata.create_all(engine)
    path=tmp_path/"source.mkv";path.write_bytes(b"test")
    with Session(engine) as db:
        db.add(TorrentArchive(info_hash="b"*40,torrent_id=1,release_id=42,file_path="x"))
        file=TorrentFile(info_hash="b"*40,torrent_id=1,release_id=42,relative_path="Show/ep.mkv",
                         full_path=str(path),size=4,ui_status="new")
        db.add(file);db.commit();fid=file.id
    barrier=Barrier(2)
    def claim(origin):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            try:
                row,owner=history.claim(db,file_ids=[fid],request_id=uuid4().hex,digest=origin,
                    base_url="http://"+origin+".test",video_id=origin,audio_id="opus",downmix="none",origin=origin)
                return "claimed"
            except history.SubmissionConflict:
                return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(claim,["auto","manual"]))
    assert sorted(results)==["claimed","conflict"]
    with Session(engine) as db:assert len(db.scalars(select(EncoderSubmission)).all())==1
