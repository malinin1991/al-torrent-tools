FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    mediainfo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# Метка сборки вне /app — не перекрывается bind-mount исходников в dev.
ARG BUILD_TIME=
ARG GIT_SHA=
RUN set -eux; \
    bt="${BUILD_TIME}"; \
    if [ -z "$bt" ]; then bt="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; fi; \
    printf '%s' "$bt" > /etc/altt_build_time; \
    printf '%s' "${GIT_SHA}" > /etc/altt_git_sha
ENV APP_BUILD_TIME_FILE=/etc/altt_build_time \
    APP_GIT_SHA_FILE=/etc/altt_git_sha
