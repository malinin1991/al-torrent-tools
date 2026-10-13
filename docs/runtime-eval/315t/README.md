# Входы эксперимента regular 3.15 / 3.15t

Это отдельный development experiment, **не production Dockerfile**.
Совместимость: [PYTHON_315T_COMPATIBILITY.md](../../PYTHON_315T_COMPATIBILITY.md).
Исходные metadata/imports/images/hashes:
[python315t_provenance.json](../../python315t_provenance.json).
Протокол и результаты:
[PYTHON_315T_BENCHMARKS.md](../../PYTHON_315T_BENCHMARKS.md).

Production `psycopg-binary==3.3.6` не устанавливается на cp315t. Поэтому
эксперимент использует **одинаковый pure psycopg3.3.6 + system libpq17.11**
во всех трёх режимах. Это отдельный контракт из 46 distributions.
Wheels/ABI/версии для обхода блокера не подменяются; force-GIL-off отсутствует.
По [полному PyPI filename audit](psycopg-binary-all-wheel-files.json)
free-threaded wheels отсутствуют на всех архитектурах, включая Linux x86_64;
реальные runtime tests ниже выполнены только на native arm64.

## Сборка

Сначала подготовить regular native 3.15 image по
[предыдущей инструкции](../README.md), не меняя production. Затем создать
свой временный context с копией текущего `backend/`, исключив `.env*`,
`data/`, `__pycache__`, caches и virtualenvs. Здесь `${eval_backend}` —
эта копия, `${eval_artifacts}` — абсолютный путь к этой директории,
`${eval_ftbase}`/`${eval_dual}` — новые временные каталоги.

```sh
cp "$eval_artifacts/requirements-pure.txt" "$eval_backend/"
cp "$eval_artifacts/constraints-pure.txt" "$eval_backend/"
cp "$eval_artifacts/Dockerfile.python315t-native" "$eval_ftbase/Dockerfile"
```

Поместить официальный `Python-3.15.0.tar.xz` в `${eval_ftbase}`.
Dockerfile проверяет SHA-256
`ba4bed1ba346b916890b76d9e320451420aa69f6408997d33c66482eeae3d575`.
Используется исходная PGO+LTO/shared сборка с `--disable-gil` и
`--without-system-libmpdec`, без JIT, build parallelism ограничен 4.

```sh
docker build --platform linux/arm64 --progress plain \
  -t altt-python-eval:315t-nativebase-20261013 "$eval_ftbase"
docker build --platform linux/arm64 --progress plain \
  -f "$eval_artifacts/Dockerfile.regular315-pure" \
  -t altt-python-eval:315-regular-pure-20261013 "$eval_backend"
docker build --platform linux/arm64 --progress plain \
  -f "$eval_artifacts/Dockerfile.altt315t-pure" \
  -t altt-python-eval:315t-pure-fixed-20261013 "$eval_backend"
cp "$eval_artifacts/Dockerfile.dual" "$eval_dual/Dockerfile"
cp "$eval_artifacts"/run_*.sh "$eval_dual/"
docker build --platform linux/arm64 --progress plain \
  -t altt-python-eval:315-vs-315t-pure-20261013 "$eval_dual"
```

`Dockerfile.altt315t-pure` — исправленный канонический context: libpq5
сохраняется при удалении build tools. Для точного аудита фактически
проверенного initial+repair image chain также сохранены
`Dockerfile.altt315t-pure-initial` и `Dockerfile.libpq-repair`.
Первый initial cleanup ошибочно удалял libpq5; repair layer возвращал её.
Никаких версий, application code или backend-реализации этот fix не менял.
Исправленный одношаговый context логически эквивалентен, но image digest
может отличаться. Теги, apt и build dependencies могут меняться: повторный
run обязан заново проверять версии/native libraries/GIL, а не полагаться
на прежний verdict. Source archive COPY также создаёт дополнительный layer.

## Режимы и проверки

В общем image находятся:

- `/opt/runtime-modes/run_regular.sh`: regular315, GIL включён.
- `/opt/runtime-modes/run_t_gil1.sh`: free-threaded315t с `-X gil=1`.
- `/opt/runtime-modes/run_t_default.sh`: free-threaded315t default,
  `PYTHON_GIL` удаляется из env; **force0 не применяется**.

Аудит imports выполняется без сети, на read-only копии приложения:

```sh
docker run --rm --network none -v "$eval_backend:/app:ro" \
  -v "$eval_artifacts:/inputs:ro" -v "$eval_output:/output" \
  -e PYTHONDONTWRITEBYTECODE=1 altt-python-eval:315-vs-315t-pure-20261013 \
  /opt/runtime-modes/run_t_default.sh /inputs/probe_315t_imports.py \
  --output /output/imports.json
```

Не запускать GIL-off benchmark при ошибке imports или автоматическом
включении GIL. `runtime_provenance_ft.py` проверяет native libraries и
сохраняет configure flags, effective GIL и distributions. `run_mode_pytest.py`
проверяет GIL до pytest import и после suite; для него передать
`PYTHONPATH=/app` и `ALTT_EVAL_EXPECT_GIL=1` либо `0`.

Для полного suite использовать только собственный disposable PostgreSQL16,
его URL в `DATABASE_URL`/`ALTT_TEST_POSTGRES_URL`, read-only source mount
и отдельную БД для каждого режима. Штатный `scripts/entrypoint.sh` запускает
advisory-lock миграции image-default315t, затем переданную mode wrapper.
Установленные 1011 тестов успешно прошли во всех трёх режимах, включая
реальные PG races и 62 HEVC helper tests; отсутствие GIL проверено после них.

Benchmark команды и протокол приведены в
[PYTHON_315T_BENCHMARKS.md](../../PYTHON_315T_BENCHMARKS.md).
Точная версия driver основной 12-triplet серии сохранена как
`benchmark_free_threading.main-measured.py` до дополнительного CLI-патча.
Измерять на одинаковом source snapshot и native architecture; сборки,
pytest и другие нагрузки одновременно не запускать.

Созданные для проверки PostgreSQL container, его anonymous volume и network
удалены перед измерениями. Experimental images и временные доказательства
сохранены; существующие контейнеры, БД и данные не использовались.
