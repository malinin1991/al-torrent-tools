"""Isolated delivery state-machine tests and real PostgreSQL claim races."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import os
import threading
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import PipelineEvent, Setting, TelegramBotAccess, TelegramOutbox, TorrentPipeline, TrackedRelease
from app.services import telegram_outbox as out
from app.services.telegram_access import cancel_pending_hevc_outbox
from app.services.telegram_notify import enqueue_pipeline_telegram_notification

NOW = datetime(2026, 10, 13, 12)
TOKEN = "test-nonstandard-secret"


@compiles(JSONB, "sqlite")
def jsonb_sqlite(*args, **kwargs):
    return "JSON"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def configure(db, bot_key="primary", enabled=True):
    prefix = "telegram" if bot_key == "primary" else "telegram_hevc"
    for key, value in {f"{prefix}_enabled": str(enabled).lower(), f"{prefix}_bot_token": TOKEN}.items():
        db.merge(Setting(key=key, value=value))
    db.commit()


def enqueue(db, *, bot_key="primary", status="pending", **kw):
    row = TelegramOutbox(bot_key=bot_key, chat_id="-100", payload_json={"text": "test event"},
                         status=status, created_at=NOW, **kw)
    db.add(row)
    db.commit()
    return row


def pipeline(db, tg_status="queued"):
    row = TorrentPipeline(release_id=1, torrent_id=10, info_hash="a"*40, status="done", tg_status=tg_status)
    db.add(row)
    db.commit()
    return row


def approve(db, chat_id=-100):
    db.add(TelegramBotAccess(bot_key="hevc", subject_type="chat", telegram_id=chat_id,
                            status="approved", created_at=NOW, updated_at=NOW))
    db.commit()


def configure_tracking(db):
    configure(db)
    db.merge(Setting(key="telegram_chat_id", value="-100"))
    db.merge(TrackedRelease(release_id=1, release_alias="test", title="Test", enabled=True, source="ui"))
    db.commit()


@pytest.mark.parametrize("status,kind", [("failed", "delivery_unknown"), ("failed", "permanent"), ("cancelled", "acl_rejected")])
def test_primary_producer_never_recreates_terminal_event_even_with_stale_pipeline(db, status, kind):
    configure_tracking(db)
    pipe = pipeline(db, tg_status="skipped")
    with Session(db.get_bind(), expire_on_commit=False) as stale:
        stale_pipe = stale.get(TorrentPipeline, pipe.id)
        assert stale_pipe.tg_status == "skipped"
        enqueue_pipeline_telegram_notification(db, pipe)
        row = db.scalar(select(TelegramOutbox))
        assert row.dedupe_key == f"primary_pipeline:{pipe.id}"
        claim = out.claim_next_outbox(db, bot_key="primary", now=NOW)
        assert out.finalize_outbox(db, row.id, claim.token, status=status, error_kind=kind, now=NOW)
        # Fresh status guard and legacy/stale-object guard both forbid revival.
        enqueue_pipeline_telegram_notification(db, pipe)
        db.merge(Setting(key="telegram_chat_id", value="-200"))
        db.commit()
        enqueue_pipeline_telegram_notification(stale, stale_pipe)
        assert db.scalar(select(func.count()).select_from(TelegramOutbox)) == 1
        assert out.claim_next_outbox(db, bot_key="primary", now=NOW) is None
        assert row.status == status


@pytest.mark.parametrize("status", ["pending", "sending", "sent", "failed", "cancelled"])
def test_primary_legacy_null_dedupe_history_blocks_new_event(db, status):
    configure_tracking(db)
    pipe = pipeline(db, tg_status="skipped")
    row = enqueue(db, pipeline_id=pipe.id, status=status, dedupe_key=None)
    enqueue_pipeline_telegram_notification(db, pipe)
    assert db.scalar(select(func.count()).select_from(TelegramOutbox)) == 1
    assert row.dedupe_key is None and row.status == status


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_primary_terminal_pipeline_guard_does_not_create_new_event(db, status):
    configure_tracking(db)
    pipe = pipeline(db, tg_status=status)
    enqueue_pipeline_telegram_notification(db, pipe)
    assert db.scalar(select(func.count()).select_from(TelegramOutbox)) == 0


def test_explicit_operational_reset_allows_new_pipeline_notification(db):
    from app.services.db_maintenance import reset_operational_state
    configure_tracking(db)
    pipe = pipeline(db, tg_status="skipped")
    enqueue_pipeline_telegram_notification(db, pipe)
    assert reset_operational_state(db)["outbox_removed"] == 1
    db.expunge_all()
    new_pipe = pipeline(db, tg_status="skipped")
    enqueue_pipeline_telegram_notification(db, new_pipe)
    assert db.scalar(select(func.count()).select_from(TelegramOutbox)) == 1
    assert out.claim_next_outbox(db, bot_key="primary", now=NOW) is not None


def test_claims_one_item_and_stale_owner_cannot_finalize(db):
    a, b = enqueue(db), enqueue(db)
    first = out.claim_next_outbox(db, bot_key="primary", now=NOW)
    assert first.id == a.id
    assert a.status == "sending" and b.status == "pending"
    assert first.attempts == 1 and a.lease_until == NOW + timedelta(seconds=120)
    assert not out.finalize_outbox(db, a.id, "wrong", status="sent", now=NOW)
    assert not out.renew_claim(db, first, now=NOW + timedelta(seconds=120))
    assert not out.finalize_outbox(db, a.id, first.token, status="sent", now=NOW + timedelta(seconds=120))
    assert out.recover_expired_claims(db, bot_key="primary", now=NOW + timedelta(seconds=121)) == 1
    assert a.status == "failed" and a.last_error_kind == "delivery_unknown"
    assert not out.finalize_outbox(db, a.id, first.token, status="sent", now=NOW)
    assert out.claim_next_outbox(db, bot_key="primary", now=NOW + timedelta(seconds=121)).id == b.id


def test_recovery_manual_retry_requires_ack_and_preserves_dedupe_and_attempts(db):
    configure(db)
    row = enqueue(db, dedupe_key="stable:event")
    claim = out.claim_next_outbox(db, bot_key="primary", now=NOW)
    out.recover_expired_claims(db, bot_key="primary", now=NOW + timedelta(seconds=121))
    with pytest.raises(out.OutboxRetryError) as exc:
        out.retry_failed_outbox(db, row.id, now=NOW)
    assert exc.value.status_code == 422
    result = out.retry_failed_outbox(db, row.id, acknowledge_unknown=True, now=NOW)
    assert result["status"] == "pending" and result["attempts"] == 1
    assert row.dedupe_key == "stable:event"
    replacement = out.claim_next_outbox(db, bot_key="primary", now=NOW)
    assert replacement.token != claim.token and replacement.attempts == 2
    assert not out.finalize_outbox(db, row.id, claim.token, status="sent", now=NOW)
    assert out.finalize_outbox(db, row.id, replacement.token, status="sent", message_id=8, now=NOW)
    assert row.telegram_message_id == 8
    with pytest.raises(out.OutboxRetryError):
        out.retry_failed_outbox(db, row.id, acknowledge_unknown=True)


@pytest.mark.parametrize("state", ["pending", "sending", "sent", "cancelled"])
def test_retry_never_resurrects_nonfailed(db, state):
    configure(db)
    row = enqueue(db, status=state)
    with pytest.raises(out.OutboxRetryError) as exc:
        out.retry_failed_outbox(db, row.id)
    assert exc.value.code == "state"


def test_retry_disabled_and_acl_blocked(db):
    configure(db, "hevc")
    row = enqueue(db, bot_key="hevc", status="failed", last_error_kind="permanent")
    with pytest.raises(out.OutboxRetryError) as exc:
        out.retry_failed_outbox(db, row.id)
    assert exc.value.code == "acl"
    approve(db)
    configure(db, "hevc", enabled=False)
    with pytest.raises(out.OutboxRetryError) as exc:
        out.retry_failed_outbox(db, row.id)
    assert exc.value.code == "disabled"


def test_acl_cancellation_invalidates_inflight_token(db):
    approve(db)
    row = enqueue(db, bot_key="hevc")
    claim = out.claim_next_outbox(db, bot_key="hevc", now=NOW)
    assert cancel_pending_hevc_outbox(db, -100) == 1
    db.commit()
    assert row.status == "cancelled" and row.claim_token is None
    assert not out.renew_claim(db, claim, now=NOW)
    assert not out.finalize_outbox(db, row.id, claim.token, status="sent", now=NOW)


def test_sent_and_audit_are_atomic_and_hevc_does_not_change_primary(db, monkeypatch):
    pipe = pipeline(db)
    primary = enqueue(db, pipeline_id=pipe.id)
    claim = out.claim_next_outbox(db, bot_key="primary", now=NOW)
    assert out.finalize_outbox(db, primary.id, claim.token, status="sent", message_id=3, now=NOW)
    assert pipe.tg_status == "sent"
    event = db.scalar(select(PipelineEvent))
    assert event.event_type == "tg_sent" and event.details_json["bot_key"] == "primary"
    hevc = enqueue(db, bot_key="hevc", pipeline_id=pipe.id)
    hevc_claim = out.claim_next_outbox(db, bot_key="hevc", now=NOW)
    assert out.finalize_outbox(db, hevc.id, hevc_claim.token, status="failed", error_kind="permanent", now=NOW)
    assert pipe.tg_status == "sent"
    late_primary = enqueue(db, pipeline_id=pipe.id)
    late_claim = out.claim_next_outbox(db, bot_key="primary", now=NOW)
    assert out.finalize_outbox(db, late_primary.id, late_claim.token, status="failed", error_kind="permanent", now=NOW)
    assert pipe.tg_status == "sent"


def test_failed_commit_rolls_back_receipt_pipeline_and_audit(db, monkeypatch):
    pipe = pipeline(db)
    row = enqueue(db, pipeline_id=pipe.id)
    claim = out.claim_next_outbox(db, bot_key="primary", now=NOW)
    def fail_commit():
        raise RuntimeError("mock commit failure")
    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="commit"):
        out.finalize_outbox(db, row.id, claim.token, status="sent", now=NOW)
    assert row.status == "sending" and pipe.tg_status == "queued"
    assert db.scalar(select(func.count()).select_from(PipelineEvent)) == 0


def test_first20_errors_do_not_starve_new_items_and_no_transaction_during_http(db, monkeypatch):
    configure(db)
    rows = [enqueue(db) for _ in range(21)]
    attempts = []
    async def send(**kw):
        assert not db.in_transaction()
        item = db.get(TelegramOutbox, rows[len(attempts)].id)
        db.commit()
        attempts.append(item.id)
        if item.id <= rows[19].id:
            raise out.TelegramDeliveryError("retry later", kind="temporary")
        return 1
    monkeypatch.setattr(out, "send_outbox_message", send)
    monkeypatch.setattr(out, "utcnow", lambda: NOW)
    assert asyncio.run(out.drain_outbox(db)) == {"sent": 0, "failed": 20, "skipped": 0}
    assert rows[20].attempts == 0 and rows[0].next_attempt_at > NOW
    assert asyncio.run(out.drain_outbox(db)) == {"sent": 1, "failed": 0, "skipped": 0}
    assert rows[20].status == "sent"
    assert all(row.attempts == 1 for row in rows)


def test_429_persists_profile_cooldown_for_new_enqueue_and_separate_bot(db, monkeypatch):
    configure(db)
    first = enqueue(db)
    async def send(**kw):
        raise out.TelegramDeliveryError("Too Many Requests", kind="rate_limited", retry_after=300, api_code=429)
    monkeypatch.setattr(out, "send_outbox_message", send)
    monkeypatch.setattr(out, "utcnow", lambda: NOW)
    assert asyncio.run(out.drain_outbox(db))["failed"] == 1
    new = enqueue(db)
    assert first.next_attempt_at == NOW + timedelta(seconds=300)
    assert out.claim_next_outbox(db, bot_key="primary", now=NOW) is None
    out.set_bot_cooldown(db, bot_key="primary", until=NOW + timedelta(seconds=30))
    assert out.claim_next_outbox(db, bot_key="primary", now=NOW + timedelta(seconds=299)) is None
    hevc = enqueue(db, bot_key="hevc")
    assert out.claim_next_outbox(db, bot_key="hevc", now=NOW).id == hevc.id
    summary = out.outbox_summary(db, bot_key="primary", now=NOW)
    assert summary["ready"] == 0 and summary["deferred"] == 2
    assert out.claim_next_outbox(db, bot_key="primary", now=NOW + timedelta(seconds=300)).id == new.id


def test_401_stops_batch_and_disabled_profile_does_not_claim(db, monkeypatch):
    configure(db)
    a, b = enqueue(db), enqueue(db)
    calls = []
    async def send(**kw):
        calls.append(1)
        raise out.TelegramDeliveryError("bad token", kind="configuration", api_code=401)
    monkeypatch.setattr(out, "send_outbox_message", send)
    monkeypatch.setattr(out, "utcnow", lambda: NOW)
    asyncio.run(out.drain_outbox(db))
    assert len(calls) == 1 and a.status == "failed" and b.attempts == 0
    configure(db, enabled=False)
    asyncio.run(out.drain_outbox(db))
    assert b.status == "pending" and b.attempts == 0


def test_delivery_unknown_is_not_automatically_repeated(db, monkeypatch):
    configure(db)
    row = enqueue(db)
    calls = []
    async def send(**kw):
        calls.append(1)
        raise out.TelegramDeliveryError("read timed out", kind="delivery_unknown")
    monkeypatch.setattr(out, "send_outbox_message", send)
    monkeypatch.setattr(out, "utcnow", lambda: NOW)
    asyncio.run(out.drain_outbox(db))
    asyncio.run(out.drain_outbox(db))
    assert calls == [1] and row.status == "failed" and row.last_error_kind == "delivery_unknown"


def test_lost_ownership_before_http_does_not_send(db, monkeypatch):
    configure(db)
    row = enqueue(db)
    monkeypatch.setattr(out, "renew_claim", lambda db, claim: False)
    async def forbidden_send(**kw):
        pytest.fail("lost owner sent HTTP")
    monkeypatch.setattr(out, "send_outbox_message", forbidden_send)
    assert asyncio.run(out.drain_outbox(db, limit=1))["sent"] == 0
    assert row.status == "sending"


def test_acl_revoke_during_http_discards_late_receipt(db, monkeypatch):
    configure(db, "hevc")
    approve(db)
    row = enqueue(db, bot_key="hevc")
    async def send(**kw):
        assert not db.in_transaction()
        assert cancel_pending_hevc_outbox(db, -100) == 1
        db.commit()
        return 61
    monkeypatch.setattr(out, "send_outbox_message", send)
    result = asyncio.run(out.drain_outbox(db, bot_key="hevc"))
    assert result["sent"] == 0
    assert row.status == "cancelled" and row.telegram_message_id is None


def test_commit_failure_after_http_does_not_send_again(db, monkeypatch):
    configure(db)
    row = enqueue(db)
    calls = []
    async def send(**kw):
        calls.append(1)
        def fail_commit():
            raise RuntimeError("failed receipt commit")
        monkeypatch.setattr(db, "commit", fail_commit)
        return 77
    monkeypatch.setattr(out, "send_outbox_message", send)
    with pytest.raises(RuntimeError, match="receipt commit"):
        asyncio.run(out.drain_outbox(db))
    assert calls == [1] and row.status == "sending"


def install_response(monkeypatch, *, status=200, data=None, body=None, error=None):
    class Client:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, json):
            if error: raise error
            return httpx.Response(status, json=data) if body is None else httpx.Response(status, content=body)
    monkeypatch.setattr(out.httpx, "AsyncClient", Client)


@pytest.mark.parametrize("status,data,body,kind", [
    (403, {"ok": False, "error_code": 403, "description": "kicked"}, None, "permanent"),
    (400, {"ok": False, "error_code": 400, "description": "bad entities"}, None, "permanent"),
    (401, {"ok": False, "error_code": 401}, None, "configuration"),
    (500, {"ok": False, "error_code": 500}, None, "temporary"),
    (502, None, b"<html>gateway</html>", "delivery_unknown"),
    (200, None, b"broken JSON", "delivery_unknown"),
    (200, {"ok": True}, None, "delivery_unknown"),
])
def test_structured_and_ambiguous_http_errors(monkeypatch, status, data, body, kind):
    install_response(monkeypatch, status=status, data=data, body=body)
    with pytest.raises(out.TelegramDeliveryError) as exc:
        asyncio.run(out.send_outbox_message(token=TOKEN, base_url="https://isolated.invalid", chat_id="-1", payload={"text": "x"}))
    assert exc.value.kind == kind


@pytest.mark.parametrize("value,expected", [(42, 42), (0, 0), ("42", None), (True, None), (-1, None), (None, None)])
def test_retry_after_protocol_validation(monkeypatch, value, expected):
    install_response(monkeypatch, status=429, data={"ok": False, "error_code": 429, "parameters": {"retry_after": value}})
    with pytest.raises(out.TelegramDeliveryError) as exc:
        asyncio.run(out.send_outbox_message(token=TOKEN, base_url="https://isolated.invalid", chat_id="-1", payload={"text": "x"}))
    assert exc.value.kind == "rate_limited" and exc.value.retry_after == expected


@pytest.mark.parametrize("error,kind", [
    (httpx.ConnectTimeout("connect"), "temporary"),
    (httpx.ConnectError("connect"), "temporary"),
    (httpx.PoolTimeout("pool"), "temporary"),
    (httpx.ReadTimeout("read"), "delivery_unknown"),
    (httpx.WriteTimeout("write"), "delivery_unknown"),
    (httpx.ReadError("read"), "delivery_unknown"),
])
def test_transport_failure_classification(monkeypatch, error, kind):
    install_response(monkeypatch, error=error)
    with pytest.raises(out.TelegramDeliveryError) as exc:
        asyncio.run(out.send_outbox_message(token=TOKEN, base_url="https://isolated.invalid", chat_id="-1", payload={"text": "x"}))
    assert exc.value.kind == kind


def test_send_receipt_and_error_scrubbing(monkeypatch, db):
    install_response(monkeypatch, data={"ok": True, "result": {"message_id": 81}})
    assert asyncio.run(out.send_outbox_message(token=TOKEN, base_url="https://isolated.invalid", chat_id="-1", payload={"text": "x"})) == 81
    row = enqueue(db)
    claim = out.claim_next_outbox(db, bot_key="primary", now=NOW)
    out.finalize_outbox(db, row.id, claim.token, status="failed", error="URL /bot"+TOKEN+"/sendMessage", secret_token=TOKEN, now=NOW)
    assert TOKEN not in row.last_error
    result = out.list_outbox_page(db)["items"][0]
    assert "payload_json" not in result and "claim_token" not in result
    assert TOKEN not in str(result)
    assert result["created_at"].endswith("Z")


def test_total_http_timeout_is_unknown(monkeypatch):
    class Client:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kw):
            await asyncio.sleep(10)
    monkeypatch.setattr(out.httpx, "AsyncClient", Client)
    monkeypatch.setattr(out, "HTTP_TOTAL_SECONDS", 0.005)
    with pytest.raises(out.TelegramDeliveryError) as exc:
        asyncio.run(out.send_outbox_message(token=TOKEN, base_url="https://isolated.invalid", chat_id="-1", payload={"text": "x"}))
    assert exc.value.kind == "delivery_unknown"


def test_summary_filter_paging_and_invalid_filters(db):
    enqueue(db)
    enqueue(db, bot_key="hevc", status="failed", last_error_kind="delivery_unknown")
    page = out.list_outbox_page(db, bot_key="hevc", status="failed", page_size=1)
    assert page["total"] == 1 and page["summary"]["unknown"] == 1
    assert page["items"][0]["status_label"] == "Доставка неизвестна"
    with pytest.raises(ValueError):
        out.list_outbox_page(db, bot_key="bogus")
    with pytest.raises(ValueError):
        out.list_outbox_page(db, status="bogus")


@pytest.fixture
def pg_engine():
    url = os.getenv("ALTT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("ALTT_TEST_POSTGRES_URL отдельной тестовой БД не задан")
    if not url.startswith(("postgresql://", "postgresql+psycopg://")):
        pytest.fail("Требуется отдельная PostgreSQL test DB")
    schema = "altt_outbox_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=10000"})
    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_pg_competing_claims_have_one_owner(pg_engine):
    with Session(pg_engine) as db:
        row_id = enqueue(db).id
    barrier = threading.Barrier(2)
    def take(_):
        with Session(pg_engine) as db:
            barrier.wait(timeout=10)
            claim = out.claim_next_outbox(db, bot_key="primary", now=NOW)
            return claim.id if claim else None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(take, range(2)))
    assert results.count(row_id) == 1 and results.count(None) == 1


def test_pg_competing_drains_dispatch_once_without_holding_transaction(pg_engine, monkeypatch):
    with Session(pg_engine) as db:
        configure(db)
        enqueue(db)
    first_sent = threading.Event()
    allow_receipt = threading.Event()
    calls = []
    async def send(**kw):
        calls.append(kw["chat_id"])
        first_sent.set()
        assert await asyncio.to_thread(allow_receipt.wait, 10)
        return 42
    monkeypatch.setattr(out, "send_outbox_message", send)
    def drain():
        with Session(pg_engine) as db:
            return asyncio.run(out.drain_outbox(db))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(drain)
        assert first_sent.wait(10)
        second = pool.submit(drain)
        assert second.result(timeout=10)["sent"] == 0
        allow_receipt.set()
        assert first.result(timeout=10)["sent"] == 1
    assert calls == ["-100"]


def test_pg_concurrent_manual_retry_only_one_transition(pg_engine):
    with Session(pg_engine) as db:
        configure(db)
        row_id = enqueue(db, status="failed", last_error_kind="permanent").id
    barrier = threading.Barrier(2)
    def retry(_):
        with Session(pg_engine) as db:
            barrier.wait(timeout=10)
            try:
                out.retry_failed_outbox(db, row_id)
                return True
            except out.OutboxRetryError as exc:
                assert exc.code == "state"
                return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(retry, range(2)))
    assert sorted(results) == [False, True]


def test_pg_concurrent_cooldown_never_shortens(pg_engine):
    barrier = threading.Barrier(2)
    def pause(seconds):
        with Session(pg_engine) as db:
            barrier.wait(timeout=10)
            out.set_bot_cooldown(db, bot_key="primary", until=NOW + timedelta(seconds=seconds))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(pause, [600, 30]))
    with Session(pg_engine) as db:
        assert out._cooldown_until(db.get(Setting, "telegram_outbox_cooldown_primary")) == NOW + timedelta(seconds=600)


def test_pg_old_owner_cannot_overwrite_retry_receipt(pg_engine):
    with Session(pg_engine) as owner, Session(pg_engine) as recovery:
        row_id = enqueue(owner).id
        old = out.claim_next_outbox(owner, bot_key="primary", now=NOW)
        configure(recovery)
        assert out.recover_expired_claims(recovery, bot_key="primary", now=NOW + timedelta(seconds=121)) == 1
        out.retry_failed_outbox(recovery, row_id, acknowledge_unknown=True, now=NOW + timedelta(seconds=121))
        new = out.claim_next_outbox(recovery, bot_key="primary", now=NOW + timedelta(seconds=121))
        assert out.finalize_outbox(recovery, row_id, new.token, status="sent", message_id=99, now=NOW + timedelta(seconds=122))
        assert not out.finalize_outbox(owner, row_id, old.token, status="failed", error_kind="permanent", now=NOW + timedelta(seconds=1))
        owner.expire_all()
        assert owner.get(TelegramOutbox, row_id).telegram_message_id == 99


def test_pg_claim_skips_independently_locked_row(pg_engine):
    with Session(pg_engine) as db:
        first, second = enqueue(db).id, enqueue(db).id
    with Session(pg_engine) as holder, Session(pg_engine) as consumer:
        holder.scalar(select(TelegramOutbox).where(TelegramOutbox.id == first).with_for_update())
        assert out.claim_next_outbox(consumer, bot_key="primary", now=NOW).id == second
        holder.rollback()


def test_pg_parallel_primary_outcomes_never_overwrite_sent(pg_engine):
    with Session(pg_engine) as db:
        pipe_id = pipeline(db).id
        enqueue(db, pipeline_id=pipe_id)
        enqueue(db, pipeline_id=pipe_id)
        a = out.claim_next_outbox(db, bot_key="primary", now=NOW)
        b = out.claim_next_outbox(db, bot_key="primary", now=NOW)
    barrier = threading.Barrier(2)
    def finish(args):
        claim, status = args
        with Session(pg_engine) as db:
            barrier.wait(timeout=10)
            assert out.finalize_outbox(db, claim.id, claim.token, status=status, now=NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(finish, [(a, "sent"), (b, "failed")]))
    with Session(pg_engine) as db:
        assert db.get(TorrentPipeline, pipe_id).tg_status == "sent"
        assert db.scalar(select(func.count()).select_from(PipelineEvent)) == 2


def test_pg_concurrent_primary_producers_deduplicate_atomically(pg_engine, monkeypatch):
    with Session(pg_engine) as db:
        configure_tracking(db)
        pipe_id = pipeline(db, tg_status="skipped").id
    # Both legacy-history checks must see no event, exercising the unique insert
    # rather than only the sequential existing-row fast path.
    barrier = threading.Barrier(2)
    original_tracked = __import__("app.services.telegram_notify", fromlist=["is_release_tracked"]).is_release_tracked
    def tracked(db, release_id):
        result = original_tracked(db, release_id)
        barrier.wait(timeout=10)
        return result
    monkeypatch.setattr("app.services.telegram_notify.is_release_tracked", tracked)
    def produce(_):
        with Session(pg_engine) as db:
            pipe = db.get(TorrentPipeline, pipe_id)
            pipe.status = "master_added"  # Dirty caller state must not autoflush before outbox identity is claimed.
            enqueue_pipeline_telegram_notification(db, pipe)
            return pipe.tg_status
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(produce, range(2))) == ["queued", "queued"]
    with Session(pg_engine) as db:
        rows = db.scalars(select(TelegramOutbox)).all()
        assert len(rows) == 1 and rows[0].dedupe_key == f"primary_pipeline:{pipe_id}"
        assert db.scalar(select(func.count()).select_from(PipelineEvent).where(PipelineEvent.event_type == "tg_queued")) == 1
