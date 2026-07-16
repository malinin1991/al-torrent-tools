# AL Torrent Tools — промпты для Multitask

Копируй промпт целиком в новый чат Cursor (Agent mode).  
Для Multitask: запускай агентов **одной сессии** параллельно только после завершения фазы 0.

Проект: `/Users/geekaz0id/PycharmProjects/al-torrent-tools`  
Контекст: [PROJECT_PLAN.md](./PROJECT_PLAN.md)

---

## Фаза 0 — Каркас (ОДИН агент, НЕ Multitask)

```
Создай каркас проекта AL Torrent Tools в /Users/geekaz0id/PycharmProjects/al-torrent-tools.

Цель: веб-сервис для джобов с торрентами AniLibria + qBittorrent. Только AniLibria API v1, без парсинга HTML.

Сделай:
1. Структуру backend/ по PROJECT_PLAN.md (FastAPI, SQLAlchemy 2, Alembic, отдельный worker.py).
2. docker-compose.yml: api, worker, postgres:16, volumes для data/torrents.
3. Модели БД: settings, extra_urls, jobs, job_logs, seen_torrents, torrent_pipeline, torrent_archive, cleanup_rules, qb_clients.
4. Alembic: начальная миграция.
5. AniLibriaClient (httpx async) в app/providers/anilibria/client.py:
   - base_url из настроек (default https://anilibria.top/api/v1)
   - методы: health(), get_schedule_week(), get_release(), get_releases_list(), catalog_releases(), get_torrents_for_release(), download_torrent_file()
   - поддержка include/exclude, Bearer token опционально
   - fallback на второй base_url при ошибке сети
6. JobRunner в app/services/job_runner.py: создание job, статусы pending/running/success/failed, запись job_logs.
7. REST API:
   - GET /health
   - GET/POST /api/jobs, GET /api/jobs/{id}
   - GET/PUT /api/settings
   - CRUD /api/extra-urls
8. Минимальный UI (HTMX + Jinja): dashboard с последними джобами, заглушки страниц настроек.
9. requirements.txt, .env.example, README.md на русском.
10. Скачай OpenAPI в docs/anilibria-v1.openapi.json с https://anilibria.top/storage/api/docs/v1?aniliberty-api-v1-docs.json

НЕ реализуй бизнес-логику джобов ongoing/full_sync/cleanup/pipeline — только каркас и контракты.

Соглашения:
- Python 3.14+
- Комментарии и коммиты на русском
- Не трогать ongoing_monitor_new
- Переиспользуй идеи из ongoing_monitor_new/src/common.py только для qbittorrent_add и torrent_info_hash (скопируй и адаптируй в services/qbittorrent.py как заглушки)
```

**Проверка перед фазой 1:**
- [ ] `docker compose up` поднимает api + worker + postgres
- [ ] `GET /health` и `AniLibriaClient.health()` работают
- [ ] Миграции применяются
- [ ] CRUD extra-urls и settings через API

---

## Фаза 1 — Multitask (4 агента параллельно)

Запусти **4 чата/агента одновременно**. Каждый трогает только свои файлы (см. ограничения).

### Агент 1A — Ongoing job

```
Проект: /Users/geekaz0id/PycharmProjects/al-torrent-tools
Фаза 1, агент 1A. Каркас из фазы 0 уже готов — НЕ меняй models.py, docker-compose, AniLibriaClient.

Реализуй джоб ongoing в backend/app/jobs/ongoing.py:

1. Логика:
   - GET schedule/week через AniLibriaClient
   - Загрузить enabled extra_urls из БД, получить релизы по alias/id
   - Для каждого релиза: get_torrents_for_release()
   - Дедупликация через seen_torrents (torrent_id / info_hash)
   - Новые торренты → передать в TorrentProcessor (создай app/services/torrent_processor.py):
     - скачать .torrent через download_torrent_file()
     - добавить в qBittorrent master (из qb_clients role=master)
     - записать seen_torrents, job_logs
   - Пауза между релизами из settings (scrape_pause_sec, scrape_pause_every)

2. Зарегистрируй job type "ongoing" в JobRunner.
3. APScheduler в worker.py: интервал из settings (default 120 сек).
4. API: POST /api/jobs/ongoing/run — ручной запуск.

НЕ трогай: full_sync, cleanup, UI, pipeline, archive.
Файлы: jobs/ongoing.py, services/torrent_processor.py (базовая версия без slave), worker scheduler hook.
```

