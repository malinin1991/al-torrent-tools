# Сборка изолированного MiniJinja experiment

Этот каталог содержит только входы отдельного development experiment.
Production-код, production image и `backend/constraints.txt` не изменяются.
Результаты измерений и вывод о выборе renderer находятся в отдельном отчёте;
команды ниже сами по себе не подтверждают совместимость или ускорение.

## Что сравниваем

Обе базы собраны ранее из официального CPython **3.15.0** для native
`linux/arm64`, с PGO/LTO/shared и без JIT. Отличие free-threaded сборки —
`--disable-gil`. Исходный Python archive и параметры записаны в
[`../315t/README.md`](../315t/README.md) и
[`../../python315t_provenance.json`](../../python315t_provenance.json).
Входы по умолчанию:

- regular: `altt-python-eval:315-regular-pure-20261013`;
- free-threaded: `altt-python-eval:315t-pure-fixed-20261013`.

Перед использованием сохранить **текущий** `docker image inspect` обеих
баз, платформу и ID: локальный tag не является неизменяемой provenance.
Прежние recorded IDs: regular
`sha256:c22a5df4dab477150c6933f22f10d096d35c563ba409d480c41e00b55afc268c`,
free-threaded
`sha256:8f2ea17188e004132c4e4800a1d9ad61fb1ff177ae6b86ed30ef1e9e81d30b52`.
Они приведены для сопоставления, а не вместо нового inspect.

У базы октября 13 четыре Python pins отстают от текущего приложения:
FastAPI, Starlette, pydantic-settings и pluggy. Одинаковый
`requirements-upgrade.txt` обновляет их в обеих сборках. Полный snapshot
pins сохранён в `constraints.txt`. Унаследованные PyYAML и websockets
сохраняются одинаковыми; они не являются новыми production требованиями.

**Общий экспериментальный контракт:** pure `psycopg==3.3.6` + system libpq.
Production `psycopg-binary==3.3.6` отсутствует: опубликованных wheels для
3.15t в прежнем аудите нет. Не ставить binary wheel regular-ABI в
free-threaded runtime и не скрывать fallback с `PYTHON_GIL=0`.

## Один исходный MiniJinja для двух интерпретаторов

