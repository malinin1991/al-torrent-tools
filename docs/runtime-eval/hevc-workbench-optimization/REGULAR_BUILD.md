# Проверка рабочего образа на обычном Python 3.15

17 октября 2026 года из финального снимка backend собраны и проверены оба
target рабочего Dockerfile для **Linux ARM64**. Основной runtime остаётся
обычным **CPython 3.15.0 с GIL**, без JIT, с **psycopg-binary 3.3.6** и
**Jinja2 3.1.6**. Временные изменения для отменённой миграции на 3.15t удалены;
оптимизация подготовки workbench сохранена.

Собранные локальные образы:

- `runtime`: `altt-workbench:py315-20261017-161151`, image ID
  `sha256:0ac66f155e9197729bfb1b8dc219a3f341175a660df238b4f74544134fefae32`.
- `development`: `altt-workbench-dev:py315-20261017-161151`, image ID
  `sha256:0a835fd064c702ac9c6d4fb7a33d22e696f6d38354d404bfc1be95b0bcf2576f`.

Оба probe завершились успешно: `free_threaded_build=false`, `gil_enabled=true`,
`jit_available=false`, psycopg backend `binary`, libpq **18.6**, системная
MediaInfo **26.10**. Bundled libmediainfo удалена. Публичный Decimal API работает
с Python fallback; `_decimal` в этих образах отсутствует. `pip check` успешен.
В `runtime` нет pytest/watchfiles, в `development` они присутствуют.
[Build receipt](regular-build-20261017/build-results.json),
[runtime probe](regular-build-20261017/runtime-probe.json),
[development probe](regular-build-20261017/development-probe.json).

Полный suite выполнен в точном development image с отдельной PostgreSQL
**16.15**: **1504 passed, 1 skipped**, около **19 секунд**. Единственный skip —
модуль изолированного эксперимента MiniJinja: optional-зависимость
`minijinja==3.0.1` в рабочий образ не устанавливается. Сохранилось одно
предупреждение Starlette о TestClient/httpx. Проверки runtime до импорта pytest,
после загрузки plugins и после тестов подтверждают ordinary/GIL-on/binary
psycopg; все 263 source hashes до и после suite совпали. `compileall` и
`pip check` также завершились с кодом 0.
[Итоговый receipt](regular-build-20261017/tests/final-test-receipt.json),
[runtime checkpoints](regular-build-20261017/tests/attempt-02/pytest-runtime-summary.json),
[лог](regular-build-20261017/tests/attempt-02/pytest.log),
[команды](regular-build-20261017/tests/attempt-02/test-commands.jsonl).

Первая попытка сохранена: **1502 passed, 2 failed, 1 skipped**. Launcher задавал
`TORRENT_STORAGE_DIR` с basename `altt-test-torrents` и подменял AniLibria URL
на `http://invalid.invalid`, нарушая ожидаемые fallback defaults двух тестов.
Повторный запуск убрал только эти глобальные overrides; application/test
sources и image ID остались прежними. Это исправление окружения запуска,
а не исправление приложения после неуспешных тестов.
[Первая попытка](regular-build-20261017/tests/pytest.log),
[причина повтора](regular-build-20261017/tests/attempt-02/attempt-reason.json).

Точный runtime image дополнительно запущен через штатный entrypoint и Uvicorn
с uvloop/httptools на отдельной одноразовой БД. Alembic дошёл до
`0027_encoder_job_tracking`; `/health/live`, `/hevc-workbench` и live partial
`/hevc-workbench/live?view=all&page=1&per_page=30` ответили **200**.
[HTTP/migration receipt](regular-build-20261017/runtime-api-smoke.json).

Снимок содержит **263 файла**, без рабочего `.env`; независимая сверка
подтвердила совпадение с рабочим backend. Build marker содержит Git HEAD
`4e78fcfb10de16abd8bebf13232196ce84c1d5d0`; образ также включает незакоммиченную
оптимизацию workbench. Точный состав задаёт manifest. SHA-256 сервиса
`app/services/hevc_workbench.py`:
`e5838d5cef83af43407130d12e9d6044773e5a459186b2b4f70944054af51ef4`.
SHA-256 [source manifest](regular-build-20261017/source-manifest.json):
`fda3f83ef334ded095e3e2acb49e89d558ef6eacc3aa6f7b6ce719e57188b99a`;
[архива исходников](regular-build-20261017/source-backend.tar.gz):
`2f71eed9ee03027ebad3866464ba5e9dd62bb8057126959fa61ee7ad10fd199e`.

Одноразовые PostgreSQL и сеть удалены; локальные образы сохранены.
[Cleanup receipt](regular-build-20261017/cleanup.json) и
[SHA256SUMS материалов](regular-build-20261017/SHA256SUMS).

Это функциональная проверка локальных ARM64-образов. Публикация в registry,
развёртывание Unraid и новые измерения производительности в неё не входят.
Прежние измерения и их ограничения сохранены в [RESULTS.md](RESULTS.md).
