# Структурированный результат синхронизаций и точечный повтор

Статус: проект проверен и реализован в отдельном потоке синхронизаций; общую схему, runner, API и UI интегрирует основной поток.

## Подтверждённая проблема

`TorrentProcessor._process_release()` перехватывает исключения отдельных торрентов, сохраняет pipeline failure, увеличивает локальный `errors` и возвращает `stats["errors"]`. Но `empty_release_stats()` не содержит `errors`, а `merge_release_stats()` складывает исключительно ключи из `empty_release_stats()`. Поэтому `ongoing`, `full_sync`, `full_meta_sync` теряют вложенные ошибки. `meta_sync` и `force_release_sync` также завершаются без исключения при таком результате. Runner считает отсутствие исключения успехом.

Дополнительно `ongoing` перехватывает ошибки релизов и недоступность расписания, пишет warning и возвращает `None`. `backoffice_sync` проверяет вложенный errors, но превращает его в общий RuntimeError, теряя точные идентификаторы проблемных торрентов. `_force_load_release_torrents()` считает ошибки download/qB как skipped; `_refresh_release_meta()` сохраняет предупреждение при ошибке чтения карточки. Эти явно перехваченные ошибки тоже должны попадать в структурированный результат.

Изолированное воспроизведение 2026-10-13: выполнены AST-извлечённые **настоящие** три метода агрегирования из текущего `torrent_processor.py`, без импорта приложения, БД или сети. Вход `{total: 2, new: 1, skipped: 1, errors: 1}` даёт `aggregate.errors=MISSING`; итоговый текст: `итого: релизов=1, добавлено=0, обновлено=0, пропущено=1, waiting_master=0`. Assert подтверждает потерю errors. Воспроизведение не вызывает реальные сервисы.

## Охват и контракт

Охват: `ongoing`, `full_sync` (включая вложенный backoffice), `full_meta_sync`, `meta_sync`, `force_release_sync`, самостоятельный `backoffice_sync`, новый ограниченный `retry_sync_failures`. Hash, MediaInfo, cleanup, pipeline-reconcile и encoder submission сохраняют свои контракты; новая очередь повторов не отправляет что-либо в Kensetsu.

Добавляется терминальный статус `partial`, отображение «Завершено с ошибками». `success` означает завершённый предусмотренный обход без ошибок обязательных операций; ожидающий qB pipeline допускает success с явным счётчиком ожидания. `partial` означает выполненную часть работы и сохранённые проблемы/неполное discovery. `failed` остаётся для необработанного исключения, недоступности БД, невозможности выполнить основную операцию. `cancelled` при stop/reclaim/shutdown всегда приоритетнее partial. Исторические jobs без структурированного результата остаются как есть — никаких выводов из текста старых логов.

Обработчики по-прежнему могут возвращать `None`. Общий сервис `sync_outcomes` фиксирует summary и issues во время выполнения и перед нормальным завершением. В конце при наличии ошибок поднимается отдельный `JobPartialFailure` с безопасной краткой строкой. Runner ловит его **до** generic Exception и устанавливает partial только при ещё принадлежащем ему running/stopping состоянии; актуальный stopping становится cancelled. Исключение является только терминальным сигналом: результат и список ошибок должны быть committed до него и переживать rollback, отмену и финализацию runner. Контракт согласован с потоком JobRunner.

Для непрерывного прогресса summary/issue сохраняются после каждого релиза и каждого перехваченного сбоя. При SQL-ошибке сначала rollback, затем запись issue в новой транзакции. Если запись результата невозможна, ошибку нельзя превращать в success; она уходит в runner как failed. JobPartialFailure не нужно использовать для ожидающего qB.

## Счётчики

`jobs.result_json` nullable JSONB, version=1. Небольшой документ содержит `scan_complete`, `discovery_complete`, отдельные секции `releases`, `torrents`, `operations`, `issue_count`, `retry_of` (если есть). В секциях объектов — `visited`, `processed`, `skipped`, `waiting`, `errors`. У каждой записи объекта один итог: error > waiting > processed > skipped. Например, релиз с одним успешным и одним ошибочным торрентом: release.errors=1, torrent.processed=1, torrent.errors=1. Число проблем (`issue_count`) может быть больше числа объектов с ошибкой, если у торрента несколько операций/ролей.

