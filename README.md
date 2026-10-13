# AL Torrent Tools

Веб-сервис для автоматизации торрент-джобов AniLibria, архивирования `.torrent` и пайплайна `master -> slave` для qBittorrent.

## Текущий статус

Реализованы:

- синхронизация релизов (`ongoing`, `full_sync`, `force_release_sync`) и обновление метаданных;
- архив версий `.torrent`, состав файлов, BLAKE3 и история изменений;
- pipeline `master → slave` с webhook, polling, retry и возобновлением отменённых раздач;
- отдельные `cleanup_master` / `cleanup_slave`, очистка orphan-файлов и старых логов;
- сопоставление AVC/HEVC, MediaInfo и отправка файлов во внешний Video Kensetsu;
- два Telegram-профиля: подписки на релизы и контроль HEVC, с управлением доступом;
- Jinja2/HTMX UI: релизы, джобы, архив, pipeline, настройки, дополнительные релизы, доступ Telegram и информация о системе;
- live-обновление UI через **SSE** (`GET /ui/events`) + HTMX partials.

Текущая архитектура и правила данных описаны в [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md), краткая карта для разработки — в [AGENTS.md](AGENTS.md).

### UI live (SSE)

Открытые страницы (`/`, `/jobs`, `/pipeline`, `/pipeline/{id}`, `/releases`, `/archive`, `/info`) подписываются на `EventSource /ui/events?channels=…`. Сервер проверяет change-token’ы по БД с паузой 3 с между проходами и отправляет именованное событие при изменении токена. Для активных pipeline токен также меняется каждые 3 с, чтобы обновлять прогресс qB; канал `info` имеет временной tick 30 с. Клиент точечно подтягивает HTML-partial (`show:none`, сохранение скролла и `<details>`), без HTMX interval-poll. Inbound webhook qBittorrent (`/api/webhooks/qb/complete`) меняет БД; браузер получает обновление через SSE.

Страницы `/settings` и `/extra-urls` остаются без live (только действия пользователя).

Перед прокси (OpenResty/nginx) для `/ui/events` нужны streaming-настройки, иначе тело буферизуется и браузер видит «висящий» EventSource (в HAR часто `status: 0`, 0 bytes):

```nginx
location /ui/events {
    proxy_pass http://altt_upstream;  # ваш upstream API
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    chunked_transfer_encoding on;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

## Требования

- Для локального запуска: обычный CPython `3.15`, PostgreSQL 16 и зависимости из `backend/requirements.txt`, закреплённые в `backend/constraints.txt`. Для разработки и тестов — `backend/requirements-dev.txt`.
- Для сбора MediaInfo локально нужна доступная библиотека libmediainfo; Docker-образ устанавливает и проверяет MediaInfo >=26.
- Для запуска контейнеров: Docker + Docker Compose; Python и PostgreSQL на хосте не требуются.

## Установка локально

```bash
# Из корня репозитория:
cd backend
python3.15 -m venv .venv
source .venv/bin/activate
pip install -c constraints.txt -r requirements-dev.txt
cp .env.example .env
```

Перед запуском укажи в `backend/.env` URL доступной PostgreSQL с созданной БД, например `postgresql+psycopg://altt:<пароль>@127.0.0.1:5432/altt`. Значение `@postgres:5432` из примера предназначено для сети Compose: текущий Compose не публикует порт PostgreSQL на хост.

Применение миграций и запуск API из `backend/` с активированной venv:

