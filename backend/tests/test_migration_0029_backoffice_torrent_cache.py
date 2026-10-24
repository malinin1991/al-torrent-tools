"""Disposable Nova cache migration, separate from durable archive/SLA data."""

from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
import json
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    BackofficeTorrentCache,
    EncoderAutoEvent,
    EncoderAutoItem,
    EncoderAutoSource,
    EncoderFileReadiness,
    EncoderSubmission,
    PipelineEvent,
    Release,
    ReleaseEncoderPolicy,
    Setting,
    TorrentArchive,
    TorrentFile,
    TorrentPipeline,
)


_BACKEND = Path(__file__).parents[1]
_PREVIOUS = "0028_automatic_encoder"
_REVISION = "0029_backoffice_torrent_cache"
_CACHE = "backoffice_torrent_cache"
_STAMP = datetime(2026, 10, 25, 1, 2, 3, 456789)
_UPLOADED = datetime(2026, 10, 23, 7, 8, 9)
_INFO_HASH = "a" * 40


def test_0029_offline_postgresql_ddl_only_adds_disposable_cache():
    spec = spec_from_file_location(
        "migration_0029_cache", _BACKEND / "alembic" / "versions" / f"{_REVISION}.py"
    )
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.revision == _REVISION
    assert migration.down_revision == _PREVIOUS

    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()

    statements = [" ".join(sql.split()) for sql in output.getvalue().split(";") if sql.strip()]
    assert len(statements) == 2
    assert statements[0].startswith(f"CREATE TABLE {_CACHE} (")
    assert "PRIMARY KEY (provider_key, release_id, torrent_id)" in statements[0]
    assert "payload_json JSONB NOT NULL" in statements[0]
    assert "fetched_at TIMESTAMP WITHOUT TIME ZONE NOT NULL" in statements[0]
    assert statements[1] == f"DROP TABLE {_CACHE}"


@pytest.fixture
def pg(monkeypatch):
    raw = os.getenv("ALTT_TEST_POSTGRES_URL")
    if not raw:
        pytest.skip("ALTT_TEST_POSTGRES_URL отдельной тестовой БД не задан")
    if not raw.startswith(("postgresql://", "postgresql+psycopg://")):
        pytest.fail("Требуется отдельная PostgreSQL test DB")

    schema = "altt_migration_0029_" + uuid4().hex
    admin = create_engine(raw)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = make_url(raw).update_query_dict(
        {"options": f"-csearch_path={schema}"}
    ).render_as_string(hide_password=False)
    engine = create_engine(url)
    from app.core.config import settings

    monkeypatch.setattr(settings, "database_url", url)
    config = Config(str(_BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND / "alembic"))
    try:
        yield engine, config
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _seed_durable_history(engine):
    with Session(engine) as db:
        db.add(Release(release_id=42, release_alias="cache-test", title="Кэш Nova", source="backoffice", source_checked_at=_STAMP))
        db.add(TorrentArchive(
            id=1, info_hash=_INFO_HASH, release_id=42, torrent_id=101,
            file_path="/test/archive.torrent", torrent_type="WEB-DL 1080p AVC",
            torrent_description="1-3", quality_json={"codec": {"label": "AVC"}},
            api_created_at=_UPLOADED, created_at=_STAMP, backoffice_trackers=True,
            notification_baseline=True,
        ))
        db.add(TorrentFile(
            id=1, release_id=42, torrent_id=101, info_hash=_INFO_HASH,
            relative_path="Show/03.mkv", size=123, ui_status="new",
            created_at=_STAMP, updated_at=_STAMP,
        ))
        pipeline = TorrentPipeline(
            id=1, release_id=42, torrent_id=101, info_hash=_INFO_HASH,
            status="done", created_at=_STAMP,
        )
        pipeline.events.append(PipelineEvent(
            id=1, event_type="hevc_status", from_status="missing", to_status="overdue",
            details_json={"overdue": True, "missing": True, "sla_hours": 24},
            message="Preserve HEVC history", created_at=_STAMP,
        ))
        db.add(pipeline)
        setting = db.get(Setting, "video_kensetsu_auto_enabled")
        assert setting is not None
        setting.value = "true"
        setting.updated_at = _STAMP
        db.add(Setting(key="migration_0029_sentinel", value="keep", updated_at=_STAMP))
        db.add(ReleaseEncoderPolicy(
            release_id=42, video_id="hevc", audio_id="copy", revision=7, generation=3,
            participating=True, presets_json={"preset": "keep"}, updated_at=_STAMP,
        ))
        db.add(EncoderAutoSource(
            id=1, release_id=42, generation=3, info_hash=_INFO_HASH,
            baseline=True, created_at=_STAMP,
        ))
        db.add(EncoderFileReadiness(
            id=1, info_hash=_INFO_HASH, relative_path="Show/03.mkv",
            full_path="/test/Show/03.mkv", content_hash="b" * 64, size=123,
            mtime_ns=123456789, ui_status="new", checked_at=_STAMP,
        ))
        db.add(EncoderAutoItem(
            id=1, release_id=42, generation=3, info_hash=_INFO_HASH,
            relative_path="Show/03.mkv", file_id=1, state="ready",
            block_reason="", policy_revision=7, next_attempt_at=_STAMP,
            created_at=_STAMP, updated_at=_STAMP,
        ))
        db.add(EncoderAutoEvent(
            id=1, item_id=1, release_id=42, code="ready",
            details_json={"keep": True}, created_at=_STAMP,
        ))
        db.add(EncoderSubmission(
            id=1, request_id="migration-0029-kept-receipt", request_digest="c" * 64,
            base_url="http://encoder.test", video_id="hevc", audio_id="copy",
            origin="auto", auto_item_id=1, policy_revision=7,
            payload_json={"paths": ["/test/Show/03.mkv"]},
            recovery_json={"state": "keep"}, created_at=_STAMP,
            updated_at=_STAMP, expires_at=_STAMP,
        ))
        db.commit()