`processed` — предусмотренные операции объекта выполнены; не равно только newly-added. `skipped` — проверенный объект без требуемых изменений или вне источника/срока; ошибки не прячутся в skipped. `waiting` — подтверждённая временная недоступность qB/существующее waiting состояние, уже обслуживаемое pipeline retry. Старые счётчики added/updated/comments/tags/renames сохраняются как `operations` и не суммируются в число торрентов. `visited` и максимум известных кандидатов не выдают неизвестный объём каталога за полный total.

Ошибки discovery (расписание, страница каталога, индекс Nova, разрешение extra alias) имеют собственный issue и `discovery_complete=false`. Для ошибок без release_id отдельный счётчик release.errors не выдумывается. Неудачное discovery не считается пустым валидным результатом. Если выполнен fallback/watch, результат partial, а список включает недоступный discovery. Кнопка точечного повтора для такого issue отключена с понятной причиной «Требуется новый обход»; нельзя сделать вид, что повтор известных release IDs восстановил пропущенную часть каталога.

## Хранение проблем и миграция

Новая таблица `job_sync_failures` (полный список, без потери после UI-лимита):

- `id`, `job_id` FK jobs ON DELETE CASCADE, `created_at` naive UTC;
- `scope` release/torrent/discovery, `release_id` nullable, `torrent_id` nullable, `info_hash` nullable (нормализованная точная версия, если известна), `source` site/backoffice/unknown;
- `operation` (process/meta/force_load/source_policy/catalog/schedule/index/extra_ref), `qb_role` nullable master/slave;
- `code` из контролируемого перечня, `message` безопасная краткая строка, `retryable` bool;
- `retry_params` JSONB: только серверный allowlist необходимых флагов исходной операции, без URL с passkey, паролей, raw payload, трекеров или traceback;
- `object_key` детерминированный не-null ключ scope+id/hash+operation+role, unique(job_id, object_key), `attempts` для нескольких сбоев того же объекта в одном job.

Индексы `(job_id,id)` для keyset-пагинации и `(job_id,retryable,id)` для подготовки повтора. Nullable ID объектов намеренно не FK на текущую карточку/архив: ошибка старой версии должна оставаться историческим фактом при смене источника или архива. JSON summary хранится отдельно от полного списка. Миграцию добавляет root после согласования всех проектов; существующие строки jobs получают NULL. PostgreSQL JSONB сохраняется; SQLite-тесты используют уже принятый тестовый compile strategy JSONB. Миграция проверяется на отдельной PostgreSQL.

Ограничение message 500 символов после `redact_secrets()`, контролируемые code/operation; неизвестное исключение обычно отображается как тип + безопасное объяснение. Структурированные поля и повтор никогда не зависят от разбора строки сообщения. Не сохранять `.env`, raw HTTP bodies, full URL, auth/cookie/token/passkey. В БД сообщения редактируются до вставки, а не только logging filter. В UI действует Jinja escaping.

## Точный и безопасный повтор

Новый **отдельный** job `retry_sync_failures`, исходный job/status/params/result остаются историческими. Запуск только явным действием пользователя. `retry_of` указывает исходный job. API принимает `failure_ids` до 100 за запрос, либо режим «все доступные» с явным серверным пределом одной задачи и последовательными порциями. Остальные проблемы не обрезаются: UI показывает остаток и позволяет следующий job. Дубликаты IDs/версий внутри запроса объединяются.

При создании сервер читает issues исходного терминального job, проверяет их принадлежность/allowlist/retryable и сохраняет ограниченный snapshot идентичности+операции в params **нового** job. Клиент не передаёт произвольные release IDs, hash или флаги force. Snapshot позволяет уже принятому retry пережить retention родительского job; отсутствие исходного job после очистки не должно превращать принятый retry в потерянную работу. При активном исходном job — HTTP 409, при неизвестных issue IDs — 400/404; второй одновременный retry сериализует существующая JobRunner-дедупликация нового типа. Отдельная атомарная дедупликация по retry_of может быть добавлена, если будет разрешено несколько retry jobs разных родителей.

Для torrent scope processor получает точный набор `(torrent_id, expected_info_hash, operation, role)` и работает под прежним release/source lock. Он читает **полный валидированный список текущего выбранного источника**, проверяет ID+hash и только потом применяет операцию к выбранным торрентовым версиям. Не ограничивать ответ API одной карточкой, не использовать существование soft-deleted карточки как доказательство присутствия. Hash изменился/ID исчез — безопасный skipped с reason `stale_target`, без загрузки новой версии вместо согласованной. Если hash торрента исходно неизвестен, точечный повтор недоступен: пользователю явно предлагается синхронизация релиза. ID-only wildcard для torrent scope запрещён и при создании, и в processor. Ошибка до определения состава релиза может иметь отдельный явно обозначенный release scope.

