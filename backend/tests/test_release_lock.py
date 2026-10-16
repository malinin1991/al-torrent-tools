"""Release ownership must not leak session advisory locks into the pool."""

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from app.services.release_lock import release_lock


class Connection:
    def __init__(self, *, acquired=True, fail=None):
        self.acquired = acquired
        self.fail = fail
        self.calls = []
        self.commits = 0

    def scalar(self, statement, params):
        self.calls.append("acquire")
        if self.fail == "acquire":
            raise RuntimeError("acquire response lost")
        return self.acquired

    def execute(self, statement, params):
        self.calls.append("unlock")
        if self.fail == "unlock":
            raise RuntimeError("unlock failed")

    def commit(self):
        self.commits += 1
        self.calls.append("commit")
        if self.fail == f"commit_{self.commits}":
            raise RuntimeError("commit failed")

    def invalidate(self):
        self.calls.append("invalidate")

    def close(self):
        self.calls.append("close")


def database(connection, dialect="postgresql"):
    return SimpleNamespace(get_bind=lambda: SimpleNamespace(
        dialect=SimpleNamespace(name=dialect), connect=lambda: connection))


def test_release_body_runs_without_open_transaction_and_unlocks_after_error():
    connection = Connection()
    with pytest.raises(ValueError, match="processing failed"):
        with release_lock(database(connection), 17):
            assert connection.calls == ["acquire", "commit"]
            raise ValueError("processing failed")
    assert connection.calls == ["acquire", "commit", "unlock", "commit", "close"]


@pytest.mark.parametrize("failure", ["acquire", "commit_1", "unlock", "commit_2"])
def test_uncertain_lock_outcome_discards_connection_before_pool_return(failure):
    connection = Connection(fail=failure)
    with pytest.raises(RuntimeError):
        with release_lock(database(connection), 17):
            pass
    assert connection.calls[-2:] == ["invalidate", "close"]


def test_busy_release_does_not_run_body_or_unlock_another_owner():
    connection = Connection(acquired=False)
    with pytest.raises(RuntimeError, match="уже синхронизируется"):
        with release_lock(database(connection), 17):
            pytest.fail("busy release must not be processed")
    assert connection.calls == ["acquire", "commit", "close"]


def test_other_dialects_do_not_open_a_lock_connection():
    with release_lock(database(None, "sqlite"), 17):
        pass


@pytest.fixture
def pg_engine():
    url = os.getenv("ALTT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("ALTT_TEST_POSTGRES_URL отдельной тестовой БД не задан")
    engine = create_engine(url, pool_size=2, max_overflow=0)
    yield engine
    engine.dispose()


def test_postgres_release_lock_survives_commit_without_idle_transaction(pg_engine):
    release_id = uuid4().int & 0x7FFFFFFF
    db = SimpleNamespace(get_bind=lambda: pg_engine)
    with release_lock(db, release_id):
        with pg_engine.connect() as contender:
            locked = contender.scalar(text("SELECT pg_try_advisory_lock(1937006964, :rid)"), {"rid": release_id})
            assert not locked
            states = contender.execute(text("""
                SELECT activity.state FROM pg_stat_activity AS activity
                JOIN pg_locks AS locks ON locks.pid = activity.pid
                WHERE locks.locktype = 'advisory' AND locks.classid = 1937006964
                    AND locks.objid = :rid AND locks.granted
            """), {"rid": release_id}).scalars().all()
            assert states == ["idle"]
    with pg_engine.connect() as contender:
        assert contender.scalar(text("SELECT pg_try_advisory_lock(1937006964, :rid)"), {"rid": release_id})
        contender.execute(text("SELECT pg_advisory_unlock(1937006964, :rid)"), {"rid": release_id})
        contender.commit()
