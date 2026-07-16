# AL Torrent Tools — план проекта

Веб-сервис для автоматизации работы с торрентами AniLibria: джобы, настройки, UI, pipeline master→slave, архив `.torrent`.

**Не форк** `ongoing_monitor_new` — новый репозиторий с нуля. Логику переносим по смыслу, не копируем файлы целиком.

---

## Цели

| Функция | Источник идеи |
|---------|---------------|
| Мониторинг ongoing (расписание + обновления) | `ongoing_monitor_new/ongoing_monitor.py` |
| Полная синхронизация каталога | `ongoing_monitor_new/app.py` |
| Дополнительные URL релизов | `data/update.yaml` → CRUD в БД |
| Добавление в qBittorrent master → ожидание → slave | новая логика |
| Очистка неактуальных торрентов | `qBittorrent-AL-remove-old/main.py` |
| Архив `.torrent` с метаданными | новая логика |
| Настройки через UI | `.env` → БД + API |

---

## Стек

| Слой | Выбор |
|------|-------|
| Backend | Python 3.12+, FastAPI, SQLAlchemy 2, Alembic |
| БД | **PostgreSQL 16** (параллельные записи от джобов) |
| Файлы | Volume `data/torrents/` для архива `.torrent` |
| AL API | [AniLibria API v1](https://anilibria.top/api/docs/v1#/) — **только API**, без парсинга HTML |
| qBittorrent | `qbittorrent-api` |
| Jobs | Отдельный `worker` процесс + таблица `jobs` + APScheduler |
| UI | HTMX + Jinja2 (фаза 1) — быстрый старт |

---

## AniLibria API v1

- Base: `https://anilibria.top/api/v1`
- OpenAPI: https://anilibria.top/storage/api/docs/v1?aniliberty-api-v1-docs.json
- Auth (опционально): `POST /accounts/users/auth/login` → Bearer token
- Поля: `include` / `exclude` на всех GET

### Ключевые endpoints

| Задача | Endpoint |
|--------|----------|
| Ongoing / расписание | `GET /anime/schedule/week` |
| Последние релизы | `GET /anime/releases/latest` |
| Полный каталог | `GET /anime/catalog/releases` (пагинация) |
| Релиз по alias/id | `GET /anime/releases/{idOrAlias}` |
| Пакет релизов | `GET /anime/releases/list?aliases=...` |
| Торренты релиза | `GET /anime/torrents/release/{releaseId}` |
| Скачать `.torrent` | `GET /anime/torrents/{hashOrId}/file` |
| Health | `GET /app/status` |

### Дедупликация (замена `block_hash`)

Таблица `seen_torrents`: уникальность по `torrent_id` (API) и/или `info_hash`. Новый торрент = записи ещё нет.

---

## Архитектура

```
al-torrent-tools/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── api/           # REST + webhooks
│   │   ├── core/          # config, scheduler
│   │   ├── db/            # models, session
│   │   ├── providers/
│   │   │   └── anilibria/ # HTTP-клиент v1
│   │   ├── services/
│   │   │   ├── qbittorrent.py
│   │   │   ├── torrent_archive.py
│   │   │   └── pipeline.py
│   │   └── jobs/
│   │       ├── ongoing.py
│   │       ├── full_sync.py
│   │       ├── cleanup.py
│   │       └── single_download.py
│   ├── worker.py
│   └── alembic/
├── frontend/              # templates (HTMX)
├── data/torrents/         # архив файлов
├── docs/
│   ├── PROJECT_PLAN.md    # этот файл
│   ├── MULTITASK_PROMPTS.md
│   └── anilibria-v1.openapi.json  # кэш схемы
├── docker-compose.yml
└── README.md
```

### Docker Compose

| Сервис | Роль |
|--------|------|
| `api` | FastAPI, UI, REST |
| `worker` | Джобы, pipeline polling |
| `postgres` | Метаданные, состояния |
| volumes | `data/torrents`, pg data |

---

## Схема БД (черновик)

```sql
settings (key, value, updated_at)

extra_urls (id, release_alias, release_id, note, enabled, created_at)

jobs (id, type, status, params_json, started_at, finished_at, error)
job_logs (id, job_id, level, message, created_at)

seen_torrents (
  torrent_id PK, info_hash UNIQUE, release_id,
  uploaded_at, processed_at
)

torrent_pipeline (
  id, info_hash, release_id, torrent_id,
  status,  -- discovered|master_added|master_complete|slave_added|done|failed
  master_added_at, slave_added_at, error, created_at
)

torrent_archive (
  id, info_hash, torrent_id, release_id, release_alias,
  anime_name, category, description, quality_json,
  file_path, file_size, created_at
)

cleanup_rules (
  id, name, tracker_host, message_contains,
  include_errored, delete_files, target_client, enabled
)

qb_clients (
  id, name, role,  -- master | slave
  host, port, username, password_encrypted, enabled
)
```

---

## Pipeline master → slave

```
discovered → master_added → master_complete → slave_added → done
                  ↓                ↓
               failed           failed (timeout)
```

- **Webhook** (предпочтительно): qBittorrent → `POST /api/webhooks/qb/complete?hash=...`
- **Polling** (fallback): worker опрашивает `torrents/info` по `info_hash`

---

## Фазы и Multitask

| Фаза | Режим | Срок |
|------|-------|------|
| **0** Каркас | 1 агент, последовательно | 2–3 дня |
| **1** Core jobs + UI settings | Multitask, 4 агента | 3–5 дней |
| **2** Архив + pipeline | Multitask, 2 агента | 3–5 дней |
| **3** Интеграция + polish | 1 агент | 2–3 дня |

**Промпты для каждой сессии:** [MULTITASK_PROMPTS.md](./MULTITASK_PROMPTS.md)

---

## Правила для Multitask

1. **Фаза 0 обязательна** — без каркаса параллельные агенты конфликтуют.
2. **Не параллелить:** `db/models.py`, миграции Alembic, `docker-compose.yml`, `AniLibriaClient` (только агент 0).
3. **Интеграция через интерфейсы:** джобы вызывают `AniLibriaClient`, `QbittorrentService`, `JobRepository` — не лезут в чужие файлы.
4. **Контракты фазы 0** (агенты 1.x должны соблюдать):
   - `app/providers/anilibria/client.py` — публичный API клиента
   - `app/db/models.py` — все модели
   - `app/services/job_runner.py` — запуск/статус джобов
5. **Коммиты на русском.**

---

## Что переносить из старых проектов

| Из | Что |
|----|-----|
| `ongoing_monitor_new/common.py` | `torrent_info_hash`, `qbittorrent_add`, обработка 409 duplicate |
| `ongoing_monitor_new/common.py` | `_set_release_comment` (qBittorrent ≥ 5.2) |
| `qBittorrent-AL-remove-old/main.py` | правила удаления по tracker status + errored |
| `ongoing_monitor_new` | идеи пауз между запросами (`scrape_pause_*`) |

**Не переносить:** BeautifulSoup, `libria_login` через HTML, `update.yaml` как файл.

---

## Риски

1. API v1 может меняться — держать OpenAPI в `docs/`, healthcheck `/app/status`.
2. Master/slave — самый сложный кусок; не стартовать до готовности `JobRunner` и `torrent_pipeline`.
3. Секреты qBittorrent — env или шифрование в БД; не коммитить `.env`.

---

## Критерии готовности MVP

- [ ] UI: настройки AL API (primary/fallback URL), qBittorrent master/slave
- [ ] CRUD extra URLs
- [ ] Джоб ongoing по расписанию
- [ ] Ручной запуск full_sync и single release
- [ ] Логи джобов в UI
- [ ] Cleanup с dry-run
- [ ] (Фаза 2) Архив и скачивание `.torrent`
- [ ] (Фаза 2) Pipeline master→slave