Если ошибка была на уровне release до определения torrent scope, повторяет этот **один релиз**, с явной подписью в UI «Весь релиз»; это не повтор каталога. Все source policies, trackers, pipeline atomic claims, seen-history и sticky ui_status применяются обычным путём. Force-load сохраняет явно запрошенный флаг, но ограничивает torrent/role из issue; обычный retry не приобретает force-флаг. Backoffice baseline suppression сохраняется из контекста оригинальной ошибки и текущих notification_baseline архивов; повтор начального импорта не должен послать HEVC/TG уведомление как о новом релизе.

Retry не делает глобальный public api_present sweep, не запускает full_sync/backoffice-discovery/backfill, не удаляет checkpoint/seen/pipeline для всех релизов. Полный API-present контекст релиза может быть обновлён лишь после валидированного полного списка источника. При subset retry нельзя ставить checkpoint как «весь релиз обработан»: остаётся invalidated до обычного полного прохода; ошибочный checkpoint не используется для пропуска повторяемого объекта. All-seen/meta branches тоже ограничивают операции выбранными торрентовыми версиями. Исторический failure остаётся, новый job показывает результат и ссылку на родителя.

Qb waiting не создаёт retryable failure. Повтор не обходит waiting/retry worker. Никакого вызова `/encoder`, submission manager или автоматического повтора Kensetsu unknown в этом сценарии.

## Классификация скрытых ошибок

1. Исключения в torrent processing → точный torrent issue; release summary выводится из torrent outcomes, nested errors не теряются при merge.
2. Ошибка целого релиза → release issue; обход продолжается для независимых релизов, финал partial. `JobStopRequested` всегда пробрасывается без преобразования.
3. Force-load download/qB operation exception → torrent+role issue, вместо скрытого skipped. Недоступный qB до операции — waiting, а не failure. False comment/tag write при подтверждённом наличии объекта — issue соответствующей metadata операции; отсутствующий на qB объект — skipped, а отсутствие ответа/неопределённость — waiting/предупреждение, без выдуманного успеха.
4. Ошибка получения release metadata с fallback из архива не является полным успехом запрошенного refresh: release/meta issue, существующие метаданные не стираются. Сохранить нынешние проверки неполных жанров/состава.
5. Ошибка полного каталога/Nova → incomplete discovery; опасный глобальный api_present sweep пропускается. Успешные другие релизы остаются видимыми в summary.
6. `full_sync` с вложенным backoffice использует один collector/result и один итог в конце. Вложенный handler не поднимает partial преждевременно и не затирает уже собранную public часть. Baseline completed устанавливается только при полном error-free discovery/processing, как требует текущий смысл.

## UI, REST, live updates

Root добавляет общий UI/API/schema контракт в своих файлах, чтобы не конфликтовать с другими потоками:

- Jobs/dashboard/catalog: отдельный янтарный partial, локализованная подпись, фильтр. Числа processed/skipped/waiting/errors видны в детали над логами; историческим jobs — «Структурированный итог недоступен».
- Деталь: issues по 50, максимум 100 за страницу, keyset after_id, точные release/torrent/hash (короткий hash с полным title), операция/роль/безопасная причина, ссылка на релиз/pipeline при существовании. Для failed/partial/cancelled видны уже сохранённые проблемы; retry возможен только для терминального родителя.
- Чекбоксы retryable issues, «Повторить выбранные», «Повторить доступные» и явное число. Для discovery/stale/non-retryable строк объяснение и обычный переход к нужному job type. Новый job открывается отдельно; ссылка на исходный.
- `GET /api/jobs` сохраняет текущие поля и добавляет result summary; `GET /api/jobs/{id}` добавляет result (не весь безлимитный список). `GET /api/jobs/{id}/failures?after_id&limit` возвращает items/next_after_id/has_more/total. `POST /api/jobs/{id}/retry-failures` создаёт новый job, отдаёт 202/id/retry_of/count. Общий POST /api/jobs не должен принимать непроверенные snapshots — validator вынести в общий creation path.
- jobs SSE token включает изменение persisted result и max failure id/updated signal; final статус сам по себе недостаточен для progress. HTMX сохраняет выбранный job, раскрытия, выбранные IDs, текущую страницу проблем и прокрутку. No new polling loop.
- Worker вручную запускаемые задачи не становится общей persistent queue в рамках этого изменения. Регистрируется новый handler в едином runner; текущий schedule_job механизм сохраняется. Cleanup удаляет issues каскадом вместе с job; reset-state учитывает новую таблицу.

