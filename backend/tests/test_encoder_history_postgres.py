"""Real independent-connection claims, only in a disposable test schema."""

from concurrent.futures import ThreadPoolExecutor
import os
import threading
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import EncoderSubmission, TorrentFile
from app.services.encoder_history import SubmissionConflict, claim, request_digest


@pytest.fixture
def pg_engine():
    url = os.getenv("ALTT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("ALTT_TEST_POSTGRES_URL отдельной тестовой БД не задан")
    if not url.startswith(("postgresql://", "postgresql+psycopg://")):
        pytest.fail("Требуется отдельная PostgreSQL test DB")
    schema = "altt_encoder_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            db.add(TorrentFile(id=1, torrent_id=11, release_id=22, info_hash="a" * 40,
                               relative_path="Show/01.mkv", full_path="/anilibria/Show/01.mkv", size=123))
            db.commit()
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def race(pg_engine, same_key):
    barrier = threading.Barrier(2)
    common_id = uuid4().hex
    digest = request_digest([1], "hevc", "copy", "none", None)

    def invoke(index):
        with Session(pg_engine) as db:
            barrier.wait(timeout=10)
            try:
                group, owner = claim(db, file_ids=[1], request_id=common_id if same_key else uuid4().hex,
                                     digest=digest, base_url="http://isolated-encoder.test",
                                     video_id="hevc", audio_id="copy", downmix="none")
                return group.id, owner
            except SubmissionConflict:
                return None, False

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(invoke, range(2)))
    with Session(pg_engine) as db:
        assert db.scalar(select(func.count()).select_from(EncoderSubmission)) == 1
    assert sum(owner for _, owner in results) == 1
    return results


def test_same_request_id_has_one_owner_and_returns_same_receipt(pg_engine):
    results = race(pg_engine, same_key=True)
    assert results[0][0] is not None
    assert results[0][0] == results[1][0]


def test_overlapping_actions_with_different_ids_cannot_dispatch_twice(pg_engine):
    results = race(pg_engine, same_key=False)
    assert sum(group_id is None for group_id, _ in results) == 1
