#!/bin/sh
set -e
cd /app
# Сериализация миграций: api/worker/telegram-bot стартуют параллельно.
PYTHONPATH=/app python scripts/migrate_with_lock.py
exec "$@"
