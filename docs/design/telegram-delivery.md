# Надёжная доставка Telegram

Дата: 2026-10-13. Статус: проект проверен; сервис доставки реализован, UI/общая миграция интегрируются root. Согласован безопасный default для неопределённой доставки.

## Проблема и воспроизведение

`telegram_outbox.drain_outbox()` читает первые 20 `pending` по ID, затем делает HTTP и безусловно меняет ORM-объект. Claim, срока владения и срока следующей попытки нет. Единственный штатный отправитель — `_outbox_loop` процесса соответствующего бота (`primary` / `hevc`); worker формирует события и уведомления, но сам outbox не отправляет. Второй экземпляр бота, перезапуск или другой вызывающий drain не защищены от конкуренции.

На Python 3.15.0, SQLAlchemy 2.1.4 и SQLite `:memory:` выполнены две изолированные проверки исходного кода. `send_outbox_message` заменён coroutine-заглушкой; настройки заменены объектом с тестовым токеном. Рабочая директория `/private/tmp`, приложение импортировано через явный `sys.path`, локальный `.env` не читался. Ни одного запроса Telegram не сделано.

- 21 запись primary, первые 20 всегда возвращают mock `HTTP 500`. Три drain: каждый `sent=0, failed=20`; запись №21 осталась `pending, attempts=0`. Доказана блокировка хвоста ошибочным началом очереди.
- Два drain с отдельными Session и барьером внутри mock send: оба вызвали send для одного `chat_id`, оба вернули `sent=1`. Доказано отсутствие резервирования. SQLite-проверка воспроизводит алгоритмическую гонку; корректность будущей блокировки нужно проверять настоящим PostgreSQL.

Другие обнаруженные границы:

- `send_outbox_message()` превращает HTTP/API-ошибки в строковый RuntimeError и теряет `error_code` / `parameters.retry_after`.
- Forbidden в HEVC-группе остаётся pending бесконечно; терминальность сейчас выделена только для HEVC ЛС.
- `mark_outbox_sent()` обновляет `TorrentPipeline.tg_status` при любом `pipeline_id`. У HEVC overdue также есть `pipeline_id`, хотя уведомление относится к другому профилю. Изменять primary `tg_status` следует только для primary.
- Отправка и `tg_sent` сейчас коммитятся отдельно: подтверждение и аудит могут разойтись при падении.
- UI «Информация» показывает только число pending. Токен live UI не видит изменение ошибки/попыток при неизменном числе записей; HEVC heartbeat также не включён.

## Границы и гарантии

Сохраняются существующие `bot_key`, payload события и уникальность `(bot_key, dedupe_key)`, включая welcome, new release, overdue transition и missing SLA. Нельзя переименовывать dedupe keys, пересоздавать строки для retry, повторно уведомлять baseline или пересчитывать исходное событие по текущим данным. `refresh_source_link()` выполняется перед каждой фактической отправкой: актуальность ссылки не меняет содержание события. ACL HEVC проверяется непосредственно перед HTTP; primary не использует ACL HEVC.

В БД одновременно существует только один действующий владелец записи. Старый владелец не может записать результат после смены token или отмены. PostgreSQL-транзакция никогда не удерживается через `await` HTTP. На пачку из 20 записей lease заранее не берётся: каждый item резервируется непосредственно перед своей отправкой.

Telegram `sendMessage` не предоставляет ключ идемпотентности клиента. Падение между приёмом сообщения Telegram и коммитом локального подтверждения оставляет неопределённость. Exactly-once не обещаем. Безопасный default: неопределённый исход переводится в `failed` с `last_error_kind=delivery_unknown`, автоматического повтора нет. UI показывает отдельную пометку «Доставка неизвестна»; ручной повтор требует явного подтверждения возможного дубля.

Отзыв ACL не способен отозвать HTTP-запрос, уже принятый внешним API. Он инвалидирует локальные `pending` и `sending`, запрещает новые отправки после проверки и делает позднюю запись старого владельца неуспешной. Это ограничение явно описывается; не удерживаем ACL-lock на время сетевого запроса.

## Схема и миграция

Одна линейная Alembic-миграция после текущего head; точный номер назначает root, чтобы не создать несколько голов. В `telegram_outbox` добавить:

- `next_attempt_at DateTime nullable`: null означает готово сейчас; существующие pending остаются доставляемыми.
- `claim_token String(64) nullable`: случайный UUID на каждое резервирование, не идентификатор процесса.
- `lease_until DateTime nullable`: срок владения в naive UTC.
- `last_attempt_at DateTime nullable`: время последнего резервирования для отправки.
- `last_error_kind String(32) nullable`: `temporary`, `rate_limited`, `configuration`, `permanent`, `delivery_unknown`, `acl_rejected`.
- `updated_at DateTime not null`: backfill через `coalesce(sent_at, created_at, now() at time zone 'UTC')`, затем application default `utcnow()` и DB default UTC для прямых insert.
- `telegram_message_id BigInteger nullable`: идентификатор подтверждённого ответа API; только диагностика, не гарантия идемпотентности.

