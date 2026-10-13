"""Один processor на release_id между разными типами джобов / процессами."""

from contextlib import contextmanager
from sqlalchemy import text


@contextmanager
def release_lock(db, release_id):
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        yield
        return
    # Отдельное соединение: многочисленные commit processor не освобождают lock.
    with bind.connect() as connection:
        locked = connection.scalar(text("SELECT pg_try_advisory_lock(1937006964, :rid)"), {"rid": int(release_id)})
        if not locked:
            raise RuntimeError(f"Релиз {release_id} уже синхронизируется; повтор в следующем обходе")
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(1937006964, :rid)"), {"rid": int(release_id)})