```bash
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

В отдельном терминале, также из `backend/` и с активированной venv:

```bash
python worker.py
```

## Запуск через Docker Compose

```bash
# Из корня репозитория:
cp backend/.env.example backend/.env
docker compose up --build
```

Если меняли `command`/`entrypoint` в `docker-compose.yml`, пересоздайте контейнеры (`docker compose up --build`), иначе `api`/`worker` могут стартовать без `alembic upgrade head` и падать на отсутствующих таблицах.

После запуска:

- UI/API: [http://localhost:8000](http://localhost:8000)
- проверка БД без обращения к AniLibria: [http://localhost:8000/health/live](http://localhost:8000/health/live)
- проверка AniLibria API: [http://localhost:8000/health](http://localhost:8000/health)

Compose запускает PostgreSQL, API, worker и два Telegram-бота. Выключенные или ещё не настроенные боты ожидают настройки. Подробнее — [telegram-bot/README.md](telegram-bot/README.md).

Торрент-файлы пишутся по канону `TORRENT_STORAGE_DIR` → `settings.torrent_storage_dir` (локально default — `<repo>/data/torrents`, в Docker Compose — `/app/data/torrents` с volume `./data/torrents`). Медиа монтируется отдельно: `./data/anilibria` → `/anilibria:rw`. При старте API, worker и обоих ботов entrypoint выполняет `alembic upgrade head` под PostgreSQL advisory lock, сериализующим миграции.

## Сборка образа и деплой на Unraid

Один образ используется для `api`, `worker`, `telegram-bot` и `telegram-hevc-bot`. Контекст сборки — `./backend`. Публикуемый target по умолчанию — `runtime`: рабочие исходники, миграции и runtime-зависимости. Локальный `docker-compose.yml` выбирает target `development` с тестами, `pytest`, `watchfiles` и исследовательскими скриптами.

MediaInfo ставится из репозитория MediaArea (≥26.x). MIME вложений Matroska (FileMimeType) читается лёгким pure-Python EBML walk секции Attachments — **без** mkvtoolnix. Сборка **падает**, если после `apt install` версия MediaInfo ниже 26 (типичный признак: apt тихо взял пакет Debian вместо MediaArea).

Локально (`docker-compose.yml`): `docker compose build --no-cache`, затем `docker compose up -d`.

Прод (Unraid / registry): multi-arch `buildx ... --push` как ниже, затем `docker compose -f docker-compose.unraid.yml pull` и `docker compose -f docker-compose.unraid.yml up -d`.

Если на `/info` всё ещё старая `libmediainfo` (например 24.12) при свежем MediaArea в образе — виноват **bundled** `.so` внутри wheel `pymediainfo` (он грузится раньше system). Dockerfile после `pip install` удаляет `libmediainfo*` из пакета и проверяет, что pymediainfo видит ≥26. Пересобери образ и подтяни новый digest на Unraid.

Мультиархитектурная сборка и пуш в registry.

Один раз создай именованный builder `container` (драйвер `docker-container` нужен для multi-arch `--push`; обычный `default`/`desktop-linux` для этого не подходит):

```bash
docker buildx create --name container --driver docker-container --use
docker buildx inspect --bootstrap
```

PowerShell (то же самое):

```powershell
docker buildx create --name container --driver docker-container --use
docker buildx inspect --bootstrap
```

Проверка: `docker buildx ls` — в списке должен быть `container` со статусом `running`. Если builder уже есть, команду `create` можно пропустить.

Затем сборка (multi-arch; `--provenance=false --sbom=false` — без OCI attestations, иначе Unraid вечно показывает Update available):

```bash
# Из корня репозитория:

docker buildx build \
  --builder=container \
  --platform=linux/amd64,linux/arm64/v8 \
  --provenance=false \
  --sbom=false \
  --build-arg BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --build-arg GIT_SHA="$(git rev-parse --short HEAD)" \
  -t registry.mageek.su/al-torrent-tools:latest \
  --push \
  ./backend
```

PowerShell (Windows):

```powershell
# Из корня репозитория:

docker buildx build `
  --builder=container `
  --platform=linux/amd64,linux/arm64/v8 `
  --provenance=false `
  --sbom=false `
  --build-arg BUILD_TIME="$((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))" `
  --build-arg GIT_SHA="$(git rev-parse --short HEAD)" `
  -t registry.mageek.su/al-torrent-tools:latest `
  --push `
  ./backend
```

В образе пишется метка сборки (`/etc/altt_build_time`); на странице **Информация** видно дату/время образа и короткий git SHA — чтобы проверить, что Unraid действительно подтянул новый `latest`.

Новый BuildKit по умолчанию кладёт в registry OCI index + provenance/SBOM (`unknown/unknown`). Unraid сравнивает digests криво и после такого пуша вечно пишет Update available. Флаги выше отключают attestations — получается классический multi-arch list, как раньше на macOS.

На Unraid (Compose Manager или `docker compose`):

1. Создай каталог для архива `.torrent`, например `/mnt/user/appdata/al-torrent-tools/torrents`.
2. Рядом со стеком положи `.env` (на основе `backend/.env.example`). Обязательно:

```env
APP_ENV=prod
SECRET_KEY=<длинный-случайный-секрет>
POSTGRES_PASSWORD=<пароль>
DATABASE_URL=postgresql+psycopg://altt:<пароль>@postgres:5432/altt
```

`DATABASE_URL` задаётся только в `.env` (хост сервиса — `postgres`). Пароль в `DATABASE_URL` и `POSTGRES_PASSWORD` должен совпадать. Не подставляй `${…}` внутрь URL — на Unraid это ломает строку (получается хост вроде `$@postgres`).