`attempts` остаётся накопительным счётчиком резервирований для попытки доставки, включая падение после claim. Не сбрасывается при ручном повторе. Конечное ограничение числа временных ошибок сейчас не вводим: сеть может быть недоступна длительно; повтор с интервалом до часа не блокирует другие записи. Permanent/unknown требуют человека.

Состояния: `pending`, **`sending`**, `sent`, `failed`, `cancelled`. Новая отдельная таблица попыток не нужна для текущего объёма задачи; сохраняется последняя причина, счётчик, сроки, результат Telegram. Уникальность dedupe и история sent/cancelled остаются прежними.

Индексы: `(bot_key, status, next_attempt_at, id)` для due-выборки; `(bot_key, status, lease_until)` для восстановления. Индексы и поля должны совпадать в ORM и миграции. Downgrade сначала переводит `sending` в `failed` с указанием неизвестного исхода, затем убирает поля/индексы. При обновлении следует остановить старые bot-процессы: старая версия не соблюдает claims.

Для общего cooldown профиля используем существующую `settings`, по одной внутренней записи `telegram_outbox_cooldown_primary` / `telegram_outbox_cooldown_hevc`. Значение — каноническая UTC ISO-дата, не секрет. Установка атомарная с максимумом старого/нового deadline, чтобы конкурентный 429 не сократил паузу. Записи не относятся к пользовательским runtime-настройкам. При claim кратко блокируется запись cooldown профиля, затем item; фиксированный порядок блокировок одинаков у claim и установки cooldown. Новые enqueue поэтому также соблюдают cooldown. По истечении ничего удалять не требуется.

## Резервирование, отправка и восстановление

1. `drain_outbox` сам проверяет `config.enabled` и наличие token, даже при вызове вне bot loop. При выключенном профиле ничего не резервирует.
2. В короткой транзакции восстановить просроченные `sending`: `failed / delivery_unknown`, очистить token/lease, записать причину и `updated_at`. Recovery не меняет dedupe/payload и не запускает HTTP. CAS включает прежний token, `status=sending` и истёкший lease. Выполнить recovery даже при выключенном профиле допустимо, новых отправок это не создаёт.
3. `claim_next_outbox(db, bot_key, now)` проверяет общий cooldown; выбирает одну due `pending`, `ORDER BY coalesce(next_attempt_at, created_at), id FOR UPDATE SKIP LOCKED`. Под row lock переводит в sending, задаёт новый token и lease, увеличивает attempts, ставит last_attempt_at/updated_at, коммитит. PostgreSQL прямо допускает SKIP LOCKED для нескольких потребителей queue-like таблиц. SQLite fallback годится для бизнес-тестов, а не доказательства конкурентной безопасности.
4. Для этой записи загрузить актуальный ACL и source link. ACL отказ → fenced `cancelled / acl_rejected`; некорректный payload → fenced `failed / permanent`. Проверить ownership непосредственно перед HTTP через условное обновление/renew token, status=sending и lease_until>now; commit. Нулевой rowcount — не отправлять.
5. Lease 120 секунд; HTTP ограничен **общим** `asyncio.timeout(25)` дополнительно к httpx timeouts. Так lease значительно длиннее штатного запроса и небольшой DB-обработки. Не нужен heartbeat на каждый HTTP; при подтверждении владение проверяется снова. Зависший/замороженный процесс всё равно способен перейти предел lease: его результат будет отклонён, recovery пометит delivery_unknown.
6. HTTP возвращает небольшой receipt (`message_id`), либо структурированную ошибку с category, API/HTTP code, retry_after, sanitized description. Не сериализовать request/URL с токеном в БД. Все диагностические строки до записи пропустить через `redact_secrets`, дополнительно удалить точное значение используемого token: regex недостаточен для нестандартных/тестовых токенов. Ограничить длину 2000.
7. Finalization только `WHERE id=:id AND status='sending' AND claim_token=:token AND lease_until>now`. Обновление строки, изменение **primary** pipeline.tg_status и PipelineEvent записываются в одной транзакции. HEVC может оставить соответствующий audit с bot_key, но не менять primary tg_status. Если CAS не прошёл, не считать sent/failed и не писать вторичный audit от прежнего владельца. Не повторять HTTP из-за ошибки локального коммита.
8. Повторять claim/send максимум `limit` раз, затем вернуть статистику. Сохраняются ключи `sent/failed/skipped`; дополнительные `deferred/recovered/lost_claim` допустимы. Никакой async-параллельной работы одной SQLAlchemy Session.

