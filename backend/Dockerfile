FROM python:3.14-slim

# MediaInfo CLI + libmediainfo из официального репозитория MediaArea
# (свежее, чем пакет Debian freeze). После pip — см. ниже: удаляем bundled
# libmediainfo из wheel pymediainfo, иначе /info показывает ~24.x из пакета.
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
# Wheel pymediainfo тащит свой libmediainfo.so (~24.x) и грузит его ПЕРВЫМ
# из каталога пакета — системный MediaArea (≥26) при этом игнорируется.
# Удаляем bundled .so и проверяем, что ctypes/pymediainfo берёт system lib.
RUN set -eux; \
    pip install --no-cache-dir -r /app/requirements.txt; \
    python -c "from pathlib import Path; import pymediainfo; pkg=Path(pymediainfo.__file__).resolve().parent; paths=list(pkg.glob('libmediainfo*')); [p.unlink() for p in paths]; print('removed bundled:', ', '.join(p.name for p in paths) or '(none)')"; \
    python -c "from pymediainfo import MediaInfo; _l,_h,ver,tup=MediaInfo._get_library(); print(f'pymediainfo uses libmediainfo {ver} from system'); assert tup and tup[0] >= 26, ver"

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
