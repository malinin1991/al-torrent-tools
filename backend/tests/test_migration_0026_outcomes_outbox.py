"""Upgrade existing 0025 data, downgrade and re-upgrade in a private PG schema."""
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url


def test_0026_preserves_existing_data_and_backfills_utc(monkeypatch):
    raw = os.getenv("ALTT_TEST_POSTGRES_URL")
    if not raw:
        pytest.skip("ALTT_TEST_POSTGRES_URL отдельной тестовой БД не задан")
    if not raw.startswith(("postgresql://", "postgresql+psycopg://")):
        pytest.fail("Требуется отдельная PostgreSQL test DB")
    schema = "altt_migration_0026_" + uuid4().hex
    admin = create_engine(raw)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = make_url(raw).update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
    from app.core.config import settings
    monkeypatch.setattr(settings, "database_url", url)
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "alembic"))
    engine = create_engine(url)
    try:
        command.upgrade(config, "0025_encoder_history")
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO jobs(id,type,status,params_json,created_at) VALUES (1,'ongoing','success','{}','2026-10-01 00:00:00')"))
            connection.execute(text("INSERT INTO telegram_outbox(id,bot_key,dedupe_key,chat_id,payload_json,status,attempts,created_at,sent_at) VALUES "
                "(1,'primary','p','1','{}','pending',2,'2026-10-01 01:00:00',NULL),"
                "(2,'hevc','h','-100','{}','sent',1,'2026-10-01 01:00:00','2026-10-01 02:00:00')"))
        command.upgrade(config, "head")
        with engine.begin() as connection:
            rows = connection.execute(text("SELECT id,status,attempts,dedupe_key,updated_at FROM telegram_outbox ORDER BY id")).all()
            assert [tuple(row[:4]) for row in rows] == [(1,"pending",2,"p"),(2,"sent",1,"h")]
            assert str(rows[0].updated_at) == "2026-10-01 01:00:00"
            assert str(rows[1].updated_at) == "2026-10-01 02:00:00"
            indexes = {item["name"] for item in inspect(connection).get_indexes("telegram_outbox")}
            assert {"ix_telegram_outbox_due", "ix_telegram_outbox_lease"} <= indexes
            connection.execute(text("UPDATE jobs SET status='partial',result_json='{}' WHERE id=1"))
            connection.execute(text("UPDATE telegram_outbox SET status='sending',claim_token='test',lease_until=timezone('UTC',now()) WHERE id=1"))
        command.downgrade(config, "0025_encoder_history")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT status FROM jobs WHERE id=1")) == "failed"
            assert connection.scalar(text("SELECT status FROM telegram_outbox WHERE id=1")) == "failed"
            assert not inspect(connection).has_table("job_sync_failures")
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert inspect(connection).has_table("job_sync_failures")
            assert connection.scalar(text("SELECT count(*) FROM telegram_outbox")) == 2
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
