# Атомарное выполнение JobRunner

Дата проекта: 2026-10-13. Область: пункт 1 согласованной дорожной карты. На этапе проектирования код приложения не изменён.

## Проверенное текущее поведение

- `run_job` читает Job до advisory lock, ждёт блокирующий `pg_advisory_lock`, затем без проверки пишет `running`. Воспроизводимый изолированный вызов с MagicMock Session показал, что для каждого из `success`, `failed`, `cancelled`, `stopping`, `running` handler вызывается и запись становится `success`, теряя прежнюю ошибку.
- Повторный запуск того же job_id ждёт первого владельца lock, затем исполняет завершённую задачу повторно.
- `reclaim_orphan_jobs` отменяет старые `pending` без run-lock. Между выборкой кандидата и отменой runner может начать handler.
- Для `running` reclaim берёт session lock на ORM-соединении, снимает его до общего commit, а затем сохраняет загруженный ранее ORM-объект. Это оставляет окно между проверкой владения, записью и commit.
- `request_stop`, явная отмена и финализация пишут через ORM после проверки прочитанного состояния. Конкурентная финализация/отмена может сделать проверенное состояние устаревшим.
- Живой run-lock правильно отделён от ORM Session: reconnect рабочей сессии не должен освобождать владение задачей. Этот принцип сохраняется.

Проверки до реализации: из `backend/` на доступном локальном Python 3.13.2 выполнены `tests/test_job_cancel.py`, `tests/test_job_schedule.py`, `tests/test_job_create_api.py`: **19 passed**. Это изолированные mock-тесты, не проверка PostgreSQL advisory locks. Никаких production-соединений и внешних сервисов для воспроизведения не использовано.

## Контракт состояний

Единственный вход в выполнение: `pending → running`. Все прочие состояния, включая будущий `partial`, не допускают вызова handler. Повторный вызов `run_job` возвращает актуальный Job без изменения состояния, времён, ошибки или журналов старта/успеха. Отсутствующий Job и неизвестный тип сохраняют существующие ошибки контракта.

Остановка: атомарно `running → stopping`; затем runner завершает `stopping → cancelled`. `cancelled` и другие терминальные состояния никогда не возрождаются и не перезаписываются финализацией. Существующий cancelled reason сохраняется. Отмена имеет приоритет над результатом handler, включая частичный результат пункта 2.

Новая таблица/миграция для пункта 1 не нужна. Параметры запуска, create-time дедупликация по типу/ключу, ручной запуск в потоке API, существующие checkpoints и pipeline-аудит сохраняются.

## Предлагаемая реализация

### 1. Владение и claim

`_hold_run_lock(job_id)` использует отдельное Connection и неблокирующий `pg_try_advisory_lock` того же namespace. Возвращает Connection при успехе; при занятом lock закрывает соединение и возвращает None. Commit после lock убирает idle-in-transaction; session lock сохраняется. У второго запуска не возникает ожидания завершения handler и расхода фонового потока на повторную работу.

Порядок runner: lock → условный `UPDATE jobs SET status='running', started_at=:now, error=NULL, finished_at=NULL WHERE id=:id AND status='pending' RETURNING id` → лог старта → commit → handler. Handler вызывается только при полученном id. `synchronize_session=False` допускается только с явным refresh/expire загруженного Job после операции; свежие параметры и тип не берутся из прежней identity map. Регистрация в `_active_job_ids` выполняется только владельцем успешного claim. Локальный флаг владения запрещает второму no-op вызову удалить job_id первого из этого set.

При ошибке claim транзакция откатывается, lock освобождается, handler не вызывается. Если процесс погиб после commit claim, существующий reclaim находит освободившийся session lock.

### 2. Reclaim

Для каждого кандидата, включая pending, берётся тот же отдельный неблокирующий run-lock. Если он занят, задача пропускается. После получения lock выполняется новый запрос состояния с обновлением identity map, затем заново проверяется критерий: pending grace либо текущий running/stopping; для stale fallback — актуальная последняя активность. Терминальная запись пропускается.

Отмена выполняется условным UPDATE только активного состояния с RETURNING. Лог добавляется только для реально изменённой записи. **Commit отмены и лога происходит до освобождения dedicated lock**, отдельно для каждого изменённого job_id. Это не удерживает raw-соединение через commit ORM Session и не возвращает advisory lock в пул. Любая ошибка откатывает ORM-транзакцию и освобождает lock в finally.

### 3. Stop, shutdown и финализация

`request_stop` использует `UPDATE ... WHERE id=:id AND status='running' RETURNING id`, добавляет warning только при успехе, commit и refresh. При проигранной гонке возвращает существующий JobStopError с актуальным статусом; терминальный Job не становится stopping. `request_stop_latest_running` выбирает кандидата как раньше, а последнее решение принимает этот условный UPDATE.

`cancel_jobs_by_ids` и явный `cancel_stale_jobs` используют условное изменение активных состояний, не запись устаревших Job. Эти явные отмены могут отменять живой handler; кооперативные проверки в нём сохраняются. Причина и finished_at устанавливаются только реально отменённым записям; уже завершённые задачи не меняются.

