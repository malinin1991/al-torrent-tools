# ALTT и Python 3.15t: конкретные границы рефакторинга

> Обновление решения, 13.10.2026: для основного проекта выбран прямой переход
> на обычный CPython 3.15.0. Ниже сохранены результаты и состояние завершённого
> эксперимента; прежнее решение оставить 3.14 относится к его моменту. Текущие
> изменения и проверка рабочего образа — [PYTHON_315_MIGRATION.md](PYTHON_315_MIGRATION.md).
> Переход на 3.15t и экспериментальный HEVC-пул не принят.

Дата аудита: 13 октября 2026 года. Production-пути не переключены на новый
интерпретатор или параллельный pairing. Ниже отделены проверенный код,
экспериментальный рефакторинг и предложения, для которых ещё нужны измерения.

## Что различается в трёх режимах

Сравниваются обычный CPython 3.15, сборка 3.15t с включённым GIL и та же
сборка 3.15t с реально выключенным GIL. Второй режим — контроль стоимости
free-threaded сборки; это не обычный CPython с другим именем исполняемого файла.

[Официальное руководство](https://docs.python.org/3.15/howto/free-threading-python.html)
различает поддержку free threading в сборке (`Py_GIL_DISABLED`) и действующее
состояние GIL (`sys._is_gil_enabled()`). Native extension может вернуть GIL при
импорте. Поэтому состояние проверяется после полного набора импортов, а не
только в пустом `python -c`. Принудительное `-X gil=0` не является доказательством
совместимости расширения.

Снятие GIL позволяет нескольким Python-потокам считать на разных ядрах. Оно не
делит одну функцию автоматически на части, не делает Session конкурентной и
не гарантирует меньший RSS. Накладные расходы сборки и время освобождения
памяти описаны в [PEP 703](https://peps.python.org/pep-0703/). Решение требует
сравнения времени, CPU и памяти конкретного проекта.

## Где параллелизм уже существует

**Веб-страницы.** `main.py:releases_page/releases_live` объявлены обычным `def`.
FastAPI/Starlette уже выполняют такие обработчики в thread pool. С выключенным
GIL несколько одновременных запросов могут считать Python-код параллельно
без внутреннего пула на каждый запрос. Один запрос останется последовательным.
Это отдельная проверка throughput/p95 при 1/2/4 клиентах; её нельзя заменять
микробенчмарком pairing. У Starlette есть общий лимит anyio thread tokens;
вложенный пул HEVC на каждый запрос может умножить число активных CPU-задач.
[Thread pool Starlette](https://starlette.dev/threadpool/).

**Ручные джобы.** `job_runner.py:JobRunner.schedule_job()` уже использует
`asyncio.to_thread(_thread_main)`. Внутри потока создаются собственная Session
и event loop `asyncio.run(...)`. `_active_job_ids` защищён `threading.Lock`;
долговременный run-lock держится отдельным PostgreSQL-соединением.

**Хеширование.** `file_hasher.py:hash_paths_parallel()` уже имеет ограниченный
`ThreadPoolExecutor` на 1–8 файлов, по одному независимому hasher на файл,
`Queue` для логов и запись результатов в БД на вызывающем потоке. Это хорошая
граница, которую не нужно переделывать для t. `hash_file_blake3()` читает
бинарные блоки по 4 MiB по умолчанию. В проверенном upstream
[BLAKE3 1.0.11](https://github.com/oconnor663/blake3-py/blob/master/Cargo.toml)
[native update](https://raw.githubusercontent.com/oconnor663/blake3-py/master/src/lib.rs)
освобождает GIL начиная с 2048 байт и имеет собственную синхронизацию.
Следовательно, обычный Python уже допускает параллельную native-обработку
таких блоков. Ускорение от t может касаться Python-обвязки, но диск, memory
bandwidth и БД никуда не исчезают. Одновременное включение внутреннего
`max_threads` BLAKE3 и пула по файлам требует общего лимита, иначе получится
избыточное число потоков. На Unraid реальный массив важнее cached-file теста.

**MediaInfo.** `jobs/mediainfo_sync.py` уже имеет full-режим с четырьмя workers
по умолчанию и Session на каждый worker. Счётчик старта защищён Lock, логи
возвращаются через Queue. `mediainfo.py:parse_media_file()` вызывает native
libmediainfo через pymediainfo/ctypes; `CDLL` отпускает GIL вокруг C-вызова.
После этого остаются Python XML/JSON, summary и EBML attachments. Их долю
нужно профилировать отдельно. Документация предупреждает о shared options
libmediainfo при параллельных вызовах с разными параметрами; текущий код
использует одинаковый `full=True` и не задаёт `mediainfo_options`.
[pymediainfo 7.0.1](https://pymediainfo.readthedocs.io/en/stable/pymediainfo.html),
[ctypes.CDLL](https://docs.python.org/3.15/library/ctypes.html#ctypes.CDLL).

**SLA-проверка.** `worker.py:_sync_hevc_overdue()` уже выносит весь
`sync_overdue_notifications()` в поток и создаёт Session внутри него.
Это не блокирует основной event loop; отдельный внутренний CPU-пул пока
не применяется.

**Кодирование Kensetsu.** ALTT отправляет пути и параметры по HTTP. Видео
кодирует внешний сервис; смена Python или параллельный pairing не ускоряет
его FFmpeg/энкодер. Можно измерять только подготовку и задержку отправки ALTT.

## Первый кандидат: HEVC без ORM в рабочих потоках

`hevc_pairing.py:find_unpaired_avc()` группирует строки по `release_id`.
Наборы presence/exact/cover, drafts и исторические якоря создаются локально
на каждый релиз. Это естественная граница независимых CPU-задач: **целый релиз
со всей историей**, а не отдельный торрент. Разделение одного релиза потеряет
исторический якорь SLA и может изменить решения missing/overdue.

Для проверки создан изолированный
`backend/scripts/hevc_parallel_experiment.py`; production его не импортирует:

1. `snapshot_archives(rows, file_keys)` выполняется на потоке владельца Session.
   `HevcSnapshot`, `ReleaseSnapshot`, `ArchiveSnapshot` — frozen dataclass с
   tuples/frozensets, JSON metadata хранится строкой. Исходные ORM-объекты,
   Session и изменяемые nested dict не пересекают границу.
2. `evaluate_snapshot(snapshot, workers=0, ...)` выполняет тот же новый
   алгоритм последовательно. Это контроль стоимости самого рефакторинга.
3. `workers=1/2/4` распределяет contiguous release-batches. По умолчанию
   32 релиза на задачу: 128 релизов → четыре задачи, а не 128 futures.
   Число собственных futures в полёте не превышает `workers` даже при большем
   переданном executor. `executor=None` измеряет создание/закрытие пула;
   внешний executor принадлежит вызывающему коду и позволяет измерить hot pool.
4. Внутри каждой задачи JSON восстанавливается в собственные dict и вызывается
   **неизменённый** `find_unpaired_avc()`. Общий `now` выбирается до dispatch.
   Результаты объединяются в исходном порядке релизов и строк, независимо от
   порядка завершения потоков. Записей в БД, HTTP и файлового I/O там нет.

JSON copy/thaw — намеренная стоимость безопасной границы. Её нельзя скрывать
при выводе о выгоде рефакторинга. Просто `frozen=True` поверх поля с общим
изменяемым dict недостаточно. Замена `quality_json` на `frozendict` также не
подходит без отдельного изменения контракта: действующий pairing проверяет
`isinstance(quality_json, dict)`.

62 проверки `tests/test_hevc_parallel_experiment.py` прошли на обычном
CPython 3.15.0 локально, затем вошли в полный набор **1011 passed в каждом из
трёх Docker-режимов** regular / t GIL-on / t default GIL-off с настоящим
PostgreSQL 16. Сравниваются полные `UnpairedAvc`, включая порядок, aware `now`,
исторический label-only rename, file keys, ignore, `require_active=False`,
SLA и ссылки на выбранную пару. Дополнительно проверяются изменение входных
объектов после snapshot, ограничение workers, ошибка задачи и сохранение
работоспособности внешнего пула. Успех тестов не равен доказательству ускорения.

## Более раннее улучшение страницы: не пересчитывать одинаковое

В `releases_view.py:list_release_groups()` при `hevc=overdue` сначала
`release_ids_matching_hevc_filter(hevc_archives)`, затем
`max_overdue_hours_by_release_id(hevc_archives)` вызывают **два полных**
`find_unpaired_avc()` для одного набора архива/file keys/include_ignored.
Ниже `unpaired_by_archive_id()` считает бейджи ещё раз для каждого релиза
страницы.

Приоритет перед production-пулом: один результат полного расчёта внутри
запроса → индексы по archive/release → фильтр и сортировка. Это сокращает
работу на любом Python и не требует дополнительного CPU/памяти на потоки.
Бейджи намеренно не включают ignored, тогда как фильтр может включать:
нельзя слепо переиспользовать результат с другим `include_ignored`.
История релиза и file keys должны оставаться полными; нельзя обрезать их
пагинацией перед pairing. Один фиксированный `now` нужен для согласованного
SLA в пределах запроса.

`hevc_notifications.py:sync_overdue_notifications()` также сначала считает
весь архив, затем повторно загружает и считает просроченные релизы в
`sync_hevc_pair_events_for_release()`. Здесь повторное чтение связано с
записью событий/outbox; перенос готового результата допустим только с
проверкой его актуальности перед транзакцией. Сначала оптимизируется
read-only страница, затем отдельно сценарий уведомлений.

Эти предложения не внедрены: benchmark сравнивает один и тот же объём
вычислений, чтобы не приписать устранение повторной работы снятию GIL.

## Session, клиенты и caches: где появятся гонки при наивном переносе

**Session и ORM.** `db/session.py` содержит общий Engine/Session factory;
Session выдаётся на запрос или создаётся внутри worker. Разделять между
потоками Session, Connection, ORM objects с lazy/expired attributes нельзя.
Внешняя thread-safe Connection psycopg не делает Session безопасной:
операции одной connection всё равно сериализованы и разделяют транзакцию.
Параллельные записи требуют отдельных Session/транзакций и ограниченного
числа соединений. CPU-задачам HEVC Session вообще не нужна.
[SQLAlchemy 2.1](https://docs.sqlalchemy.org/en/21/orm/session_basics.html#is-the-session-thread-safe-is-asyncsession-safe-to-share-in-concurrent-tasks),
[psycopg concurrent operations](https://www.psycopg.org/psycopg3/docs/advanced/async.html).

**Source client.** `providers/anilibria/sources.py:ReleaseSourceClient` хранит
`db`, `_releases`, `_torrents`; `remember()` не только кеширует, но и пишет
метаданные через ту же Session. Это job-scoped объект, не готовый общий
thread-safe cache. Распараллеливание `processor.process_release()` на одном
processor/client создаст одновременные обращения к Session. Если такой
эксперимент понадобится, каждому заданию нужны независимые Session/client,
а наружу возвращаются immutable результаты; `release_lock` и atomic
pipeline claims сохраняются.

**Backoffice.** `backoffice.py:BackofficeClient` изменяет `_cookies` и
`_authenticated`; цепочка login/CSRF зависит от них. Даже `asyncio.gather()`
на одном экземпляре может создать пересекающиеся login-последовательности;
GIL эту логическую гонку не исправляет. Для конкурентных запросов нужна
отдельная схема authentication single-flight внутри одного event loop
или отдельные job-owned экземпляры. Пустой результат при ошибке/неполной
пагинации по-прежнему запрещён.

**qB-клиенты.** `pipeline.py:TorrentPipelineService` содержит Session и
лениво созданный `_master_client`; `torrent_processor.py` также работает
с собственными авторизованными qB-клиентами. Проверенной гарантии безопасного
совместного использования такого service/client несколькими потоками нет.
Предпочтительны один владелец либо отдельный client на worker. Глобальный
клиент на весь API/worker создавать не следует. История master→slave и
PipelineEvent требуют прежних DB claims независимо от GIL.

**HTTP pooling.** В `client.py` и `backoffice.py` новый `httpx.AsyncClient`
открывается для каждого запроса. Job-scoped client с ограничениями соединений
может уменьшить TCP/TLS/auth overhead без t. Однако `schedule_job()` запускает
отдельные event loops, поэтому общий global AsyncClient между всеми потоками
не подходит: клиент должен открываться/закрываться владельцем своего loop.
Изменение оправдывается HTTP-замером, а не CPU-бенчмарком.
[HTTPX scoped clients](https://www.python-httpx.org/async/#opening-and-closing-clients).

**Состояние процесса.** `_active_job_ids` уже имеет Lock.
`_background_tasks` и `asyncio.Task` должны оставаться на event loop,
создавшем их; это не повод раздать их workers и добавить один глобальный
Lock. Реестр `_handlers` регистрируется при старте и затем только читается.
`qbittorrent.py:_comment_unsupported_warned` имеет check/set без Lock:
при параллельном доступе возможен повтор warning, но это не потеря архивов.
При требовании строго одного предупреждения достаточно локальной
синхронизации; глобальная сериализация всех qB-вызовов не нужна.
[asyncio thread ownership](https://docs.python.org/3.15/library/asyncio-dev.html#concurrency-and-multithreading).

**Повторный запуск job_id.** В текущем `JobRunner.run_job()` после получения
блокировки статус безусловно становится `running`; перед этим нет повторного
atomic claim для допустимого состояния. Поэтому блокировка сериализует два
запуска одного ID, но не гарантирует «handler ровно один раз»: следующий
ожидавший запуск может стартовать уже завершённый или отменённый джоб. Это
существующий дефект baseline, а не ошибка, внесённая free threading. До
увеличения параллелизма джобов нужен отдельный регрессионный сценарий на
конкурирующие процессы и проверка состояния после блокировки. Изолированный
HEVC helper вообще не запускает джобы и этот сценарий не изменяет.

**Кеш страницы.** Переиспользовать результат внутри одного immutable snapshot
безопаснее, чем добавить глобальный dict cache. Долгоживущий кеш HEVC должен
учитывать изменения архива, состава файлов, `ignore_hevc`, source policy и
время перехода SLA. Одного `release_id` или api markers недостаточно:
SLA меняется независимо от синхронизации API. Возвращать пользователю общий
изменяемый объект из кеша также нельзя.

## Приоритеты после эксперимента

Пользователь уточнил критерий: главный результат — сокращение wall time
долгой операции, CPU time и RAM имеют меньший приоритет. Рост ресурсов сам
по себе не исключает оптимизацию. Измеренный выигрыш HEVC compute около 19%
на t-off с новым snapshot и hot4 соответствует этому направлению; ускорение
всего запроса или фонового задания пока не подтверждено. Один проход по
128 тестовым релизам занимал примерно 10.7 ms → 8.8 ms: абсолютная экономия
около 1.9 ms пока не доказывает ускорение долгой операции. Ресурсы по-прежнему
наблюдаются, чтобы выбранный параллелизм не увеличивал очередь и время
соседних задач на том же сервере.

1. **Необходимый gate:** совместимые native dependencies, действующий GIL
   после imports, одинаковые backend/build/CPU quota; затем correctness,
   время/CPU/RSS и конкуренция. На текущем preflight pinned
   `psycopg-binary==3.3.6` не имеет подходящего cp315t/abi3t wheel для выбранного
   Linux arm64. Одобренный исследовательский fallback — одинаковый
   `psycopg==3.3.6` pure backend + system libpq во всех трёх режимах.
   Это **не подтверждение** существующего production binary stack.
2. **Сократить повторную работу HEVC в запросе.** Проверить число вычислений,
   p95 страницы и неизменность фильтров/сортировки/SLA. Это не t-специфично.
3. **Проверить эксперимент HEVC.** Сначала original vs новый sequential,
   затем тот же refactor с 1/2/4 потоками на regular/t-on/t-off. Если выигрыш
   только для больших каталогов, production нужен порог и общий ограниченный
   процессный pool, а не pool на каждый запрос. Сравнить также concurrent
   sync endpoints без внутреннего пула.
4. **Убрать блокирующие участки worker event loop.** `_run_by_type`, qB polling,
   retry/reclaim и `ui_events.py:sse_event_stream` выполняют синхронные
   DB/qB-операции внутри async-кода. В нужных местах выносится целый
   синхронный участок с созданием Session внутри потока; передавать текущую
   Session в `to_thread()` нельзя. Эффект — responsiveness/heartbeat и
   независимость заданий, он возможен и на обычном Python.
5. **I/O и DB batch до увеличения pools.** В hash/MediaInfo gate остаются
   последовательные запросы по файлам. Предварительная пакетная выборка
   size/mtime/hash на потоке Session и scoped HTTP reuse могут быть полезнее
   дополнительных CPU workers. После этого отдельно измеряются реальные
   файлы/массив, а не только RAM cache. Stop, path boundary, incomplete files,
   sticky `ui_status` и 25-file commit semantics не меняются без регрессий.

Для throughput недостаточно наблюдать «загружены четыре ядра»: больше CPU
может означать больше ресурсов при той же задержке. Считаются фиксированный
объём работы, wall/CPU, peak RSS, p95, idle/после-прогрева RSS, а при
конкурентных запросах — ещё DB connections и queue wait.

## Что дают именно новые возможности 3.15

`profiling.sampling` уже доступен через opt-in `scripts/profile_runtime.py`:
он нужен, чтобы отделить Python CPU от native/I/O и выбрать реальный участок
работы. Профиль снимается отдельно от migration benchmark. На macOS
проверенный attach ограничен правами чтения памяти; успех не заявляется.
[Tachyon](https://docs.python.org/3.15/library/profiling.sampling.html).
GIL-профиль применяется к GIL-on режимам; для t-off информативнее wall/CPU
и распределение работы по потокам. Пустой GIL-профиль сам по себе ничего
не доказывает о полезной скорости приложения.

`frozendict`, sentinel и lazy imports не заменяют ownership, транзакции и
лимиты ресурсов. В эксперименте достаточно frozen dataclass/tuple/JSON
snapshot, поддерживаемых обычным Python. Новый free-threaded ABI облегчает
доставку совместимых wheels, но не превращает старый бинарный wheel в
подходящий и не устраняет гонки source/auth/Session.

## Проверенное t-окружение и границы вывода

Агент совместимости проверил одинаковые 46 зависимостей, pure psycopg 3.3.6
и system libpq 170011 во всех трёх исследовательских режимах. 43 отдельных
fresh-process import probes, включая app/main/worker/Telegram/native modules,
не вызвали автоматического включения GIL. Перед/после полного pytest GIL:
regular `True→True`, t GIL-on `True→True`, t default `False→False`.
Полные 1011 тестов с PostgreSQL 16 прошли в каждом режиме.

`thread_inherit_context` и `context_aware_warnings` равны 1 у обеих t-сборок
и 0 у regular: включение GIL в t не превращает всю сборку в regular.
Одинаковые configure-поля `WITH_PYMALLOC/WITH_MIMALLOC` не считаются
доказательством одинакового фактического allocator. Provenance сохранён
агентом совместимости отдельно; production binary backend по-прежнему не
воспроизведён. Это не повод переносить эти результаты на Unraid amd64
без проверки целевой платформы.

Сравнительные времена находятся в отдельном [benchmark-отчёте](PYTHON_315T_BENCHMARKS.md).
До принятия всех gates польза внутреннего HEVC-пула остаётся проверяемой
гипотезой, а production остаётся прежним.