3. Подними стек из `docker-compose.unraid.yml` (образ `registry.mageek.su/al-torrent-tools:latest`, без bind-mount исходников и без `--reload`). У `api`, `worker` и обоих Telegram-ботов стоит `pull_policy: always`. Каталог `/mnt/user/anilibria` монтируется в API и worker как `/anilibria:rw`, поскольку ручной `orphan_cleanup` выполняет удаление из процесса API, а фоновые операции — из worker.

```bash
docker compose -f docker-compose.unraid.yml pull
docker compose -f docker-compose.unraid.yml up -d
```

После обновления сверь дату сборки на `/info`.

UI/API: `http://UNRAID_IP:8000`, health: `http://UNRAID_IP:8000/health`.

## Переменные окружения

Полный пример находится в `backend/.env.example`.

Основные переменные:

- `APP_NAME` - имя приложения.
- `APP_ENV` - окружение (`dev`, `prod` и т.д.).
- `SECRET_KEY` - обязательный секрет для окружений вне `dev`.
- `DATABASE_URL` - строка подключения к PostgreSQL.
- `ANILIBRIA_BASE_URL` - основной URL AniLibria API.
- `ANILIBRIA_FALLBACK_BASE_URL` - резервный URL AniLibria API.
- `ANILIBRIA_BEARER_TOKEN` - bearer token (можно задать вручную или получить через вход в UI).
- `ANILIBRIA_PASSKEY` - passkey для announce; также может быть получен из профиля после входа в API.
- `ANILIBRIA_ADMIN_BASE_URL` - единственный URL админки Nova (например `https://adminka.example`), без fallback. Настраивается отдельно в web; пустое значение отключает источник админки.
- `ANILIBRIA_ADMIN_URL_TEMPLATE` - шаблон ссылки на релиз Nova с `{release_id}`; пустое значение скрывает ссылку. На URL API админки не влияет.
- `ANILIBRIA_REQUEST_RETRIES` - число попыток на каждый base URL, включая первую (минимум 1); повторы при `429/5xx` и сетевых ошибках.
- `ANILIBRIA_RETRY_DELAY_MS` - задержка между повторами в миллисекундах.
- `SCRAPE_PAUSE_EVERY` - после скольких релизов делать паузу в `ongoing/full_sync`.
- `SCRAPE_PAUSE_SEC` - длительность паузы между пакетами запросов.
- `ONGOING_INTERVAL_SEC` - интервал фонового запуска `ongoing`.
- `BACKOFFICE_INTERVAL_SEC` - отдельный интервал `backoffice_sync`: 900 с по умолчанию, минимум 60 с; изменяется в UI.
- `CLEANUP_INTERVAL_SEC` - интервал фонового запуска `cleanup_master` и `cleanup_slave`.
- `CLEANUP_ALLOW_DELETE` - разрешить реальное удаление в qB (`true` по умолчанию; `false` — только отчёт в логах).
- `PIPELINE_MASTER_MIN_AGE_MIN` - минимальный возраст `master_added` и `slave_added` перед fallback polling.
- `PIPELINE_RECONCILE_INTERVAL_SEC` - интервал полной сверки pipeline ↔ master (по умолчанию 600).
- `JOB_STALE_MINUTES` - порог неактивности для reclaim pending/running/stopping (по умолчанию 30 минут, с учётом run-lock).
- `TORRENT_STORAGE_DIR` - каталог для `.torrent` архива.
- `ANILIBRIA_MEDIA_ROOT` - корень медиа, по умолчанию `/anilibria`.
- `FILE_HASH_CHUNK_SIZE` - размер блока BLAKE3, по умолчанию 4 194 304 байта.
- `FILE_HASH_WORKERS` - число потоков хеширования, по умолчанию 3; настройка UI `file_hash_workers` имеет приоритет, допустимый диапазон 1–8.

URL/token/passkey AniLibria и интервалы из UI имеют приоритет над env (DB override → env default). Логин и пароль AniLibria вводятся в **Настройки → AniLiberty API** и сохраняются в БД; переменные `ANILIBRIA_LOGIN` / `ANILIBRIA_PASSWORD` не поддерживаются конфигурацией приложения. Настройки Telegram и Video Kensetsu также задаются в UI и хранятся в БД.

## Надёжность задач и рабочий список HEVC

Запуск одного `job_id` разрешён только атомарным переходом `pending → running`. Повторный вызов завершённой/отменённой задачи не исполняет обработчик. Reclaim проверяет состояние под отдельной блокировкой и фиксирует отмену до её освобождения; остановка имеет приоритет над результатом.

Синхронизации сохраняют счётчики релизов/торрентов и полный список проблем. `partial` («Завершено с ошибками») отличается от `success`; ожидание qB показано отдельно. Неполный каталог не запускает глобальный `api_present` sweep и не завершает первоначальный baseline. В деталях джоба доступны причины и явный повтор выбранных ошибок отдельным запуском. Повтор проверяет источник, ID, hash и роль qB; неизвестная версия не подменяется текущей. До 100 проблем за запуск, остальные доступны через пагинацию.