Используется опубликованный
[`minijinja-3.0.1.tar.gz`](https://pypi.org/project/minijinja/3.0.1/#files),
SHA-256
`7e1b012a56414134adc46a79a649b529c35a3b955ccb8407a4ce34441c9842ce`.
`fetch_source.py` скачивает только этот артефакт и проверяет фиксированный
hash; уже имеющийся файл также проверяется. Архив передаётся в disposable
build context и не должен добавляться в репозиторий.

Rust/Python исходники и Cargo.toml **не патчатся**. Исходный sdist включает
monorepo Cargo.lock с 428 packages, но не включает большинство workspace
members (CLI, JS, demos). Наше дополнительное требование `--locked` остановило
первую попытку; ручного pruning до 113 packages также оказалось недостаточно.
Это ограничения нашего строгого build recipe, **не доказательство отказа
обычного pip install**.

Финальный рецепт сначала запускает настоящий `cargo metadata --format-version
1` без компиляции. Cargo нормализует workspace/feature-dependent edges до
106 packages. Перед сборкой проверяется каждый результат: его
name/version/source должен существовать в исходном lock, а checksum должен
точно совпадать. Добавленные/upgraded packages и изменения checksum запрещены.
Все retained versions/sources/checksums сохранены. Исходный SHA-256 lock:
`3707ef6fc40b282be017312e3ba14e857267de36aa8970462972d55ebc2bbdf8`;
полученный Cargo SHA-256:
`876f00d2ee00cb15a8a72361c708e39f79e52215e9c23fffc948d496696b9ea5`.
Этот final hash закреплён в build helper, чтобы обе базы использовали один
и тот же resolved graph. Оба lock, полный diff, metadata stdout/stderr и
`lock-normalization.json` сохраняются в build artifacts.

Оба image затем собирают release wheel через `maturin==1.15.0` и `--locked`,
с interpreter текущей базы и проверенным Cargo-normalized lock. У upstream
Python binding PyO3 `0.29.3` с `abi3-py310`.
[PyO3 описывает](https://pyo3.rs/v0.29.3/free-threading.html), что
free-threaded interpreter игнорирует limited-API feature; менять её вручную
для этого эксперимента не требуется. Ожидаются regular ABI3 wheel и
версионный `cp315t` wheel. Фактические tags, нативные members, SHA wheel,
Rust compiler, configure flags и вывод PyO3 записываются при сборке.
Если тег free-threaded wheel не содержит `cp315t`, сборка останавливается.

Debian `rustc`/`cargo` и C toolchain ставятся только на build stage и
удаляются после сборки. Apt-зеркала не закреплены snapshot-датой, поэтому
новая сборка обязана сопоставить версии компиляторов в двух
`build-provenance.json`; это воспроизводимый рецепт с проверкой входов,
а не обещание bit-for-bit image reproducibility. Cargo build parallelism
ограничен двумя jobs для машины с 8 GiB RAM.

## Подготовка и сборка

Из корня репозитория создать **отдельный** context. Он не содержит backend,
`.env`, media, torrent archives или локальные credentials:

```sh
eval_inputs="$PWD/docs/runtime-eval/minijinja-workbench"
eval_context="$(mktemp -d /private/tmp/altt-minijinja-context.XXXXXX)"
cp "$eval_inputs/Dockerfile" "$eval_inputs/constraints.txt" \
  "$eval_inputs/requirements-upgrade.txt" "$eval_inputs/requirements-build.txt" \
  "$eval_inputs/build_minijinja.py" "$eval_inputs/probe_runtime.py" \
  "$eval_context/"
python3 "$eval_inputs/fetch_source.py" --output-dir "$eval_context"
```

Собирать последовательно, без benchmark/pytest и других сборок параллельно:

```sh
docker build --platform linux/arm64 --progress plain \
  --build-arg BASE_IMAGE=altt-python-eval:315-regular-pure-20261013 \
  --build-arg CARGO_BUILD_JOBS=2 \
  -t altt-minijinja-eval:regular-20261017 "$eval_context"
docker build --platform linux/arm64 --progress plain \
  --build-arg BASE_IMAGE=altt-python-eval:315t-pure-fixed-20261013 \
  --build-arg CARGO_BUILD_JOBS=2 \
  -t altt-minijinja-eval:ft-20261017 "$eval_context"
```

Base image содержит старый snapshot приложения. Для содержательных проверок
использовать один и тот же **текущий read-only snapshot** `backend/` в `/app`
и одну копию harness. Нельзя сравнивать приложение из разных base layers.

## Проверки перед измерениями

Docker build выполняет `pip check` и запускает engine/import smoke test.
В `/opt/altt-minijinja-eval/build/` остаются `build-inputs.json`,
`build-provenance.json`, `source-Cargo.lock`, wheel и `runtime-probe.json`.
Сохранить их вместе с image IDs, manifest актуального backend snapshot,
constraints/harness hashes и raw результатами каждого запуска.

Для свежего процесса с текущим приложением (здесь `eval_backend` —
изолированная копия backend, `eval_output` — отдельный каталог результатов):

```sh
docker run --rm --network none --read-only --tmpfs /tmp \
  -v "$eval_backend:/app:ro" -v "$eval_output:/output" \
  -e PYTHONDONTWRITEBYTECODE=1 \
  altt-minijinja-eval:ft-20261017 \
  python /opt/altt-minijinja-eval/probe_runtime.py \
  --application-root /app --import-module app.main \
  --import-module worker --threads 8 --renders 512 \
  --expect-gil 0 --output /output/probe-ft.json
```

Для regular image передать `--expect-gil 1`. Третий контрольный режим
получается из того же FT image через `python -X gil=1 ... --expect-gil 1`.
`PYTHON_GIL` не задаётся ни в одном режиме. `-X gil=0` запрещён probe.

Probe фиксирует эффективный GIL до imports, после каждого critical import
и после конкурентных render; любое несоответствие завершает процесс
ошибкой. Дополнительно проверяются shared MiniJinja environment,
независимые request contexts, Python dataclass attributes, callback filter
и escaping относительно Jinja. Это проверка корректности и fallback,
**не benchmark** и не доказательство многократного ускорения.

Engine поддержка free-threading не делает безопасными общий изменяемый
SQLAlchemy Session, request state или наши callbacks. При многопоточном
рендеринге каждому запросу нужны собственные подготовленные данные;
настройки environment задаются до измерений, а render не обращается к БД.
Сама работа внутри async route остаётся синхронной, если harness явно
не отправляет её в пул потоков.