Финализация runner выполняется одной условной операцией только для текущих `running`/`stopping`. SQL CASE для текущего `stopping` выбирает cancelled независимо от вычисленного ранее success/failed/partial. Уже cancelled или иной терминальный результат сохраняется. Лог результата пишется в той же транзакции, что и успешная смена состояния, поэтому проигранная отмене финализация не оставляет ложный лог успеха. После ошибки handler необходимо rollback неработоспособной транзакции перед записью failed. Времена — `utcnow()`, finished_at существующей отмены не затирается.

### 4. Интеграция с пунктом 2

С агентом sync согласован проект `JobPartialFailure(summary)`, терминальный `partial` и отдельный сервис сохранения результатов. В пункте 1 тип handler пока `Awaitable[None]`, контракт результатов не меняется. Агент пункта 2 добавит обработку JobPartialFailure перед generic Exception в выделенном блоке выбора `final_status/final_error`; условная финализация принимает любой согласованный терминальный статус и сохраняет приоритет остановки/отмены. Запуск допускается исключительно из pending, поэтому список терминальных статусов не надо дублировать для claim.

## Файлы реализации

- `backend/app/services/job_runner.py`: lock lifecycle, атомарные claim/stop/cancel/finalize, fresh checks при reclaim.
- `backend/tests/test_job_cancel.py`: адаптация существующих mock-тестов к conditional SQL и проверке порядка commit/unlock.
- `backend/tests/test_job_schedule.py`: регрессия двукратного schedule без второго handler и без удаления владельца из active set.
- Новый `backend/tests/test_job_execution_atomic.py`: SQLite-тесты реального SQL для Job/JobLog, JSONB compile shim только в тесте, гонки stale identity map и терминальных переходов.
- Новый `backend/tests/test_job_execution_postgres.py`: opt-in через `ALTT_TEST_POSTGRES_URL`, уникальная временная схема, ограниченные statement/lock timeout, 2+ независимых Session/Connection, без рабочих таблиц.

`main.py`/`worker.py` менять не требуется: их вызовы reclaim/shutdown/run_job сохраняют интерфейсы. Изменения контракта partial/миграция принадлежат пункту 2 и согласуются отдельно.

## Критерии приёмки

1. Для любого статуса кроме pending handler не вызывается; поля записи и существующие логи сохранены.
2. Два конкурентных запуска одного pending вызывают handler ровно один раз. Повторный запуск занятого job_id быстро возвращает актуальное состояние и не снимает владение первого.
3. Отмена, committed между предварительным чтением и claim, побеждает запуск. Проверяется на настоящих разных ORM Session со старой identity map.
4. Старый pending, ставший running до reclaim, защищён живым lock; pending-reclaim, выигравший lock первым, отменяет запись до того, как runner может пройти claim.
5. Reclaim, прочитавший старый running до завершения, после получения lock не отменяет success/failed/partial; commit отмены всегда предшествует unlock.
6. Stop, пришедший одновременно с финализацией, либо переводит running в stopping и получает cancelled, либо честно отклоняется после terminal. Терминальная задача не становится stopping.
7. Shutdown/cancelled причина не затирается success, exception или asyncio cancellation позднего handler. Только реальные переходы оставляют логи.
8. Ошибка claim не вызывает handler; ошибка handler с failed SQL-транзакцией всё ещё корректно финализируется; lock и active set очищаются на всех выходах.
9. Отдельное raw-владение переживает commit/reconnect ORM; потерянное raw-соединение позволяет reclaim отменить сироту. Reclaim не трогает задачи другого живого контейнера.
10. Уникальность create_job по типу и параметру не меняется. Изолированные unit/SQLite проверки проходят; реальные PostgreSQL гонки проходят в отдельной dev-БД. Проверка CPython 3.15 выполняется родителем в общем завершающем прогоне.

Ограничение: выделенный session lock доказывает живость соединения, а не exactly-once внешних побочных действий после потери соединения. Кооперативная остановка и идемпотентность конкретных handler остаются необходимыми; миграция ручной работы в постоянную очередь worker находится вне этой задачи.

## Реализация и проверка

Реализация согласована родителем и выполнена в `job_runner.py`. Для устранения узкого окна graceful shutdown владелец успешного SQL claim попадает в process-local active set перед commit публикации running; ошибка commit очищает set и откатывает claim. Неуспешный повторный запуск не меняет set владельца. Ошибки commit/unlock выделенного соединения вызывают invalidate, чтобы session advisory lock не вернулся в пул.

Старые cancellation-регрессии переведены с MagicMock Session на реальный изолированный SQLite SQL, сохранив исходные сценарии. Добавлены условные переходы, stale identity map, приоритет отмены, persisted partial summary, broken flush transaction, порядок commit/unlock и ошибочный commit старта. Локальный целевой прогон на Python 3.13.2: **92 passed** (`test_job_cancel`, `test_job_schedule`, `test_job_create_api`, `test_job_execution_atomic`, `test_force_release_sync`, `test_job_catalog`).

Созданы 15 opt-in PostgreSQL race cases в `test_job_execution_postgres.py`: разные Connection/Session, приватная схема и различные advisory job_id, ограниченные timeout. В stop/finalize гонке факт блокирования второго UPDATE проверяется через `pg_blocking_pids`, а не по задержке. Переданы родителю для запуска на обычном CPython 3.15 и отдельной тестовой PostgreSQL; локально без `ALTT_TEST_POSTGRES_URL` они честно пропускаются. Production-соединения не использовались.
