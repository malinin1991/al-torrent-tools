#!/usr/bin/env python3
"""alembic upgrade head под Postgres advisory lock.

api / worker / telegram-bot стартуют параллельно и все вызывают миграции —
без лока гонка на CREATE TABLE даёт:
  duplicate key value violates unique constraint "pg_type_typname_nsp_index"
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from sqlalchemy import create_engine, text

from app.core.config import settings

# Стабильный ключ для al-torrent-tools (session-level advisory lock).
LOCK_KEY = 2704343046  # 0xA1170006
LOCK_TIMEOUT_SEC = int(os.environ.get("ALTT_MIGRATE_LOCK_TIMEOUT_SEC", "300"))
POLL_SEC = float(os.environ.get("ALTT_MIGRATE_LOCK_POLL_SEC", "2"))


def main() -> int:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        deadline = time.monotonic() + LOCK_TIMEOUT_SEC
        while True:
            got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar()
            if got:
                break
            if time.monotonic() >= deadline:
                print(
                    f"timeout {LOCK_TIMEOUT_SEC}s waiting for alembic advisory lock",
                    file=sys.stderr,
                )
                return 1
            print("waiting for alembic advisory lock held by another service...", flush=True)
            time.sleep(POLL_SEC)

        try:
            print("alembic upgrade head (lock acquired)", flush=True)
            completed = subprocess.run(["alembic", "upgrade", "head"], check=False)
            return int(completed.returncode)
        finally:
            unlocked = conn.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY}
            ).scalar()
            if not unlocked:
                print("warning: alembic advisory lock was not held on unlock", file=sys.stderr)
            else:
                print("alembic advisory lock released", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
