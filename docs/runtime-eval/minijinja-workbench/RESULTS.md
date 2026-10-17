# MiniJinja 3 / HEVC workbench: эксперимент 17 октября 2026

Эксперимент изолирован от рабочего приложения. Production requirements,
Dockerfile, маршруты, шаблоны и бизнес-правила не переключены. Это замена
renderer внутри FastAPI, а не смена веб-фреймворка.


**Вывод:** MiniJinja 3 технически работает на настоящем 3.15t без GIL и ускоряет
рендер фрагмента. Для полной обработки live-запроса workbench убедительного
выигрыша над Jinja не получено. Миграцию ради скорости этого экрана сейчас
отложить; следующим шагом профилировать подготовку context. Jinja тоже прошла
проверки без GIL — менять renderer только ради включения free-threading не нужно.

## Что реализовано

`backend/experiments/minijinja_workbench/prototype.py` содержит адаптер MiniJinja
для существующего `TemplateResponse`. Он заранее загружает три неизменённых
шаблона: `base.html`, `hevc_workbench.html`, `partials/hevc_workbench_live.html`.
Оригинальный route factory и service workbench используются без изменений.

Перед MiniJinja контекст копируется в явный снимок из простых значений:
dict/list/строки/числа. Request, Session, ORM и произвольные Python-объекты в
него не попадают. Это предотвращает неявный доступ renderer к приложению;
стоимость копирования включена в основной замер. При этом Python dict/list
всё ещё читаются через binding: рендер нельзя называть полностью Rust-only.

Контроль `snapshot_jinja` использует тот же снимок и предварительно загруженные
шаблоны. Он помогает отделить эффект движка от изменения loader/cache и
преобразования контекста. Основной baseline `current_jinja` сохраняет реальный
Jinja2Templates с FileSystemLoader и исходным Request.

## Сборка и GIL

MiniJinja **3.0.1** собран из опубликованного PyPI sdist с SHA-256
`7e1b012a56414134adc46a79a649b529c35a3b955ccb8407a4ce34441c9842ce`.
Rust/Python исходники и Cargo.toml не менялись. Rust/cargo 1.85.1,
GCC 14.2, maturin 1.15.0; дополнительные Rust/PyO3 flags не вводились.

Получены два native wheel:

- ordinary: `minijinja-3.0.1-cp310-abi3-manylinux_2_38_aarch64.whl`;
- free-threaded: `minijinja-3.0.1-cp315-cp315t-manylinux_2_38_aarch64.whl`.

Это настоящий CPython **3.15.0**, Linux ARM64, PGO/LTO, без JIT. Вторая сборка
имеет `--disable-gil` и native `Py_GIL_DISABLED`. Списки установленных Python
пакетов двух образов совпадают, в том числе Jinja2 3.1.6 / MarkupSafe 3.0.4.

С опубликованным monorepo Cargo.lock наша строгая сборка `maturin --locked`
не прошла: состав workspace в sdist меньше. Полный Cargo metadata нормализовал
lock с 428 до 106 packages. Все оставшиеся versions/sources/checksums сохранены,
добавлений/обновлений нет. Effective lock SHA-256:
`876f00d2ee00cb15a8a72361c708e39f79e52215e9c23fffc948d496696b9ea5`.
Исходный lock, effective lock, diff и проверки сохранены. Это не доказывает
ошибку обычного `pip install` без `--locked`; такой вывод не делается.

Нативный smoke probe использует одно общее Environment, 256 рендеров и восемь
реально участвовавших потоков, включая Python filter callback и dataclass
attributes. Результат и escaping сверены с Jinja. В 3.15t GIL остался выключен
после imports и рендеров; принудительное `PYTHON_GIL=0` не использовалось.

## Корректность

