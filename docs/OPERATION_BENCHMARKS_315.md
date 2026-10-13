# Целые операции HEVC / MediaInfo: CPython 3.15 и 3.15t

Итоговое решение: рабочий runtime — обычный CPython 3.15.0 с GIL; в рабочий код перенесён default incremental4 с сохранением explicit workers1. Выбор policy сделан по приоритету typical wall time: median real4 улучшился на27.67%, generated96 на46.53%. P95 nonregression НЕ доказан: real4 point384→454ms, generated96 774→815ms, широкие CI сохранены. HEVC pool и 3.15t остаются экспериментальными. Это явный tradeoff median latency, не объявление p95 pass.

До/после сравниваются одинаковые реальные операции с PostgreSQL 16.15 и native MediaInfo 26.10 на Linux ARM64 в Docker Desktop. Основной критерий — wall latency. Core и supplement — независимые серии по 12 сбалансированных rounds, по 216 samples и 18 исключённых correctness/warmup samples. Все выбросы сохранены.

HEVC оптимизация устраняет повторный full pairing; MediaInfo собирает результаты native parse в bounded batch до 25 и записывает их на caller Session. В core full scan сохраняет4 parse workers, incremental1 — промежуточная policy; окончательный incremental4 измерен отдельно; whole hash follow-up использует 4 вместо последовательного parse. Финальные5 production modules byte-identical проверенному workers candidate;1041 full tests прошли во всех4режимах (regular pure/t-on/t-off/regular binary).

Regular dedup улучшил HEVC HTTP примерно на14–16%; batch ускорил generated96 MediaInfo на25–41%. На user4 отдельный full scan и whole hash не доказали устойчивого ускорения. HEVC persistent pool не окупился по whole HTTP против dedup regular; t-off compute improvement не заменяет end-to-end latency. Ordinary runtime выбран root; Unraid AMD64 и production binary backend performance здесь не измерены.

## Измерения

Ниже абсолютные median / p95 указаны в ms. Процент median — медиана парных отношений, поэтому может отличаться от отношения двух абсолютных медиан. CI: 10 000 paired bootstrap resamples, seed 315314. Для p95 пересчитываются квантили на парных bootstrap indices. Диагностический допуск отсутствия регрессии — 3%; CI, пересекающий предел, означает неопределённость, а не pass. CPU/RAM вторичны и не являются отказным gate.

### HEVC: первый HTTP request нового процесса

Regular: **718.25 / 731.94 → 613.65 / 650.08 ms**. Парный median: -14.04% (95% CI -15.52…-12.65%); p95: -11.18% (95% CI -14.58…-10.15%).

Оптимизированный t GIL-off: **708.81 / 768.02 ms**. Относительно optimized regular: median +16.50% (95% CI +12.04…+19.39%); p95 +18.14% (95% CI +12.86…+23.83%).

CPU time процесса, median before regular / after regular / after t-off: 689.43 / 584.05 / 681.83 ms; t-off vs after regular +16.27% (95% CI +13.59…+20.56%). Whole-child VmHWM: 144.18 / 144.77 / 176.76 MiB; парная t-off разница +31.94 MiB (95% CI +30.84…+32.07).

### HEVC: HTTP request после двух прогревочных

Regular: **652.96 / 682.65 → 553.79 / 566.13 ms**. Парный median: -15.85% (95% CI -16.51…-14.72%); p95: -17.07% (95% CI -18.03…-14.42%).

Оптимизированный t GIL-off: **640.78 / 662.76 ms**. Относительно optimized regular: median +16.54% (95% CI +12.76…+17.60%); p95 +17.07% (95% CI +14.48…+19.21%).

CPU time процесса, median before regular / after regular / after t-off: 634.76 / 534.26 / 621.95 ms; t-off vs after regular +17.31% (95% CI +13.64…+18.53%). Whole-child VmHWM: 150.71 / 151.17 / 203.20 MiB; парная t-off разница +52.43 MiB (95% CI +46.17…+57.23).

### MediaInfo full: 4 настоящих MKV

Regular: **284.15 / 338.01 → 276.98 / 363.54 ms**. Парный median: -0.96% (95% CI -7.29…+2.34%); p95: +7.55% (95% CI -21.08…+34.92%).

Оптимизированный t GIL-off: **288.01 / 405.41 ms**. Относительно optimized regular: median +4.68% (95% CI -3.74…+6.55%); p95 +11.52% (95% CI -13.05…+46.25%).

CPU time процесса, median before regular / after regular / after t-off: 325.84 / 301.37 / 310.59 ms; t-off vs after regular +3.50% (95% CI -0.65…+9.65%). Whole-child VmHWM: 98.30 / 98.47 / 124.47 MiB; парная t-off разница +25.85 MiB (95% CI +24.52…+26.00).

