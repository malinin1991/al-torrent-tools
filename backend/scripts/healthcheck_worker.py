#!/usr/bin/env python3
"""Docker healthcheck для worker: свежий heartbeat-файл."""

from __future__ import annotations

import sys
import time
from pathlib import Path

HEALTH_FILE = Path("/tmp/altt_worker_healthy")
# worker обновляет файл раз в ~60 с; запас на alembic/startup и нагрузку.
MAX_AGE_SEC = 180


def main() -> int:
    if not HEALTH_FILE.is_file():
        print("worker heartbeat missing", file=sys.stderr)
        return 1
    age = time.time() - HEALTH_FILE.stat().st_mtime
    if age > MAX_AGE_SEC:
        print(f"worker heartbeat stale ({age:.0f}s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