## Классификация ошибок

- Успех: корректный JSON `ok=true`, result Message с integer message_id. Подтверждение без ожидаемого результата — неопределённый ответ, а не доказанный sent.
- Явный ответ Bot API `ok=false`, error_code 429, валидный integer `parameters.retry_after` → pending/rate_limited, deadline не раньше указанного интервала; остановить текущий drain и поставить общий cooldown профиля. При отсутствующем/невалидном retry_after использовать backoff. Не спать с удерживаемой Session.
- Явный структурированный `ok=false` с 5xx → pending/temporary. HTTP 5xx с HTML, пустым/повреждённым телом или непроверенным gateway-ответом не доказывает отказ API → failed/delivery_unknown. Нельзя автоматически считать любой 502 безопасным для повтора.
- `ConnectError`, `ConnectTimeout`, `PoolTimeout` до передачи запроса → pending/temporary. Read/write timeout, read/write error, отмена/общий timeout во время отправки, обрыв после передачи → failed/delivery_unknown. При `CancelledError` итог может записать recovery, если завершающую транзакцию безопасно выполнить уже нельзя.
- Явный 401 → failed/configuration и cooldown профиля минимум 60 секунд; прекратить drain. Нужен исправленный token/base URL, а не непрерывное повторение всей очереди.
- Явные 400/403 и прочие 4xx, кроме 429 → failed/permanent для обоих профилей и ЛС/групп. Ошибки разметки, blocked/kicked/chat-not-found не исправляются повтором без изменения условий. `migrate_to_chat_id` не меняет ACL/chat/dedupe автоматически: причина показывается пользователю.
- Неожиданная локальная ошибка до HTTP → failed/permanent с безопасной диагностикой; неожиданная ошибка, когда передача могла начаться → failed/delivery_unknown.

Backoff временных ошибок: `min(3600, 30 * 2**min(attempts-1, 7))` секунд плюс небольшой детерминированный jitter. Итоговый deadline не раньше server retry_after. Поскольку failed попытки уходят в будущее, следующие due записи больше не заблокированы первыми 20. Для обычных временных ошибок не нужен bot-wide cooldown; 429 и проблемы конфигурации требуют его.

## Ручной повтор и интерфейс

Новая страница `/telegram-outbox`, фильтры bot_key/status, ограниченная выдача и стабильная пагинация. В «Информации» для каждого профиля: готовы сейчас / ждут retry / отправляются / требуют внимания; ссылка открывает соответствующий фильтр. Счётчик pending сохраняется для совместимости, но не заменяет детализацию.

Строка: дата создания, профиль, адресат, вид события и release_id, статус, attempts, last_attempt_at, next_attempt_at либо lease_until, безопасная последняя ошибка. `sent` может показывать message_id. Текст payload/token/произвольный URL не выдавать по умолчанию. Ошибки экранируются Jinja2. Для `failed + delivery_unknown` отдельная заметная надпись «Доставка неизвестна: повтор может создать дубль».

`retry_failed_outbox(db, outbox_id, *, bot_key, acknowledge_unknown=False) -> outcome`:

- Только существующая failed строка и точный профиль. sent/cancelled/sending/pending не перезапускаются; возвращается конфликт, без создания новой записи.
- Для unknown требуется `acknowledge_unknown=True`; UI показывает отдельную checkbox/confirmation рядом с кнопкой. Для definite permanent/configuration обычная кнопка «Повторить после исправления причины».
- Проверить enabled/token; для HEVC проверить текущий approved recipient. Отклонённый ACL нельзя обойти retry; отменённые ACL записи не возрождаем.
- CAS `status=failed` под короткой row-lock транзакцией; pending, next_attempt_at=now, очистить token/lease, attempts сохранить. Последнюю причину можно сохранить до следующего результата для диагностики. Повтор не отправляет HTTP сам, это работа bot loop. Два concurrent retry дают только один переход.
- Для configuration разрешается сократить bot cooldown при явном повторе после исправления настроек, под тем же lock профиля; 429 cooldown никогда не обходится ручным действием.
- primary pipeline.tg_status переводится в pending/queued и аудируется в той же транзакции, если pipeline существует и ещё не sent. HEVC его не меняет.

Маршруты root: GET page/live, POST `/telegram-outbox/{id}/retry`; REST GET `/api/telegram-outbox`, POST `/api/telegram-outbox/{id}/retry` с 404/409/422 для отсутствующего/конфликта/неподтверждённого unknown. bot_key валидируется строго primary/hevc. Сервис возвращает готовую причину отказа, чтобы HTML/REST были согласованы.

