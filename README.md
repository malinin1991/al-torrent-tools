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

- Для локального запуска: Python `3.14`, PostgreSQL 16 и зависимости из `backend/requirements.txt`.
- Для сбора MediaInfo локально нужна доступная библиотека libmediainfo; Docker-образ устанавливает и проверяет MediaInfo >=26.
- Для запуска контейнеров: Docker + Docker Compose; Python и PostgreSQL на хосте не требуются.

## Установка локально

```bash
# Из корня репозитория:
cd backend
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
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

Один образ используется для `api`, `worker`, `telegram-bot` и `telegram-hevc-bot`. Контекст сборки — `./backend`.

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
- `ANILIBRIA_REQUEST_RETRIES` - число попыток на каждый base URL, включая первую (минимум 1); повторы при `429/5xx` и сетевых ошибках.
- `ANILIBRIA_RETRY_DELAY_MS` - задержка между повторами в миллисекундах.
- `SCRAPE_PAUSE_EVERY` - после скольких релизов делать паузу в `ongoing/full_sync`.
- `SCRAPE_PAUSE_SEC` - длительность паузы между пакетами запросов.
- `ONGOING_INTERVAL_SEC` - интервал фонового запуска `ongoing`.
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

## Выполнение джобов и расписание

Ручные запуски из UI/API выполняются в фоне процесса API через `JobRunner.schedule_job`, в отдельном потоке со своей DB Session. Worker запускает джобы по расписанию; таблица `jobs` хранит состояние, а не служит очередью, которую worker постоянно выбирает. Advisory locks защищают от конкурирующих запусков; остановка джоба кооперативная.

В `backend/worker.py` настроены:

- `ongoing` — каждые 120 с по умолчанию;
- `cleanup_master` и `cleanup_slave` — каждый час, всегда `dry_run=True` при запуске по расписанию;
- `full_sync` — ежедневно в 08:00 по локальному времени процесса worker, без `force_qb_load`; timezone явно не задана в Compose;
- `cleanup_logs` — каждые 24 часа;
- fallback polling master и slave — каждые 60 с;
- `pipeline_reconcile` — каждые 600 с по умолчанию, минимум 60 с;
- retry `waiting_master` / `waiting_slave` и reclaim зависших джобов — каждые 5 минут.

Worker перечитывает настраиваемые интервалы примерно раз в минуту. Для реального удаления через cleanup нужен ручной apply (`dry_run=False`) и разрешение `CLEANUP_ALLOW_DELETE`.

### Пропуск неизменившихся релизов

`ongoing` / `full_sync` используют маркеры `updated_at`/`fresh_at` из списка и `release_checkpoints`, чтобы пропускать неизменившиеся релизы. Внутри обработки дополнительно проверяются fingerprint списка торрентов и `seen_torrents`. Это сокращает запросы за деталями и повторное скачивание `.torrent`; принудительная синхронизация и изменение hash требуют повторной обработки.

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
python -m pytest -q
python -m compileall -q app tests worker.py
```

Запускай из `backend/`: корневой `app/` содержит пустые пакеты и может перекрыть импорт рабочего приложения. Большинство тестов использует mock-объекты; названия `e2e` не означают проверку живых qBittorrent/Telegram/AniLibria.

Проверка data migration 0018 на PostgreSQL по умолчанию пропускается. Для её запуска укажи отдельную тестовую БД (из `backend/`):

```bash
ALTT_TEST_POSTGRES_URL='postgresql+psycopg://altt:<пароль>@127.0.0.1:5432/altt_test' \
  python -m pytest -q tests/test_migration_0018_ignore_low_quality_avc.py
```