Во всех трёх режимах — ordinary, 3.15t `-X gil=1`, 3.15t default — выполнено
**70 passed, 0 skipped/failed/errors**. Это четыре модуля: workbench service,
workbench UI, live partials и 19 новых проверок прототипа. Проверяются в том числе
escaping, пустые данные, отсутствующие файлы, снимки без общих mutable-значений,
контекстные процессоры и изоляция конкурентных запросов. Фактический GIL
проверен в 152 checkpoints каждого тестового запуска, включая `app.main`.

В каждом режиме выполнены **54 HTTP-проверки**: 18 сочетаний page/live,
filters/search/page × три renderer. Они достигают branches offsite/Unicode/
HTML-метасимволов, unknown receipts, unavailable и changed rows. DOM совпал во
всех сравнениях; исходные HTML bytes у MiniJinja отличаются в 18 случаях.
Например, `/` экранируется как `&#x2f;`. Сравнение сохраняет атрибуты, порядок
и текст, а также точные script/style/pre/code/textarea; нормализуются entities
и форматирующие пробелы обычного HTML. Первые различия bytes записаны в JSON.

GIL во всех 333 checkpoints каждого correctness-запуска соответствует режиму:
false у default 3.15t, true у остальных. В каждом запуске выполнено 912 SELECT,
без SQL writes/commits. Существующие JS-проверки workbench также прошли:
selection/version/SSE/details/scroll/send/conflict. Это не браузерный SSE E2E.

## Границы измерения

Стенд: Apple M2 Pro, Docker Desktop Linux ARM64, VM 10 CPU / 8 216 862 720 bytes
RAM. PostgreSQL **16.15**, отдельная internal network и база, tmpfs; fsync,
full_page_writes и synchronous_commit включены. Production сервисы и данные
не используются. Посторонние пользовательские контейнеры не останавливались.

В обоих runtime одинаковые **pure psycopg 3.3.6 + libpq 17.11**. Это осознанная
граница: production `psycopg-binary` не имеет подходящего t-wheel и здесь не
сравнивается. Результат ARM64 не переносится на AMD64/Unraid без проверки.

Fixture: 1024 релиза, 5248 архивов, 43 392 файловые записи; медиа — реальные
маленькие синтетические файлы. Domain clock фиксирован до imports. Основной
URL `/hevc-workbench/live?view=all&page=1&per_page=30`. Полная страница проверена
на корректность, её производительность отдельным сценарием не измеряется.

HTTP: один ASGI loop, shared Environment, до четырёх AnyIO route threads,
оригинальные SQL/HEVC/readiness и контекстный процессор cached Kensetsu settings.
Измеряются SQL, Python-вычисления, filesystem probe видимых строк, снимок,
рендер и готовый HTML response. TCP/TLS и браузер исключены. Warmup и запуск
процесса исключены из метрик; DOM/hash validation выполняется после таймера.

Render-only использует тот же service context, подготовленный до таймера;
снимок кандидата входит в timer. Контроль snapshot_jinja образует отдельную
парную серию. Объём и порядок зафиксированы в [PROTOCOL.md](PROTOCOL.md): восемь
раундов на конфигурацию, восемь HTTP-запросов либо 64 render в каждом sample.
Outliers не удаляются. 95% CI — paired block bootstrap целых раундов (5000
ресемплов, seed 315003). P95 при 64 HTTP наблюдениях — исследовательская оценка,
не доказательство отсутствия tail-регрессии.

Первая неполная серия (29 samples) целиком исключена из primary: `pmset`
подтвердил сон host, UTC timestamps расходились с monotonic elapsed. Она
сохранена вместе с `suspension-audit.json`. Новая полная серия запущена в
`results-uninterrupted` с `caffeinate -is`; её объём и порядок не менялись.
Никакие отдельные медленные/быстрые наблюдения из primary не удаляются.

CPU/wall относится ко всему процессу, включая ASGI driver, без CPU PostgreSQL.
RSS/HWM включают последующую проверку результата: это whole-process diagnostic,
не точный peak renderer. Перекрытие calls не доказывает CPU-параллелизм само по
себе; оно рассматривается вместе с GIL checkpoints, worker IDs и CPU/wall.