REST: `GET /api/jobs/{id}/failures?after_id=0&limit=50`, `POST /api/jobs/{id}/retry-failures` с `{"failure_ids":[...]}`. Общий `POST /api/jobs` для `retry_sync_failures` принимает только `retry_of` и `failure_ids`: цели и флаги восстанавливает сервер. Итог находится в поле `result` существующих job endpoints.

Страница **«Рабочий список HEVC»** (`/hevc-workbench`) объединяет причину потребности HEVC, точную версию исходника, доступность и последнюю квитанцию Kensetsu. Есть представления готовых, просроченных, недоступных и неоднозначно принятых файлов. Общие счётчики доступности относятся к снимку БД; диск проверяется только для видимой страницы, с временем проверки и объяснением расхождений. Выбор и раскрытия сохраняются при SSE. Отправка использует прежний endpoint и защиту повторов; проверяемый исходник и активное хеширование блокируют отправку, включая повторную проверку после выбора пресетов.

Страница **«Доставка Telegram»** (`/telegram-outbox`) показывает оба профиля, попытки, время повтора и безопасную причину ошибки. Сообщения резервируются по одному с lease/token; временные ошибки повторяются с задержкой, `429` задаёт общий cooldown профиля. Неопределённая доставка не повторяется автоматически: сначала требуется проверка Telegram и явное подтверждение возможного дубля. Обычная синхронизация не создаёт новое уведомление в обход этого решения. HEVC-доставка не изменяет primary `tg_status` pipeline.

Схему добавляет миграция `0026_job_outcomes_outbox`. При обновлении остановите старые API, worker и оба процесса ботов и замените их согласованной версией: старый код не соблюдает новые claims/dedupe. Entry point применяет миграцию под существующей PostgreSQL-блокировкой. Постоянная очередь worker и отслеживание самого кодирования Kensetsu остаются отдельными задачами.

Дизайн: [запуск задач](docs/design/job-execution.md), [результаты sync](docs/design/sync-outcomes.md), [Telegram](docs/design/telegram-delivery.md), [HEVC](docs/design/hevc-workbench.md). Для текущей модели данных сохранён [PostgreSQL](docs/design/data-store.md).

## История отправок в Video Kensetsu

Страница **«Отправки в Kensetsu»** (`/encoder-history`) хранит отдельные попытки отправки и результат приёма каждого файла. Доступны поиск по релизу, пути и ID внешнего задания, фильтр «Требуют внимания», параметры отправки и диагностический ответ. Последняя квитанция отображается рядом с файлом на страницах релизов и архива; изменения приходят через SSE.

Статусы относятся к передаче исходника:

- **Отправляется** — подготовка или HTTP-запрос ещё выполняются.
- **Принят Kensetsu** — ответ подтверждает приём. Это не означает завершение кодирования или появление HEVC.
- **Не отправлен** — запрос для этого файла не начат либо соединение заведомо не установлено.
- **Отклонён Kensetsu** — получен явный отказ для файла.
- **Приём не подтверждён** — запрос мог быть принят, но ответ потерян, неоднозначен или противоречив. Перед повтором нужно проверить Kensetsu.

В квитанции сохраняются дата, сервер, пресеты и их снимок, release/torrent ID, infohash и точный относительный/полный путь. Размер и время изменения фиксируются перед передачей; BLAKE3 добавляется только при наличии актуального кешированного хеша. История переживает смену версии торрента, удаление исходных записей и оба сброса состояния в настройках. Автоматической очистки истории пока нет. Данные настроек авторизации не копируются в журнал; диагностические снимки ограничены по размеру и маскируют секреты.

Повтор создаёт новую попытку со ссылкой на предыдущую. Кнопка пакетного повтора выбирает только неотправленные и отклонённые файлы. Для принятого файла требуется подтверждение повторной отправки; для неизвестного результата — подтверждение проверки Kensetsu. Старая версия не заменяется текущей автоматически. Автоматических сетевых повторов отправки нет.

UI использует `request_id`: повтор того же действия возвращает уже сохранённую квитанцию. API-клиентам также следует передавать стабильный `request_id` в существующие single/batch endpoints `/api/torrent-files/.../send-to-encoder`. Без него сервер создаёт новый идентификатор; защита от повторной отправки той же версии с теми же параметрами всё равно действует. Список и детали доступны через `GET /api/encoder-submissions` и `GET /api/encoder-submissions/{id}`. При ошибке после создания попытки её ID возвращается в `X-Encoder-Submission-Id`.

