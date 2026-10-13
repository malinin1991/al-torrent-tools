# Проверка совместимости Python 3.15

> Обновление решения, 13.10.2026: для основного проекта выбран прямой переход
> на обычный CPython 3.15.0. Ниже сохранены результаты и состояние завершённого
> эксперимента; прежнее решение оставить 3.14 относится к его моменту. Текущие
> изменения и проверка рабочего образа — [PYTHON_315_MIGRATION.md](PYTHON_315_MIGRATION.md).
> Переход на 3.15t и экспериментальный HEVC-пул не принят.

Дата проверки: 13 октября 2026. Это отчёт об изолированной проверке,
а не подтверждение миграции production.

## Проверенные версии и происхождение

- [Python 3.15.0](https://www.python.org/downloads/release/python-3150/)
  — стабильный релиз от 9 октября 2026, не release candidate.
- [Python 3.14.8](https://www.python.org/downloads/release/python-3148/)
  — последний доступный релиз ветки 3.14 на дату проверки, от 30 сентября 2026.
- Dockerfile проекта использует `python:3.14-slim`, поэтому основной кандидат
  сравнивался с 3.14.8. Фактическая patch-версия работающего контейнера Unraid
  не запрашивалась. Локально до проверки был доступен Python 3.13.2.
- Оба новых интерпретатора собраны из официальных source archives python.org
  только в `/private/tmp/altt-python-runtime-eval`. Системный Python не менялся.
- SHA-256 `Python-3.14.8.tar.xz`:
  `c2215904f02b175596dc49351585104f4bc20341e1c47378b26a2c274360ce73`.
- SHA-256 `Python-3.15.0.tar.xz`:
  `ba4bed1ba346b916890b76d9e320451420aa69f6408997d33c66482eeae3d575`.
  Оба значения совпали с опубликованными python.org.

Среда сборки: macOS 27.0.1, arm64, Apple Clang 21.0.0
(`clang-2100.3.34.2`), OpenSSL 3.6.3. Обе сборки используют PGO
(`--enable-optimizations`), обычный GIL, без JIT, без LTO и без явно
включённого tail-call interpreter. Это не сборки для Linux/Unraid.

## Сборка и зависимости

Общие configure-аргументы, кроме версии в prefix:

```sh
./configure \
  --prefix=/private/tmp/altt-python-runtime-eval/python-VERSION \
  --enable-optimizations \
  --with-openssl=/opt/homebrew/opt/openssl@3 \
  --with-ensurepip=install
make -j4
make install
```

Для 3.15 добавлен `--without-system-libmpdec`. Причина: в 3.15
[убран автоматический fallback к bundled libmpdec](https://docs.python.org/3.15/whatsnew/3.15.html#build-changes).
Первоначальная сборка успешно завершалась, но не содержала `_decimal`
и использовала медленный pure-Python fallback. Такая сборка не использовалась
для итогового сравнения. После пересборки обе версии имеют native `_decimal`
с одинаковой libmpdec 2.5.1. Для production предпочтительно отдельно проверить
libmpdec в целевом Docker-образе; bundled library планируется убрать в 3.16.

Из `backend/requirements.txt` сначала установлены зависимости в `venv314`,
затем его `pip freeze` использован как constraints для `venv315`. Итоговые
списки совпадают полностью, включая pip 26.2.1. `pip check` успешен в обеих
средах. Lock-файлы проекта и production requirements не изменялись.

Основные версии: FastAPI 0.143.0, Starlette 1.7.0, SQLAlchemy 2.1.4,
Alembic 1.20.0, psycopg/psycopg-binary 3.3.6, Pydantic 2.14.1,
pydantic-core 2.50.1, pydantic-settings 2.15.0, uvicorn 0.54.0,
uvloop 0.23.0, httptools 0.9.0, httpx 0.28.1, APScheduler 3.11.3,
qbittorrent-api 2026.10.0, python-telegram-bot 22.8, BLAKE3 1.0.11,
pymediainfo 7.0.1, PyYAML 6.0.3, pytest 9.1.1.

На arm64 macOS все зависимости 3.14 установились из wheels. Для 3.15
PyYAML 6.0.3 потребовал source build. Первая установка PyYAML прошла
без ошибки, но потеряла C-ускоритель LibYAML. Для корректного сравнения
в `/private/tmp` собрана [LibYAML 0.2.5](https://pyyaml.org/wiki/LibYAML)
из официального архива, затем PyYAML пересобран:

```sh
CFLAGS='-I/private/tmp/altt-python-runtime-eval/libyaml/include' \
LDFLAGS='-L/private/tmp/altt-python-runtime-eval/libyaml/lib' \
PYYAML_FORCE_LIBYAML=1 \
  /private/tmp/altt-python-runtime-eval/venv315/bin/python -m pip install \
  --no-cache-dir --no-deps --force-reinstall --no-binary=pyyaml PyYAML==6.0.3
```

В итоговых средах `yaml.__with_libyaml__` равно `True`, LibYAML — 0.2.5.
Этот дополнительный build prerequisite следует учитывать, если для целевой
платформы также нет wheel. Остальные зависимости 3.15 установились из wheels.

## Проверки

До новых изменений в проекте локальный Python 3.13.2:
`905 passed, 1 skipped`. Снимок `backend/` без `.env`, медиа, torrents
и кешей сохранён в `/private/tmp/altt-python-runtime-eval/baseline-backend`.
На этом одинаковом снимке:

- Python 3.14.8: `905 passed, 1 skipped`.
- Python 3.15.0: `905 passed, 1 skipped`.
- `python -m compileall -q app tests worker.py`: успешно на обеих версиях.
- `python -m pip check`: успешно на обеих версиях.
- Native smoke: BLAKE3 known vector, psycopg/libpq import, uvloop coroutine,
  httptools/pydantic-core import и SQLAlchemy SQLite `SELECT 1`: успешно.
- `_decimal`, `_ssl`, `_lzma`, `_bz2`, `_sqlite3`, `_zstd`, `_ctypes` доступны
  в обеих сборках. Не собраны одинаково необязательные `_gdbm` и `_tkinter`.
- `sys._is_gil_enabled()` — `True`, `sys._jit.is_available()` и
  `sys._jit.is_enabled()` — `False` в обеих сборках.
- `sys.flags.utf8_mode`: 0 на 3.14, 1 на 3.15; это штатное изменение default.

Команда для повторного запуска на снимке:

```sh
cd /private/tmp/altt-python-runtime-eval/baseline-backend
env -u ALTT_TEST_POSTGRES_URL \
  /private/tmp/altt-python-runtime-eval/venv315/bin/python -m pytest -q
```

Единственный пропуск — opt-in PostgreSQL migration test без отдельной
`ALTT_TEST_POSTGRES_URL`. Время этих запусков не используется как benchmark:
параллельно могли выполняться сборки, прогрев и служебные операции.

## Ограничения и решение о миграции

MacOS wheel pymediainfo 7.0.1 в обеих средах загружает bundled
`libmediainfo.0.dylib` версии 24.12. Это ниже production-инварианта MediaInfo
>= 26. Поэтому smoke подтверждает загрузку библиотеки, но не подтверждает
целевую media-обработку с MediaArea 26+. Dockerfile удаляет bundled library
и проверяет системную версию; эта защита сохраняется.

Реальные AniLiberty/Nova, qBittorrent, Telegram и Kensetsu не проверялись.
В macOS-прогонах также не использовался реальный PostgreSQL. Native wheels и runtime на Linux x86_64
не покрываются успешной установкой на macOS arm64.

### Дополнительная проверка production Dockerfile на native Linux arm64

Собраны два отдельных тестовых образа из копии baseline-исходников без
`.env` и данных. Временные Dockerfile отличаются `FROM`, а также явно
используют общий constraints-файл для воспроизводимости зависимостей.
Production Dockerfile, Compose и запущенный стек не менялись.

- Текущий `FROM python:3.14-slim` фактически предоставил **Python 3.14.7**,
  GCC 14.2.0; поэтому Docker-проверка сравнивает 3.14.7 с 3.15.0,
  а не 3.14.8 с 3.15.0, как macOS source-проверка.
- `FROM python:3.15-slim` предоставил стабильный **Python 3.15.0**,
  GCC 14.2.0. Оба образа: native aarch64, Linux 6.12.76-linuxkit,
  Debian/glibc 2.41, GIL включён, JIT отсутствует; configure сообщает
  PGO, shared Python и LTO.
- Оба ALTT-образа сохранили исходные проверки MediaInfo >= 26 и удаление
  bundled library. CLI и pymediainfo действительно используют системную
  MediaInfo **26.10**. Ограничение macOS wheel 24.12 здесь устранено.
- На обоих образах выполнено **906 passed, 0 skipped**: включён
  PostgreSQL-тест 0018 на собственном одноразовом PostgreSQL 16.
- На отдельных пустых disposable БД обе версии выполнили `alembic upgrade
  head` до `0024_backoffice_source` через штатный `scripts/entrypoint.sh`
  и `scripts/migrate_with_lock.py`, с получением/освобождением advisory lock.
  Новая миграция истории отправок этим baseline-прогоном ещё не проверялась.

При этом обнаружен **блокер для простой замены FROM**: фактический
официальный 3.15-slim образ не содержит `_decimal`. PyYAML 6.0.3 устанавливается
из source без LibYAML C-ускорителя. На 3.14-slim `_decimal` с libmpdec 2.5.1
и LibYAML доступны. Успешные unit-тесты не делают эти среды равноценными
по производительности. Это согласуется с опубликованным
[Dockerfile официального 3.15-slim](https://raw.githubusercontent.com/docker-library/python/master/3.15/slim-trixie/Dockerfile):
в нём нет libmpdec-dev и явного включения bundled libmpdec. Production-переход
не подтверждён; исправление native build проверено только во временном
кандидате.

### Исправленный временный кандидат и финальная функциональность

На базе опубликованного официального Dockerfile отдельно собран Python 3.15.0
с явным `--without-system-libmpdec`, сохранив исходные PGO+LTO/shared flags.
Использован тот же проверенный официальный source archive; число build threads
ограничено четырьмя. В ALTT stage PyYAML собран с LibYAML из Debian;
`_decimal` и `yaml.__with_libyaml__` проверяются явно, после pip compiler
удаляется. Проверки системной MediaInfo >= 26 сохранены.

Временный кандидат `altt-python-eval:315-native-20261013` и baseline
содержат native `_decimal`/libmpdec **2.5.1**, LibYAML **0.2.5**,
системную MediaInfo **26.10**. Создан отдельный disposable dual-runtime образ
`altt-python-eval:dual-native-20261013`: Python 3.14.7 помещён в
`/opt/python314`, Python 3.15.0 — в `/usr/local`. Проверены sys.prefix,
native libraries и совпадение всех **47 installed distributions**, включая pip.
Он позволяет измерять процессы в одном Linux-окружении без запуска нового
Docker-контейнера на каждую пробу.

Финальный снимок функциональности истории отправок, без `.env` и данных:
`/private/tmp/altt-python-runtime-eval/final-backend`. На нём:

- macOS Python 3.14.8 и 3.15.0: **946 passed, 3 skipped** на каждой версии.
- Native Linux arm64 Docker Python 3.14.7 и исправленный 3.15.0:
  **949 passed, 0 skipped** на каждой версии.
- В Docker включены обе проверки claim-конкуренции на независимых
  PostgreSQL 16 соединениях: одинаковый request_id получает одного владельца
  и общую квитанцию; разные request_id для одного исходника не запускают
  двойную отправку. Тесты создают собственные уникальные схемы и удаляют их.
- `compileall` для app/tests/worker/scripts успешен на обеих host-версиях.
- На двух disposable PostgreSQL БД проверен upgrade
  `0024_backoffice_source -> 0025_encoder_history` через штатный entrypoint
  и advisory lock. До upgrade сохранены искусственные release, archive,
  file со sticky `changed`, pipeline, audit event и setting. После upgrade
  JSON-снимки всех шести типов сущностей совпали точно; новые таблицы пусты.

После последнего исправления маскирования словарных `api_key`/`X-Api-Key`
полные прогоны повторены на отдельном снимке
`/private/tmp/altt-python-runtime-eval/final-redaction-backend`: снова
**946 passed, 3 skipped** на обеих host-версиях и **949 passed, 0 skipped**
на обеих Docker-версиях с PostgreSQL 16; `compileall` успешен. Повторно
созданные disposable PostgreSQL container, volume и network удалены.

Снимок benchmark `final-backend` сохранён без изменений. Между его кодом
и окончательным тестовым снимком изменены только redaction regex в
`app/services/encoder_history.py` и две проверки в существующем тесте.
SHA-256 app-manifest benchmark:
`e443f1ad9d45a4920deed466228450a094a3596e4d0cbc2cde8e506b88086ef3`;
окончательного app-manifest:
`401a3366d447adee68e5d0c6c0a8ecf55e64eb772cb3ccfc82a48aeb6d5462ed`.
Полный hash тестового снимка и хеши двух изменённых файлов сохранены в
`final_redaction_revalidation` provenance JSON. После redaction-правки
производительность заново не измерялась; решение не мигрировать сохраняется.

Image IDs, registry base digests, architecture, точные runtime build flags,
зависимости и результаты закреплены в
[python315_docker_provenance.json](python315_docker_provenance.json).
Этот исправленный кандидат не является изменением production Dockerfile.
Linux amd64/Unraid hardware не проверялись; решение о миграции требует
отдельного вывода о производительности и ресурсах.

### Итог решения

Production остаётся на **Python 3.14**. Финальная серия из 32 paired repeats
на native Linux arm64 дала **inconclusive**: критерий отсутствия регрессии
не доказан. Успешные 949 тестов и исправление native libraries подтверждают
функциональную совместимость кандидата, но не заменяют этот performance gate.
Подробные результаты и исходные измерения находятся в
[PYTHON_RUNTIME_BENCHMARKS.md](PYTHON_RUNTIME_BENCHMARKS.md) и
[Linux benchmark JSON](runtime_benchmark_docker_3147_3150_linux_arm64.json).

Точные экспериментальные Dockerfile, constraints и инструкции сохранены в
[runtime-eval/README.md](runtime-eval/README.md). Они не подключены к
production сборке. Собственные одноразовые PostgreSQL container, его volume
и network удалены после проверки; существующий стек не затрагивался.

Docker-доказательства в `/private/tmp/altt-python-runtime-eval`:
`docker314-build.log`, `docker315-build.log`, `docker-provenance314.json`,
`docker-provenance315.json`, `docker314-baseline-tests.log`,
`docker315-baseline-tests.log`, `docker314-migrations.log`,
`docker315-migrations.log`. Временные образы —
`altt-python-eval:314-20261013` и `altt-python-eval:315-20261013`.

Новая функциональность и окончательное решение о версии должны учитывать
отдельный повторяемый benchmark, итоговый feature suite и целевой Docker.
Описание применимых возможностей 3.15 находится в
[PYTHON_315_FEATURES.md](PYTHON_315_FEATURES.md).

Исходные временные доказательства: `provenance314.json`, `provenance315.json`,
`constraints-shared.txt`, `install314-report.json`, `install315-report.json`,
`install315-pyyaml-report.json`, `baseline314-tests.log`,
`baseline315-tests.log` в `/private/tmp/altt-python-runtime-eval`.
Временный каталог может быть очищен ОС; ключевые результаты закреплены выше.
