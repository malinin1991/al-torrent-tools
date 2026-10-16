# Прямой переход на обычный CPython 3.15

Решение от 13 октября 2026 года: обновить основной runtime проекта до обычного
[CPython 3.15.0](https://www.python.org/downloads/release/python-3150/), с GIL,
без JIT и free threading. Это решение принято после обсуждения замеров;
первоначальный консервативный benchmark gate больше не определяет выбранную
версию проекта. Измерения и их неопределённость сохранены без изменений.

## Объём перехода

- Основной Dockerfile использует официальный regular Python 3.15.0 на Debian
  trixie. Экспериментальные `altt-python-eval:*` образы не являются его базой.
- `requirements.txt` содержит runtime-зависимости, включая `psycopg[binary]`,
  `uvloop` и `httptools`. `requirements-dev.txt` добавляет pytest и watchfiles.
  `backend/constraints.txt` закрепляет проверенные версии обоих наборов;
  constraints сами по себе пакеты не устанавливают.
- Системная MediaInfo >=26 и удаление bundled библиотеки pymediainfo сохраняются.
  Нативный `_decimal` вручную не собирается; стандартный `decimal` использует
  Python fallback, когда C-ускорителя нет. PyYAML/LibYAML удалены, поскольку
  приложение не использует YAML-конфигурацию.
- Бизнес-код, схема БД и правила HEVC не меняются из-за версии Python.
  Миграция `0025_encoder_history` относится к отдельной функции истории отправок.
- `hevc_parallel_experiment.py` остаётся экспериментом; рабочие call sites
  его не используют. Opt-in profiler 3.15 автоматически не запускается.

## Почему прежний hold снят

Функциональная проверка обычного 3.15 уже была успешна. В первоначальном
сравнении Linux ARM64 median wall/CPU находились в диагностическом допуске,
но p95 имел широкие интервалы, не исключавшие регрессию. Это неопределённость,
а не доказательство замедления всего приложения.

Прямой переход выбран как обновление поддерживаемой среды с сохранением
поведения, без обещания выигрыша скорости. Старые benchmark JSON, выбросы и
доверительные интервалы не пересчитаны ради нового решения. Производительность
на native Unraid x86-64 отдельно не подтверждена; функциональная проверка под
эмуляцией, если выполнена, не является её заменой.

Предыдущий Linux benchmark использовал экспериментальный образ с полной
сборкой CPython из исходников. Итоговый рабочий образ сохраняет официальный
интерпретатор и первоначально условно собирал отсутствующий `_decimal`; повторный
benchmark именно этого образа не выполнялся.

Результаты прежних исследований:
[regular 3.14/3.15](PYTHON_RUNTIME_BENCHMARKS.md),
[совместимость](PYTHON_315_COMPATIBILITY.md),
[отдельный t-эксперимент](PYTHON_315T_BENCHMARKS.md).

## Историческая проверка первоначального Dockerfile

13 октября 2026 года собран именно рабочий `backend/Dockerfile`, с обычным
`python:3.15.0-slim-trixie` и `backend/constraints.txt`:

- Linux ARM64, нативно: **1011 passed**, без пропусков.
- Linux x86-64, под эмуляцией на ARM64: **1011 passed**, без пропусков.
- В обоих случаях PostgreSQL 16 была отдельной одноразовой тестовой БД;
  реальные qBittorrent, Kensetsu и Telegram в тестах не использовались.
- После удаления компилятора проверены 20 native/application imports,
  native Decimal/libmpdec 2.5.1, LibYAML 0.2.5, системная MediaInfo 26.10
  и binary psycopg 3.3.6. GIL оставался включён, JIT выключен.
- Установленные 46 application distributions совпали с constraints.
  Сборка `_decimal` прошла upstream decimal suite на обеих архитектурах:
  736 тестов всего, 727 успешных, 9 пропущенных.
- На обеих архитектурах штатный entrypoint выполнил Alembic `0024 → 0025`
  под PostgreSQL advisory lock. Проверены шесть типов подготовленных старых
  данных: они сохранились после обновления.
- Одноразовый PostgreSQL-контейнер, его volume и тестовая сеть удалены.
  Пользовательские контейнеры и volumes не изменялись.

Точные image IDs, входные файлы, проверки и логи сохранены отдельно от старого
benchmark: [migration provenance](python315_migration_provenance.json)
и [материалы проверки](runtime-eval/migration315/).

## Обновление окружения

Все четыре Python-сервиса Compose используют один backend Dockerfile. Для
обновления нужна пересборка образа и пересоздание контейнеров API, worker и
обоих ботов; данные PostgreSQL и существующие media/torrent volumes сохраняются.
Обычный entrypoint по-прежнему применяет Alembic под advisory lock.

Локальный `docker-compose.yml` собирает образ из `backend/`. Unraid Compose
использует образ из registry: изменение Dockerfile само по себе сервер не
обновляет. Для него сначала нужно опубликовать новый multi-arch образ, затем
выполнить pull и пересоздать четыре Python-сервиса по инструкции в README.

Первоначальная проверка перехода выполнялась в отдельных тестовых образах и
одноразовой БД. Последующие публикации в registry фиксируются в `docs/releases/`;
обновление контейнеров на Unraid выполняется отдельным шагом.

Локально нужно создать новую venv, сохранив старую до проверки:

```sh
cd backend
python3.15 -m venv .venv315
source .venv315/bin/activate
python -m pip install -c constraints.txt -r requirements-dev.txt
python -m pytest -q
```

Не устанавливать C-wheels из venv 3.14 в новую среду и не использовать
экспериментальные pure-psycopg constraints из t-проверки для обычной миграции.

Перед обновлением Unraid сохранить текущий image digest/tag и обычную резервную
копию БД. Откат runtime — возврат предыдущего образа либо старой локальной venv.
Сама смена Python не требует `alembic downgrade`; изменения схемы отдельных
функций следует оценивать независимо, сохраняя новые данные.

## Последующие обновления

Patch-версия Python закреплена в Dockerfile. При её обновлении используются
готовые runtime wheels (`--only-binary=:all:`); при отсутствии подходящего wheel
сборка останавливается, вместо скрытой установки компилятора. `_decimal`
из upstream-образа может присутствовать или отсутствовать: smoke проверяет
работу публичного Decimal API без требования к C-ускорителю.

Повторная проверка 13 октября показала: удаление `_decimal` в одноразовых
контейнерах опубликованного релиза даёт по 1288 passed без пропусков на ARM64
и x86-64, включая PostgreSQL-тесты. Отдельно проверены Pydantic Decimal и
binary psycopg с Decimal roundtrip в текстовом и бинарном режимах.
После этого собственная сборка `_decimal` удалена из рабочего Dockerfile.

Проверка extras выявила неиспользуемые PyYAML/LibYAML и WebSocket support,
которые приходили из `uvicorn[standard]`. Uvicorn теперь явно сохраняет только
uvloop/httptools; python-dotenv остаётся зависимостью pydantic-settings.
pytest/watchfiles и тестовые/benchmark файлы находятся в target `development`;
по умолчанию публикуется `runtime`. Системная MediaInfo >=26, очистка bundled
libmediainfo, BLAKE3 и binary psycopg сохраняются, поскольку участвуют в работе.

Новая сборка без собственных native-расширений проверена на обеих архитектурах:
по 1288 passed без пропусков в локальном QA-слое поверх точного опубликованного
runtime digest (добавлены только dev-зависимости, тесты и скрипты их fixtures).
Рабочие app/alembic/worker исходники сверены по SHA-256 со snapshot сборки.
На самом runtime-образе проверены pip check, Decimal/psycopg roundtrip,
отсутствие компилятора и исключённых пакетов, миграции entrypoint до `0026`,
health и семь HTML/static endpoints. Target `development` отдельно собран на
ARM64: pytest/watchfiles доступны, Uvicorn выбирает WatchFilesReload.

Обновление constraints — отдельное изменение с тестами, а не скрытая часть
следующего `docker build`. Установка под новой minor-версией требует повторной
проверки зависимостей, native imports и миграций.

## Актуальность зависимостей 17 октября 2026

Все **44** закреплённых пакета сверены с актуальными стабильными, не отозванными
релизами PyPI. Для обычного CPython 3.15.0 есть совместимые wheels на Linux ARM64,
Linux x86-64 и macOS ARM64. Найдены и внесены четыре обновления:

- [FastAPI](https://pypi.org/project/fastapi/0.143.3/): 0.143.0 → **0.143.3**.
- [Starlette](https://pypi.org/project/starlette/1.8.0/): 1.7.0 → **1.8.0**.
- [pydantic-settings](https://pypi.org/project/pydantic-settings/2.16.0/): 2.15.0 → **2.16.0**.
- [pluggy](https://pypi.org/project/pluggy/1.7.0/): 1.6.0 → **1.7.0**.

Остальные 40 pins уже соответствовали последним стабильным версиям. Независимый
pip resolver без constraints выбрал ровно тот же набор: **39 runtime** и
**5 dev-only** пакетов. Новых транзитивных зависимостей не появилось.

В чистых venv внутри имевшихся Linux CPython 3.15.0 образов обе архитектуры
установили все 44 пакета с `--only-binary=:all:` из официального PyPI, без
компиляции. Списки версий полностью совпали с обновлённым `backend/constraints.txt`;
`pip check` успешен. На ARM64 нативно и x86-64 под эмуляцией отдельно выполнены:

- по **1448 passed, 0 skipped** с собственными БД PostgreSQL 16.15;
- native imports, известный BLAKE3-вектор, uvloop coroutine и разбор HTTP через httptools;
- binary psycopg, SQLAlchemy `SELECT 1`, Pydantic Decimal и PostgreSQL Decimal
  round trip в текстовом и бинарном режимах;
- системная MediaInfo **26.10**, удаление bundled `libmediainfo.so.0` и реальный
  разбор сгенерированного WAV;
- полная цепочка `alembic upgrade head` до `0027_encoder_job_tracking`;
- lifecycle приложения и 13 HTML/API/static endpoints, включая health и OpenAPI.

[MediaInfo 26.10](https://mediaarea.net/MediaInfo/ChangeLog) остаётся актуальным
стабильным релизом. Python 3.15.0, системная библиотека, Dockerfile и набор
runtime extras не менялись. Для macOS проверены метаданные wheels; выполнение
обновлённой среды на macOS этим прогоном не подтверждается.

Существующее предупреждение TestClient/httpx сохраняется. Предупреждение
дублированного OpenAPI operation ID webhook дополнительно воспроизведено на
предыдущем FastAPI 0.143.0; оно не внесено обновлением зависимостей.

Точные wheel filenames/SHA-256, версии, проверенные manifests и результаты:
[dependency provenance](python315_dependencies_20261017.json). Использованы
одноразовые среды и синтетические данные, без рабочего `.env` и внешних
интеграций. Полная пересборка/публикация Docker-образа и обновление Unraid
в эту проверку не входят.

## Публикация Docker 17 октября 2026

После проверки зависимостей собран рабочий `runtime` из полного снимка текущего
backend, включая незакоммиченные UI/технические изменения. Опубликованы
`registry.mageek.su/al-torrent-tools:latest` и версионный тег
`release-20261016-184030` для `linux/amd64` и `linux/arm64`.

Общий digest: `sha256:a70663b5aeae8244944a1b9b0e96a906b5550a9cd40480a8c53b6ddd17d0842d`.
Имена тегов и build timestamps используют UTC; дата раздела — Asia/Omsk.

Каждый точный runtime проверен отдельно: SHA-256 163 встроенных файлов,
`pip check`, нативные зависимости, MediaInfo 26.10, отсутствие компилятора,
dev-пакетов и локального `.env`. QA-слой добавлял только тестовые зависимости
и fixtures, сохраняя все 14 runtime-слоёв и не подменяя app/alembic/worker.
На каждой архитектуре прошло **1448 тестов без пропусков**, штатный entrypoint
применил миграции к своей PostgreSQL 16.15, 15 реальных HTTP endpoints и SSE
успешно проверены. Три JS-сценария проверены на том же снимке исходников.

После публикации обе архитектуры повторно загружены через `latest` из registry;
runtime и исходники сверены ещё раз. Индекс содержит ровно две целевые
архитектуры, без provenance/SBOM attestations. Полные digest, source manifest,
версии и результаты — [отчёт релиза](releases/release-20261016-184030.json).
Рабочие интеграции и Unraid не изменялись.