def _durable_snapshot(engine):
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names()) - {_CACHE, "alembic_version"}
        return {
            name: sorted(
                json.dumps(row, sort_keys=True, ensure_ascii=False)
                for row in connection.scalars(text(f'SELECT to_jsonb(t) FROM "{name}" AS t'))
            )
            for name in sorted(tables)
        }


def test_0029_upgrade_downgrade_reupgrade_preserves_archive_sla_and_0028_data(pg):
    engine, config = pg
    command.upgrade(config, _PREVIOUS)
    _seed_durable_history(engine)
    before = _durable_snapshot(engine)

    command.upgrade(config, _REVISION)
    assert _durable_snapshot(engine) == before
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert set(inspector.get_table_names()) == set(before) | {_CACHE, "alembic_version"}
        assert inspector.get_pk_constraint(_CACHE)["constrained_columns"] == [
            "provider_key", "release_id", "torrent_id"
        ]
        columns = {column["name"]: column for column in inspector.get_columns(_CACHE)}
        assert isinstance(columns["payload_json"]["type"], JSONB)
        assert columns["fetched_at"]["type"].timezone is False
        assert all(column["nullable"] is False for column in columns.values())
        assert connection.scalar(text(f"SELECT count(*) FROM {_CACHE}")) == 0
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _REVISION

    payload = {"id": 101, "filename": "Свадьба светлячков.torrent", "meta": {"empty": [], "valid": True, "nullable": None}}
    entries = [
        {"provider_key": provider, "release_id": release, "torrent_id": torrent,
         "version": 1, "index_fingerprint": "d" * 64, "payload_json": payload,
         "payload_digest": "e" * 64, "fetched_at": _STAMP}
        for provider, release, torrent in (
            ("a" * 64, 42, 101), ("b" * 64, 42, 101),
            ("a" * 64, 43, 101), ("a" * 64, 42, 102),
        )
    ]
    with engine.begin() as connection:
        connection.execute(BackofficeTorrentCache.__table__.insert(), entries)
    with Session(engine) as db:
        row = db.get(BackofficeTorrentCache, ("a" * 64, 42, 101))
        assert row.payload_json == payload
        assert row.fetched_at == _STAMP and row.fetched_at.tzinfo is None
        assert row.version == 1 and row.payload_digest == "e" * 64
        assert len(db.scalars(select(BackofficeTorrentCache)).all()) == 4
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(BackofficeTorrentCache.__table__.insert(), entries[0])
    assert _durable_snapshot(engine) == before

    command.downgrade(config, _PREVIOUS)
    with engine.connect() as connection:
        assert set(inspect(connection).get_table_names()) == set(before) | {"alembic_version"}
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _PREVIOUS
    assert _durable_snapshot(engine) == before

    command.upgrade(config, _REVISION)
    with engine.connect() as connection:
        assert connection.scalar(text(f"SELECT count(*) FROM {_CACHE}")) == 0
    assert _durable_snapshot(engine) == before
