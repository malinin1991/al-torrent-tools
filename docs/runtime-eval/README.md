# Изолированный эксперимент Python 3.15

Это артефакты проверки 13 октября 2026, **не production-конфигурация**.
Production остаётся на `python:3.14-slim`: native Linux arm64 benchmark
дал `inconclusive`, поэтому отсутствие регрессии не доказано.

Результаты: [совместимость](../PYTHON_315_COMPATIBILITY.md),
[точный provenance](../python315_docker_provenance.json),
[benchmark](../PYTHON_RUNTIME_BENCHMARKS.md).

Здесь сохранены точные временные Dockerfile, общий freeze зависимостей,
smoke и migration-fixture scripts. Исходный официальный Python Dockerfile
сохранён отдельно. В native-кандидате явно включена bundled libmpdec 2.5.1;
PyYAML компилируется с LibYAML 0.2.5. MediaInfo >= 26 проверяется как в
основном Dockerfile. Значения `.env`, медиа и production данные отсутствуют.

Теги `python:*`/`debian:*`, apt repositories и изолированные build dependencies
могут меняться. Наблюдавшиеся image IDs/digests и runtime версии сохранены
в provenance JSON. Повторный эксперимент должен заново проверить разрешённые
образы, native libraries и равенство зависимостей; это не bit-for-bit сборка.
Для сравнения используется один снимок приложения на обоих интерпретаторах.

## Сборка своих тестовых образов

Выполнять из корня репозитория. Команды создают только новые test images и
временную копию исходников, не запускают Compose и не монтируют production.
Платформа ниже — native arm64. Результаты не доказывают производительность
на amd64/Unraid; QEMU нельзя смешивать с native измерениями.

```sh
eval_project="$PWD"
eval_artifacts="$eval_project/docs/runtime-eval"
eval_root="$(mktemp -d /private/tmp/altt-python-eval.XXXXXX)"
mkdir -p "$eval_root/python315" "$eval_root/backend" "$eval_root/dual"

rsync -a --exclude='.env*' --exclude='data/' --exclude='__pycache__/' \
  --exclude='*.pyc' --exclude='.pytest_cache/' --exclude='.cursor/' \
  --exclude='.codex/' --exclude='.agents/' --exclude='venv/' \
  --exclude='.venv/' "$eval_project/backend/" "$eval_root/backend/"
cp "$eval_artifacts/constraints-shared.txt" "$eval_root/backend/"
cp "$eval_artifacts/Dockerfile.python315-native" "$eval_root/python315/Dockerfile"
curl --fail --location \
  --output "$eval_root/python315/Python-3.15.0.tar.xz" \
  https://www.python.org/ftp/python/3.15.0/Python-3.15.0.tar.xz
```

Source SHA-256 проверяется внутри сохранённого Dockerfile:
`ba4bed1ba346b916890b76d9e320451420aa69f6408997d33c66482eeae3d575`.

```sh
docker build --platform linux/arm64 --progress plain \
  -t altt-python-eval:315-nativebase-20261013 "$eval_root/python315"
docker build --platform linux/arm64 --progress plain \
  -f "$eval_artifacts/Dockerfile.altt314" \
  -t altt-python-eval:314-20261013 "$eval_root/backend"
docker build --platform linux/arm64 --progress plain \
  -f "$eval_artifacts/Dockerfile.altt315-native" \
  -t altt-python-eval:315-native-20261013 "$eval_root/backend"
cp "$eval_artifacts/Dockerfile.dual" "$eval_root/dual/Dockerfile"
cp "$eval_artifacts/run314.sh" "$eval_root/dual/"
docker build --platform linux/arm64 --progress plain \
  -t altt-python-eval:dual-native-20261013 "$eval_root/dual"
```

`Dockerfile.altt315-stock` воспроизводит исходный кандидат с отсутствующим
native `_decimal` и LibYAML. Для итогового benchmark он не использовался.
Временная native-base сборка использует COPY source archive для локальной
проверки; архив занимает дополнительный слой. Это ещё одна причина не
переносить экспериментальные Dockerfile напрямую в production.

## Smoke, тесты и benchmark

```sh
docker run --rm --network none \
  -v "$eval_artifacts/runtime_provenance.py:/tmp/provenance.py:ro" \
  altt-python-eval:dual-native-20261013 \
  /opt/python314/run314.sh /tmp/provenance.py
docker run --rm --network none \
  -v "$eval_artifacts/runtime_provenance.py:/tmp/provenance.py:ro" \
  altt-python-eval:dual-native-20261013 python /tmp/provenance.py
docker run --rm --network none -v "$eval_root/backend:/app:ro" \
  -e PYTHONDONTWRITEBYTECODE=1 altt-python-eval:315-native-20261013 \
  python -m pytest -q -p no:cacheprovider
```

Без `ALTT_TEST_POSTGRES_URL` три PostgreSQL-теста пропускаются. Для полного
прогона создать собственные network/PostgreSQL 16 container без host ports,
передать только его URL как `DATABASE_URL` и `ALTT_TEST_POSTGRES_URL`.
`test_encoder_history_postgres.py` использует уникальные временные схемы.
Для проверки сохранения старых данных сначала применить миграции до
`0024_backoffice_source` на этой пустой disposable БД, запустить
`check_migration.py seed /verification/before.json`, выполнить upgrade head
через `scripts/entrypoint.sh` и затем
`check_migration.py verify /verification/before.json`.
Для скрипта добавить `PYTHONPATH=/app`; `/verification` — свой временный
bind mount. Скрипт отказывается seed при неподходящей версии схемы.

Команды benchmark и критерии median/p95/CPU/RSS приведены в
[PYTHON_RUNTIME_BENCHMARKS.md](../PYTHON_RUNTIME_BENCHMARKS.md).
В dual image пути — `/opt/python314/run314.sh` и `/usr/local/bin/python`.
Не запускать сборки/тесты одновременно с измерениями.

Созданные для первоначальной проверки PostgreSQL container, его volume и
network удалены. Тестовые images и `/private/tmp` доказательства оставлены;
экспериментальные артефакты выше позволяют повторить проверку после их очистки.