## Почему важна полная обработка запроса

До pagination сервис читает все archives/files, строит составы и pairing,
определяет receipts/jobs/metadata, формирует кандидатов, считает filters и
сортирует. Только после этого выбирает 30 строк. Filesystem resolve/scandir
и pipeline decoration ограничены видимой страницей: все 43 392 пути на диске
не сканируются. См. `hevc_workbench.py:138–246`,
`hevc_pairing.py:file_keys_by_archive_id/find_unpaired_avc`.

В fixture один HTTP-запрос выполняет 17 SELECT: 13 service queries и четыре
settings reads в фазе HTML response. Поле audit `render` этой фазы включает
контекстный процессор и HTMLResponse, поэтому оно не обозначает чистый
template CPU. Причину нелинейного поведения C=4 отдельный profiler не измеряет:
приписывать её только GC, SQL contention или allocations нельзя.

Воспроизведение и synthetic preview описаны в [RUN.md](RUN.md), сборка —
в [BUILD.md](BUILD.md). Preview не подключает SSE и отправку в Kensetsu.

## Измеренные результаты

Завершены все **192 samples / 24 конфигурации**: 512 измеренных HTTP-ответов,
4096 render и 4096 snapshot-control render. Во всех 8704 операциях — одинаковый
DOM и 30 строк; все 512 HTTP-ответов имеют status 200. SQL writes/commits отсутствуют. Проверены 23 424 GIL
checkpoints. HTTP выполняет 136 SELECT на sample (17 на запрос), render — ноль
в измеренном интервале. Хеши семи ключевых исходников совпали, 47 distributions
одинаковы между runtime. Основная непрерывная HTTP-серия: 09:44:47–10:26:43 UTC;
максимальное расхождение UTC/monotonic между запусками — 0,0062 секунды.

### HTTP: живое обновление workbench

Ниже median одного запроса и общий throughput измеренных batches. Это закрытая
нагрузка с одним либо четырьмя одновременными клиентами, без сетевого транспорта:

- **Обычный 3.15, C1:** Jinja 1,220 с / 0,810 запроса/с;
  MiniJinja 1,222 с / 0,815 запроса/с.
- **Обычный 3.15, C4:** Jinja 26,999 с / 0,147 запроса/с;
  MiniJinja 27,497 с / 0,144 запроса/с.
- **3.15t без GIL, C1:** Jinja 1,410 с / 0,704 запроса/с;
  MiniJinja 1,412 с / 0,707 запроса/с.
- **3.15t без GIL, C4:** Jinja 5,167 с / 0,769 запроса/с;
  MiniJinja 5,021 с / 0,781 запроса/с.

Главное сравнение renderer на целевом runtime, **3.15t/C4**: MiniJinja даёт
**+1,54% throughput**, 95% CI **от −1,99% до +4,84%**. Median ниже на 2,82%,
CI изменения от −6,01% до +0,95%. Интервалы включают отсутствие улучшения;
устойчивый выигрыш полной обработки не подтверждён. P95: Jinja 5,451 с,
MiniJinja 5,513 с; 64 наблюдения на конфигурацию не позволяют обещать tail parity.

На ordinary/C4 MiniJinja немного хуже: throughput −1,78%, CI от −2,76% до
−0,70%. При C1 убедительного отличия движков нет в обоих runtime. Абсолютные
секунды относятся к этому большому synthetic fixture, а не к production SLA.

### Что изменяет runtime

На том же MiniJinja переход ordinary → GIL-off при C4 даёт **5,42× throughput**
(CI 5,26–5,55×) и median 27,497 → 5,021 с. У текущей Jinja похожий результат:
5,24× throughput. Следовательно, большой эффект относится к runtime и этому
конкурентному пути с pure psycopg, а не к замене шаблонизатора.

