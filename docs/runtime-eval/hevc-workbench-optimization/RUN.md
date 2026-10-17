# Повтор эксперимента HEVC workbench

Все команды ниже относятся к одноразовым ресурсам эксперимента. Основной
контракт и заранее выбранный объём — в [PROTOCOL.md](PROTOCOL.md).
Используются Linux ARM64 Docker images с CPython 3.15.0 PGO/LTO, без JIT.
Сборка баз и MiniJinja описана в [предыдущем эксперименте](../minijinja-workbench/BUILD.md).

Для точного повтора нужны сохранённые baseline/optimized snapshots, а не
текущий изменяемый checkout. В snapshots одинаковы все файлы, кроме
`app/services/hevc_workbench.py`. Baseline совпадает с Git HEAD
`4e78fcfb10de16abd8bebf13232196ce84c1d5d0`; исходный файл проверяется по SHA-256
`0ed990e00bb6e4e1c2bff88d48b1490a09e926d66f3f147b2929e95a289fb8db`.
`freeze_variants.py` создаёт такую пару до изменения исходного HEAD,
исключая `.env`, данные, окружения и caches. Архивы и manifests сохраняются
в итоговых evidence.

## Fixture

Создать отдельные internal network, volume и PostgreSQL без публикации порта:

```sh
docker network create --internal --label altt.workbench-opt=20261017 altt-workbench-opt-20261017
docker volume create --label altt.workbench-opt=20261017 altt-workbench-opt-media-20261017
docker run -d --name altt-workbench-opt-pg-20261017 \
  --label altt.workbench-opt=20261017 --network altt-workbench-opt-20261017 \
  --mount type=tmpfs,destination=/var/lib/postgresql/data,tmpfs-size=1073741824 \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=altt_minijinja_workbench postgres:16
```

Дождаться `pg_isready`. Подготовить fixture существующим harness:

```sh
python3 docs/runtime-eval/minijinja-workbench/run_matrix.py \
  --source /private/tmp/altt-workbench-opt-20261017/frozen/baseline/backend \
  --output /private/tmp/altt-workbench-opt-20261017/results --stage prepare \
  --network altt-workbench-opt-20261017 \
  --media-volume altt-workbench-opt-media-20261017 \
  --database-url postgresql+psycopg://postgres@altt-workbench-opt-pg-20261017:5432/altt_minijinja_workbench
```

Ожидается 1024 releases, 5248 archives, 43392 files, input SHA-256
`0580f8e13c78aab82ca5743d7c0dbb20efe649875384ac20c7be683b1460bc4f`.
Реальные media, `.env` и внешние приложения не нужны. Часы предметной области
фиксированы на `2026-10-17T00:00:00Z`; часы измерения остаются настоящими.

Для PostgreSQL-регрессий используется другая БД
`altt_workbench_opt_regressions`, через `ALTT_TEST_POSTGRES_URL`.
Тесты не должны писать в benchmark fixture.

## Проверки и профили

`backend/scripts/check_hevc_workbench_optimization.py` запускается внутри
выбранного контейнера с `--source-backend /app`, `--database-url`,
`--media-root /bench/media`, `--mode regular|ft_on|ft_off`, `--output`.
Он сравнивает 40 полных контекстов с захваченным baseline и проверяет
реальную PostgreSQL CASE-ветку, read-only транзакции и GIL. Для `ft_on`
интерпретатор запускается с `-X gil=1`; для `ft_off` запрещено принудительное
отключение GIL.

`backend/scripts/profile_hevc_workbench.py` принимает те же входы, а также
`--concurrency 1 --requests 1 --passes cprofile phases`. Профили — отдельная
диагностика, не performance samples. Допустим отдельный `allocations` pass.
Исходник не переписывается; AST выделяет фазы исходной функции.
Все runtime/source/media mounts read-only, JSON/.prof выводятся отдельно.
Существующие результаты не заменяются.

Перед timing запустить полную pytest-базу из `/app` с отдельной тестовой БД;
HTML/DOM MiniJinja проверить старым `run_matrix.py --stage validate` на
optimized snapshot. Образы C-driver дополнительно собраны из непатченного
`psycopg-c==3.3.6`; их recipe, проверенные входы, wheel hashes, natural GIL
probes и список пакетов входят в evidence. Production constraints неизменны.

## Измерения

На macOS держать отдельный процесс `caffeinate -is` до конца всех серий.
Не запускать одновременно сборки, тесты, профилировщики или анализ bootstrap.
Каждый driver запускает дочерние процессы по неизменяемым image IDs,
контролирует wall/monotonic clock и повторно сверяет source hashes.
При прерывании не заменять отдельные samples: сохранить всю попытку и
использовать новый каталог для полной серии.

```sh
python3 docs/runtime-eval/hevc-workbench-optimization/run_comparison.py \
  --baseline-source /private/tmp/altt-workbench-opt-20261017/frozen/baseline/backend \
  --optimized-source /private/tmp/altt-workbench-opt-20261017/frozen/optimized/backend \
  --output /private/tmp/altt-workbench-opt-20261017/comparison-python

python3 docs/runtime-eval/hevc-workbench-optimization/run_comparison.py \
  --baseline-source /private/tmp/altt-workbench-opt-20261017/frozen/baseline/backend \
  --optimized-source /private/tmp/altt-workbench-opt-20261017/frozen/optimized/backend \
  --output /private/tmp/altt-workbench-opt-20261017/comparison-c \
  --regular-image altt-workbench-opt-c:regular-20261017 \
  --ft-image altt-workbench-opt-c:ft-20261017

python3 docs/runtime-eval/hevc-workbench-optimization/run_renderer_comparison.py \
  --source /private/tmp/altt-workbench-opt-20261017/frozen/optimized/backend \
  --output /private/tmp/altt-workbench-opt-20261017/renderer-c \
  --regular-image altt-workbench-opt-c:regular-20261017 \
  --ft-image altt-workbench-opt-c:ft-20261017
```

Каждая серия — 64 samples / 512 HTTP requests, без автоматического повтора.
`--stage provenance` и затем `--stage http` позволяют разделить только эти
две стадии, сохраняя общий входной контракт.

```sh
python3 docs/runtime-eval/hevc-workbench-optimization/analyze.py \
  --input /private/tmp/altt-workbench-opt-20261017/comparison-python \
  --output /private/tmp/altt-workbench-opt-20261017/comparison-python-analysis.json

python3 docs/runtime-eval/hevc-workbench-optimization/analyze.py \
  --input /private/tmp/altt-workbench-opt-20261017/comparison-c \
  --expected-psycopg-implementation c \
  --output /private/tmp/altt-workbench-opt-20261017/comparison-c-analysis.json

python3 docs/runtime-eval/hevc-workbench-optimization/analyze_renderer.py \
  --input /private/tmp/altt-workbench-opt-20261017/renderer-c \
  --output /private/tmp/altt-workbench-opt-20261017/renderer-c-analysis.json
```

Анализ требует полную матрицу, SQL/GIL/thread/source/clock evidence и парные
раунды; сохраняет все выбросы. Новый анализатор может читать перемещённые
evidence: исходные пути остаются частью записанного контракта Docker.

## Завершение

Сначала сохранить raw JSON, logs, manifests, snapshots и native build
provenance. Затем удалить только собственный контейнер
`altt-workbench-opt-pg-20261017`, volume `altt-workbench-opt-media-20261017`
и network `altt-workbench-opt-20261017`, остановить свой `caffeinate`.
Образы можно оставить для воспроизведения. Не использовать глобальный prune.
