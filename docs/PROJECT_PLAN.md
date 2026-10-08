# AL Torrent Tools — текущая архитектура

Документ обновлён по коду проекта 2026-10-08. Имя `PROJECT_PLAN.md` сохранено для существующих ссылок; первоначальный план доступен в истории Git. Старые задания этапа создания проекта находятся в [архиве промптов](MULTITASK_PROMPTS.md).

Установка, переменные окружения и деплой: [README](../README.md). Краткие инструкции для разработки: [AGENTS.md](../AGENTS.md).

## Назначение и стек

AL Torrent Tools синхронизирует релизы AniLibria/AniLiberty, архивирует версии `.torrent`, передаёт раздачи с qBittorrent master на slave, отслеживает файлы и соответствие AVC/HEVC. Дополнительные функции: Telegram-подписки и HEVC-уведомления, MediaInfo, отправка медиа во внешний Video Kensetsu.

Целевая версия Python — 3.14 (`backend/Dockerfile`). Backend использует FastAPI, синхронный SQLAlchemy, PostgreSQL 16, psycopg, Alembic и APScheduler. HTTP-клиент AniLibria реализован на httpx, qB — через qbittorrent-api. UI — Jinja2 + HTMX + JavaScript/CSS в шаблонах, без отдельного frontend-проекта.

## Структура

- `backend/app/main.py` — HTML-страницы, формы, partials, SSE, health и lifecycle API.
- `backend/app/api/rest.py` — REST `/api`, webhook qB, экземпляр JobRunner и регистрация обработчиков.
- `backend/app/core/config.py` — настройки из окружения и `.env`.
- `backend/app/db/` — модели и синхронные DB Session.
- `backend/app/providers/anilibria/client.py` — API-клиент, retries, fallback URL, bearer token и passkey.
- `backend/app/jobs/` — обработчики сценариев синхронизации, cleanup, hashing и pipeline recovery.
- `backend/app/services/` — доменная логика и внешние интеграции.
- `backend/app/templates/` и `templates/partials/` — страницы и HTMX-фрагменты; `backend/app/static/` — статические ресурсы.
- `backend/app/telegram_bot/` — процессы профилей `primary` и `hevc`.
- `backend/worker.py` — APScheduler и polling pipeline.
- `backend/alembic/` — миграции; `backend/scripts/` — entrypoint и healthchecks.
- `backend/tests/` — тесты; `docs/anilibria-v1.openapi.json` — локальный снимок схемы внешнего API.
- `data/torrents/` — локальный архив `.torrent`; `data/anilibria/` — host-каталог медиа в dev Compose.

Корневые `app/` и `telegram-bot/` не содержат самостоятельных реализаций сервисов. Команды Python запускаются из `backend/`.

## Процессы и выполнение джобов

Compose запускает пять сервисов: `postgres`, `api`, `worker`, `telegram-bot`, `telegram-hevc-bot`. Четыре Python-сервиса используют общий backend-образ. Entry point применяет Alembic-миграции под PostgreSQL advisory lock, чтобы параллельный старт не создавал гонки схемы.

Ручные джобы из UI/API планируются в процессе API через `JobRunner.schedule_job`: отдельный поток, свой event loop и своя DB Session. Worker запускает обработчики по расписанию. Таблица `jobs` хранит состояние и параметры; это не общая очередь, которую забирает worker.

`JobRunner` управляет статусами `pending`, `running`, `stopping`, `success`, `failed`, `cancelled`, дедупликацией и кооперативной остановкой. PostgreSQL advisory locks используются при создании и выполнении джобов; reclaim обрабатывает осиротевшие и зависшие запуски.

Актуальные типы и их описание находятся в `services/job_catalog.py`, регистрация — в `api/rest.py`, расписания — в `worker.py`. Для одного релиза используется `force_release_sync`; отдельного `single_download.py` нет. Cleanup разделён на `cleanup_master` и `cleanup_slave`; legacy API-тип `cleanup` преобразуется в `cleanup_master`.

## Синхронизация и архив

`ongoing` / `full_sync` получают данные API и передают релизы в `TorrentProcessor`. `release_checkpoints` содержит маркеры `updated_at` / `fresh_at` и fingerprint торрентов, позволяющие пропускать неизменившиеся данные.

`seen_torrents` хранит текущий обработанный hash для `torrent_id`: `torrent_id` — primary key, `info_hash` имеет отдельное ограничение уникальности. Новая версия с изменившимся hash должна обрабатываться и при прежнем `torrent_id`.

`TorrentArchiveService` сохраняет `.torrent` и метаданные версии. `api_present` означает наличие в текущем ответе AniLibria, `superseded` — замену другой версией того же torrent_id. Эти признаки не равны статусу pipeline или наличию медиа на диске. Исторические версии используются при сравнении файлов и расчёте HEVC timeline.

