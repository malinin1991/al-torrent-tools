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
