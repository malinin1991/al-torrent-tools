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
    connection = bind.connect()
    try:
        try:
            locked = connection.scalar(text("SELECT pg_try_advisory_lock(1937006964, :rid)"), {"rid": int(release_id)})
            # Session locks переживают commit. Не держим idle-in-transaction
            # на всё время сетевых запросов и обработки релиза.
            connection.commit()
        except BaseException:
            # Acquire мог пройти на сервере, даже если ответ/commit потерян.
            # Rollback при возврате в pool session lock не освобождает.
            connection.invalidate()
            raise
        if not locked:
            raise RuntimeError(f"Релиз {release_id} уже синхронизируется; повтор в следующем обходе")
        try:
            yield
        finally:
            try:
                connection.execute(text("SELECT pg_advisory_unlock(1937006964, :rid)"), {"rid": int(release_id)})
                connection.commit()
            except BaseException:
                # Не возвращаем потенциально заблокированное соединение в pool.
                connection.invalidate()
                raise
    finally:
        connection.close()