При C1 у MiniJinja переход на 3.15t увеличивает median на **15,57%**
(CI +13,47…+16,49%) и снижает throughput на 13,22%. Более того, внутри самого
3.15t переход C1 → C4 увеличивает throughput только на **10,53%**
(CI +7,34…+13,26%), при росте CPU/wall с 0,99 до 3,82 ядра. CPU на ответ —
1,400 → 4,894 CPU-с, около **3,5×** (точечный пересчёт из raw CPU / requests,
без отдельного CI). PostgreSQL CPU в это число не входит. Это работа на
нескольких ядрах, но не четырёхкратное масштабирование полного запроса.

Причина сильного падения ordinary/C4 отдельно не установлена. Benchmark не
разделяет GC, allocations, драйвер, SQLAlchemy и остальные части service;
причинный вывод о любом из них потребовал бы profiling. Сравнение с несколькими
обычными Uvicorn worker processes также не проводилось.

### Отдельный рендер и контроль снимка

На **3.15t/C4** текущая Jinja: **2036 render/с**, MiniJinja: **5428 render/с**.
Отношение **2,67×**, CI **2,54–2,79×**. Median render 1,848 → 0,665 мс.
На 3.15t/C1: 937 → 1871 render/с, median 1,011 → 0,500 мс. Материализация
контекста MiniJinja включена; SQL и filesystem readiness исключены.

В отдельной парной серии с одинаковым snapshot и preloaded templates на
3.15t/C4: snapshot Jinja **1751 render/с**, MiniJinja **5593 render/с**;
отношение **3,19×**, CI **3,10–3,28×**, median 2,196 → 0,659 мс. Выигрыш
сохраняется при этом контроле и не объясняется только удалением mtime checks.
Эту серию нельзя подмешивать к основной как дополнительные paired samples.

Почему ускорение renderer почти незаметно в HTTP: в существующих audit spans
MiniJinja/3.15t/C4 median подготовки context — **5013,9 мс**, фазы HTML response
— **3,10 мс**. Подготовка context занимает **99,918% суммы worker phase times**.
Фазы разных запросов могут перекрываться; это не распределение общего batch
wall и не точный CPU profile. Фаза response включает cached settings, snapshot,
renderer и HTMLResponse. Данные согласуются с устройством service: глобальная
подготовка истории выполняется до pagination.

## Практическое решение

После всех timed runs прошёл отдельный **Uvicorn/TCP smoke**: по восемь
HTTP-ответов каждого движка (full/live/search при C1/C4), DOM совпал.
В реальных sync worker threads GIL оставался false. Performance-числа из этой
проверки не собирались; Uvicorn использовал asyncio/h11. SSE swaps, выполнение
браузерного JavaScript и отправка в Kensetsu не проверялись E2E.
Первый TCP smoke не достиг сервера: Docker 29 не опубликовал HostPort на
internal-only network, хотя PortBindings были заданы. Его failure сохранён;
успешный повтор использует собственный temporary bridge для localhost и
отдельную internal DB network. Оба preview-контейнера и bridge удалены.

Прототип пригоден для дальнейших экспериментов с MiniJinja и компонентами UI:
сборка, escaping, оригинальный workbench и конкурентный GIL-off путь проверены.
Полную миграцию не стоит обосновывать ускорением этого экрана: выигрыш renderer
составляет доли миллисекунды на фоне секунд подготовки данных.

Сначала разделить profiling service на SQL/fetch/hydration → file keys/pairing →
receipts/meta → построение кандидатов → counts/sort → page probes. После этого
можно оценивать cache/view model или более узкие выборки, сохраняя исторические
правила HEVC. Просто перенести LIMIT 30 в начало нельзя: matching и порядок
зависят от других версий торрентов и SLA. В этом эксперименте бизнес-логика
не оптимизировалась.

Все численные результаты и CI находятся в [analysis.json](evidence/analysis.json),
исходные samples/логи/команды/снимок исходников — в
[raw-evidence.tar.gz](evidence/raw-evidence.tar.gz), контрольные суммы — в
[SHA256SUMS](evidence/SHA256SUMS). Существующие production-файлы не изменены.