### MediaInfo changed incremental: 4 настоящих MKV

Regular: **360.47 / 387.74 → 360.25 / 399.04 ms**. Парный median: +0.44% (95% CI -5.54…+7.81%); p95: +2.91% (95% CI -3.22…+10.40%).

Оптимизированный t GIL-off: **352.30 / 382.38 ms**. Относительно optimized regular: median -0.09% (95% CI -7.49…+3.11%); p95 -4.17% (95% CI -8.45…+1.60%).

CPU time процесса, median before regular / after regular / after t-off: 167.58 / 168.10 / 167.98 ms; t-off vs after regular +0.18% (95% CI -3.32…+4.68%). Whole-child VmHWM: 95.02 / 95.64 / 118.12 MiB; парная t-off разница +21.99 MiB (95% CI +20.89…+22.43).

### MediaInfo full: 96 созданных MKV

Regular: **673.10 / 687.38 → 396.83 / 418.74 ms**. Парный median: -41.23% (95% CI -42.45…-37.46%); p95: -39.08% (95% CI -41.61…-37.82%).

Оптимизированный t GIL-off: **400.33 / 523.32 ms**. Относительно optimized regular: median +0.92% (95% CI -5.62…+5.20%); p95 +24.97% (95% CI -2.44…+39.76%).

CPU time процесса, median before regular / after regular / after t-off: 1096.32 / 672.39 / 689.44 ms; t-off vs after regular +2.43% (95% CI -0.99…+7.57%). Whole-child VmHWM: 99.18 / 100.19 / 128.17 MiB; парная t-off разница +27.75 MiB (95% CI +27.10…+28.10).

### MediaInfo changed incremental: 96 созданных MKV

Regular: **744.12 / 823.29 → 558.37 / 610.92 ms**. Парный median: -24.64% (95% CI -29.07…-21.84%); p95: -25.79% (95% CI -30.34…-19.80%).

Оптимизированный t GIL-off: **585.26 / 631.05 ms**. Относительно optimized regular: median +6.14% (95% CI +2.47…+7.99%); p95 +3.29% (95% CI -1.54…+8.42%).

CPU time процесса, median before regular / after regular / after t-off: 627.94 / 468.09 / 494.20 ms; t-off vs after regular +6.81% (95% CI +2.99…+8.81%). Whole-child VmHWM: 95.69 / 97.43 / 119.68 MiB; парная t-off разница +21.97 MiB (95% CI +21.24…+23.18).

### Whole hash handler: 4 настоящих MKV, qB inventory fixture

Regular: **2789.21 / 3396.72 → 2783.61 / 2884.31 ms**. Парный median: -2.48% (95% CI -11.58…+1.88%); p95: -15.09% (95% CI -23.33…+1.98%).

Оптимизированный t GIL-off: **2554.46 / 3056.55 ms**. Относительно optimized regular: median -6.28% (95% CI -10.03…+6.01%); p95 +5.97% (95% CI -7.84…+7.44%).

CPU time процесса, median before regular / after regular / after t-off: 3793.43 / 4024.09 / 4043.68 ms; t-off vs after regular +1.15% (95% CI -1.74…+3.97%). Whole-child VmHWM: 117.25 / 116.06 / 145.64 MiB; парная t-off разница +29.42 MiB (95% CI +27.73…+30.30).

## Отдельный HEVC pool candidate: dedup-only → dedup+pool

Самостоятельная серия12rounds/72samples плюс6 excluded warmups. Baseline здесь — optimized-final dedup-only, candidate — snapshot+lazy persistent4 pool. SQL/ORM/snapshot/serialization/compute/merge/Jinja входят actual request. Четыре contiguous release tasks заданы до измерений:1024→4×256. created_count0→1→1 подтверждён; regular/t-on cold3threads→warm4, t-off4→4. Ни production, ни optimized-final этим кандидатом не изменены.

### первый request

Regular before→after median/p95: **610.08/630.34 → 660.09/683.03 ms**. Парный median +8.38% (95% CI +6.19…+10.84%); p95 +8.36% (95% CI +6.11…+10.61%).

T-off candidate median/p95: **679.42/950.77 ms**; относительно dedup-only regular: median +11.69% (95% CI +10.46…+21.78%); p95 +50.83% (95% CI +14.09…+65.95%).

Regular CPU before→after median 582.51→633.35ms, +9.00% (95% CI +6.93…+11.02%); whole-child VmHWM 144.94→144.94MiB, paired delta -0.02MiB (95% CI -0.89…+0.10).

