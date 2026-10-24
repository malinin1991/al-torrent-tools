# AL Torrent Tools — текущая архитектура

Документ обновлён по коду проекта 2026-10-18. Имя `PROJECT_PLAN.md` сохранено для существующих ссылок; первоначальный план доступен в истории Git. Старые задания этапа создания проекта находятся в [архиве промптов](MULTITASK_PROMPTS.md).

Установка, переменные окружения и деплой: [README](../README.md). Краткие инструкции для разработки: [AGENTS.md](../AGENTS.md).

## Назначение и стек

AL Torrent Tools синхронизирует релизы AniLibria/AniLiberty, архивирует версии `.torrent`, передаёт раздачи с qBittorrent master на slave, отслеживает файлы и соответствие AVC/HEVC. Дополнительные функции: Telegram-подписки и HEVC-уведомления, MediaInfo, отправка медиа во внешний Video Kensetsu.

Целевая версия — обычный CPython 3.15.0 с GIL (`backend/Dockerfile`), без JIT/t. Проверенные Python-зависимости закреплены в `backend/constraints.txt`. Backend использует FastAPI, синхронный SQLAlchemy, PostgreSQL 16, psycopg, Alembic и APScheduler. HTTP-клиент AniLibria реализован на httpx, qB — через qbittorrent-api. UI — Jinja2 + HTMX + JavaScript/CSS в шаблонах, без отдельного frontend-проекта.

## Структура

- `backend/app/main.py` — HTML-страницы, формы, partials, SSE, health и lifecycle API.
- `backend/app/api/rest.py` — REST `/api`, webhook qB, экземпляр JobRunner и регистрация обработчиков.
- `backend/app/core/config.py` — настройки из окружения и `.env`.
- `backend/app/db/` — модели и синхронные DB Session.
- `backend/app/providers/anilibria/client.py` — API-клиент, retries, fallback URL, bearer token и passkey.
- `providers/anilibria/backoffice.py` — read-only Nova с cookie/CSRF-входом; `sources.py` — выбор site → hidden/copyright-blocked backoffice.
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

Ручные джобы из UI/API и scheduled джобы worker планируются через `JobRunner.schedule_job`: ограниченный пул потоков своего процесса, свой event loop и своя DB Session на выполнение. Dispatch в executor происходит до возврата из `schedule_job`, wrapper защищён от отмены через shield; отмена самого задания остаётся кооперативной через БД. Executor принадлежит процессу, поэтому закрытие временного loop родительского джоба не теряет дочерние hash jobs и не ждёт их завершения. APScheduler callback ждёт свой джоб, сохраняя `max_instances`/`coalesce`. Master/slave polling также вынесен из scheduler loop в поток с собственной Session. Таблица `jobs` хранит состояние и параметры; это не общая очередь, которую забирает worker.

Session advisory lock релиза держится на отдельном соединении с commit после acquire/unlock, без idle transaction на время обработки. Неопределённый исход acquire или ошибка unlock инвалидирует соединение перед возвратом в pool.

`JobRunner` управляет статусами `pending`, `running`, `stopping`, `success`, `partial`, `failed`, `cancelled`, дедупликацией и кооперативной остановкой. Запуск — условный атомарный `pending → running` после отдельного неблокирующего advisory lock. Reclaim повторно проверяет состояние под тем же lock и коммитит отмену до unlock; stop/cancel/finalization используют условные UPDATE. Поздний handler не возрождает терминальную задачу; stopping/cancelled имеют приоритет над success/partial.

`services/sync_outcomes.py` сохраняет `jobs.result_json/result_updated_at` и полный список `job_sync_failures`; `JobPartialFailure` — только сигнал уже сохранённого частичного результата. Полные проходы проверяют полноту каталога до sweep/baseline. Новый `retry_sync_failures` получает server-validated snapshots не более 100 проблем, проверяет точный source/ID/hash/роль под release lock и не запускает глобальный sweep/checkpoint для subset. Неизвестный hash запрещает точечный torrent retry. UI/API интеграция — `api/operations_ui.py`.