Прерванная попытка после 10 минут при следующем чтении журнала/квитанций или отправке переводится в «Не отправлен», если внешний запрос ещё не начинался, либо в «Приём не подтверждён», если начинался. Перезапуск не отправляет её повторно. Таблицы добавляет миграция `0025_encoder_history`; обычный Docker entrypoint применяет её при запуске.

## Python 3.15

Для основного образа выбран обычный **CPython 3.15.0 с GIL**, без JIT и free threading. Переход обновляет runtime; бизнес-код и правила HEVC не требуют рефакторинга. `constraints.txt` сохраняет проверенные версии библиотек, включая `psycopg[binary]`. Собственная сборка `_decimal` удалена: стандартный `decimal` использует встроенный Python fallback, достаточный для текущего приложения. Uvicorn устанавливается с `uvloop` и `httptools`, без неиспользуемых YAML/WebSocket extras. Компилятор, LibYAML и тестовые зависимости в production-образ не устанавливаются; системная MediaInfo >=26 и проверка удаления bundled libmediainfo сохранены.

Итоговый рабочий Dockerfile собран для Linux ARM64 и x86-64: в каждом образе прошли **1011 тестов** с отдельной PostgreSQL 16. ARM64 проверен нативно, x86-64 — под эмуляцией; это подтверждает функциональность, но не скорость на Unraid.

Решение о прямом переходе принято после сравнения и обсуждения его ограничений: отсутствие регрессии p95 прежний ARM64 benchmark не доказал. Обновление runtime не является обещанием ускорения на Unraid. Сборка, окончательные проверки и порядок обновления/отката описаны в [миграции на 3.15](docs/PYTHON_315_MIGRATION.md). При обновлении локальной установки нужна новая venv под 3.15; старую среду 3.14 не переиспользовать.

Методика, исходные замеры и ограничения: [сравнение runtime](docs/PYTHON_RUNTIME_BENCHMARKS.md), [совместимость](docs/PYTHON_315_COMPATIBILITY.md). Для отдельной диагностики 3.15 добавлен opt-in sampling profiler; использование и ограничения описаны в [возможностях Python 3.15](docs/PYTHON_315_FEATURES.md). По умолчанию профилирование не запускается.

Отдельно проверены regular 3.15, `3.15t` с GIL и без него: по **1011 успешных тестов** с PostgreSQL в каждом режиме. Проверка использует одинаковый экспериментальный pure psycopg/libpq backend: у `psycopg-binary 3.3.6` нет t-wheels ни для ARM64, ни для x86-64. Production-зависимости не заменены.

Для HEVC реализован изолированный рефакторинг `backend/scripts/hevc_parallel_experiment.py`: immutable snapshot и ограниченный пул потоков с сохранением прежних правил. Свежий snapshot + постоянный пул из четырёх потоков на t без GIL сократил median wall time примерно на **19%** относительно исходного расчёта на regular 3.15, но увеличил CPU time примерно на **98%** и peak RSS на **28.5 MiB**. JSON и Jinja на t замедлились; общий переход на t и включение helper в production не выполнены. Это Linux ARM64 compute-замер без SQL, HTTP и кодирования, а не проверка скорости Unraid.

Подробности: [три runtime и рефакторинг](docs/PYTHON_315T_BENCHMARKS.md), [совместимость 3.15t](docs/PYTHON_315T_COMPATIBILITY.md), [границы и приоритеты оптимизации](docs/PYTHON_315T_OPTIMIZATION.md). Экспериментальные сборки, wrappers и зависимости сохранены для повторения на целевой архитектуре.

## Оптимизация HEVC и MediaInfo

HEVC-фильтр «Просрочено» использует один полный расчёт пар для фильтрации и сортировки. Правила SLA, история версий и бейджи сохранены.

Пакетная обработка MediaInfo объединяет проверки кеша и записи в PostgreSQL. Разбор файлов выполняет ограниченный пул; готовые отчёты сохраняются пакетами до 25. Полный, инкрементальный MediaInfo и обработка после хеширования по умолчанию используют до четырёх потоков. Явный `workers=1` и другие поддерживаемые значения сохраняются. Остановка сохраняет уже завершённые результаты; одиночное извлечение для UI работает по прежнему контракту.

На окончательном коде прошли по **1041 тесту без пропусков** в regular 3.15, 3.15t с GIL, 3.15t без GIL и обычном рабочем образе с binary psycopg. Включены PostgreSQL-проверки пересекающихся задач.

