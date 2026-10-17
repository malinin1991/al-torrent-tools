# HEVC workbench / MiniJinja: протокол эксперимента

Дата: 2026-10-17. Рабочий UI и его зависимости не переключаются. FastAPI,
HTMX, SSE, сервис workbench и SQL остаются общими; проверяется другой renderer.

## Вопросы

1. Собирается ли Python binding MiniJinja 3.0.1 на CPython 3.15t?
2. Остаётся ли GIL выключенным после imports, compile, filters и concurrent render?
3. Сохраняется ли HTML/DOM поведение полной страницы и live partial?
4. Меняются ли полная HTTP-задержка и throughput при 1 и 4 клиентах?

## Общий контракт

Native Linux ARM64 в Docker Desktop, ordinary CPython 3.15 и default GIL-off
3.15t из существующей пары PGO/LTO-сборок. Обе тонкие надстройки получают
одинаковые текущие Python pins и MiniJinja из одного SHA256 source archive.
Обычный ABI3 wheel не используется как wheel для t-интерпретатора.
Если monorepo Cargo.lock требует нормализации для состава sdist, сохранить
исходный/effective lock, точный diff и проверку retained versions/checksums.
Обычный pip source install без `--locked` отдельно не объявляется неработающим.

PostgreSQL 16 — собственный disposable container без host ports, на отдельной
internal network. Только база с префиксом `altt_minijinja`; рабочие БД,
qBittorrent, Telegram и Kensetsu не используются. Production psycopg-binary
не имеет t-wheels, поэтому в обоих режимах используется один pure psycopg +
system libpq. Это ограничение сравнения с production binary-контрактом.

Source snapshot не содержит .env, пользовательские медиа, data и venv.
Fixture использует 1024 синтетических релиза с AVC/HEVC history и реальные
маленькие файлы под изолированным media root. MediaInfo/video decoding не
измеряются. Domain UTC clock фиксирован до imports. PostgreSQL durability
не отключается; storage — временный tmpfs, не производственный диск.

## Корректность до timing

Проверить full page и partial, все views, поиск/пагинацию, готовые и отсутствующие
файлы, receipts, offsite source, Unicode и HTML metacharacters. Предпочтительно
полное совпадение HTML bytes. Различия escaping/formatting допустимы только
после строгого сравнения parsed DOM с сохранением исходного diff. Текст,
порядок строк, href, disabled, data-source-key и data-sse-key не игнорировать.

Во всех режимах проверить фактический GIL до/после imports, compile, warmup,
timed batches и из worker threads. Автоматическое включение GIL блокирует
публикацию результата как GIL-off; `PYTHON_GIL=0` не используется.
3.15t с `-X gil=1` — дополнительный correctness/control.

## Основная серия: заранее фиксированный объём

`current_jinja` / `minijinja` × `regular` / `ft_off` × concurrency 1 / 4.
В каждой комбинации 8 measured rounds по 8 завершённых запросов: 64 latency
наблюдения на комбинацию. Основной URL:
`/hevc-workbench/live?view=all&page=1&per_page=30`.
SQL/HEVC вычисления видят весь каталог; HTML содержит до 30 строк.

Один процесс, один ASGI event loop, один shared renderer Environment,
один httpx.AsyncClient с ASGITransport. Sync endpoint выполняется в AnyIO
threadpool. Число клиентов задаёт ограничение одновременных запросов.
TCP/TLS, браузер, выполнение JavaScript и настоящие SSE swaps не измеряются.
Lifespan приложения не запускается: нет фоновых расписаний/внешних запросов.

Перед timing прогреть шаблоны, соединения PostgreSQL и worker threads.
Для обоих движков использовать оригинальный route factory, одинаковые
context processors (включая cached Kensetsu state), Session/pool настройки.
MiniJinja context materialization входит в timed path. Вычисления результата,
DOM/hash validation и assertions выполняются после latency timer.

В каждом round движки идут парой с чередованием AB/BA; порядок runtime и
concurrency балансируется. Измерения идут последовательно, без параллельных
builds/pytest. Другие пользовательские контейнеры не останавливаются; состояние
ресурсов и ограничения CPU/RAM сохраняются как часть окружения.
На macOS перед полной серией удерживать `caffeinate -is` до завершения stage;
проверить power assertions и согласованность UTC/monotonic elapsed. Эта защита
добавлена после обнаружения сна в первой неполной серии: вся она исключена
из primary, а не отфильтрована по значениям latency. Размер новой серии прежний.

## Дополнительные проверки

Render-only на том же context: 8 rounds, 64 render на sample, concurrency 1/4,
ordinary и GIL-off. SQL и filesystem подготовка исключены, специфичное для
адаптера преобразование контекста включено. Дополнительный `snapshot_jinja`
контроль использует тот же primitive snapshot и предварительно загруженные
шаблоны, чтобы отдельно оценить влияние преобразования/loader configuration.
Полная страница проверяется на корректность; основной HTTP endpoint — live.
Дополнительные результаты хранятся отдельно от основной серии.

## Результат и пределы вывода

Сохраняются все per-request latencies, batch wall/CPU, throughput=N/batch wall,
RSS/HWM, статусы, digest результата, SQL/read-write counters, GIL checkpoints,
worker thread IDs и peak active calls. CPU процесса включает ASGI load driver;
PostgreSQL CPU отдельно в него не входит.
RSS/HWM снимаются после проверки HTML, а в render-only также после закрытия
пула: это диагностика памяти всего процесса, а не точный peak renderer.
Перекрытие active calls подтверждает конкурентное выполнение, но само по себе
не доказывает параллельную загрузку CPU: для неё нужен также CPU/wall.

Сравнения попарные внутри round. Bootstrap ресемплирует целые paired batches,
не выдавая concurrent requests за независимые наблюдения. Все outliers
сохраняются. Median и p95 абсолютные/относительные показатели публикуются
вместе с 95% CI. P95 при 64 запросах на комбинацию — исследовательский,
недостаточный для строгого доказательства tail non-regression.

Основной критерий целесообразности — корректность, полная HTTP-задержка и
throughput при C=4. Выигрыш короткого render-only не переносится на всю
страницу. Результат ARM64 не объявляется результатом Unraid AMD64.
После просмотра результатов число основной серии не увеличивается ради
желаемого вывода. Исправления harness/correctness требуют новой явно названной
серии, старые измерения сохраняются отдельно.