Актуальные типы и их описание находятся в `services/job_catalog.py`, регистрация — в `api/rest.py`, расписания — в `worker.py`. Для одного релиза используется `force_release_sync`; отдельного `single_download.py` нет. Cleanup разделён на `cleanup_master` и `cleanup_slave`; legacy API-тип `cleanup` преобразуется в `cleanup_master`.

## Синхронизация и архив

`ongoing` / `full_sync` получают данные API и передают релизы в `TorrentProcessor`. `release_checkpoints` содержит маркеры `updated_at` / `fresh_at` и fingerprint торрентов. `ongoing` всегда проверяет список торрентов, даже при прежних markers; они управляют только обновлением метаданных qB. `ongoing_watch` сохраняет релизы после исчезновения из расписания на 14 дней, продлевая срок при любом новом ID торрента. Кэш проверяется в каждом запуске `ongoing`; подтверждённый 404 релиза и истечение срока удаляют только запись кэша. Сбой расписания не запускает сроки исчезновения.

`seen_torrents` хранит текущий обработанный hash для `torrent_id`: `torrent_id` — primary key, `info_hash` имеет отдельное ограничение уникальности. Новая версия с изменившимся hash должна обрабатываться и при прежнем `torrent_id`.

`TorrentArchiveService` сохраняет `.torrent` и метаданные версии. `api_present` означает наличие в текущем списке торрентов выбранного источника, `superseded` — замену другой версией того же torrent_id. Эти признаки не равны статусу pipeline или наличию медиа на диске. Исторические версии используются при сравнении файлов и расчёте HEVC timeline.

Публичный клиент обращается к AniLibria API v1. Используемые endpoints и параметры определены в `providers/anilibria/client.py`; локальный OpenAPI — справочный снимок, а не гарантия актуальности внешнего сервиса.

При настроенном `anilibria_admin_base_url` (один URL без fallback; шаблон ссылок задаётся отдельно) фабрика возвращает `ReleaseSourceClient`. Публичная карточка имеет приоритет, если не скрыта и не заблокирована по правообладателю; геоблокировка сама по себе оставляет `site`. Nova используется только для hidden/copyright-blocked релизов. ID релизов и торрентов общие. Клиент Nova разрешает только GET данных и POST `/login`, извлекает CSRF из формы, обновляет cookie-сессию и валидирует полную пагинацию. Ошибки сети/авторизации не превращаются в отсутствие релиза. При retry pipeline проверяет одновременно ID и hash в актуальном списке торрентов, поскольку soft-deleted карточка может отвечать 200.

`backoffice_sync` обходит все подходящие релизы каждые 900 с (DB override, минимум 60 с), включая неизвестные и ранее импортированные, вернувшиеся на сайт. Обход начинается с полного индекса Nova. Если есть новые кандидаты, публичный каталог используется как необязательное ускорение с общим лимитом 30 с: только полностью проверенная пагинация и подтверждённые обычные публичные карточки позволяют исключить site-релизы. У штатной публичной карточки нет поля is_hidden и copyright=false; явный hidden=true, null/некорректные признаки и отсутствие copyright не разрешают исключение. Ошибка или неполнота каталога отменяет это ускорение целиком и не мешает обходу Nova, не меняет источник или результат джобы. Когда все кандидаты ранее импортированы, каталог не читается. Только явно обычные карточки с обоими признаками hidden/copyright=false исключаются по самому индексу; при недоступном ускорении неполные поля требуют чтения карточки. Страницы Nova и периодический прогресс обработки, включая пропуски, пишутся на уровне info и видны в UI. Ранее импортированные релизы проверяются независимо от этих признаков. Публичная карточка подтверждает выбор источника под `release_lock` processor. При ошибке проверки последний подтверждённый backoffice продолжает обновляться из Nova с прежними source/hidden/copyright и `source_checked_at`; site и неизвестные источники откладываются без изменения архивов и api_present. После первой ошибки новые проверки сайта откладываются до следующего запуска, чтобы не повторять таймаут на каждом релизе. Отложенные кандидаты видны в результате джобы и не позволяют завершить baseline. В остальных сценариях синхронизации сетевой сбой по-прежнему не разрешает переход в Nova. Полный и полный metadata-sync также включают этот обход. Public sweep исключает backoffice-релизы. `release_lock.py` сериализует processor одного релиза между разными типами джобов PostgreSQL advisory lock, удерживаемым отдельным соединением через commit.