Критерий выбора — время выполнения целой операции. CPU time и RAM измеряются как цена ускорения и сами по себе не блокируют изменение. Сравнение HTTP-страницы с PostgreSQL/Jinja и MediaInfo на настоящих MKV описано в [замерах целых операций](docs/OPERATION_BENCHMARKS_315.md). Основной runtime остаётся обычным 3.15; перенос на t требует отдельного решения по результатам сравнения.

В измеренном окружении HEVC ускорился примерно на 16%, медиана инкрементального MediaInfo на четырёх настоящих MKV — на 28%. Default4 выбран ради типичного времени выполнения; p95 MediaInfo остаётся неопределённым и не объявлен успешным gate. Полное хеширование этих файлов уверенного ускорения не показало. Параллельный HEVC-кандидат на t проиграл оптимизированному regular на всей странице и остался экспериментом. Эти результаты Linux ARM64 Docker Desktop с тёплым файловым кешем не гарантируют ту же скорость на Unraid.

## Выполнение джобов и расписание

Ручные запуски из UI/API выполняются в фоне процесса API через `JobRunner.schedule_job`, в отдельном потоке со своей DB Session. Worker запускает джобы по расписанию; таблица `jobs` хранит состояние, а не служит очередью, которую worker постоянно выбирает. Advisory locks защищают от конкурирующих запусков; остановка джоба кооперативная.

В `backend/worker.py` настроены:

- `ongoing` — каждые 120 с по умолчанию;
- `backoffice_sync` — каждые 900 с по умолчанию, если настроен URL админки;
- `cleanup_master` и `cleanup_slave` — каждый час, всегда `dry_run=True` при запуске по расписанию;
- `full_sync` — ежедневно в 08:00 по локальному времени процесса worker, без `force_qb_load`; timezone явно не задана в Compose;
- `cleanup_logs` — каждые 24 часа;
- fallback polling master и slave — каждые 60 с;
- проверка переходов HEVC в просрочку — каждые 60 с, независимо от API checkpoints;
- `pipeline_reconcile` — каждые 600 с по умолчанию, минимум 60 с;
- retry `waiting_master` / `waiting_slave` и reclaim зависших джобов — каждые 5 минут.

Worker перечитывает настраиваемые интервалы примерно раз в минуту. Для реального удаления через cleanup нужен ручной apply (`dry_run=False`) и разрешение `CLEANUP_ALLOW_DELETE`.

HEVC-бот отправляет уведомления о просрочке в одобренные через `/telegram-access` групповые чаты. В эти же чаты отправляется «Обнаружен новый релиз», если первый сохранённый торрент релиза — AVC, после успешной проверки хешей всех выбранных файлов в `hash_torrent`. Проверенные кешированные хеши учитываются; ошибки, неполные проходы и архивные версии не вызывают уведомление. Повторное хеширование не дублирует сообщение о новом релизе для того же чата.

Для `missing`, когда HEVC ещё не было, через 24 часа отправляется отдельное уведомление, только если релиз впервые появился в локальном архиве с 1 октября 2026 года по Новосибирску. Оно не переводит `missing` в `overdue` и не повторяется для того же релиза/чата. Подробные правила — в [инструкции Telegram](telegram-bot/README.md).

### Второй источник: админка

В настройках задайте «URL админки» отдельно от шаблона ссылки на релиз. При обновлении со сборки, использовавшей адрес из шаблона, заполните новое поле: шаблон больше не включает источник автоматически.

Используются сохранённые логин и пароль AniLiberty, отдельная cookie-сессия Nova и существующий passkey. Клиент разрешает только GET данных и POST `/login` для входа. Релизы, торренты и любые другие данные админки не изменяются.

Жанры читаются из выбранных ID и `options` поля `genres` в карточке Nova; участники — полным обходом связи `members` (UUID, отображаемый ник, роль). Эти данные заполняют web и архив, а жанры также становятся метками qBittorrent на master/slave. Подтверждённое удаление всех жанров убирает только ранее управляемые жанровые метки; пользовательские и метки источника сохраняются. Ошибка чтения связи не затирает ранее сохранённые метаданные.

Сайт имеет приоритет, если публичная карточка доступна и не содержит `is_hidden` / `is_blocked_by_copyrights`. Геоблокировка сама по себе не меняет источник. Иначе из админки импортируются только скрытые и заблокированные по правообладателю релизы, включая ранее неизвестные. Сетевые ошибки не считаются отсутствием релиза. Полный обход админки также выполняется при `full_sync` и `full_meta_sync`; публичный sweep не снимает админские архивы с учёта.

В qBittorrent поддерживаются метки `site` либо `hidden` / `blocked` (обе, если установлены оба флага). Жанры и пользовательские метки сохраняются. У торрентов из админки полностью заменяется список трекеров в архивном `.torrent` и на master/slave; raw `info`, infohash, раздача и медиа сохраняются. Passkey берётся из настроек. Возврат на сайт меняет метки, сохраняя уже применённые трекеры. Ранее импортированные релизы продолжают проверяться отдельным обходом, чтобы исправлять метки после сбоев qB.