T-off CPU median 752.67 ms, изменение к тому же reference +28.75% (95% CI +27.48…+43.33%); whole-child VmHWM 195.93 MiB, парная разница +50.90 MiB (95% CI +49.40…+51.35).

Внутри t-off dedup→pool: median -1.84% (95% CI -4.00…+2.42%); p95 +32.63% (95% CI -0.10…+45.73%). Это сравнение самого t режима, не выигрыш перед обычным regular runtime.

### warm request

Regular before→after median/p95: **543.79/574.94 → 584.59/654.32 ms**. Парный median +7.28% (95% CI +6.38…+11.58%); p95 +13.81% (95% CI +2.30…+25.57%).

T-off candidate median/p95: **620.02/721.26 ms**; относительно dedup-only regular: median +13.82% (95% CI +11.97…+14.86%); p95 +25.45% (95% CI +6.32…+40.82%).

Regular CPU before→after median 524.80→565.95ms, +7.64% (95% CI +6.42…+12.17%); whole-child VmHWM 152.53→151.15MiB, paired delta -0.65MiB (95% CI -1.92…-0.44).

T-off CPU median 700.34 ms, изменение к тому же reference +33.37% (95% CI +30.76…+33.96%); whole-child VmHWM 229.15 MiB, парная разница +76.27 MiB (95% CI +65.77…+79.53).

Внутри t-off dedup→pool: median -3.83% (95% CI -4.12…-1.65%); p95 +9.90% (95% CI -3.92…+19.28%). Это сравнение самого t режима, не выигрыш перед обычным regular runtime.

## Отдельный финальный MediaInfo candidate: original → batch+incremental4

Самостоятельная серия12rounds/144samples плюс12 excluded warmups. Baseline ORIGINAL sequential incremental1/per-file commits, candidate batch+default incremental4; explicit workers1 сохраняется. Params только mode=incremental, без workers override. Core batch+worker1 остаётся промежуточной отдельной серией; эффекты не складываются и cross-series paired CI не вычисляются. Все samples выполняют настоящий native parse4/96 и DB updates, peak active parse calls диагностируются без подмены parser.

### real4 changed

Regular before→after median/p95: **359.15/384.00 → 259.35/453.99 ms**. Парный median -27.67% (95% CI -31.35…-23.53%); p95 +18.23% (95% CI -31.36…+29.63%).

T-off candidate median/p95: **258.93/269.56 ms**; относительно final regular candidate: median +0.61% (95% CI -5.38…+2.36%); p95 -40.62% (95% CI -45.72…+2.54%).

Regular CPU before→after median 170.90→284.76ms, +68.55% (95% CI +59.07…+81.40%); whole-child VmHWM 95.10→98.07MiB, paired delta +2.99MiB (95% CI +1.18…+3.01).

T-off CPU median 291.71 ms, изменение к тому же reference +2.07% (95% CI -6.14…+7.43%); whole-child VmHWM 127.60 MiB, парная разница +29.35 MiB (95% CI +28.44…+30.24).

### generated96 changed

Regular before→after median/p95: **704.96/773.61 → 383.56/814.91 ms**. Парный median -46.53% (95% CI -48.05…-41.30%); p95 +5.34% (95% CI -47.14…+17.91%).

T-off candidate median/p95: **373.02/402.88 ms**; относительно final regular candidate: median -5.15% (95% CI -11.37…+2.67%); p95 -50.56% (95% CI -56.13…-2.26%).

Regular CPU before→after median 594.65→644.60ms, +6.27% (95% CI +4.33…+17.28%); whole-child VmHWM 97.46→99.84MiB, paired delta +2.53MiB (95% CI +2.27…+4.32).

T-off CPU median 632.83 ms, изменение к тому же reference -2.77% (95% CI -8.38…+0.22%); whole-child VmHWM 130.91 MiB, парная разница +30.95 MiB (95% CI +29.97…+31.27).

## Граница и ограничения

HEVC timer — TestClient GET до полностью готового HTTP response, включая PostgreSQL SQL/ORM, grouping, pairing, sorting и Jinja. Transport ASGI in-process: TCP/TLS/browser исключены. Regex/assert/full HTML SHA выполняются после timer. HEVC UTC clock фиксирован только в harness на 2026-10-13T00:00:00; fixture dates без offset сохраняют fresh/missing/overdue/history/ignored сценарии. Cold означает первый request нового процесса; import/startup вне request timer, БД/диск не объявляются холодными. Production pool в HEVC отсутствует.