`releases.source/is_hidden/source_checked_at` хранит последний подтверждённый источник. `torrent_policy.py` заменяет announce/announce-list без изменения raw info, обновляет полный список трекеров в qB и управляет только метками `site/hidden/blocked`. `torrent_archive.backoffice_trackers` сохраняет политику для данного hash даже после возврата на сайт; source tags при этом переключаются. Архив, pipeline и медиа сохраняются. Жанры имеют отдельный список управляемых меток, пользовательские метки сохраняются.

Первый успешный полный обход фиксирует baseline в settings; флаг `torrent_archive.notification_baseline` подавляет HEVC-уведомления первоначального импорта, в том числе после отложенного hash/retry. Незавершённый импорт сохраняет baseline до успешного завершения. Новые AVC после baseline уведомляются по обычным правилам, само состояние HEVC продолжает рассчитываться.

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

`hevc_pairing.py` определяет `missing`, `overdue`, `type_mismatch`, учитывает диапазоны эпизодов, качество, тип рипа, историю версий и `ignore_hevc`. SLA — 24 часа, окно парной загрузки HEVC→AVC — 2 часа. Наличие HEVC объединяет web-типы в общий presence-слот, но SLA-якорь и правка ярлыка по составу файлов наследуются только внутри одинакового нормализованного типа рипа и качества: старые WEBRip не старят новый WEB-DL. Подробные исключения описаны в docstring модуля и тестах `test_hevc*`.

В HEVC-фильтре страницы релизов полный pairing выполняется один раз на запрос. Фильтрация release IDs и сортировка overdue используют проекции одного результата с общим SLA clock. Бейджи страницы сохраняют собственный расчёт с прежним `include_ignored`; глобального кеша pairing и production-пула HEVC нет.

`mediainfo.py` сохраняет метаданные медиа; `matroska_attachments.py` читает MIME вложений собственным EBML-парсером. Docker устанавливает MediaInfo >=26 и удаляет bundled lib из pymediainfo wheel. `video_kensetsu.py` интегрирует внешний сервис кодирования, пресеты видео/аудио и пакетную отправку файлов.

Пакетные сценарии используют `mediainfo_paths_parallel`: gate читает метаданные порциями по 500 путей, native parsing выполняется ограниченным пулом, Session и записи остаются у вызывающего потока. Full scan, incremental и MediaInfo после hash по умолчанию используют до четырёх потоков; явный `workers` full/incremental сохраняется, включая 1. До 25 готовых отчётов записываются atomic upsert по `full_path` в одинаковом порядке canonical path и коммитятся; stop дожидается уже запущенных разборов и сохраняет последний неполный пакет. Проверяются границы media root, incomplete и identity файла до/после parse. Одиночный API извлечения MediaInfo для UI сохраняет прежний контракт. Это отдельная пакетная запись метаданных, не изменение sticky/hash/encoder-history транзакций.

### Квитанции и выполнение Kensetsu

`encoder_history.py` ведёт durable-журнал приёма исходников, независимо от pipeline, sticky `ui_status` и HEVC pairing. `encoder_submissions` хранит действие пользователя и параметры, `encoder_submission_items` — снимок каждого исходника и статус `sending/accepted/not_sent/rejected/unknown`. FK на release/archive/file отсутствуют намеренно: история сохраняется после смены версии, удаления исходника и reset. Миграция — `0025_encoder_history`.

Уникальный `request_id` возвращает одну квитанцию при повторе HTTP-действия; другой payload с тем же ключом даёт 409. PostgreSQL advisory xact locks сериализуют пересекающиеся отправки по серверу, infohash, exact relative_path и пресетам. Принятый или неизвестный результат требует явного подтверждения повтора, активную попытку повторить нельзя. Повтор создаёт новое действие с `retry_of_id`; принадлежность файла исходной версии перепроверяется.