### Агент 1B — Full sync job

```
Проект: /Users/geekaz0id/PycharmProjects/al-torrent-tools
Фаза 1, агент 1B. Каркас готов — НЕ меняй models.py, docker-compose, AniLibriaClient.

Реализуй джоб full_sync в backend/app/jobs/full_sync.py:

1. Обход GET /anime/catalog/releases с пагинацией (meta.pagination.total_pages).
2. include=id,alias,names,season — минимум полей.
3. Для каждого релиза — та же цепочка что ongoing: torrents → dedup → TorrentProcessor.
4. Пауза каждые N релизов (settings: scrape_pause_every, scrape_pause_sec).
5. Прогресс в job_logs ("страница 3/171, релиз X").
6. Job type "full_sync", POST /api/jobs/full-sync/run.

Используй TorrentProcessor из services/ — если его ещё нет, создай минимальный интерфейс (Protocol) в services/torrent_processor.py и реализацию, согласованную с агентом 1A (только публичные методы: process_release(release_id)).

НЕ трогай: ongoing scheduler, cleanup, UI, pipeline, archive.
```

### Агент 1C — Cleanup job

```
Проект: /Users/geekaz0id/PycharmProjects/al-torrent-tools
Фаза 1, агент 1C. Каркас готов.

Портируй логику очистки из /Users/geekaz0id/PycharmProjects/qBittorrent-AL-remove-old/src/main.py в backend/app/jobs/cleanup.py и app/services/torrent_cleanup.py.

1. Функция find_removable_torrents(client, rules):
   - state_enum.is_errored → удалить (если rule.include_errored)
   - tracker.status == 4 + tracker.url содержит rule.tracker_host + msg содержит rule.message_contains
2. Режимы: dry_run (только лог) | delete (torrents_delete delete_files из rule).
3. target_client: master | slave | both — из cleanup_rules / qb_clients.
4. CRUD cleanup_rules в API: /api/cleanup-rules.
5. Job type "cleanup", scheduler interval из settings (default 3600 сек).
6. POST /api/jobs/cleanup/run?dry_run=true|false.

Правила по умолчанию в миграции или seed:
- tracker_host: tr.libria.fun:2710
- message_contains: Торрент не зарегистрирован
- delete_files: false

НЕ трогай: ongoing, full_sync, pipeline, archive, AniLibriaClient.
```

### Агент 1D — UI настроек и extra URLs

```
Проект: /Users/geekaz0id/PycharmProjects/al-torrent-tools
Фаза 1, агент 1D. Каркас готов.

Сделай HTMX UI в backend/app/templates/:

1. /settings — форма:
   - AL API primary URL, fallback URL
   - AL login/password (опционально, для passkey)
   - qBittorrent master: host, port, login, password
   - qBittorrent slave: host, port, login, password (опционально)
   - scrape_pause_every, scrape_pause_sec
   - ongoing_interval_sec, cleanup_interval_sec

2. /extra-urls — таблица CRUD:
   - release_alias, note, enabled
   - добавление / редактирование / удаление через HTMX без перезагрузки

3. /jobs — список джобов с фильтром по type/status, детальная страница с job_logs.

4. Dashboard / — кнопки: «Запустить ongoing», «Full sync», «Cleanup (dry-run)», «Cleanup (удалить)».

Стиль: простой, тёмная тема. Не подключай React.
НЕ меняй бизнес-логику джобов — только templates, static, api routes для HTML если нужно.
НЕ трогай: jobs/*.py кроме регистрации роутов в main если нужно.
```

**После фазы 1 (1 агент, интеграция):**

```
Проект: al-torrent-tools. Слей результаты фазы 1: разреши конфликты в torrent_processor.py, worker.py, main.py. Убедись что ongoing и full_sync используют один TorrentProcessor. Прогони docker compose up, проверь ручной запуск всех трёх джобов. Исправь импорты и тесты если сломались.
```

---

## Фаза 2 — Multitask (2 агента параллельно)

