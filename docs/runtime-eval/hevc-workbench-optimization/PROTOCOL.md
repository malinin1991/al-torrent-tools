# Подготовка HEVC workbench: протокол до сравнительных измерений

2026-10-17. Цель — найти и устранить затраты подготовки данных без изменения
правил HEVC, истории, доступности действий и формы выдаваемого контекста.

## Диагностика

До изменений сохранить текущий `hevc_workbench.py` побайтно и SHA-256. Снять
cProfile и отдельные фазовые wall timings на обычном 3.15 и default GIL-off
3.15t. Профилирование — диагностика, не источник сравнительных performance
цифр: profiler/tracing/tracemalloc меняют стоимость операций. Проверки SQL
различают execute и fetch/type processing; execute не выдаётся за полную цену
загрузки данных. После реализации повторить тот же диагностический проход.

Не добавлять общий mutable cache, process/thread pool, раннюю pagination или
новые правила matching ради выигрыша. Оптимизировать по измеренному профилю.
Исторические строки, raw/exact identity, порядок pairing и pre-filesystem
counts/ready pagination сохраняются. Изменение представления строк допустимо.

## Контракт сравнения

Baseline и optimized — два неизменяемых backend snapshot, с manifests, исходным
архивом и хешами кода. Оба используют текущую Jinja2Templates, тот же исходный
route factory, UI и benchmark harness из предыдущего MiniJinja-эксперимента.
Смена renderer не входит в before/after. Старые результаты не подмешиваются.

Одинаковые существующие CPython **3.15.0** ordinary / free-threaded images,
Linux ARM64 в Docker Desktop, PGO/LTO, без JIT, одинаковые pins и pure
psycopg 3.3.6 + system libpq. Это не production psycopg-binary/Unraid AMD64.
Фактический GIL проверяется при imports/setup/warmup/запросах в worker threads;
`PYTHON_GIL=0` / `-X gil=0` запрещены. 3.15t с `-X gil=1` — correctness control.

Собственные disposable PostgreSQL 16, internal network и tiny media volume;
никаких production DB, .env, qB/Telegram/Kensetsu requests или scheduler.
Та же fixture предыдущего эксперимента: **1024 releases / 5248 archives /
43 392 files** и фиксированный domain UTC clock. PostgreSQL на tmpfs;
fsync/full_page_writes/synchronous_commit включены. Timed SQL read-only.

Основной URL: `/hevc-workbench/live?view=all&page=1&per_page=30`.
Один ASGI event loop / shared renderer Environment / AnyIO limiter 4;
закрытая нагрузка C1 либо C4, запросы входят в timer после client semaphore.
SQL, fetch/conversion, HEVC, readiness видимой страницы, cached shell settings,
рендер и HTMLResponse внутри timer. TCP/TLS, browser JS и реальные SSE swaps
вне performance-серии. Lifecycle внешних сервисов не запускается.

Перед каждым sample — четыре concurrent warmup requests, проверка четырёх
route workers и одинакового HTML. Import/startup/warmup вне timer. Декодирование
HTML/DOM/hash и assertions после timer. RSS/HWM — диагностика всего процесса,
включая setup, прогрев и проверку результата, а не точный peak service.

## Корректность перед performance

Differential tests сравнивают весь context нового service с сохранённой старой
функцией при фиксированном clock: все views/search/pagination, история и SLA,
duplicate hashes, receipts, unavailable/changed rows, sticky status, offsite
links и обновления БД/файлов между вызовами. Проверять отсутствие SQL mutations.
Существующие HEVC, workbench, live UI/encoder regressions также обязательны.

На большой fixture проверить DOM/контекст baseline и optimized на обычном 3.15,
3.15t GIL-on и 3.15t GIL-off. Исходные templates не меняются. Все расхождения
до измерений расследуются; output нельзя нормализовать ради сокрытия изменения
статусов, порядка, атрибутов, counts или actions.

## Заранее фиксированный объём

**8 rounds × 8 requests** для каждой комбинации:
baseline/optimized × ordinary/GIL-off × C1/C4. Всего **64 samples / 512 requests**,
64 latency observations на конфигурацию. Порядок четырёх runtime/concurrency
групп вращается и разворачивается; варианты идут рядом AB/BA, по четыре раза.
Каждая группа занимает каждую позицию дважды за восемь раундов.

Все measured runs последовательны, без параллельных builds/pytest/profilers.
На macOS `caffeinate -is` удерживается на протяжении эксперимента; UTC и
monotonic duration сверяются. Чужие контейнеры не останавливаются. При внешнем
прерывании сохранить исключённую серию целиком и запустить новую в другом
каталоге, не заменяя отдельные samples.

## Анализ и решение

Median/p95 — pooled request latency, throughput = total requests / total batch
wall. CPU/wall и CPU на ответ — process CPU, включая ASGI driver, без PostgreSQL.
95% CI — **5000 paired block bootstrap** целых раундов, seed **315017**.
Outliers не удаляются; p95 с 64 observations остаётся исследовательским.

Сравнения: optimized/baseline в каждом runtime/C; GIL-off/ordinary внутри
каждого варианта/C; C4/C1 внутри каждого runtime/варианта; optimized GIL-off
против baseline ordinary. Отдельно сообщить абсолютные задержки, расход CPU
и memory. Не называть рост CPU линейным ускорением, не приписывать изменение
одному компоненту без диагностического подтверждения.

После результатов объём основной серии не увеличивать ради желаемого вывода.
Финальная проверка: исходники замеров соответствуют финальному production diff;
документировать оптимизацию, тесты, ограничения и воспроизводимые raw evidence.

## Отдельный контроль драйвера (до его измерений)

Дополнительно собираем без native patches `psycopg-c==3.3.6` поверх обоих
исходных images. Его результат не смешивается с основной pure-серией.
При успешном natural GIL probe повторяем тот же заранее фиксированный дизайн
64 samples / 512 requests в отдельном каталоге, с равными backend/libpq/pins
внутри пары. Это проверка зависимости вывода от драйвера, а не решение
об изменении production-зависимостей. C-реализация и production binary wheels
могут иметь разные libpq/сборку; ARM64 Docker не заменяет Unraid AMD64.

## Повтор сравнения шаблонизаторов после оптимизации

Отдельная серия на одном frozen optimized backend и C-backend psycopg:
Jinja2 / MiniJinja 3.0.1 × ordinary / natural GIL-off × C1/C4; те же
8 rounds × 8 requests, всего 64 samples / 512 HTTP requests. Пары engine
идут рядом AB/BA, группы вращаются по тому же расписанию. Никакого смешивания
с before/after или прежними измерениями. HTML может отличаться эквивалентным
escaping, DOM, строки, действия и статусы должны совпадать. Все остальные
границы, прогрев, источник, clock guard и bootstrap seed315017 сохраняются.
Дополнительно показываем wall duration context-span внутри HTTP-запросов:
она включает ожидание I/O/планировщика, не является чистым CPU и её pooled
median нельзя складывать с pooled median других span.