Intent и отметка начала запроса коммитятся до внешнего POST. После async-проверки пресетов повторно проверяются идентичность и stat исходника. Ошибки до передачи дают `not_sent`, явный отказ — `rejected`, потеря/противоречивость ответа — `unknown`. Batch сопоставляется только по точному полному пути (`input_path` в новом API), без basename и позиции массива. Legacy single-ответ с точным `filename` допускается только для единственного фактически отправленного исходника, без противоречивого пути. Полный однозначный счётчик приёма может подтвердить группу без привязки job ID к отдельным файлам. Истёкшие через 600 с попытки восстанавливаются worker либо при чтении/новой отправке; неизвестный исход не повторяет POST автоматически.

`encoder_tracking.py` опрашивается worker каждые 30 с: до 20 заданий за проход, не более четырёх HTTP-запросов одновременно. `encoder_jobs` хранит наблюдаемые `planned/in_progress/done/error/cancelled`, прогресс, даты, ошибки выполнения и отдельные ошибки связи, наличие и размер результата. Активные задания получают следующий опрос через 30 с, конечные — через 5 минут; ошибки увеличивают задержку до 15 минут. Lease/token и атомарные claims защищают от конкуренции; DB Session закрыта на время HTTP. Проверяется сохранённый сервер отправки и точный `input_path`, если он есть. Готовность берётся из `output_available`; для legacy после `done` используется HEAD download без скачивания видео. Отключённая интеграция останавливает опрос, сохраняя историю. Миграция `0027_encoder_job_tracking` создаёт наблюдения для принятых job IDs и исправляет доказуемые старые single-квитанции по сохранённому ответу, без внешнего POST.

UI `/encoder-history` показывает приём отдельно от выполнения и готовности, фильтры активных/готовых/ошибок/недоступности, даты проверок и ссылку скачивания. POST `/api/encoder-submissions/receipts` получает последние квитанции для файлов небольшими пакетами. SSE-канал `encoder` использует max ID/updated_at и временной tick для незавершённых попыток; запись наблюдения обновляет token родительской отправки. HTMX обновляет журнал с сохранением раскрытий и прокрутки, JS — квитанции рядом с файлами. `done` не означает появления HEVC-раздачи и не меняет pairing или sticky-статусы.

### Автоматический кодировщик

`services/automatic_encoder.py` ведёт назначения `release_encoder_policies`, границы наблюдения `encoder_auto_sources`, доказательства hash-settle `encoder_file_readiness` и устойчивые кандидаты `encoder_auto_items` с аудитом `encoder_auto_events`. Первый запуск фиксирует известные версии, включая архивы без состава. Новые версии регистрируются при сохранении архива; хеширование атомарно сохраняет доказательство и кандидата. Включение, tracking UI/ботов, сохранение архива и intent отправки координируются коротким PostgreSQL transaction advisory lock; он не держится во время сети или хеширования. Пауза сохраняет участие, снятие tracking начинает новую generation при возврате. Sticky, pipeline и HEVC pairing не изменяются.

Worker каждые 30 с сверяет ограниченную порцию кандидатов, восстанавливает истёкшие claims и отправляет один готовый исходник через общий `encoder_submission.py`. Все AVC-варианты выбранных файлов tracked-релиза используют его собственные video/audio/downmix либо действующие effective defaults Kensetsu, если оба ID назначения отсутствуют. Для автоматического режима defaults проверяются перед новой отправкой; фактические параметры фиксируются в квитанции, а policy остаётся без индивидуального назначения. Некорректная частичная пара ID не считается автоматическим режимом. Недостающее доказательство восстанавливается обычным hash job, не чтением видео в отправителе. Lease/token, ревизия назначения и повторные source guards защищают подготовку; durable `request_started_at` отделяет безопасное перепланирование от неизвестного побочного действия. Новые столбцы истории сохраняют origin, revision, frozen payload и восстановленную квитанцию. Связь auto-кандидата указывает конкретную строку квитанции, включая ручные batch.

