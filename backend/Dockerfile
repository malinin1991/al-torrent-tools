FROM python:3.14-slim

# MediaInfo CLI + libmediainfo из официального репозитория MediaArea
# (свежее, чем пакет Debian freeze; pymediainfo линкуется к libmediainfo0v5).
# MIME вложений Matroska читаем лёгким pure-Python EBML walk (без mkvtoolnix).
# Если MediaArea недоступен и apt уйдёт на Debian (25.x / 24.x) — сборка падает на проверке версии.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates wget; \
    wget --https-only -q -O /tmp/repo-mediaarea.deb \
      https://mediaarea.net/repo/deb/repo-mediaarea_1.0-27_all.deb; \
    # Лёгкая проверка: это debian-пакет репозитория, не HTML/ошибка зеркала.
    dpkg-deb -I /tmp/repo-mediaarea.deb >/dev/null; \
    dpkg -i /tmp/repo-mediaarea.deb; \
    apt-get update; \
    apt-get install -y --no-install-recommends mediainfo libmediainfo0v5 libzen0v5; \
    ldconfig; \
    mediainfo --Version; \
    ver="$(mediainfo --Version 2>&1 | sed -n 's/.*MediaInfoLib - v\([0-9][0-9.]*\).*/\1/p' | head -1)"; \
    major="$(printf '%s' "$ver" | cut -d. -f1)"; \
    if [ -z "$ver" ] || [ "$major" -lt 26 ]; then \
      echo "ERROR: expected MediaArea MediaInfo >= 26.x, got: '${ver:-unknown}'" >&2; \
      apt-cache policy mediainfo libmediainfo0v5 >&2 || true; \
      exit 1; \
    fi; \
    apt-get purge -y --auto-remove wget; \
    rm -f /tmp/repo-mediaarea.deb; \
    rm -rf /var/lib/apt/lists/*

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
