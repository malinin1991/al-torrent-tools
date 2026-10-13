# Совместимость CPython 3.15t: отдельный эксперимент

> Обновление решения, 13.10.2026: для основного проекта выбран прямой переход
> на обычный CPython 3.15.0. Ниже сохранены результаты и состояние завершённого
> эксперимента; прежнее решение оставить 3.14 относится к его моменту. Текущие
> изменения и проверка рабочего образа — [PYTHON_315_MIGRATION.md](PYTHON_315_MIGRATION.md).
> Переход на 3.15t и экспериментальный HEVC-пул не принят.

Дата: 13 октября 2026. Production остаётся на Python 3.14. Этот эксперимент
не меняет Dockerfile, requirements, пользовательские данные и запущенный стек.

## Что сравнивается

Один официальный source release **CPython 3.15.0**, две сборки и три режима:

- Обычная 3.15.0, GIL включён.
- Free-threaded 3.15.0 (`--disable-gil`) с `-X gil=1`.
- Та же free-threaded сборка в default режиме, GIL выключен.

По [официальной документации](https://docs.python.org/3.15/howto/free-threading-python.html)
build variant и эффективный GIL — разные признаки. `Py_GIL_DISABLED=1`
сохраняется и при runtime GIL включённом. Несовместимый C-extension может
автоматически включить GIL; поэтому наличие wheel ещё не доказывает,
что приложение работает без него. Здесь `-X gil=0` и `PYTHON_GIL=0`
не используются ни для обхода несовместимости, ни для измерений.

Сборки native Linux **arm64**, GCC 14.2.0, PGO + LTO + shared Python,
без JIT. Regular source SHA-256 и 315t source SHA-256 совпадают:
`ba4bed1ba346b916890b76d9e320451420aa69f6408997d33c66482eeae3d575`.
Официальный source archive и исходный Dockerfile сохранены в предыдущем
эксперименте; для 315t добавлен `--disable-gil`. Bundled libmpdec включена
явно, чтобы исключить потерю native `_decimal`.

## Production-зависимости: подтверждённый блокер

Текущий `psycopg[binary]` требует `psycopg-binary`. Для зафиксированного
`psycopg-binary==3.3.6` проверены **все 66 опубликованных filenames**:
free-threaded wheels (`cp313t`/`cp314t`/`cp315t`/`abi3t`) отсутствуют вообще,
включая Linux **aarch64 и x86_64**; source distribution также отсутствует.
Обычные `cp315-cp315` Linux wheels есть для обеих архитектур. Следовательно,
packaging-блокер установлен и для Linux amd64; это не перенос ARM-only
измерения на Unraid. Сам runtime/performance на Unraid amd64 не проверялся.
Полный список сохранён в
[psycopg-binary-all-wheel-files.json](runtime-eval/315t/psycopg-binary-all-wheel-files.json).
Dry-run на настоящем ARM64 315t завершается
`No matching distribution found`. Проверка проведена по
[официальным PyPI metadata](https://pypi.org/pypi/psycopg-binary/3.3.6/json).

Поэтому исходный production-контракт с 47 installed distributions
**не установлен и не проверен на 315t**. Обычные `cp315` C-wheels нельзя
переносить в `cp315t`, даже при GIL включённом. Версии пакетов, ABI и код
расширений для обхода блокера не изменялись.

## Отдельный общий pure-backend контракт

Для сравнения согласован отдельный эксперимент: **psycopg 3.3.6 с pure-Python
реализацией и системной libpq** во всех трёх режимах. Это поддерживаемый
[вариант установки psycopg](https://www.psycopg.org/psycopg3/docs/basic/install.html),
но он отличается от production binary backend. Нельзя переносить результаты
этого эксперимента на исходный binary-контракт.

Все **46 installed distributions**, включая pip 26.2.1, совпадают точно
между тремя режимами. Единственное отличие от прежнего Python dependency
набора — отсутствие `psycopg-binary`; остальные версии сохранены.

Подтверждены одинаковые native компоненты:

- `psycopg.pq.__impl__ == 'python'`, system libpq **17.11** (`170011`).
- Native `_decimal`, libmpdec **2.5.1**.
- PyYAML **6.0.3** с LibYAML **0.2.5**, `yaml.__with_libyaml__ == True`.
- Системная MediaInfo **26.10**, bundled library pymediainfo удалена.
- SQLAlchemy 2.1.4, pydantic-core 2.50.1, BLAKE3 1.0.11, uvloop 0.23.0,
  httptools 0.9.0 и остальные версии — без upgrades.

PyYAML и watchfiles 1.3.0 потребовали source builds для 315t; установлены
только временные build prerequisites, после сборки compiler/Rust toolchain
удалены. Первый экспериментальный Docker cleanup случайно удалил libpq5;
ошибка исправлена отдельным записанным layer и в каноническом Dockerfile.
Это исправление тестового окружения, не изменение приложения или backend.

## Проверки GIL и приложения

`probe_315t_imports.py` перечисляет установленные Python C-extensions,
используемые stdlib extensions и основные package/application imports.
Каждый из **43 imports** выполняется в новом процессе без force-off.
Дополнительно проверяется последовательность в одном процессе:
`app.main`, `worker`, Telegram handlers, uvloop, httptools, yaml, watchfiles,
BLAKE3, pymediainfo.

Итог на 315t: GIL **False до и после каждого импорта**, а также после всей
последовательности. Ошибок imports, warnings и автоматического включения
GIL не обнаружено. Проверено фактическое состояние, а не только wheel tags.
В этом наборе `greenlet` не установлен; его совместимость не утверждается.

В общем dual-runtime образе выполнен полный текущий suite, включая 62 теста
HEVC parallel helper и реальные PostgreSQL 16 claim-race тесты:

- Regular 3.15 pure: **1011 passed, 0 skipped**, GIL `True -> True`.
- 3.15t `-X gil=1`: **1011 passed, 0 skipped**, GIL `True -> True`.
- 3.15t default off: **1011 passed, 0 skipped**, GIL `False -> False`.

GIL проверяется до pytest import и после полного прогона. Успешный suite
не получен за счёт скрытого перехода в GIL-on. Каждая disposable БД сначала
обновлена до 0025 штатным entrypoint/advisory lock на image-default 315t.
Это не отдельный тройной сравнительный тест migration CLI: `alembic` в
этом образе использует image-default interpreter.

У обоих t-режимов `thread_inherit_context=1` и `context_aware_warnings=1`,
у regular — 0. То есть включение GIL не превращает t-build в обычную сборку.
Сырые configure/allocator flags сохранены; наличие `WITH_PYMALLOC=1` само
по себе не определяет фактический allocator free-threaded объектов.

## Границы результата и воспроизведение

Функциональная совместимость подтверждена для **отдельного pure-backend
контракта**. Production binary compatibility остаётся блокированной.
Реальные AniLiberty/Nova, qBittorrent, Telegram, Kensetsu и параллельная
обработка пользовательских медиа не проверялись; amd64/Unraid не измерялись.

Performance и эффект threaded HEVC helper исследуются отдельно. Времена
pytest не являются benchmark. Рекомендации находятся в
[PYTHON_315T_OPTIMIZATION.md](PYTHON_315T_OPTIMIZATION.md), измерения — в
[PYTHON_315T_BENCHMARKS.md](PYTHON_315T_BENCHMARKS.md) и
[raw benchmark JSON](runtime_benchmark_315_vs_315t_linux_arm64.json).
Отсутствие функциональных ошибок не означает отсутствие CPU/RSS регрессии.

Точные metadata, 43 import results, app source manifest, image IDs и хеши
входов: [python315t_provenance.json](python315t_provenance.json).
Dockerfile, wrappers, constraints и команды:
[runtime-eval/315t/README.md](runtime-eval/315t/README.md).

Предыдущие regular 3.14/3.15 benchmark snapshots сохранены. Новый снимок
для этого эксперимента —
`/private/tmp/altt-python-runtime-eval/ft-eval-final-backend`; app-manifest
SHA-256: `e9c8b20d52f40d08bfd3691a90d1a8af80452a7b97f58b4e6bb4d49644fef846`.
Собственный PostgreSQL container, volume и network удалены до benchmark;
временные images и доказательства сохранены.