Live UI: новый channel `telegram-outbox`, token включает по обоим профилям counts/status, max(id), max(updated_at), sum(attempts), next retry/lease time tick только при pending/sending. `_info_token` получает оба heartbeat и агрегаты очереди. Partial replacement сохраняет фильтры, раскрытые причины и прокрутку. Даты выдаются через `as_utc_iso()`, в БД только naive UTC.

## Проверки и критерии готовности

Обязательные isolated тесты (HTTP через заглушки/MockTransport, без live Telegram):

1. Два PostgreSQL Session/consumer через барьер: одна строка → ровно один mock HTTP; две строки → разные claims, отсутствует ожидание чужого сетевого запроса.
2. Старый token после expiry/recovery/reclaim/ACL cancellation не меняет outbox, pipeline или audit; lost ownership до HTTP даёт ноль send.
3. Lease на один item: пачка 20 не резервируется заранее; artificial slow first send не истекает владение следующих.
4. Первые 20 временно ошибочных + 21-й корректный: после первого backoff 21-й отправляется, старые не отправляются до срока. Контрольная дата инъектируется, real sleep не нужен.
5. 429 retry_after, missing/invalid/bool retry_after, cooldown при новом enqueue и параллельных 429 (не сокращается), изоляция primary/hevc.
6. Structured 5xx повторяется; HTML 502, broken JSON 200, ReadTimeout/WriteTimeout и crash после отправки → failed/delivery_unknown без автоматического повторного HTTP; ConnectTimeout повторяется.
7. 400/403 permanent для primary/hevc/ЛС/групп; 401 останавливает drain; payload/source refresh/MarkdownV2/inline keyboard сохраняются.
8. Manual retry только failed, один CAS на concurrent clicks, unknown требует acknowledgement, revoked ACL/disabled bot не обходятся, dedupe и attempts сохранены.
9. Атомарность primary tg_status + tg_sent audit, rollback при ошибке commit, HEVC overdue не меняет primary status.
10. Старые enqueue/baseline/dedupe/ACL tests проходят; тест ожидания бесконечного retry HEVC group Forbidden меняется согласно новому контракту permanent.
11. PostgreSQL migration upgrade/downgrade/upgrade с существующими pending/sent/failed/cancelled, сохранёнными дедуп-ключами и timestamps. sqlite не заменяет этот тест.
12. Диагностика/REST не содержат token, raw request URL; escaped ошибки; live token реагирует на attempts/error при неизменном count, на HEVC heartbeat, на retry deadline.

## Владение файлами для реализации

Telegram agent: `backend/app/services/telegram_outbox.py`, `telegram_notify.py`, `telegram_access.py`, при необходимости `backend/app/telegram_bot/__main__.py`; новый `backend/tests/test_telegram_outbox_delivery.py`, изменения `test_telegram_notify.py`, `test_telegram_access.py`, `test_pipeline_events_e2e.py` только по изменившемуся контракту.

Root: общий `backend/app/db/models.py`, одна общая Alembic-миграция, `backend/app/main.py`, `backend/app/api/rest.py`, `backend/app/services/system_status.py`, `backend/app/services/ui_events.py`, `templates/base.html`, `templates/partials/info_live.html`, новая страница/partial очереди и интеграционные UI/API тесты. HEVC notifications producer не требует переписывания.

## Источники протокола

- [Telegram Bot API — Making requests](https://core.telegram.org/bots/api#making-requests): структурированный ответ `ok`, `error_code`, `description`, `parameters`.
- [Telegram Bot API — ResponseParameters](https://core.telegram.org/bots/api#responseparameters): retry_after и migrate_to_chat_id.
- [Telegram Bot API — sendMessage](https://core.telegram.org/bots/api#sendmessage): успешный результат Message; клиентский idempotency key не определён.
- [PostgreSQL 16 — SELECT, locking clause](https://www.postgresql.org/docs/16/sql-select.html#SQL-FOR-UPDATE-SHARE): FOR UPDATE SKIP LOCKED для нескольких потребителей очереди.

## Проверка реализации

Узкий локальный запуск на обычном CPython 3.15.0: **98 passed, 7 skipped**. Проверены новый state machine, имеющиеся Telegram notify/ACL и pipeline-events регрессии. Семь skipped — реальные PostgreSQL concurrency-тесты, которые root запускает отдельно на изолированной тестовой БД. Компиляция изменённых сервисов/тестов успешна. Live Telegram не использовался.

Старые mark_outbox_* helpers сохраняют имена, но требуют явно переданный claim_token: извлекать актуальный token из ORM-объекта нельзя, иначе устаревший владелец после refresh мог бы принять token новой попытки. Запись primary tg_status использует условный SQL UPDATE, чтобы конкурентный поздний failed/pending не затёр sent.
