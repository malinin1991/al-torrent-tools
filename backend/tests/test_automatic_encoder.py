"""Automatic source boundaries and real SQL receipts, with external HTTP isolated."""
import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from blake3 import blake3
import httpx
import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models import (
    DiskFileHash, EncoderAutoItem, EncoderAutoSource, EncoderFileReadiness, EncoderSubmission,
    EncoderSubmissionItem, Job, ReleaseEncoderPolicy, Setting, TorrentArchive, TorrentFile, TrackedRelease,
)
from app.services import automatic_encoder as auto, encoder_history as history, encoder_submission as sender
from app.services.encoder_receipts import recover_receipts
from app.services.runtime_settings import upsert_setting
from app.services.telegram_notify import set_tracked_enabled, upsert_tracked_release
from app.utils.datetime_fmt import utcnow


@compiles(JSONB, "sqlite")
def jsonb_sqlite(*args, **kwargs):
    return "JSON"


LAYERS = {"videos":[{"id":"hevc", "name":"HEVC", "crf":23}],
          "audios":[{"id":"opus", "name":"Opus", "available":True},
                    {"id":"copy", "name":"Copy", "available":True}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "automatic.sqlite"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, autoflush=False)
    from app.core.config import settings
    monkeypatch.setattr(settings, "anilibria_media_root", str(tmp_path))
    async def presets(*args, **kwargs):
        return LAYERS
    monkeypatch.setattr(sender, "video_kensetsu_list_presets", presets)
    with factory() as db:
        for key, value in ((auto.AUTO_KEY,"true"),("video_kensetsu_enabled","true"),
                           ("video_kensetsu_base_url","http://encoder.test")):
            upsert_setting(db, key, value)
        upsert_tracked_release(db, release_id=42, release_alias="test", commit=False)
        auto.activate(db)
        policy = db.get(ReleaseEncoderPolicy,42)
        policy.video_id, policy.audio_id = "hevc", "opus"
        policy.presets_json = {"video":LAYERS["videos"][0], "audio":LAYERS["audios"][0]}
        policy.revision = 1
        db.commit()
    yield factory, tmp_path
    engine.dispose()


def source(db, root, *, release_id=42, codec="AVC", path="Show/ep01.mkv", status="new", ready=True, content=b"video", info_hash=None):
    info_hash = info_hash or uuid4().hex + "a"*8
    full = root / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(content)
    archive = TorrentArchive(release_id=release_id, torrent_id=hash(info_hash) % 1000000,
        info_hash=info_hash, file_path=info_hash+".torrent", quality_json={"codec":{"value":codec}},
        torrent_type=codec+" 1080p WEBRip")
    auto.lock(db)
    db.add(archive)
    auto.observe_archive(db, archive)
    file = TorrentFile(release_id=release_id, torrent_id=archive.torrent_id, info_hash=info_hash,
        relative_path=path, full_path=str(full), size=len(content), ui_status=status, selected=True,
        media_present=True)
    db.add(file)
    db.flush()
    if ready:
        disk = db.scalar(select(DiskFileHash).where(DiskFileHash.full_path==str(full)))
        if disk is None:
            disk=DiskFileHash(full_path=str(full))
            db.add(disk)
        disk.size, disk.mtime = full.stat().st_size, full.stat().st_mtime
        disk.content_hash, disk.hash_algo = blake3(content).hexdigest(), "blake3"
        db.flush()
        auto.record_readiness(db,file,disk)
    db.commit()
    return file, archive


def test_activation_captures_versions_even_without_composition(env):
    factory, root = env
    with factory() as db:
        # Recreate first enable with an archive whose files are not known yet.
        db.query(EncoderAutoSource).delete()
        db.get(ReleaseEncoderPolicy,42).participating=False
        archive=TorrentArchive(release_id=42,torrent_id=1,info_hash="b"*40,file_path="x")
        db.add(archive)
        db.flush()
        auto.tracking_changed(db,42,True)
        db.commit()
        assert db.scalar(select(EncoderAutoSource)).baseline
        file=TorrentFile(release_id=42,torrent_id=1,info_hash="b"*40,relative_path="late.mkv",ui_status="new")
        db.add(file);db.flush()
        assert auto.register_file(db,file) is None
        assert auto.register_file(db,file,explicit=True) is not None


def test_automatic_preset_preserves_future_candidate_and_pause(env):
    factory, root=env
    with factory() as db:
        f,_=source(db,root)
        item=db.scalar(select(EncoderAutoItem))
        p=db.get(ReleaseEncoderPolicy,42);p.video_id=None;db.commit()
        assert auto.assess(db,item)==("waiting","preset")
        p.audio_id=None;db.commit()
        assert auto.assess(db,item)==("ready","ready")
        upsert_setting(db,auto.AUTO_KEY,"false");db.commit()
        assert auto.assess(db,item)==("waiting","paused")
        f2,_=source(db,root,path="Show/ep02.mkv")
        assert db.scalar(select(func.count()).select_from(EncoderAutoItem))==2
        upsert_setting(db,auto.AUTO_KEY,"true");auto.activate(db);db.commit()
        assert auto.assess(db,item)==("ready","ready")
        assert db.get(ReleaseEncoderPolicy,42).generation==1


def automatic_policy(db):
    policy = db.get(ReleaseEncoderPolicy, 42)
    policy.video_id = policy.audio_id = None
    policy.presets_json = {}
    policy.revision += 1
    db.commit()
    return policy


def test_unassigned_policy_uses_current_defaults_and_saves_actual_receipt(env, monkeypatch):
    factory, root = env
    with factory() as db:
        automatic_policy(db)
        # This old field is not an override in automatic mode.
        db.get(ReleaseEncoderPolicy, 42).audio_downmix = "stereo"
        source(db, root)
        item = db.scalar(select(EncoderAutoItem))
        item.state, item.block_reason = "waiting", "preset"
        db.commit()
    defaults = {"video_id": "hevc", "audio_id": "copy", "audio_downmix": "none"}
    calls = []
    async def presets(*args, **kwargs):
        assert factory.kw["bind"].pool.checkedout() == 0
        assert kwargs["strict_defaults"] is True
        return {**LAYERS, "defaults": dict(defaults)}
    async def encode(*args, **kwargs):
        calls.append(kwargs)
        return {"status_code": 200, "body": {"job": {"id": uuid4().hex, "input_path": kwargs["paths"][0]}}}
    monkeypatch.setattr(sender, "video_kensetsu_list_presets", presets)
    monkeypatch.setattr(sender, "video_kensetsu_encode", encode)
    asyncio.run(auto.tick(factory))
    with factory() as db:
        group = db.scalar(select(EncoderSubmission))
        assert (group.video_id, group.audio_id, group.audio_downmix) == ("hevc", "copy", "none")
        assert group.presets_json == {"video": LAYERS["videos"][0], "audio": LAYERS["audios"][1]}
        assert group.payload_json["audio_id"] == "copy"
        assert db.scalar(select(EncoderAutoItem)).state == "accepted"
        policy = db.get(ReleaseEncoderPolicy, 42)
        assert policy.video_id is policy.audio_id is None
        assert policy.presets_json == {}
        source(db, root, path="Show/ep02.mkv")
    defaults.update(audio_id="opus", audio_downmix="stereo")
    asyncio.run(auto.tick(factory))
    assert [(c["audio_id"], c["audio_downmix"]) for c in calls] == [("copy", "none"), ("opus", "stereo")]
    # Replaying the original request must not look at the new default at all.
    async def forbidden(*args, **kwargs):
        pytest.fail("A stored receipt must be replayed before resolving defaults")
    monkeypatch.setattr(sender, "video_kensetsu_list_presets", forbidden)
    with factory() as db:
        first = db.scalar(select(EncoderSubmission).order_by(EncoderSubmission.id))
        body = SimpleNamespace(video_id=None, audio_id=None, audio_downmix="none", request_id=first.request_id,
                               retry_of=None, allow_repeat=False, acknowledge_unknown=False)
        response = asyncio.run(sender.submit_files([first.items[0].file_id], body, db, auto_item_id=first.auto_item_id))
        assert response.status_code == 200


@pytest.mark.parametrize("defaults", [None, {}, {"video_id": "hevc", "audio_id": "copy"},
    {"video_id": "missing", "audio_id": "copy", "audio_downmix": "none"},
    {"video_id": "hevc", "audio_id": "missing", "audio_downmix": "none"},
    {"video_id": "hevc", "audio_id": "copy", "audio_downmix": "invalid"},
    {"video_id": "hevc", "audio_id": "copy", "audio_downmix": "stereo"},
    {"video_id": 12, "audio_id": "copy", "audio_downmix": "none"}])
def test_missing_or_invalid_defaults_do_not_invent_a_preset(env, monkeypatch, defaults):
    factory, root = env
    with factory() as db:
        automatic_policy(db)
        source(db, root)
    async def presets(*args, **kwargs):
        return {**LAYERS, "defaults": defaults}
    async def forbidden(*args, **kwargs):
        pytest.fail("Invalid defaults must not cause a POST")
    monkeypatch.setattr(sender, "video_kensetsu_list_presets", presets)
    monkeypatch.setattr(sender, "video_kensetsu_encode", forbidden)
    asyncio.run(auto.tick(factory))
    with factory() as db:
        assert db.scalar(select(EncoderAutoItem)).state == "waiting"
        assert db.scalar(select(EncoderAutoItem)).block_reason == "server"
        assert db.scalar(select(EncoderSubmission)) is None


def test_unavailable_default_audio_does_not_send(env, monkeypatch):
    factory, root = env
    with factory() as db:
        automatic_policy(db)
        source(db, root)
    async def presets(*args, **kwargs):
        return {**LAYERS, "audios": [{"id": "opus", "available": False}],
                "defaults": {"video_id": "hevc", "audio_id": "opus", "audio_downmix": "none"}}
    async def forbidden(*args, **kwargs):
        pytest.fail("Unavailable default audio must not cause a POST")
    monkeypatch.setattr(sender, "video_kensetsu_list_presets", presets)
    monkeypatch.setattr(sender, "video_kensetsu_encode", forbidden)
    asyncio.run(auto.tick(factory))
    with factory() as db:
        assert db.scalar(select(EncoderAutoItem)).block_reason == "server"
        assert db.scalar(select(EncoderSubmission)) is None


def test_assignment_during_default_lookup_fences_intent(env, monkeypatch):
    factory, root = env
    with factory() as db:
        automatic_policy(db)
        source(db, root)
    async def presets(*args, **kwargs):
        with factory() as db:
            policy = db.get(ReleaseEncoderPolicy, 42)
            policy.video_id, policy.audio_id = "hevc", "opus"
            policy.presets_json = {"video": LAYERS["videos"][0], "audio": LAYERS["audios"][0]}
            policy.revision += 1
            db.commit()
        return {**LAYERS, "defaults": {"video_id": "hevc", "audio_id": "copy", "audio_downmix": "none"}}
    async def forbidden(*args, **kwargs):
        pytest.fail("Changed assignment must not cause a POST")
    monkeypatch.setattr(sender, "video_kensetsu_list_presets", presets)
    monkeypatch.setattr(sender, "video_kensetsu_encode", forbidden)
    asyncio.run(auto.tick(factory))
    with factory() as db:
        assert db.scalar(select(EncoderSubmission)).request_started_at is None


def test_untrack_during_default_lookup_keeps_candidate_cancelled(env, monkeypatch):
    factory, root = env
    with factory() as db:
        automatic_policy(db)
        source(db, root)
    async def presets(*args, **kwargs):
        with factory() as db:
            set_tracked_enabled(db, 42, False)
        return {**LAYERS, "defaults": {"video_id": "hevc", "audio_id": "copy", "audio_downmix": "none"}}
    async def forbidden(*args, **kwargs):
        pytest.fail("Untracked release must not cause a POST")
    monkeypatch.setattr(sender, "video_kensetsu_list_presets", presets)
    monkeypatch.setattr(sender, "video_kensetsu_encode", forbidden)
    asyncio.run(auto.tick(factory))
    with factory() as db:
        item = db.scalar(select(EncoderAutoItem))
        assert (item.state, item.block_reason) == ("cancelled", "untracked")
        assert item.claim_token is item.lease_until is None
        assert db.scalar(select(EncoderSubmission)) is None


def test_automatic_defaults_timeout_recovers_only_by_get(env, monkeypatch):
    factory, root = env
    with factory() as db:
        automatic_policy(db)
        source(db, root)
    calls = []
    async def presets(*args, **kwargs):
        return {**LAYERS, "defaults": {"video_id": "hevc", "audio_id": "copy", "audio_downmix": "none"}}
    async def encode(*args, **kwargs):
        calls.append(kwargs)
        raise httpx.ReadTimeout("lost response")
    async def receipt(url, rid):
        with factory() as db:
            path = db.scalar(select(EncoderSubmissionItem.full_path))
        return {"status_code": 200, "body": {"request_id": rid, "job": {"id": "recovered-default", "input_path": path}}}
    monkeypatch.setattr(sender, "video_kensetsu_list_presets", presets)
    monkeypatch.setattr(sender, "video_kensetsu_encode", encode)
    monkeypatch.setattr("app.services.encoder_receipts.request_receipt", receipt)
    asyncio.run(auto.tick(factory))
    asyncio.run(auto.tick(factory))
    assert len(calls) == 1
    with factory() as db:
        group = db.scalar(select(EncoderSubmission))
        assert (group.video_id, group.audio_id, group.audio_downmix) == ("hevc", "copy", "none")
        assert db.scalar(select(EncoderAutoItem)).state == "accepted"
        assert db.get(ReleaseEncoderPolicy, 42).video_id is None


@pytest.mark.parametrize("codec,state,reason",[("AVC","ready","ready"),("HEVC","cancelled","not_avc"),("AV1","cancelled","not_avc")])
def test_only_avc_without_quality_selector(env,codec,state,reason):
    factory,root=env
    with factory() as db:
        source(db,root,codec=codec)
        assert auto.assess(db,db.scalar(select(EncoderAutoItem)))==(state,reason)


@pytest.mark.parametrize("guard",["no_hash","checking","active_hash","not_selected","stale","missing"])
def test_unready_source_never_dispatches(env,guard):
    factory,root=env
    with factory() as db:
        file,_=source(db,root,ready=guard!="no_hash")
        if guard=="no_hash":
            auto.reconcile(db)
        if guard=="checking":file.is_checking=True
        if guard=="active_hash":db.add(Job(type="hash_torrent",status="stopping",params_json={"info_hash":file.info_hash}))
        if guard=="not_selected":file.selected=False
        if guard=="stale":Path(file.full_path).write_bytes(b"different bytes")
        if guard=="missing":Path(file.full_path).unlink()
        db.commit()
        item=db.scalar(select(EncoderAutoItem))
        assert auto.assess(db,item)[0]=="waiting"
        assert file.ui_status=="new"


def test_untrack_cancels_and_readd_has_new_baseline(env):
    factory,root=env
    with factory() as db:
        file,_=source(db,root)
        set_tracked_enabled(db,42,False)
        assert db.scalar(select(EncoderAutoItem)).block_reason=="untracked"
        set_tracked_enabled(db,42,True)
        assert db.get(ReleaseEncoderPolicy,42).generation==2
        assert auto.register_file(db,file) is None
        assert db.scalar(select(func.count()).select_from(EncoderAutoItem))==1


def test_tick_submits_once_and_passes_frozen_id_without_db_connection(env,monkeypatch):
    factory,root=env
    with factory() as db:
        file,_=source(db,root)
        fid=file.id
    calls=[]
    async def encode(base_url,**kwargs):
        calls.append(kwargs)
        assert factory.kw["bind"].pool.checkedout()==0
        with factory() as db:
            group=db.scalar(select(EncoderSubmission))
            assert group.request_id==kwargs["request_id"]==group.payload_json["request_id"]
            assert group.origin=="auto" and group.request_started_at
        return {"status_code":200,"body":{"job":{"id":"external1","input_path":kwargs["paths"][0]}}}
    monkeypatch.setattr(sender,"video_kensetsu_encode",encode)
    asyncio.run(auto.tick(factory));asyncio.run(auto.tick(factory))
    assert len(calls)==1
    with factory() as db:
        assert db.scalar(select(EncoderAutoItem)).state=="accepted"
        assert db.get(TorrentFile,fid).ui_status=="new"


def test_timeout_recovers_only_by_get(env,monkeypatch):
    factory,root=env
    with factory() as db:source(db,root)
    calls=[]
    async def encode(*args,**kwargs):
        calls.append(1)
        raise httpx.ReadTimeout("lost response")
    async def receipt(url,rid):
        with factory() as db:
            path=db.scalar(select(EncoderSubmissionItem.full_path))
        return {"status_code":200,"body":{"request_id":rid,"job":{"id":"recovered","input_path":path}}}
    monkeypatch.setattr(sender,"video_kensetsu_encode",encode)
    monkeypatch.setattr("app.services.encoder_receipts.request_receipt",receipt)
    asyncio.run(auto.tick(factory));asyncio.run(auto.tick(factory))
    assert calls==[1]
    with factory() as db:
        item=db.scalar(select(EncoderAutoItem))
        assert item.state=="accepted"
        group=db.get(EncoderSubmission,item.submission_id)
        assert group.error and group.recovery_json["body"]["job"]["id"]=="recovered"


@pytest.mark.parametrize("state",["interrupted","missing","processing"])
def test_unknown_receipt_never_reposts(env,monkeypatch,state):
    factory,root=env
    with factory() as db:source(db,root)
    calls=[]
    async def encode(*args,**kwargs):
        calls.append(1);raise httpx.ReadTimeout("lost")
    async def receipt(url,rid):
        return {"status_code":404 if state=="missing" else 202,
                "body":{} if state=="missing" else {"request_id":rid,"request_status":state}}
    monkeypatch.setattr(sender,"video_kensetsu_encode",encode)
    monkeypatch.setattr("app.services.encoder_receipts.request_receipt",receipt)
    asyncio.run(auto.tick(factory));asyncio.run(auto.tick(factory))
    assert calls==[1]
    with factory() as db:
        assert db.scalar(select(EncoderAutoItem)).state in {"needs_attention","receipt_pending"}


def test_manual_auto_cross_preset_and_republished_content_guard(env):
    factory,root=env
    with factory() as db:
        f,_=source(db,root)
        group,_=history.claim(db,file_ids=[f.id],request_id=uuid4().hex,digest="one",
            base_url="http://one.test",video_id="hevc",audio_id="opus",downmix="none",origin="auto")
        group.items[0].status="accepted";db.commit()
        with pytest.raises(history.SubmissionConflict):
            history.claim(db,file_ids=[f.id],request_id=uuid4().hex,digest="two",
                base_url="http://different.test",video_id="other",audio_id="copy",downmix="none")
        f2,_=source(db,root)
        with pytest.raises(history.SubmissionConflict):
            history.claim(db,file_ids=[f2.id],request_id=uuid4().hex,digest="three",
                base_url="http://one.test",video_id="hevc",audio_id="opus",downmix="none",origin="auto")
        # The same bytes at a new exact path are a new file.
        f3,_=source(db,root,path="Show/other.mkv")
        new,owner=history.claim(db,file_ids=[f3.id],request_id=uuid4().hex,digest="four",
                base_url="http://one.test",video_id="hevc",audio_id="opus",downmix="none",origin="auto")
        assert owner


def test_preset_change_during_prepare_fences_intent(env,monkeypatch):
    factory,root=env
    with factory() as db:source(db,root)
    async def presets(*args,**kwargs):
        with factory() as db:
            db.get(ReleaseEncoderPolicy,42).revision+=1;db.commit()
        return LAYERS
    async def forbidden(*args,**kwargs):
        pytest.fail("Stale revision must not produce a POST")
    monkeypatch.setattr(sender,"video_kensetsu_list_presets",presets)
    monkeypatch.setattr(sender,"video_kensetsu_encode",forbidden)
    asyncio.run(auto.tick(factory))
    with factory() as db:
        assert db.scalar(select(EncoderSubmission)).request_started_at is None


def test_source_change_during_post_preserves_receipt_without_retry(env,monkeypatch):
    factory,root=env
    with factory() as db:source(db,root)
    async def encode(*args,**kwargs):
        Path(kwargs["paths"][0]).write_bytes(b"new version during copy")
        return {"status_code":200,"body":{"job":{"id":"accepted","input_path":kwargs["paths"][0]}}}
    monkeypatch.setattr(sender,"video_kensetsu_encode",encode)
    asyncio.run(auto.tick(factory))
    with factory() as db:
        assert db.scalar(select(EncoderAutoItem)).block_reason=="source_conflict"
        assert db.scalar(select(EncoderSubmissionItem)).status=="accepted"


@pytest.mark.parametrize("started",[False,True])
def test_crashed_claim_restarts_only_before_durable_intent(env,monkeypatch,started):
    factory,root=env
    with factory() as db:
        file,_=source(db,root)
        auto.reconcile(db)
        item_id,token=auto.claim_next(db)
        group,_=history.claim(db,file_ids=[file.id],request_id=uuid4().hex,digest="crash",
            base_url="http://encoder.test",video_id="hevc",audio_id="opus",downmix="none",
            origin="auto",auto_item_id=item_id,claim_token=token)
        group.expires_at=utcnow()-timedelta(seconds=1)
        if started:
            group.request_started_at=utcnow()-timedelta(seconds=700)
            group.payload_json={"path":file.full_path,"request_id":group.request_id}
            group.receipt_next_at=utcnow()-timedelta(seconds=1)
        item=db.get(EncoderAutoItem,item_id)
        item.lease_until=item.next_attempt_at=utcnow()-timedelta(seconds=1)
        db.commit()
    calls=[]
    async def encode(*args,**kwargs):
        calls.append(1)
        return {"status_code":200,"body":{"job":{"id":"new","input_path":kwargs["paths"][0]}}}
    async def missing(*args,**kwargs):
        return {"status_code":404,"body":{}}
    monkeypatch.setattr(sender,"video_kensetsu_encode",encode)
    monkeypatch.setattr("app.services.encoder_receipts.request_receipt",missing)
    asyncio.run(auto.tick(factory))
    assert len(calls)==(0 if started else 1)
    with factory() as db:
        assert db.scalar(select(EncoderAutoItem)).state==("needs_attention" if started else "accepted")


def test_untrack_during_preflight_cancels_without_post(env,monkeypatch):
    factory,root=env
    with factory() as db:source(db,root)
    async def presets(*args,**kwargs):
        with factory() as db:set_tracked_enabled(db,42,False)
        return LAYERS
    async def forbidden(*args,**kwargs):pytest.fail("Untracked source must not be sent")
    monkeypatch.setattr(sender,"video_kensetsu_list_presets",presets)
    monkeypatch.setattr(sender,"video_kensetsu_encode",forbidden)
    asyncio.run(auto.tick(factory))
    with factory() as db:
        item=db.scalar(select(EncoderAutoItem))
        assert item.state=="cancelled" and item.block_reason=="untracked"
        assert db.scalar(select(EncoderSubmission)).request_started_at is None


def test_changed_remote_preset_requires_resaving_policy(env,monkeypatch):
    factory,root=env
    with factory() as db:source(db,root)
    async def presets(*args,**kwargs):
        return {**LAYERS,"videos":[{**LAYERS["videos"][0],"crf":20}]}
    async def forbidden(*args,**kwargs):pytest.fail("Changed preset must not be used silently")
    monkeypatch.setattr(sender,"video_kensetsu_list_presets",presets)
    monkeypatch.setattr(sender,"video_kensetsu_encode",forbidden)
    asyncio.run(auto.tick(factory))
    with factory() as db:
        item=db.scalar(select(EncoderAutoItem))
        assert item.state=="needs_attention" and item.block_reason=="preset_changed"


def test_hash_proof_repair_uses_normal_job_with_cooldown(env):
    factory,root=env
    class Runner:
        def __init__(self):self.params=[]
        def create_job(self,db,kind,params):
            self.params.append(params)
            job=Job(type=kind,params_json=params,status="cancelled")
            db.add(job);db.commit()
            return job
    runner=Runner()
    with factory() as db:
        source(db,root,ready=False)
        auto.reconcile(db)
        assert auto.plan_hash_checks(db,runner)
        assert auto.plan_hash_checks(db,runner)==[]
        assert runner.params[0]["release_id"]==42


def test_partial_manual_receipt_does_not_accept_another_auto_source(env):
    factory,root=env
    with factory() as db:
        first,_=source(db,root,path="Show/one.mkv")
        second,_=source(db,root,path="Show/two.mkv")
        group,_=history.claim(db,file_ids=[first.id,second.id],request_id=uuid4().hex,digest="partial",
            base_url="http://encoder.test",video_id="hevc",audio_id="opus",downmix="none")
        group.items[0].status="accepted"
        group.items[1].status="unknown"
        db.commit()
        item=db.scalar(select(EncoderAutoItem).where(EncoderAutoItem.file_id==second.id))
        assert auto.assess(db,item)==("needs_attention","unknown")
        auto.settle_item(db,item,group)
        assert item.state=="needs_attention" and item.receipt_item_id==group.items[1].id


def test_timeout_and_late_receipt_preserve_source_conflict(env,monkeypatch):
    factory,root=env
    with factory() as db:source(db,root)
    calls=[]
    async def encode(*args,**kwargs):
        calls.append(1)
        Path(kwargs["paths"][0]).write_bytes(b"replaced while remote copied")
        raise httpx.ReadTimeout("lost")
    async def receipt(url,rid):
        with factory() as db:path=db.scalar(select(EncoderSubmissionItem.full_path))
        return {"status_code":200,"body":{"request_id":rid,"job":{"id":"remote","input_path":path}}}
    monkeypatch.setattr(sender,"video_kensetsu_encode",encode)
    monkeypatch.setattr("app.services.encoder_receipts.request_receipt",receipt)
    asyncio.run(auto.tick(factory))
    with factory() as db:
        assert db.scalar(select(EncoderAutoItem)).block_reason=="source_conflict"
        assert db.scalar(select(EncoderSubmissionItem)).status=="accepted"
        file,_=source(db,root,content=b"replaced while remote copied")
        auto.reconcile(db)
        item=db.scalar(select(EncoderAutoItem).where(EncoderAutoItem.file_id==file.id))
        assert item.state=="needs_attention" and item.block_reason=="source_conflict"
    asyncio.run(auto.tick(factory))
    assert calls==[1]


def test_each_release_keeps_own_preset_for_all_avc_variants(env,monkeypatch):
    factory,root=env
    with factory() as db:
        db.get(ReleaseEncoderPolicy,42).audio_downmix="stereo"
        upsert_tracked_release(db,release_id=43,release_alias="second",commit=False)
        other=db.get(ReleaseEncoderPolicy,43)
        other.video_id,other.audio_id,other.revision="hevc","copy",1
        other.presets_json={"video":LAYERS["videos"][0],"audio":LAYERS["audios"][1]}
        db.commit()
        source(db,root,path="First/1080/ep.mkv")
        source(db,root,path="First/720/ep.mkv")
        source(db,root,release_id=43,path="Second/1080/ep.mkv")
    calls=[]
    async def encode(*args,**kwargs):
        calls.append(kwargs)
        return {"status_code":200,"body":{"job":{"id":uuid4().hex,"input_path":kwargs["paths"][0]}}}
    monkeypatch.setattr(sender,"video_kensetsu_encode",encode)
    for _ in range(3):asyncio.run(auto.tick(factory))
    assert len(calls)==3
    for call in calls:
        if "/First/" in call["paths"][0]:
            assert (call["audio_id"],call["audio_downmix"])==("opus","stereo")
        else:
            assert (call["audio_id"],call["audio_downmix"])==("copy","none")


def test_shared_cooldown_updates_reuse_pending_setting(env):
    factory,_=env
    with factory() as db:
        upsert_setting(db,"encoder_auto_server_retry_at","later")
        upsert_setting(db,"encoder_auto_server_retry_at","")
        db.commit()
        assert db.get(Setting,"encoder_auto_server_retry_at").value==""