## Обязательные проверки реализации

- Regression: вложенный errors проходит empty/merge/summary; added/updated не раздуваются metadata-счётчиками; ошибок нет внутри skipped.
- Каждый из шести release-sync handlers: все успешны → success; один независимый release/torrent сломан → partial, остальные продолжают; waiting → success+waiting; unhandled fatal → failed; stop → cancelled с уже сохранёнными outcomes.
- Ongoing: сломанное расписание не запускает expiry, partial сохраняет watch fallback; extra alias ошибка видна; confirmed 404 сохраняет архив/pipeline. Existing unchanged-markers/late-HEVC regressions обязательны.
- Full + вложенный backoffice: один корректный итог, nested error не теряется; incomplete discovery не запускает destructive sweep; initial baseline suppression и completed marker сохраняют смысл.
- Scoped retry: только выбранные ID+hash и role; неизбранные objects/source metadata mutation не происходят; changed hash, удалённый source, unknown ID → stale skip; partial subset не маркирует весь checkpoint processed; глобальные sweep/backfill не вызываются.
- Retry creation rejects active parent, foreign IDs, arbitrary client snapshots, limit overflow; double request дедуплицируется. Исходный job неизменен; cleanup родителя после принятия не ломает snapshot job.
- New schema constraints/indexes/cascade проверены на PostgreSQL; summary+issue persistence переживает rollback/runner final commit/cancel. Real concurrent handler claims остаются предметом отдельного JobRunner проекта.
- UI/API escaping/redaction/passkey/token/URL не попадают в persisted issues или retry params. Пагинация >100 ошибок не теряет строки и допускает последовательный повтор всех доступных.
- HTMX/SSE terminal partial, summary progress, selection/scroll и disabled retry объяснения проверены. Encoder unknown workflow не затронут.

## Разделение владения реализацией

Поток sync outcomes: новый сервис/handler, `torrent_processor.py`, шесть release-sync jobs, соответствующие focused tests. Root: объединённая Alembic-миграция, models, main/rest, шаблоны/CSS, ui_events, catalog/cleanup/reset integration. Поток JobRunner: atomic start/finalization; partial catch встраивается после согласования владельца, а не параллельным переписыванием того же файла.


## Реализованные уточнения после независимой проверки

- `CatalogScan` подтверждает полный документированный каталог data+meta.pagination: total/pages обязательны для подтверждения, count/current_page/per_page при наличии сверяются, total/pages неизменны между страницами, IDs уникальны, промежуточные страницы непусты, итоговое число уникальных релизов равно total. Legacy raw list обрабатывается, но даёт partial и не разрешает sweep/baseline. Backoffice при неполном публичном каталоге не начинает Nova discovery.
- Metadata операции используют один набор подключений на весь refresh. Нет повторного connect только ради счётчика. Недоступный настроенный slave даёт waiting и при доступном master. False старых helper-функций проверяется по конечным metadata: no-op не является ошибкой; тихий отказ rename выявляется.
- У повторных визитов одного релиза и одной торрентовой версии приоритет errors > waiting > processed > skipped. Успешная группа не превращается в skipped из-за следующей stale цели.
- Retry группируется также по source и notification_baseline; baseline применяется только к совпавшим целям. Stale baseline target не подавляет уведомления текущей версии другого источника. После получения файла outcomes используют вычисленный final_hash.
- `jobs.result_updated_at` меняется вместе с summary для SSE. Данные фиксируются без проверки вида БД или молчаливого отключения persistence: отсутствие parent/schema — ошибка.
- `tests/test_sync_outcomes.py` содержит одинаковые сценарии SQLite и отдельной PostgreSQL schema (ALTT_TEST_POSTGRES_URL). Тесты вызывают настоящий retry handler, проверяют полный source-list, exact ID/hash/source/role, неизменность checkpoint/presence/unselected archives, snapshot после удаления родителя, реальное архивирование и baseline HEVC suppression, partial/cancel persistence, пагинацию >100 ошибок, completeness и metadata waiting/no-op. Реальные внешние сервисы не используются.