MediaInfo и hash wall — целый direct job handler, включая реальные native file IO/parse, BLAKE3 (hash case), нормализацию, production cache-gate lookups, PostgreSQL writes/commits/logging/audit. JobRunner scheduling/claim/status transitions не входят. Setup/reset/stale gate preparation/correctness queries вне wall timer. В каждом real sample parsed=4; hash sample также hashed=4, gated=0, errors=0, sticky new и hash_progress/hash_done/ui_status PipelineEvents. Generated sample parsed=96, updated=96, errors=0. Смысловые payloads / metadata / hashes / audit и полный HTML совпадают между source/runtime комбинациями. HEVC synthetic fixture:1024releases/5248archives/43392files; реальные медиа/MediaInfo rows, активные jobs и production pipeline population в этой странице не воспроизводились, поэтому это end-to-end application fixture, не нагрузочная копия пользовательской БД.

Четыре user MKV (1 500 398 629 bytes) mounted readonly, originals не изменялись. Host file cache не сбрасывался: warm cache, не физические холодные диски Unraid. Changed incremental предварительно получает настоящие reports и изменяет только их тестовый DB mtime на -60. Generated 96 × 117 980 bytes — копии собственного законного FFmpeg AVC/AAC 3s clip, отдельный batch workload; это не разнообразный real-media corpus. Whole hash подменяет только внешнюю qB inventory через FileTracker._qb_paths_and_priorities; реальные hashing/parse/DB/audit сохранены, qB HTTP latency не измерена.

46 identical distributions, pure psycopg3.3.6 + libpq170011 во всех режимах; это честная нормализация эксперимента, а не parity с production psycopg-binary. Native imports и фактический GIL True/True/False проверены, forced -Xgil=0 отсутствует. Core1036 tests, HEVC pool1106tests, окончательный workers candidate1041tests: каждый набор прошёл во всех своих normalized modes; core/workers также проверены на regular binary candidate. PostgreSQL fsync/synchronous_commit/full_page_writes ON. CPU quota=max, affinity/cpuset 0–9, Docker VM10 CPUs. Нативный Unraid AMD64 здесь не измерялся.

CPU/RSS относятся только к application child, не PostgreSQL. ru_maxrss/VmHWM — high-water mark всего child, включая imports/reset и native prepopulation changed-case до timer; rss_before_work также сохранён. Это preparation-inclusive диагностический RAM signal, не изолированный peak конкретной job. Fork/exec HWM floor возможен; /proc VmHWM сохранён отдельно.

Пилот остановлен по методическому решению root: HTTP validation ещё была внутри timer. Все его 42 завершённых samples сохранены отдельно и никогда не объединены с итогом. Окончательный exact runner и протокол v2 находятся в evidence.

## Воспроизведение

Все4 source manifests и clean archives, exact runner, dataset manifest, runtime/image/native/CPU provenance, PostgreSQL settings и полные validation logs находятся в [runtime-eval/operations315](runtime-eval/operations315/). Runtime build inputs сохранены в [runtime-eval/315t](runtime-eval/315t/). Скрипты run.sh/run_supplement.sh фиксируют readonly mounts и image tag; сверять сохранённый image ID, поскольку tag сам по себе изменяем.

Создать isolated internal network/PG без host ports, восстановить clean source snapshots в пути run.sh, подготовить собственные fixtures generate_fixtures.py; реальные MKV только readonly. Затем:

```sh
./docs/runtime-eval/operations315/run.sh --prepare --source /bench/baseline
./docs/runtime-eval/operations315/run.sh --baseline /bench/baseline --optimized /bench/optimized --rounds 12 --cases hevc_http mediainfo_real_full mediainfo_real_changed --output /bench/results/operation_benchmark_core_v2_linux_arm64.json
./docs/runtime-eval/operations315/run_supplement.sh --baseline /bench/baseline --optimized /bench/optimized --output /bench/results/operation_benchmark_supplement_linux_arm64.json
python3 docs/runtime-eval/operations315/summarize.py
```

Перед summarize.py скопировать четыре completed raw JSON в docs/operation_benchmark_core_linux_arm64.json , docs/operation_benchmark_supplement_linux_arm64.json, docs/operation_benchmark_hevc_pool_linux_arm64.json и docs/operation_benchmark_mediainfo_workers_linux_arm64.json. Повторять на свободном runtime без параллельных suites/builds. Не удалять пользовательские файлы или unrelated Docker resources.

Raw: [core](operation_benchmark_core_linux_arm64.json), [supplement](operation_benchmark_supplement_linux_arm64.json), [пилот](operation_benchmark_core_pilot_boundary_linux_arm64.json), [derived comparisons](operation_benchmark_summary_linux_arm64.json), [pool](operation_benchmark_hevc_pool_linux_arm64.json), [incremental4 candidate](operation_benchmark_mediainfo_workers_linux_arm64.json).
