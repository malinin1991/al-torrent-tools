ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS build-deps

# Isolated experiment only. The base already contains the matched pure-psycopg
# contract from ../315t; production Dockerfiles and dependencies are untouched.
ARG BASE_IMAGE
ARG CARGO_BUILD_JOBS=2
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/altt-minijinja-eval

COPY constraints.txt requirements-upgrade.txt requirements-build.txt ./
RUN set -eux; \
    test -z "${PYTHON_GIL:-}"; \
    apt-get update; \
    apt-get install -y --no-install-recommends gcc libc6-dev cargo rustc; \
    python -m pip install --no-cache-dir --no-deps -c constraints.txt -r requirements-upgrade.txt; \
    python -m pip install --no-cache-dir --no-deps -c constraints.txt -r requirements-build.txt

FROM build-deps AS experiment
ARG BASE_IMAGE
ARG CARGO_BUILD_JOBS=2
COPY build_minijinja.py probe_runtime.py ./
COPY minijinja-3.0.1.tar.gz /tmp/minijinja-3.0.1.tar.gz
RUN set -eux; \
    python build_minijinja.py --source /tmp/minijinja-3.0.1.tar.gz \
        --output-dir /opt/altt-minijinja-eval/build --base-image "${BASE_IMAGE}" \
        --jobs "${CARGO_BUILD_JOBS}"; \
    python -m pip install --no-cache-dir --no-deps /opt/altt-minijinja-eval/build/wheels/*.whl

RUN set -eux; \
    python -m pip uninstall -y maturin; \
    apt-get purge -y --auto-remove gcc libc6-dev cargo rustc; \
    rm -rf /var/lib/apt/lists/* /root/.cargo /tmp/minijinja-3.0.1.tar.gz /tmp/altt-minijinja-build; \
    python -m pip check; \
    python probe_runtime.py --output /opt/altt-minijinja-eval/build/runtime-probe.json

# No application entrypoint: runtime probes and benchmarks are invoked explicitly.
ENTRYPOINT []
CMD ["python", "/opt/altt-minijinja-eval/probe_runtime.py"]