`encoder_receipts.py` только читает современные квитанции по сохранённым серверу/ID. `processing` остаётся ожиданием; interrupted, отсутствие квитанции и противоречия требуют проверки, без повторного POST. Поздний accepted сохраняет первоначальный transport outcome. Изменение stat/identity при любом начатом исходе HTTP или позднем восстановлении помечает source_conflict, не теряя приём/job IDs. Snapshot пресета — наблюдавшиеся поля API; закрепления полной внешней YAML-конфигурации контракт не гарантирует.

`/automatic-encoder` и одноимённый SSE-канал показывают пресеты tracked-релизов, очередь, состояние worker и явное добавление существующих файлов с ограничением страницы. На `/releases` видны состояние автокодирования и индивидуальный пресет либо режим «Автоматический»; назначение и сброс доступны прямо в карточке через тот же диалог. Обе страницы обновляют revision/параметры сразу после сохранения и через SSE, сохраняя состояние страницы и открытого диалога. Policy загружаются для релизов текущей страницы одним запросом; change token релизов учитывает настройки кодировщика. `/encoder-history` фильтрует ручные/автоматические действия. Миграция `0028_automatic_encoder`; переход к defaults не требует изменения схемы. [ТЗ](design/automatic-encoder.md).

Общий `automatic_encoder.js` подключается на обеих страницах через `static_asset_url`: параметр `v` зависит от текущего содержимого файла, поэтому после обновления HTML браузер получает новый URL при изменении скрипта. Это предотвращает использование старой версии, в которой обработчик ожидал только `auto-encoder-result` и падал на странице релизов. Открытую до обновления страницу нужно перезагрузить.

### Рабочий список HEVC и доставка Telegram

`services/hevc_workbench.py` и `api/hevc_workbench.py` дают `/hevc-workbench`: один pairing с историческими якорями, exact `(info_hash, relative_path)`, последняя квитанция оконным запросом, bulk SQL и проверка FS только видимой страницы. Общие ready/unavailable фильтры и счётчики явно относятся к снимку БД. Видимые расхождения с диском подписаны отдельно. `static/hevc_workbench.js` сохраняет выбор/раскрытия/прокрутку, откладывает swap во время отправки и не обходит duplicate/unknown guards. Перед внешним POST повторно проверяются источник, stat, is_checking и активный hash job.

`telegram_outbox.py` резервирует одну due запись с `sending`, lease/token и fencing результата. `next_attempt_at` и общий cooldown профиля обрабатывают временные ошибки/429 без блокировки хвоста. Неопределённый исход — `failed/delivery_unknown`, явный ручной повтор с подтверждением. Receipt, primary `tg_status` и audit коммитятся вместе; HEVC не меняет primary-статус. Producer primary имеет стабильный dedupe и учитывает legacy строки; обычный enqueue не обходит unknown/failed/cancelled. Диагностика и повтор — `/telegram-outbox`, REST и SSE `telegram-outbox`. Схема outcomes/outbox — единая миграция `0026_job_outcomes_outbox`; старые API/worker/боты при обновлении нужно заменить согласованно.

## Данные и настройки

Полная схема определяется [моделями](../backend/app/db/models.py) и [миграциями](../backend/alembic/versions/), а не отдельным SQL-черновиком. Основные группы таблиц:

- настройки и источники: `settings`, `extra_urls`, `qb_clients`, `cleanup_rules`;
- джобы: `jobs`, `job_logs`, `job_sync_failures`;
- релизы и дедупликация: `releases`, `release_members`, `release_checkpoints`, `ongoing_watch`, `seen_torrents`;
- архив и pipeline: `torrent_archive`, `torrent_pipeline`, `pipeline_events`;
- файлы: `torrent_files`, `disk_file_hashes`, `file_change_events`, `file_mediainfo`;
- квитанции и выполнение Kensetsu: `encoder_submissions`, `encoder_submission_items`, `encoder_jobs`;
- Telegram: `tracked_releases`, `telegram_outbox`, `telegram_bot_access`.

