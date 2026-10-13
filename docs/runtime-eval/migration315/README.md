# Проверка прямой миграции на CPython 3.15.0

Это точные входы и результаты проверки production Dockerfile 13 октября 2026 года. Решение и ограничения описаны в [PYTHON_315_MIGRATION.md](../../PYTHON_315_MIGRATION.md), версии, SHA-256, image IDs и команды — в [python315_migration_provenance.json](../../python315_migration_provenance.json).

`Dockerfile`, `ensure_native_decimal.py`, `requirements.txt` и `constraints.txt` — копии проверенных входов. Собирать приложение следует с build context `backend/`, а не из этого каталога. `source-manifest.json` фиксирует 204 файла фактически проверенного кода; оба образа содержали одинаковый snapshot.

```sh
docker build --platform linux/arm64 --progress plain --file backend/Dockerfile --tag altt-python-migration:315-arm64-20261013 backend
docker build --platform linux/amd64 --progress plain --file backend/Dockerfile --tag altt-python-migration:315-amd64-20261013 backend
```

Для повторной проверки нужно создать собственные временные Docker network и PostgreSQL 16 без опубликованных портов. Команды с параметрами `OWN_EVAL_NETWORK`, `OWN_EVAL_DATABASE_URL`, `ABS_EVIDENCE_DIR` и `IMAGE` находятся в provenance JSON. Не указывать пользовательскую или production БД: `check_migration.py seed` добавляет искусственные данные в схему 0024. Последовательность проверки legacy-миграции: upgrade до 0024, `seed`, штатный entrypoint с `verify` до 0025. После завершения удалить только созданные для проверки контейнер, его volume и сеть.

`probe.py` проверяет 20 native/application imports, GIL, отсутствие JIT, binary psycopg, 46 закреплённых application distributions, native Decimal/LibYAML, системную MediaInfo ≥26, отсутствие bundled MediaInfo, компилятора и build dependencies. `run_pytest.py` проверяет состояние GIL до и после полного suite. В каждой архитектуре получено 1011 passed, 0 skipped, включая три opt-in PostgreSQL теста. `*-migration.log` подтверждают advisory lock, upgrade 0024→0025 и сохранение шести типов legacy-сущностей.

В каждой сборке выполнялись 736 upstream decimal tests: 727 успешных, 9 пропущенных, ошибок нет. Эта проверка относится к сборке расширения и отделена от 1011 тестов приложения.

ARM64 проверен нативно; AMD64 — под эмуляцией на ARM64. Время из `*-tests.log` относится к выполнению тестов и не является сравнением производительности. Финальные образы на официальном base image с минимальной сборкой `_decimal` повторно не бенчмаркировались; старый Linux benchmark измерял другой full-source кандидат и сохранил неопределённый p95 gate.