Клиент обращается к AniLibria API v1, без HTML scraping. Используемые endpoints и параметры определены в `providers/anilibria/client.py`; локальный OpenAPI — справочный снимок, а не гарантия актуальности внешнего сервиса.

## Pipeline master → slave

Основной путь: `discovered → master_added → master_complete → slave_added → done`.

- `waiting_master` / `waiting_slave` — ожидание доступности соответствующего клиента.
- `slave_added` — торрент добавлен на slave; `done` — завершение подтверждено на slave.
- `cancelled` — отмена сценария, например после исчезновения торрента из qB/API; `failed` — ошибка обработки.
- qB вызывает `GET` или `POST /api/webhooks/qb/complete?hash=...&role=master|slave`; hash и role обязательны. POST также принимает JSON с этими полями.
- Worker выполняет polling master/slave каждые 60 с, reconcile по настраиваемому интервалу и waiting-retry каждые 5 минут.
- Для отмен из-за отсутствия в qB есть `pipeline_resume_cancelled` и точечное возобновление из UI; после resume действует grace от повторного missing-cancel.

Webhook, polling и reconcile могут обрабатывать один pipeline одновременно. Атомарные claims, повторяемость операций и аудит `pipeline_events` — часть контракта.

## Файлы, HEVC и медиа

`file_tracker.py`, `qb_inventory.py`, `file_hasher.py` и `torrent_files_meta.py` отвечают за состав файлов, пути, BLAKE3 и события. Sticky `ui_status` описывает изменения конкретной версии относительно предыдущей: `new`, `changed`, `ok`, `removed`. Наличие полного файла хранится отдельно, а `checking` — временный оверлей.

Первый торрент без prior имеет статус `new` для всех файлов, даже после хеширования. Prior выбирается среди предыдущих версий того же torrent_id, с fallback на тот же release_id и пересечение exact `relative_path`. Нельзя заменять исторический статус на `ok` только потому, что файл существует на диске.

`hevc_pairing.py` определяет `missing`, `overdue`, `type_mismatch`, учитывает диапазоны эпизодов, качество, тип рипа, историю версий и `ignore_hevc`. SLA — 24 часа, окно парной загрузки HEVC→AVC — 2 часа. Подробные исключения описаны в docstring модуля и тестах `test_hevc*`.

`mediainfo.py` сохраняет метаданные медиа; `matroska_attachments.py` читает MIME вложений собственным EBML-парсером. Docker устанавливает MediaInfo >=26 и удаляет bundled lib из pymediainfo wheel. `video_kensetsu.py` интегрирует внешний сервис кодирования, пресеты видео/аудио и пакетную отправку файлов.

## Данные и настройки

Полная схема определяется [моделями](../backend/app/db/models.py) и [миграциями](../backend/alembic/versions/), а не отдельным SQL-черновиком. Основные группы таблиц:

- настройки и источники: `settings`, `extra_urls`, `qb_clients`, `cleanup_rules`;
- джобы: `jobs`, `job_logs`;
- релизы и дедупликация: `releases`, `release_members`, `release_checkpoints`, `seen_torrents`;
- архив и pipeline: `torrent_archive`, `torrent_pipeline`, `pipeline_events`;
- файлы: `torrent_files`, `disk_file_hashes`, `file_change_events`, `file_mediainfo`;
- Telegram: `tracked_releases`, `telegram_outbox`, `telegram_bot_access`.

Для поддерживающих override настроек приоритет — БД → env/default. Telegram и Video Kensetsu настраиваются в UI и хранятся в БД. Секретные поля маскируются при чтении настроек через API; название `password_encrypted` у qB пока не означает шифрование — поле содержит plaintext. Даты БД — naive UTC, браузеру передаются с указанием UTC через `as_utc_iso`.

## Live UI и health

`GET /ui/events` проверяет change tokens с паузой 3 с между проходами и посылает именованные SSE-события при изменении. Активный pipeline дополнительно использует tick 3 с для qB progress; `info` — 30 с. Браузер загружает соответствующие HTMX partials. Webhook qB не отправляет событие в браузер напрямую.

`/health/live` выполняет `SELECT 1` без вызова AniLibria и используется Docker healthcheck. `/health` обращается к AniLibria API. Worker и боты имеют отдельные heartbeat healthchecks; их файлы не заменяют проверку успешности внешних запросов.

## Проверки и развитие

Команды установки и тестов находятся в [README](../README.md#тесты). Большинство тестов использует mocks; PostgreSQL-тест миграции 0018 требует отдельный `ALTT_TEST_POSTGRES_URL`.

При изменении схемы добавляй Alembic-миграцию; при изменении UI проверяй partial и его SSE-token; при изменении pipeline и файлов сохраняй историю, идемпотентность и регрессии соответствующего сценария. Первоначальные фазы и чек-листы больше не используются как текущий backlog.
