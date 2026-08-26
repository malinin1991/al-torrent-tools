#!/usr/bin/env python3
"""Docker healthcheck для telegram-bot: свежий heartbeat-файл."""

from __future__ import annotations

import sys
import time
import os
from pathlib import Path

HEALTH_FILE = Path(
    os.environ.get("ALTT_TELEGRAM_HEALTH_FILE", "/tmp/altt_telegram_healthy")
)
MAX_AGE_SEC = 180


def main() -> int:
    if not HEALTH_FILE.is_file():
        print("telegram heartbeat missing", file=sys.stderr)
        return 1
    age = time.time() - HEALTH_FILE.stat().st_mtime
    if age > MAX_AGE_SEC:
        print(f"telegram heartbeat stale ({age:.0f}s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
