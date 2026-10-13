# Whole-operation evidence (Linux ARM64)

Основной отчёт: `../../OPERATION_BENCHMARKS_315.md`. Четыре независимые series: core before/after dedup+MediaInfo batch, generated/hash supplement, candidate-only persistent HEVC pool и original→batch/incremental4. Пилот HTTP с validation внутри timer сохранён отдельно и не объединён с окончательной выборкой.

Точные чистые source snapshots архивированы, .env/data/venv отсутствуют:

- `source-baseline.tar.gz`, manifest `source-baseline-manifest.json` (211 файлов).
- `source-optimized.tar.gz`, manifest `source-optimized-manifest.json` (213 файлов).
- `source-pool-candidate.tar.gz`, manifest `source-pool-candidate-manifest.json` (216 файлов).

`source-overlays/baseline` и `source-overlays/optimized` содержат точные пять изменённых production modules. `source-workers-candidate.tar.gz` и source-workers-candidate-manifest.json сохраняют отдельный default incremental4 candidate (214 файлов); source-overlays/workers-candidate — точные job/policy regressions. `source-overlays/pool-candidate` содержит helper, releases_view, adapter, 70 candidate regressions и predeclared chunk policy. Pool variant не подключён к production.

`runner/` хранит exact measured runners и их общие helpers, `benchmark_operations315.pilot.py` — исходный pilot. `evidence-manifest.json` содержит SHA256/bytes всех evidence inputs; raw JSON сохраняются отдельно в `docs/operation_benchmark*.json`.

`runtime-provenance.json` фиксирует 46 одинаковых distributions, native libs и GIL каждой сборки; `images.txt` — exact local Docker image IDs; `postgres-settings.json` — PostgreSQL16.15 durability ON. Runtime build inputs находятся в `../315t/`, сборки/исправления libmpdec/LibYAML/libpq описаны там. Moving Docker/apt tags сами по себе не гарантируют те же bytes в будущем.

`validation/` — 1036 passed в каждом из трёх normalized modes и regular binary candidate; `validation-pool/` — 1106 passed в каждом из трёх normalized modes. `validation-workers/` — 1041 passed в каждом из трёх normalized modes и regular binary candidate. Это full suites с настоящими PG tests, не latency measurements.

Реальные4MKV пользователя не копируются в репозиторий. `dataset-manifest.json` хранит size/mtime_ns before+after; `real-media-blake3.json` — BLAKE3, уже вычисленные actual hash job (без лишнего повторного чтения), `composition-real.bencode` — синтетическая composition fixture для FileTracker; это не torrent для загрузки из qB, piece data не проверяются этим job. `generated-seed.mkv` — собственный 3s FFmpeg AVC/AAC clip, копируемый96раз для batch workload; FFmpeg command/version/seedSHA сохранены. Этот однородный generated dataset описывает batch scaling, не разнообразие реальной медиатеки.

Создание изолированной PostgreSQL (имена ниже принадлежат только этому эксперименту, без host ports):

```sh
docker network create --internal --label altt.operation-eval=20261013 altt-operations-315-20261013
docker volume create --label altt.operation-eval=20261013 altt-operations-315-pgdata-20261013
docker run -d --name altt-operations-315-pg-20261013 --label altt.operation-eval=20261013 --network altt-operations-315-20261013 --mount type=volume,source=altt-operations-315-pgdata-20261013,target=/var/lib/postgresql/data -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=altt_operations postgres:16
```

Trust auth используется только на этой isolated internal network. `operations_tests` — отдельная БД для full suites; benchmark никогда не обращается к ней. Benchmark DBs: `operations_hevc`, `operations_media_real`, `operations_media_generated`, `operations_hash`.

Для локального воспроизведения `run*.sh` ожидают `/private/tmp/altt-operation-eval-20261013/{baseline/backend,optimized-final/backend,hevc-pool-candidate/backend,mediainfo-workers-candidate/backend,fixtures,results}`. Распаковать source archives в соответствующие backend directories. Скопировать generated-seed96раз в fixtures/generated, сохранив один corpus неизменным для всех режимов; восстановить mtime_ns каждой собственной копии по generated.inventory в manifest; либо выполнить generate_fixtures.py и зафиксировать новый manifest. В fixtures/torrents поместить composition-real.bencode как real.torrent и composition-real.manifest.json как real.manifest.json. Убедиться, что readonly media path соответствует вашим тем же4MKV, имя/размер/mtime и рассчитанные hashes сверены с manifest. Если корпус изменился — это новый эксперимент, не повтор исходной выборки.

Запускать последовательно, без concurrent tests/builds:

```sh
./docs/runtime-eval/operations315/run.sh --prepare --source /bench/baseline
./docs/runtime-eval/operations315/run.sh --baseline /bench/baseline --optimized /bench/optimized --rounds 12 --cases hevc_http mediainfo_real_full mediainfo_real_changed --output /bench/results/operation_benchmark_core_v2_linux_arm64.json
./docs/runtime-eval/operations315/run_supplement.sh --baseline /bench/baseline --optimized /bench/optimized --output /bench/results/operation_benchmark_supplement_linux_arm64.json
./docs/runtime-eval/operations315/run_pool.sh --baseline /bench/baseline --optimized /bench/optimized --rounds 12 --cases hevc_http --output /bench/results/operation_benchmark_hevc_pool_linux_arm64.json
./docs/runtime-eval/operations315/run_workers.sh --baseline /bench/baseline --optimized /bench/optimized --rounds 12 --cases mediainfo_real_changed mediainfo_generated_changed --output /bench/results/operation_benchmark_mediainfo_workers_linux_arm64.json
```

`run_pool.sh` намеренно remaps baseline=optimized-final (dedup-only), optimized=hevc-pool-candidate (dedup+pool). `cold_request` включает lazy pool creation только в pool candidate; `warm_request` использует тот же persistent pool после двух warmups. pool state0→1→1 и full HTML assertions обязательны.

Все runtime wrappers выбирают regular / free-threaded GIL-on / free-threaded default GIL-off; forced GIL-off поверх несовместимых extensions не применяется. HTTP — настоящий application handler/SQL/ORM/Jinja с in-process ASGI transport, без TCP/TLS/browser. MediaInfo/hash — полный direct handler, без JobRunner wrapper; wall включает PG waits/durable commits. CPU/RAM — application child, не PostgreSQL; HWM включает preparation. См. заранее сохранённый `PROTOCOL.md`.

После завершения удалить только свой PG container, volume и network. Shared runtime images, source snapshots, user media и unrelated Docker resources не удалять.

Workers series отдельно remaps optimized=mediainfo-workers-candidate и baseline=original. Intermediate batch+worker1 не используется как paired reference и не складывается с новым эффектом. Actual peak parse call concurrency1→4 подтверждён во всех runtime; native parsers возвращают одинаковые reports. Median улучшен, p95 regular остаётся неопределённым, outliers не удалены.
