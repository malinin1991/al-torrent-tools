# Запуск изолированного эксперимента workbench

Входные образы и сборка MiniJinja описаны в [BUILD.md](BUILD.md), границы и
заранее фиксированный объём — в [PROTOCOL.md](PROTOCOL.md). Рабочие Dockerfile,
requirements, HTML-шаблоны и routes не переключаются.

## Данные и исходники

Запускать из корня репозитория. Для нового запуска выбрать новую директорию;
driver запрещает перезаписывать предыдущие JSON и logs.

```sh
eval_root="$(mktemp -d /private/tmp/altt-minijinja-eval-XXXXXX)"
eval_tools="$PWD/docs/runtime-eval/minijinja-workbench"
python3 "$eval_tools/freeze_source.py" --backend backend --output "$eval_root/frozen/backend"
```

Только собственные disposable ресурсы, без опубликованных портов PostgreSQL:

```sh
docker network create --internal --label altt.minijinja-eval=20261017 altt-minijinja-20261017
docker volume create --label altt.minijinja-eval=20261017 altt-minijinja-media-20261017
docker run -d --name altt-minijinja-pg-20261017 --label altt.minijinja-eval=20261017 \
  --network altt-minijinja-20261017 \
  --mount type=tmpfs,destination=/var/lib/postgresql/data,tmpfs-size=1073741824 \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=altt_minijinja_workbench postgres:16
```

Trust auth ограничен этой internal network. Существующий production PostgreSQL
не используется. `--prepare` проверяет префикс базы и ownership marker тома.

```sh
python3 "$eval_tools/run_matrix.py" --source "$eval_root/frozen/backend" --output "$eval_root/results" --stage prepare
python3 "$eval_tools/validate_tests.py" --source "$eval_root/frozen/backend" --output "$eval_root/results"
python3 "$eval_tools/run_matrix.py" --source "$eval_root/frozen/backend" --output "$eval_root/results" --stage validate
```

`validate` проверяет provenance и эквивалентность full/live во всех трёх режимах:
ordinary 3.15, 3.15t GIL-on и естественный 3.15t GIL-off. Любой GIL fallback,
SQL mutation, ошибка HTTP или несовпадение DOM прерывает проверку. Source mount
read-only; приложение не запускает lifespan и внешние сервисы.
`validate_tests.py` отдельно запускает четыре regression-модуля с отключённой
сетью и проверкой GIL до/после imports, collection и каждого теста.

## Измерения

Запускать только после успешных build, GIL probe и regression checks, без
параллельных сборок/pytest. Каждый stage — отдельная заранее определённая серия.
На macOS оборачивать каждую команду измерения в `caffeinate -is`, чтобы сон
компьютера не приостанавливал Docker VM. Проверить `pmset -g assertions`.
Следить за UTC/monotonic elapsed; неполная серия с паузами сохраняется отдельно,
полная серия запускается в новой директории. На Linux префикс caffeinate убрать.

```sh
python3 "$eval_tools/run_matrix.py" --source "$eval_root/frozen/backend" --output "$eval_root/results" --stage http
python3 "$eval_tools/run_matrix.py" --source "$eval_root/frozen/backend" --output "$eval_root/results" --stage render
python3 "$eval_tools/run_matrix.py" --source "$eval_root/frozen/backend" --output "$eval_root/results" --stage rendercontrols
python3 "$eval_tools/analyze.py" --input "$eval_root/results" --output "$eval_root/analysis.json"
```

Main HTTP: original route, SQL/HEVC, filesystem readiness, cached shell context,
adapter conversion и готовый HTML response. Один ASGI event loop, до четырёх
клиентов, один shared Environment. Это не TCP/browser load test.

Render-only: тот же service context подготавливается до timer, специфичный
snapshot conversion включён. `rendercontrols` попарно сравнивает MiniJinja с
Jinja на том же snapshot/preloaded templates; не объединяется с другой серией
как будто это общая парная выборка. Поле HTTP audit `render` означает фазу
подготовки HTML-ответа, включая context processor, а не только template CPU.

Все samples, ошибки и команды сохраняются. При необходимости исправления
harness запустить новую серию в другой директории; не дополнять выборку ради
желаемого результата. Анализ требует полную матрицу и единый semantic digest.

## Просмотр прототипа

После измерений можно открыть synthetic preview:

Перед ручным просмотром выполнить отдельную функциональную TCP-проверку:

```sh
caffeinate -is python3 "$eval_tools/smoke_preview.py" --source "$eval_root/frozen/backend" --output "$eval_root/results"
```

Она по очереди поднимает Jinja/MiniJinja в собственных Uvicorn-контейнерах,
сверяет full/live/search при C1/C4 и worker-GIL через `/_experiment/runtime`.
Это correctness smoke, без performance-метрик. Контейнеры удаляются в finally.
На Linux убрать префикс caffeinate.

```sh
docker network create --label altt.minijinja-eval=20261017 altt-minijinja-preview-20261017
docker create --name altt-minijinja-preview --read-only --tmpfs /tmp \
  --network altt-minijinja-preview-20261017 -p 127.0.0.1:18080:8000 \
  --mount "type=bind,source=$eval_root/frozen/backend,target=/app,readonly" \
  --mount "type=bind,source=$eval_tools/preview.py,target=/preview.py,readonly" \
  --mount type=volume,source=altt-minijinja-media-20261017,target=/bench/media,readonly \
  -e PYTHONDONTWRITEBYTECODE=1 altt-minijinja-eval:ft-20261017 \
  python /preview.py --backend /app --engine minijinja \
  --database-url postgresql+psycopg://postgres@altt-minijinja-pg-20261017:5432/altt_minijinja_workbench \
  --media-root /bench/media --host 0.0.0.0 --port 8000
docker network connect altt-minijinja-20261017 altt-minijinja-preview
docker start -a altt-minijinja-preview
```

Адрес: `http://127.0.0.1:18080/hevc-workbench`. Для baseline заменить engine на
`current_jinja`. Это preview разметки, поиска, страниц и ручного обновления.
Отдельный bridge нужен для localhost port publication: Docker 29 не публикует
HostPort на internal-only network. Вторая сеть даёт доступ к тестовой БД.
SSE и отправка в Kensetsu не подключены; существующий JS может показывать
потерю связи автообновления. Synthetic selection ничего не отправляет.

## Завершение

Остановить preview, затем удалить только свои временные ресурсы. Тестовая БД
на tmpfs исчезнет вместе с контейнером; JSON/source evidence остаются на host.

```sh
docker rm -f altt-minijinja-pg-20261017
docker rm -f altt-minijinja-preview
docker network rm altt-minijinja-preview-20261017
docker volume rm altt-minijinja-media-20261017
docker network rm altt-minijinja-20261017
```

Посторонние контейнеры/volumes не удалять. Runtime images можно сохранить для
дальнейшей проверки; их exact IDs и build provenance входят в evidence.
