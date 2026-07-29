# AL Torrent Tools

Веб-сервис для автоматизации торрент-джобов AniLibria, архивирования `.torrent` и пайплайна `master -> slave` для qBittorrent.

## Текущий статус

Реализованы фазы 0-3 в базовом объеме:
- джобы `ongoing`, `full_sync`, `cleanup`;
- архив `.torrent` с таблицей `torrent_archive`;
- pipeline `master -> slave` с webhook и fallback polling;
- базовый UI для главной, джобов, настроек, архива, доп. релизов и пайплайна;
- live-обновление UI через **SSE** (`GET /ui/events`) + HTMX partials (без interval-poll).

### UI live (SSE)

Открытые страницы (`/`, `/jobs`, `/pipeline`, `/pipeline/{id}`, `/releases`, `/archive`, `/info`) подписываются на `EventSource /ui/events?channels=…`. Сервер раз в ~1 с считает лёгкие change-token’ы по БД и шлёт именованное событие только при изменении; клиент тогда точечно подтягивает HTML-partial (`show:none`, сохранение скролла и `<details>`). Inbound webhook qBittorrent (`/api/webhooks/qb/complete`) к браузеру не пушит — он меняет БД, после чего срабатывает SSE-токен канала `pipeline`.

Страницы `/settings` и `/extra-urls` остаются без live (только действия пользователя).

## Требования

- Python `3.14`
- Docker + Docker Compose

## Установка локально

```bash
cd /Users/geekaz0id/PycharmProjects/al-torrent-tools/backend
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Далее:

```bash
cd /Users/geekaz0id/PycharmProjects/al-torrent-tools/backend
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

В отдельном терминале:

```bash
cd /Users/geekaz0id/PycharmProjects/al-torrent-tools/backend
python worker.py
```

## Запуск через Docker Compose

```bash
cd /Users/geekaz0id/PycharmProjects/al-torrent-tools
cp backend/.env.example backend/.env
docker compose up --build
```

Если меняли `command`/`entrypoint` в `docker-compose.yml`, пересоздайте контейнеры (`docker compose up --build`), иначе `api`/`worker` могут стартовать без `alembic upgrade head` и падать на отсутствующих таблицах.