Для поддерживающих override настроек приоритет — БД → env/default. Telegram и Video Kensetsu настраиваются в UI и хранятся в БД. Секретные поля маскируются при чтении настроек через API; PUT с маской `***` или пустым секретом сохраняет существующее значение и не создаёт пустой override над env. Название `password_encrypted` у qB пока не означает шифрование — поле содержит plaintext. Даты БД — naive UTC, браузеру передаются с указанием UTC через `as_utc_iso`.

## Live UI и health

Страница релизов имеет SQL-фильтры по источнику, типу блокировки и локальному `releases.is_abandoned`. Ручная отметка «Брошен» не изменяется синхронизацией и не влияет на pipeline/HEVC. Фильтры передаются при пагинации и SSE-перезагрузке. Оба Telegram-профиля показывают offsite-название без публичной ссылки и с «Нет на сайте»; кнопка админки сохраняется. Primary `/add` и `/del` разрешают числовые ссылки (включая Nova URL) через общий release_id.

Discovery и прямой `refresh_release_meta` используют общий контекст lock → обработка → source policy. Поэтому точечный `meta_sync` по известному torrent_id обновляет метки и трекеры сразу, даже если ID/hash не изменились.

`GET /ui/events` подписывает соединение на общий `UiEventBroadcaster`, созданный в lifespan каждого процесса API. Один цикл проверяет объединение активных каналов с паузой 3 с между проходами и рассылает именованные SSE-события при изменении tokens; число вкладок не умножает SQL-проверки одних каналов. Новые каналы добавляются в следующий проход, последние отписки удаляют их. Без подписчиков цикл останавливается, при остановке API broadcaster будит streams и дожидается уже начатого чтения. Синхронный read не отменяется при последней отписке: новое соединение переиспользует ещё выполняющийся poller, не запуская параллельный SQL. У каждого подписчика ограниченный набор изменившихся каналов вместо очереди событий; медленный клиент не задерживает остальных. Первый token канала служит baseline, переподключение по-прежнему обновляет HTML на клиенте. Активный pipeline detail дополнительно использует tick 3 с для qB progress; `info` — 30 с. Браузер загружает соответствующие HTMX partials. Webhook qB не отправляет событие в браузер напрямую.

Сбор token выполняется через threadpool с собственной Session на проход; соединение закрывается до SSE yield. Ошибка SQL одного канала вызывает rollback до чтения следующего. Каналы pipeline принимают только положительные bigint ID. Канал releases включает минутный SLA tick, который наследует workbench.

HTMX 1.9.12 обслуживается из `static/vendor/` с сохранённой лицензией и контрольными суммами. Клиент держит один запрос на partial до его завершения и догоняет события, пришедшие во время запроса. Scroll, раскрытия, выбор, параметры заданий и фокус фиксируются непосредственно перед swap; отключённые сервером поля не восстанавливаются. Ошибки HTTP partial и переподключение EventSource отображаются в UI; HTTP 500 учитывается даже при resolved Promise HTMX 1.x. На узких экранах формы переносятся, широкие таблицы имеют собственную горизонтальную прокрутку.

Канал `hevc_workbench` учитывает версии/файлы, active hash jobs, квитанции, настройки и минутный SLA tick; сам token не вычисляет pairing и не обращается к FS. Jobs token учитывает сохранённый result, Telegram token — обновления/попытки и сроки повторов; info включает heartbeat обоих профилей.

`/health/live` выполняет `SELECT 1` без вызова AniLibria и используется Docker healthcheck. `/health` обращается к AniLibria API. Worker и боты имеют отдельные heartbeat healthchecks; их файлы не заменяют проверку успешности внешних запросов.

## Проверки и развитие