Первоначальный импорт ранее неизвестных релизов не создаёт HEVC-уведомления о новом релизе или отсутствии HEVC, включая отложенное хеширование. После появления новых AVC-версий обычный контроль и уведомления продолжаются. Признак первоначального импорта хранится в архиве; незавершённый обход повторяется без потери этого признака.

Страница «Релизы» фильтруется по источнику, скрытию/copyright/геоблокировке и ручной отметке «Брошен». Отметка хранится только локально, переживает синхронизацию и не отключает обработку или HEVC. В обоих Telegram-ботах админские релизы показываются без ссылки на сайт, с пометкой «Нет на сайте»; кнопка «Админка» сохраняется.

### Пропуск неизменившихся релизов

`ongoing` при каждом запуске проверяет список торрентов всех релизов из расписания, донаблюдения, extra URLs и Telegram tracking (без повторов по ID). Неизменные `updated_at`/`fresh_at` не мешают обнаружению новых торрентов; markers управляют обновлением метаданных qB. Fingerprint и `seen_torrents` предотвращают повторное скачивание `.torrent`. `full_sync` сохраняет пропуск по markers; принудительная синхронизация обрабатывает релиз независимо от них.

Донаблюдение хранится в PostgreSQL (`ongoing_watch`) и работает в той же джобе, без отдельного расписания или параметра запуска:
- первое отсутствие в успешно полученном расписании запускает срок 14 дней;
- любой новый, больший ID торрента (AVC, HEVC или другой) продлевает срок на 14 дней с момента обнаружения; повторная обработка известного ID срок не продлевает;
- возврат в расписание снимает срок; следующее исчезновение запускает его заново;
- подтверждённый HTTP 404 самого релиза удаляет запись донаблюдения. Пустой список торрентов, 404 скачивания `.torrent`, сетевые ошибки и ошибки авторизации не означают удаление релиза;
- недоступное или некорректное расписание не меняет присутствие релизов: проверяются сохранённые записи и явные подписки. Ошибка одного релиза не останавливает обход;
- по истечении срока удаляется только запись донаблюдения. Архив и pipeline сохраняются; extra URLs и tracking продолжают работать независимо.

Кэш начинает накапливаться после обновления (миграция `0023_ongoing_watch`). Уже исчезнувшие до этого релизы автоматически в него не попадут: для них можно запустить `full_sync` или добавить extra URL/tracking. `full_sync` сам кэш донаблюдения не наполняет. Сброс операционного состояния очищает кэш.

## Настройка qBittorrent

Нужно как минимум два клиента в UI **Настройки**:

1. **master** — сюда `TorrentProcessor` добавляет новые `.torrent` (скачивание контента).
2. **slave** — сюда торрент попадает **после** завершения загрузки на master.

Поля `qb_master_*` / `qb_slave_*` синхронизируются в таблицу `qb_clients` (роли `master`/`slave`).

Пароли не отдаются через `GET /api/settings` и не отображаются обратно в форме. Поле `password_encrypted` пока хранит пароль в plaintext.

### Схема master → slave

```
AniLibria / ongoing
        │
        ▼
  добавить .torrent в qB master
  pipeline: discovered → master_added
  файл сохранён в архиве data/torrents/{hash}.torrent

  если master лежит:
  pipeline: discovered → waiting_master (+ архив)
  worker каждые 5 мин → master ожил?
    ├─ торрент ещё есть в API (по hash) → добавить в master → master_added
    └─ торрента нет в API → cancelled
        │
        │  master скачивает контент
        ▼
  qBittorrent: «Run external program on torrent finished»
        │
        │  curl …?hash=%I&role=master
        ▼
  POST/GET /api/webhooks/qb/complete
        │
        │  pipeline найден по info_hash, статус master_added
        ▼
  добавить тот же .torrent в qB slave
  pipeline: master_complete → slave_added
        │
        │  slave скачивает / checking
        ▼
  qBittorrent slave: «Run external program on torrent finished»
        │
        │  curl …?hash=%I&role=slave
        ▼
  pipeline: slave_added → done (на раздаче)

  если slave лежит:
  pipeline → waiting_slave
  worker каждые 5 мин → slave ожил?
    ├─ есть в API и на master → slave_added (далее webhook/poll → done)
    └─ нет в API или нет на master → cancelled
```

Если webhook не сработал, есть запасные механики:

1. **Worker ~60 с** — для `master_added` старше `PIPELINE_MASTER_MIN_AGE_MIN`: опрос master по hash; при 100% / seeding → slave; если торрента нет на master → `cancelled`. Тот же интервал для aged `slave_added` → `done` (или `cancelled`, если нет на slave).
2. **Сверка по расписанию** (по умолчанию каждые 10 мин, `PIPELINE_RECONCILE_INTERVAL_SEC` / настройка в UI; кнопка «Сверить с master» на `/pipeline`) — все pipeline в `master_added` / `master_complete` (ещё нет на slave):
   - загружен на master → досылка на slave;
   - ещё качается / остановлен → ждём callback;
   - не найден на master → `cancelled` (с учётом grace после resume).
   Также сверка восстанавливает подходящие `failed` после ошибок соединения, если торрент найден на master.
3. **Master недоступен** — pipeline в `waiting_master`; worker каждые 5 мин проверяет master и наличие торрента в AniLibria API по hash.
4. **Slave недоступен** — pipeline в `waiting_slave`; worker каждые 5 мин: slave ожил → торрент есть в API и на master → досылка на slave; иначе `cancelled`.
5. **Ложный `cancelled` «нет на master/slave»** (торрент уже вернули в qB) — джоб `pipeline_resume_cancelled` (`/jobs` или «Возобновить cancelled» на `/pipeline`; точечно — кнопка на карточке только для qb-missing cancel). После resume poll/reconcile не cancel'ят по missing ~15 мин (grace). Webhook и force_qb_load сами cancelled не поднимают.

### Команда для qBittorrent

В **Tools → Options → Downloads** включи **Run external program on torrent finished** и вставь команду.

**Master** (после скачивания — досылка на slave):

```bash
curl -fsS -X POST "http://HOST:8000/api/webhooks/qb/complete?hash=%I&role=master"
```

**Slave** (после скачивания — pipeline → done):

```bash
curl -fsS -X POST "http://HOST:8000/api/webhooks/qb/complete?hash=%I&role=slave"
```

Локально вместо `HOST` — `127.0.0.1`; из Docker часто `host.docker.internal` или IP хоста.

Параметр **`%I`** — Info hash v1 (qBittorrent подставит сам). **`role`** обязателен (`master` или `slave`). Кавычки вокруг URL обязательны.

Проверка вручную (подставь реальный hash из UI «Пайплайн»):

```bash
curl -fsS -X POST "http://127.0.0.1:8000/api/webhooks/qb/complete?hash=YOUR_INFO_HASH&role=master"
curl -fsS -X POST "http://127.0.0.1:8000/api/webhooks/qb/complete?hash=YOUR_INFO_HASH&role=slave"
```

Ожидаемый ответ при успехе master: `{"ok":true,"status":"slave_added","pipeline_id":...}`.  
Успех slave: `{"ok":true,"status":"done","pipeline_id":...}`.  
Если hash неизвестен → `404`. Без `role` → `400`. Если торрент ещё качается → `ok:false`.

GET тоже поддерживается (удобно для отладки):

```bash
curl -fsS "http://127.0.0.1:8000/api/webhooks/qb/complete?hash=YOUR_INFO_HASH&role=master"
```

## Документация

- [Текущая архитектура и правила данных](docs/PROJECT_PLAN.md)
- [Запуск и настройка Telegram-ботов](telegram-bot/README.md)
- [Контекст для разработки](AGENTS.md)
- [Архив первоначальных промптов](docs/MULTITASK_PROMPTS.md) — не инструкция для повторного создания проекта
- `docs/anilibria-v1.openapi.json` (локальный кэш OpenAPI)

## Проверка после запуска

Минимальный smoke-check:

```bash
curl -fsS http://localhost:8000/health/live
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/api/jobs
curl -fsS http://localhost:8000/api/archive
curl -fsS http://localhost:8000/api/settings
```

## Тесты

```bash
# Из корня репозитория, с активированной venv:
cd backend
python -m pip install -c constraints.txt -r requirements-dev.txt
python -m pytest -q
python -m compileall -q app tests worker.py
```

Запускай из `backend/`: корневой `app/` содержит пустые пакеты и может перекрыть импорт рабочего приложения. Большинство тестов использует mock-объекты; названия `e2e` не означают проверку живых qBittorrent/Telegram/AniLibria.

Проверка data migration 0018 на PostgreSQL по умолчанию пропускается. Для её запуска укажи отдельную тестовую БД (из `backend/`):

```bash
ALTT_TEST_POSTGRES_URL='postgresql+psycopg://altt:<пароль>@127.0.0.1:5432/altt_test' \
  python -m pytest -q tests/test_migration_0018_ignore_low_quality_avc.py
```
