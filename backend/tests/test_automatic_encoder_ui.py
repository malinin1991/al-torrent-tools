from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates
import pytest
from sqlalchemy import select

from app.api.automatic_encoder import create_router
from app.db.models import EncoderAutoItem, EncoderAutoSource, ReleaseEncoderPolicy
from app.db.session import get_db
from app.services import automatic_encoder as auto
from app.utils.datetime_fmt import as_utc_iso
from test_automatic_encoder import env, source, LAYERS


@pytest.fixture
def client(env):
    factory,_=env
    app=FastAPI()
    templates=Jinja2Templates(directory=str(Path(__file__).parents[1]/"app/templates"))
    templates.env.filters["as_utc_iso"]=as_utc_iso
    app.include_router(create_router(templates))
    def session():
        with factory() as db:yield db
    app.dependency_overrides[get_db]=session
    with TestClient(app) as client:yield client


def test_policy_validation_and_optimistic_concurrency(client,env):
    body={"revision":1,"video_id":"hevc","audio_id":"opus","audio_downmix":"stereo"}
    response=client.put("/api/automatic-encoder/releases/42",json=body)
    assert response.status_code==200
    assert response.json()["revision"]==2
    assert response.json()["mode"]=="custom"
    assert response.json()["video_label"]=="HEVC"
    assert response.json()["audio_label"]=="Opus"
    assert client.put("/api/automatic-encoder/releases/42",json=body).status_code==409
    body.update(revision=2,audio_id="copy")
    assert client.put("/api/automatic-encoder/releases/42",json=body).status_code==400
    body.update(video_id=None,audio_id=None)
    assert client.put("/api/automatic-encoder/releases/42",json=body).status_code==200
    assert client.get("/api/automatic-encoder/releases/42").json()["video_id"] is None


def test_return_to_automatic_wakes_waiting_candidates_without_encoder_lookup(client,env,monkeypatch):
    factory,root=env
    with factory() as db:
        source(db,root)
        item=db.scalar(select(EncoderAutoItem))
        item.state,item.block_reason="needs_attention","preset_changed"
        db.commit()
    from app.services import encoder_submission
    async def forbidden(*args,**kwargs):
        pytest.fail("Returning to automatic must not depend on Kensetsu availability")
    monkeypatch.setattr(encoder_submission,"video_kensetsu_list_presets",forbidden)
    response=client.put("/api/automatic-encoder/releases/42",json={
        "revision":1,"video_id":None,"audio_id":None,"audio_downmix":"stereo"})
    assert response.status_code==200
    data=response.json()
    assert data["mode"]=="automatic" and data["audio_downmix"]=="none"
    assert data["video_label"] is None and data["audio_label"] is None
    with factory() as db:
        policy=db.get(ReleaseEncoderPolicy,42)
        assert policy.participating and policy.presets_json=={} and policy.checked_at is None
        item=db.scalar(select(EncoderAutoItem))
        assert (item.state,item.block_reason)==("waiting","settings")
    body=client.get("/automatic-encoder?release_id=42").text
    assert "настройки Kensetsu по умолчанию" in body
    assert "Требуется пресет" not in body


def test_policy_rejects_partial_selection_after_trimming(client,env):
    response=client.put("/api/automatic-encoder/releases/42",json={
        "revision":1,"video_id":"   ","audio_id":"opus"})
    assert response.status_code==422
    assert client.get("/api/automatic-encoder/releases/42").json()["revision"]==1


def test_policy_for_unconfigured_release_reports_automatic(client):
    data=client.get("/api/automatic-encoder/releases/99").json()
    assert data["mode"]=="automatic" and data["revision"]==0
    assert data["video_id"] is None and data["audio_id"] is None


def test_existing_selection_exact_identity_and_page_limit(client,env):
    factory,root=env
    with factory() as db:
        for i in range(32):
            source(db,root,path="Show/ep"+str(i)+".mkv",ready=False)
        for record in db.scalars(select(EncoderAutoSource)).all():record.baseline=True
        db.commit()
    response=client.get("/automatic-encoder?release_id=42")
    assert response.status_code==200
    assert response.text.count('class="auto-backfill-check"')==30
    assert "Следующие файлы" in response.text
    second=client.get("/automatic-encoder?release_id=42&preview_page=2")
    assert second.text.count('class="auto-backfill-check"')==2
    with factory() as db:
        from app.db.models import TorrentFile
        file=db.scalar(select(TorrentFile))
        selection={"file_id":file.id,"info_hash":file.info_hash,"relative_path":file.relative_path}
    wrong={**selection,"info_hash":"a"*40}
    assert client.post("/api/automatic-encoder/releases/42/backfill",json={"revision":1,"files":[wrong]}).status_code==409
    response=client.post("/api/automatic-encoder/releases/42/backfill",json={"revision":1,"files":[selection]})
    assert response.status_code==200 and response.json()["queued"]==1
    with factory() as db:
        assert db.scalar(select(EncoderAutoItem)).reason=="explicit_backfill"


def test_page_escapes_title_and_source_and_never_posts(client,env,monkeypatch):
    factory,root=env
    from app.db.models import TrackedRelease
    with factory() as db:
        db.get(TrackedRelease,42).title='<img src=x onerror="alert(1)">'
        source(db,root,path='Show/<script>alert(1)</script>.mkv',ready=False)
    response=client.get("/automatic-encoder?release_id=42")
    assert response.status_code==200
    assert '<img src=x' not in response.text
    assert "&lt;img" in response.text
    assert "&lt;script&gt;" in response.text
    assert 'data-ui-sse-channel="automatic_encoder"' in response.text


def test_policy_change_after_preset_lookup_rejects_other_server(client,env,monkeypatch):
    factory,_=env
    from app.services import encoder_submission
    from app.services.runtime_settings import upsert_setting
    async def presets(*args,**kwargs):
        with factory() as db:
            upsert_setting(db,"video_kensetsu_base_url","http://new.test");db.commit()
        return LAYERS
    monkeypatch.setattr(encoder_submission,"video_kensetsu_list_presets",presets)
    response=client.put("/api/automatic-encoder/releases/42",json={
        "revision":1,"video_id":"hevc","audio_id":"opus","audio_downmix":"none"})
    assert response.status_code==409
