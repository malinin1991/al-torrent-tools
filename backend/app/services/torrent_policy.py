"""Трекеры админки: изменяется только оболочка .torrent, raw info сохраняется."""

from __future__ import annotations

from urllib.parse import urlencode

BACKOFFICE_TRACKERS = (
    "http://retracker.local/announce",
    "http://tr.libria.fun:2710/announce",
    "http://tracker.mageek.su:9090/announce",
    "http://atrack.pow7.com/announce",
    "http://open.acgtracker.com:1096/announce",
    "http://pow7.com:80/announce",
    "http://secure.pow7.com/announce",
    "http://t1.pow7.com/announce",
    "http://t2.pow7.com/announce",
    "http://tracker.dler.org:6969/announce",
    "udp://open.stealth.si:80/announce",
    "udp://opentor.net:6969",
    "udp://tracker.bittor.pw:1337/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
    "udp://tracker.torrent.eu.org:451",
)
SOURCE_TAGS = frozenset({"site", "hidden", "blocked"})


def tracker_urls(passkey: str) -> list[str]:
    if not passkey.strip():
        raise ValueError("Для трекеров админки требуется passkey AniLiberty")
    return [url + "?" + urlencode({"pk": passkey.strip()}) if "tr.libria.fun:" in url else url
            for url in BACKOFFICE_TRACKERS]


def _decode(data: bytes, start: int = 0, depth: int = 0):
    if depth > 100 or start >= len(data):
        raise ValueError("Некорректный bencode")
    token = data[start:start + 1]
    if token in (b"d", b"l"):
        value = {} if token == b"d" else []
        pos = start + 1
        while data[pos:pos + 1] != b"e":
            key, pos = _decode(data, pos, depth + 1)
            if token == b"d":
                if not isinstance(key, bytes) or key in value:
                    raise ValueError("Некорректный ключ bencode")
                item, pos = _decode(data, pos, depth + 1)
                value[key] = item
            else:
                value.append(key)
        return value, pos + 1
    if token == b"i":
        end = data.index(b"e", start + 1)
        return int(data[start + 1:end]), end + 1
    colon = data.index(b":", start)
    raw_length = data[start:colon]
    if not raw_length.isdigit():
        raise ValueError("Некорректная длина bencode")
    end = colon + 1 + int(raw_length)
    if end > len(data):
        raise ValueError("Обрезанный bencode")
    return data[colon + 1:end], end


def _encode(value):
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(_encode(v) for v in value) + b"e"
    raise ValueError("Неподдерживаемое значение bencode")


def raw_torrent_fields(data: bytes) -> dict[bytes, bytes]:
    if not data.startswith(b"d"):
        raise ValueError("Ожидался bencode dictionary")
    fields, pos = {}, 1
    while data[pos:pos + 1] != b"e":
        key, pos = _decode(data, pos)
        if not isinstance(key, bytes) or key in fields:
            raise ValueError("Некорректный ключ torrent")
        start = pos
        _, pos = _decode(data, pos)
        fields[key] = data[start:pos]
    if pos != len(data) - 1 or not fields.get(b"info", b"").startswith(b"d"):
        raise ValueError("Некорректная секция info / лишние байты")
    return fields


def replace_trackers(data: bytes, passkey: str) -> bytes:
    fields = raw_torrent_fields(data)
    urls = [url.encode() for url in tracker_urls(passkey)]
    fields[b"announce"] = _encode(urls[0])
    fields[b"announce-list"] = _encode([[url] for url in urls])
    return b"d" + b"".join(_encode(key) + value for key, value in sorted(fields.items())) + b"e"


def prepare_archive_bytes(archive, data: bytes, passkey: str) -> bytes:
    if getattr(archive, "backoffice_trackers", False) is True:
        return replace_trackers(data, passkey)
    from app.services.qbittorrent import ensure_announce_passkey
    return ensure_announce_passkey(data, passkey)


def source_tags(source: str | None, hidden=False, blocked=False) -> list[str]:
    if source == "site":
        return ["site"]
    if source == "backoffice":
        return (["hidden"] if hidden else []) + (["blocked"] if blocked else [])
    return []


def reconcile_source_tags(client, info_hash: str, desired: list[str]) -> bool:
    from app.services.qbittorrent import _read_torrent_tag_list
    current = _read_torrent_tag_list(client, info_hash)
    remove = [tag for tag in current if tag.casefold() in SOURCE_TAGS and tag.casefold() not in desired]
    add = [tag for tag in desired if tag not in {t.casefold() for t in current}]
    if add:
        client.torrents_add_tags(torrent_hashes=info_hash, tags=add)
    if remove:
        client.torrents_remove_tags(torrent_hashes=info_hash, tags=remove)
    final = {tag.casefold() for tag in _read_torrent_tag_list(client, info_hash)} & SOURCE_TAGS
    if final != set(desired):
        raise RuntimeError("qB не подтвердил метки источника")
    return bool(add or remove)


def reconcile_trackers(client, info_hash: str, passkey: str) -> bool:
    desired = tracker_urls(passkey)
    raw = client.torrents_trackers(torrent_hash=info_hash)
    # qB возвращает DHT/PeX/LSD как строки с отрицательным tier; это не trackers.
    current = [str(t.get("url", "") if isinstance(t, dict) else getattr(t, "url", "")) for t in raw]
    current = [url for url in current if url.startswith(("http://", "https://", "udp://", "ws://", "wss://"))]
    add = [url for url in desired if url not in current]
    remove = [url for url in current if url not in desired]
    if add:
        client.torrents_add_trackers(torrent_hash=info_hash, urls="\n".join(add))
    if remove:
        client.torrents_remove_trackers(torrent_hash=info_hash, urls="|".join(remove))
    if add or remove:
        final = client.torrents_trackers(torrent_hash=info_hash)
        urls = {str(t.get("url", "") if isinstance(t, dict) else getattr(t, "url", "")) for t in final}
        urls = {url for url in urls if url.startswith(("http://", "https://", "udp://", "ws://", "wss://"))}
        if urls != set(desired):
            raise RuntimeError("qB не подтвердил замену трекеров")
    return bool(add or remove)
