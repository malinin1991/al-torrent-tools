# Контекст AL Torrent Tools

## Назначение и структура
- Сервис автоматизации AniLibria/AniLiberty: синхронизация релизов, архив `.torrent`, qBittorrent master → slave, история файлов, контроль AVC/HEVC, Telegram и отправка в Video Kensetsu.
- Рабочий Python-код находится в `backend/`. Корневой `app/` содержит пустые пакеты: запускай приложение и тесты из `backend/`, чтобы не импортировать их вместо рабочего кода.
- Стек: Python 3.14 (README и Dockerfile), FastAPI, синхронный SQLAlchemy, PostgreSQL 16/psycopg, Alembic, APScheduler, httpx, qbittorrent-api, python-telegram-bot, BLAKE3, pymediainfo.
- UI: Jinja2 + HTMX + JavaScript/CSS в шаблонах; отдельного frontend build/package.json нет. Основной шаблон — `backend/app/templates/base.html`, фрагменты — `templates/partials/`.
- `README.md` описывает запуск и Unraid; `docs/PROJECT_PLAN.md` — текущую архитектуру (историческое имя сохранено); `docs/MULTITASK_PROMPTS.md` — архив первоначальных заданий. При расхождениях сверяй код и тесты.

## Где искать реализацию
- `backend/app/main.py`: HTML-страницы, формы, HTMX partials, SSE `/ui/events`, health и lifecycle приложения.
- `backend/app/api/rest.py`: REST `/api`, webhook qB, создание общего `job_runner` и регистрация обработчиков джобов.
- `backend/worker.py`: расписание APScheduler, fallback polling pipeline, retry waiting master/slave, reclaim зависших джобов.
- `backend/app/jobs/`: сценарии ongoing/full_sync/meta_sync, reconcile/resume, cleanup, hashing и MediaInfo. `services/job_catalog.py` — каталог джобов и описание расписаний.
- `backend/app/services/job_runner.py`: состояния джобов, дедупликация, PostgreSQL advisory locks, кооперативная остановка. Ручные джобы запускаются через `schedule_job` в потоке процесса API со своей Session; worker не является общей очередью для всех запусков.
- `backend/app/providers/anilibria/client.py`: API-клиент; `services/runtime_settings.py`: DB overrides; `services/release_checkpoint.py`: маркеры обновлений и fingerprint для пропуска неизменившихся релизов.
- `backend/app/services/torrent_processor.py`: обработка релизов и торрентов; `torrent_archive.py`: архивирование; `qbittorrent.py` и `torrent_qb_meta.py`: взаимодействие с qB и его метаданные.
- `backend/app/services/pipeline.py`: переходы master → slave и аудит; `qb_inventory.py`, `file_tracker.py`, `file_hasher.py`, `torrent_files_meta.py`: состав, наличие и хеши файлов.
- `backend/app/services/hevc_pairing.py`: правила соответствия AVC/HEVC и SLA; `releases_view.py`: данные страницы релизов. Перед изменениями pairing читай docstring модуля и `test_hevc*`.
- `backend/app/services/mediainfo.py`, `matroska_attachments.py`, `video_kensetsu.py`: метаданные медиа, вложения Matroska и клиент внешнего энкодера.
- `backend/app/telegram_bot/`: два профиля бота, основной и `hevc`; `services/telegram_access.py`, `telegram_notify.py`, `telegram_outbox.py`, `hevc_notifications.py`: доступ, подписки и уведомления. Корневой `telegram-bot/` содержит только пояснение.
- `backend/app/db/models.py`: модели; `backend/alembic/versions/`: миграции. Архив, pipeline, файлы, disk hashes, checkpoints, job logs, pipeline events и Telegram outbox — разные сущности, не взаимозаменяемые состояния.

## Инварианты при изменениях
- Прочитай и соблюдай `.cursor/rules/sticky-file-status-history.mdc`: `ui_status` — история конкретной версии торрента, не текущее наличие файла на диске. Первый торрент целиком `new`; хеширование не превращает его в `ok`. Prior сравнивается по exact `relative_path`; `checking` — временный оверлей.
- Основной pipeline: `discovered → master_added → master_complete → slave_added → done`; недоступность qB обрабатывается через waiting/retry. `slave_added` ещё не означает завершение загрузки на slave.
- Webhook, polling и reconcile могут конкурировать: сохраняй атомарные claims, идемпотентность и PipelineEvent-аудит. Возобновление qb-missing `cancelled` имеет отдельный сценарий и grace-период.
- Не смешивай `api_present`, `superseded`, состояние pipeline и наличие медиа на диске. Исторические архивы нужны для diff файлов и HEVC timeline.
- Live UI использует EventSource и HTMX partials. `services/ui_events.py` проверяет change tokens каждые 3 с; активный pipeline также использует временной tick. При изменении отображаемых данных проверяй соответствующий token и partial, сохранение раскрытых блоков и прокрутки.
- Для настроек, поддерживающих DB override, используй существующие runtime-resolver'ы: БД имеет приоритет над env/default. Не обходи маскирование секретов и `logging_filters.py`; не сохраняй значения `.env` в заметках.
- Даты БД — naive UTC: используй `app.utils.datetime_fmt.utcnow()` и `as_utc_iso()` при отдаче браузеру.
- `.torrent` хранится через канонический storage resolver (`TORRENT_STORAGE_DIR`); локальный default — `data/torrents` в корне, Docker — `/app/data/torrents`. Медиа — отдельный `ANILIBRIA_MEDIA_ROOT` (default `/anilibria`); сохраняй проверки границ пути и неполных файлов.
- Cleanup по расписанию запускается с `dry_run=True`. Сохраняй раздельные master/slave параметры и существующие ограничения удаления.
- Изменение схемы требует Alembic-миграции. Docker entrypoint сериализует `alembic upgrade head` PostgreSQL advisory lock, поскольку несколько сервисов стартуют одновременно.

## Запуск и проверка
- Из `backend/`: установить `requirements.txt` в venv; конфигурация — `.env.example` → локальный `.env`; применить `alembic upgrade head` к выбранной dev-БД; API — `uvicorn app.main:app --reload`; worker — `python worker.py`.
- Боты из `backend/`: `python -m app.telegram_bot` и `python -m app.telegram_bot --profile hevc`. Compose поднимает PostgreSQL, API, worker и оба профиля; образ общий, build context — `backend/`.
- Проверки из `backend/`: `python -m pytest -q`; синтаксис — `python -m compileall -q app tests worker.py`. Для изменения поведения выбирай соответствующие существующие `tests/test_*.py` и регрессии на границах сценария.
- Большинство тестов изолируют БД/qB/HTTP mock-объектами; наличие `e2e` в имени не означает проверку реального стека. PostgreSQL-тест миграции 0018 требует `ALTT_TEST_POSTGRES_URL` отдельной тестовой БД.
- Docker требует MediaInfo >=26 и удаляет bundled lib из pymediainfo wheel; MIME вложений читается собственным EBML-парсером. Не убирай эти проверки при изменении образа.
- Срез знакомства 2026-10-08, HEAD `f513ff1`: на доступном Python 3.13.2 — 770 passed, 1 skipped (нет `ALTT_TEST_POSTGRES_URL`). Python 3.14, Docker и реальные внешние сервисы в этой проверке не запускались; это исходный результат, не гарантия для будущих изменений.