Команды установки и тестов находятся в [README](../README.md#тесты). Большинство тестов использует mocks; PostgreSQL-тест миграции 0018 и проверки конкурентных claim истории отправок требуют отдельный `ALTT_TEST_POSTGRES_URL`. Последние создают собственные уникальные схемы и удаляют их.

Рабочий Dockerfile использует обычный Python 3.15.0 с системной MediaInfo >=26. После повторной проверки собственная сборка `_decimal` и LibYAML удалена: Decimal fallback достаточен, YAML-конфигурация не используется. Uvicorn сохраняет uvloop/httptools; pytest/watchfiles, тесты и benchmark-скрипты выделены в Docker target `development`. Production target `runtime` содержит рабочие исходники, миграции и healthcheck/profiler-скрипты. Первоначальный образ с native-ускорителями прошёл по 1011 тестов на ARM64 и x86-64; после функциональных улучшений опубликованный образ и его вариант без `_decimal` прошли по 1288 тестов на обеих архитектурах с PostgreSQL 16. p95 benchmark остаётся неопределённым и не доказывает ускорения на Unraid. Текущее решение — [PYTHON_315_MIGRATION.md](PYTHON_315_MIGRATION.md). Исторические измерения — [PYTHON_RUNTIME_BENCHMARKS.md](PYTHON_RUNTIME_BENCHMARKS.md), provenance — [PYTHON_315_COMPATIBILITY.md](PYTHON_315_COMPATIBILITY.md). Opt-in `scripts/profile_runtime.py` использует sampling profiler 3.15 и автоматически не запускается.

Дополнительный эксперимент regular 3.15 / 3.15t GIL-on / GIL-off прошёл по 1011 тестов на общем pure psycopg/libpq backend. Изолированный `scripts/hevc_parallel_experiment.py` переносит только immutable snapshot целых релизов в bounded pool, не передавая ORM/Session. Production его не импортирует. Snapshot + hot4 на GIL-off ускорил HEVC compute примерно на 19%, ценой почти двукратного CPU time и роста памяти; JSON/Jinja показали регрессии. Исходный psycopg-binary не имеет t-wheels, а Unraid amd64 не проверен. Результаты и границы: [PYTHON_315T_BENCHMARKS.md](PYTHON_315T_BENCHMARKS.md), [аудит рефакторинга](PYTHON_315T_OPTIMIZATION.md).

Последующая проверка целых операций включила реальные PostgreSQL/Jinja/HTTP, native MediaInfo и BLAKE3 на четырёх пользовательских MKV, а также отдельный batch96 сгенерированных файлов. Рабочие оптимизации оставлены на обычном 3.15: HEVC −16% по median, incremental MediaInfo default4 около −28% на реальных MKV. P95 MediaInfo остаётся неопределённым; полное хеширование уверенного выигрыша не показало. Whole-page HEVC pool на t проиграл regular без пула, поэтому не подключён. Окончательный код прошёл по 1041 тесту в трёх экспериментальных режимах и рабочем regular binary-образе. Все независимые серии, source snapshots, проверки и ограничения — [OPERATION_BENCHMARKS_315.md](OPERATION_BENCHMARKS_315.md).

При изменении схемы добавляй Alembic-миграцию; при изменении UI проверяй partial и его SSE-token; при изменении pipeline и файлов сохраняй историю, идемпотентность и регрессии соответствующего сценария. Первоначальные фазы и чек-листы больше не используются как текущий backlog.

После реализации пунктов 1–3 и 5 дорожной карты обычный CPython 3.15 с отдельной PostgreSQL прошёл **1288 тестов без пропусков**. Проверены существующие данные при upgrade/downgrade 0026 и оба исполняемых JS client tests. Независимый peer/integration review закрыл найденные замечания; специализированный Bugbot в этой сессии недоступен и не запускался. Результат, ограничения browser smoke и условия обновления — [IMPROVEMENTS_2026_10_13.md](IMPROVEMENTS_2026_10_13.md). Временные тестовые контейнеры/БД удалены; рабочий Unraid не обновлялся.

Ревью 2026-10-17 четырьмя специализированными агентами закрыло подтверждённые ошибки UI/live, API, dispatch/locks и квитанций Kensetsu. Итог: **1448 passed без пропусков** на CPython 3.15.0/PostgreSQL 16.15 ARM64, три исполняемых JS-сценария, browser smoke desktop/mobile и реальный SSE с сохранением состояния. Замечания каждой роли, исправления и границы проверки — [REVIEW_2026_10_17.md](REVIEW_2026_10_17.md).