### Агент 2A — Архив торрентов

```
Проект: /Users/geekaz0id/PycharmProjects/al-torrent-tools
Фаза 2, агент 2A.

Реализуй app/services/torrent_archive.py:

1. При обработке нового торрента (расширь TorrentProcessor или хук):
   - сохранить .torrent в data/torrents/{info_hash}.torrent
   - записать torrent_archive: anime_name, category (season.year), description, quality JSON, release_alias, release_id, torrent_id, file_size

2. API:
   - GET /api/archive — список с пагинацией и поиском по названию
   - GET /api/archive/{id}/download — отдача файла
   - GET /api/archive/{id} — метаданные

3. UI: /archive — таблица с фильтром, кнопка скачать.

Метаданные брать из ответов AniLibria API (get_release + torrent object), не парсить HTML.

НЕ трогай: pipeline.py, webhooks.
```

### Агент 2B — Pipeline master → slave

```
Проект: /Users/geekaz0id/PycharmProjects/al-torrent-tools
Фаза 2, агент 2B.

Реализуй master→slave pipeline:

1. app/services/pipeline.py — state machine:
   discovered → master_added → master_complete → slave_added → done | failed

2. Измени TorrentProcessor:
   - добавлять только на master
   - создавать запись torrent_pipeline
   - НЕ добавлять сразу на slave

3. Webhook: POST /api/webhooks/qb/complete
   - query/body: hash (info_hash)
   - перевести pipeline в master_complete, добавить на slave, done

4. Polling fallback в worker (каждые 60 сек):
   - записи в статусе master_added старше N минут
   - проверить progress/state на master через qbittorrent-api
   - при completion → slave

5. UI: /pipeline — таблица активных/завершённых pipeline с статусами.

Документация в README: как настроить «Run external program on completion» в qBittorrent master.

НЕ трогай: archive, cleanup rules.
```

**После фазы 2 (1 агент):**

```
Интеграция фазы 2 al-torrent-tools: TorrentProcessor → archive hook + pipeline. E2E тест: mock или реальный релиз → master → webhook → slave. Обнови README с полной схемой деплоя.
```

---

## Фаза 3 — Полировка (ОДИН агент)

```
Проект: al-torrent-tools. Фаза 3 — полировка:

1. README: установка, docker compose, настройка qBittorrent webhook, переменные окружения.
2. .env.example — полный список.
3. Базовые тесты: AniLibriaClient (mock httpx), torrent_info_hash, cleanup find_removable_torrents.
4. Обработка ошибок: retry AL API, логирование в job_logs.
5. Опционально: миграция extra URLs из ongoing_monitor_new/data/update.yaml (скрипт scripts/import_extra_urls.py).
6. Проверь security: пароли не в логах, SECRET_KEY для сессий если есть UI auth.

Коммиты на русском.
```

---

## Шпаргалка: что НЕ параллелить

| Файл/область | Кто правит |
|--------------|------------|
| `db/models.py`, `alembic/` | Только фаза 0 |
| `providers/anilibria/client.py` | Только фаза 0 |
| `docker-compose.yml` | Фаза 0 и фаза 3 |
| `services/torrent_processor.py` | 1A создаёт, 1B/2B расширяют → интегратор мержит |
| `worker.py` | Интегратор после каждой фазы |
| `main.py` (роуты) | Интегратор или 1D для HTML-роутов |

---

## Порядок запуска в Cursor Multitask

```
1. [Один чат]     Фаза 0 — промпт выше
2. [Проверка]     docker compose, health, API
3. [4 чата сразу] 1A + 1B + 1C + 1D
4. [Один чат]     Интеграция фазы 1
5. [2 чата сразу] 2A + 2B
6. [Один чат]     Интеграция фазы 2
7. [Один чат]     Фаза 3
```

---

## Ссылки

- API docs: https://anilibria.top/api/docs/v1#/
- OpenAPI JSON: https://anilibria.top/storage/api/docs/v1?aniliberty-api-v1-docs.json
- Старый ongoing: `/Users/geekaz0id/PycharmProjects/ongoing_monitor_new`
- Старый cleanup: `/Users/geekaz0id/PycharmProjects/qBittorrent-AL-remove-old`