После запуска:
- UI/API: [http://localhost:8000](http://localhost:8000)
- health: [http://localhost:8000/health](http://localhost:8000/health)

Торрент-файлы пишутся по канону `TORRENT_STORAGE_DIR` → `settings.torrent_storage_dir` (локально default — `<repo>/data/torrents`, в Docker Compose — `/app/data/torrents` с volume `./data/torrents`). При старте контейнеров `api`/`worker` entrypoint выполняет `alembic upgrade head`.

## Сборка образа и деплой на Unraid

Один образ используется и для `api`, и для `worker`. Контекст сборки — `./backend`.

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
cd /Users/geekaz0id/PycharmProjects/al-torrent-tools

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
cd c:\Users\GeeKaZ0iD\Cursor\al-torrent-tools

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

3. Подними стек из `docker-compose.unraid.yml` (образ `registry.mageek.su/al-torrent-tools:latest`, без bind-mount исходников и без `--reload`). У `api` / `worker` / `telegram-bot` стоит `pull_policy: always` — при `up` compose тянет свежий digest тега `latest`.

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
- `ANILIBRIA_LOGIN` / `ANILIBRIA_PASSWORD` - опционально; в UI на странице **Настройки → AniLiberty API** можно войти через `POST /accounts/users/auth/login` ([документация](https://anilibria.top/api/docs/v1)).
- `ANILIBRIA_REQUEST_RETRIES` - количество повторов запросов к AniLibria при `429/5xx` и сетевых ошибках.
- `ANILIBRIA_RETRY_DELAY_MS` - задержка между повторами в миллисекундах.
- `SCRAPE_PAUSE_EVERY` - после скольких релизов делать паузу в `ongoing/full_sync`.
- `SCRAPE_PAUSE_SEC` - длительность паузы между пакетами запросов.
- `ONGOING_INTERVAL_SEC` - интервал фонового запуска `ongoing`.
- `CLEANUP_INTERVAL_SEC` - интервал фонового запуска `cleanup`.
- `CLEANUP_ALLOW_DELETE` - разрешить реальное удаление в qB (`true` по умолчанию; `false` — только отчёт в логах).
- `PIPELINE_MASTER_MIN_AGE_MIN` - минимальный возраст `master_added` перед fallback polling.
- `PIPELINE_RECONCILE_INTERVAL_SEC` - интервал полной сверки pipeline ↔ master (по умолчанию 600).
- `JOB_STALE_MINUTES` - порог отмены зависших pending/running (worker reclaim, по умолчанию 30).
- `TORRENT_STORAGE_DIR` - каталог для `.torrent` архива.

URL/token AniLibria и интервалы из UI имеют приоритет над env (DB override → env default).

`ongoing` / `full_sync` пропускают релизы без изменений: сравнивают `updated_at`/`fresh_at` из списка
с таблицей `release_checkpoints`, а внутри обработки — fingerprint списка торрентов
(не дергают `get_release` и не качают `.torrent`, если всё уже в `seen_torrents`).

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
        │  curl …?hash=%I
        ▼
  POST/GET /api/webhooks/qb/complete
        │
        │  pipeline найден по info_hash, статус master_added
        ▼
  добавить тот же .torrent в qB slave
  pipeline: master_complete → slave_added → done

  если slave лежит:
  pipeline → waiting_slave
  worker каждые 5 мин → slave ожил?
    ├─ есть в API и на master → slave_added → done
    └─ нет в API или нет на master → cancelled
```

Если webhook не сработал, есть запасные механики:

1. **Worker ~60 с** — для `master_added` старше `PIPELINE_MASTER_MIN_AGE_MIN`: опрос master по hash; при 100% / seeding → slave; если торрента нет на master → `cancelled`.
2. **Сверка по расписанию** (по умолчанию каждые 10 мин, `PIPELINE_RECONCILE_INTERVAL_SEC` / настройка в UI; кнопка «Сверить с master» на `/pipeline`) — все pipeline в `master_added` / `master_complete` (ещё нет на slave):
   - загружен на master → досылка на slave;
   - ещё качается / остановлен → ждём callback;
   - не найден на master → `cancelled`.
3. **Master недоступен** — pipeline в `waiting_master`; worker каждые 5 мин проверяет master и наличие торрента в AniLibria API по hash.
4. **Slave недоступен** — pipeline в `waiting_slave`; worker каждые 5 мин: slave ожил → торрент есть в API и на master → досылка на slave; иначе `cancelled`.

### Команда для qBittorrent master

В master-клиенте:

1. **Tools → Options → Downloads** (или Settings → Downloads).
2. Включи **Run external program on torrent finished** / **Run external program on completion**.
3. Вставь команду:

**Если AL Torrent Tools на той же машине, что и master (порт 8000 проброшен):**

```bash
curl -fsS -X POST "http://127.0.0.1:8000/api/webhooks/qb/complete?hash=%I"
```

**Если master в Docker / на другой машине**, подставь IP или hostname хоста, где слушает API:

```bash
curl -fsS -X POST "http://HOST:8000/api/webhooks/qb/complete?hash=%I"
```

Примеры `HOST`:
- macOS Docker Desktop → часто `host.docker.internal`
- Linux → IP хоста в docker-сети (`172.17.0.1` и т.п.) или IP LAN
- удалённый сервер → `http://altt.example.com:8000/...`

Параметр **`%I`** — Info hash v1 (qBittorrent подставит сам). Кавычки вокруг URL обязательны, иначе пробелы/спецсимволы обрежут команду.

Проверка вручную (подставь реальный hash из UI «Пайплайн»):

```bash
curl -fsS -X POST "http://127.0.0.1:8000/api/webhooks/qb/complete?hash=YOUR_INFO_HASH"
```

Ожидаемый ответ при успехе: `{"ok":true,"status":"done","pipeline_id":...}`.  
Если hash неизвестен → `404`. Если торрент на master ещё качается → `ok:false`. Если pipeline ещё не `master_added` → `ok:false` с пояснением.

GET тоже поддерживается (удобно для отладки):

```bash
curl -fsS "http://127.0.0.1:8000/api/webhooks/qb/complete?hash=%I"
```

## Документация

- [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md)
- [docs/MULTITASK_PROMPTS.md](docs/MULTITASK_PROMPTS.md)
- `docs/anilibria-v1.openapi.json` (локальный кэш OpenAPI)

## Проверка после запуска

Минимальный smoke-check:

```bash
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/api/jobs
curl -fsS http://localhost:8000/api/archive
curl -fsS http://localhost:8000/api/settings
```

## Тесты

```bash
cd /Users/geekaz0id/PycharmProjects/al-torrent-tools/backend
pytest
python -m compileall app tests worker.py
```
