import hashlib
import re
from pathlib import Path

import qbittorrentapi

_INFO_HASH_RE = re.compile(r"^[0-9a-fA-F]{32,64}$")


def is_qb_torrent_already_present(exc: BaseException) -> bool:
    """qBittorrent Conflict/409: торрент уже есть в клиенте — не ошибка."""
    if isinstance(exc, qbittorrentapi.Conflict409Error):
        return True
    message = str(exc).strip().lower()
    return message in {"conflict", "torrent already exists"}


def is_qb_unavailable(exc: BaseException) -> bool:
    """Master/slave недоступен (лежит сеть/сервис), не ошибка логина/конфига."""
    if isinstance(exc, qbittorrentapi.LoginFailed):
        return False
    if isinstance(exc, qbittorrentapi.APIConnectionError):
        return True
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    message = str(exc).strip().lower()
    needles = (
        "connection refused",
        "timed out",
        "timeout",
        "unreachable",
        "name or service not known",
        "nodename nor servname",
        "network is unreachable",
        "failed to establish",
        "connection reset",
        "temporarily unavailable",
        "server disconnected",
    )
    return any(item in message for item in needles)


def is_qb_auth_error(exc: BaseException) -> bool:
    """Неверный логин/пароль WebUI или иная ошибка авторизации."""
    if isinstance(exc, qbittorrentapi.LoginFailed):
        return True
    message = str(exc).strip().lower()
    needles = (
        "login failed",
        "fails authentication",
        "authentication",
        "unauthorized",
        "forbidden",
        "incorrect password",
        "invalid username",
        "ошибка авторизации",
    )
    return any(item in message for item in needles)


def should_wait_for_qb(exc: BaseException) -> bool:
    """Ждём клиента: сеть лежит или нужно обновить пароль в настройках."""
    return is_qb_unavailable(exc) or is_qb_auth_error(exc)


def qb_client_wait_message(role: str, exc: BaseException | None = None, *, missing: bool = False) -> str:
    """Текст для waiting_master / waiting_slave."""
    role_key = (role or "").strip().lower()
    role_label = "Master" if role_key == "master" else ("Slave" if role_key == "slave" else role or "qBittorrent")
    if missing:
        return (
            f"{role_label} не настроен. "
            f"Зайдите в Настройки (/settings) и укажите данные подключения."
        )
    if exc is not None and is_qb_auth_error(exc):
        return (
            f"{role_label}: ошибка авторизации. "
            f"Зайдите в Настройки (/settings) и обновите логин/пароль qBittorrent ({role_key or role})."
        )
    if exc is not None:
        return f"{role_label} недоступен: {exc}"
    return f"{role_label} недоступен"


# UI-статусы торрента на master (для страницы пайплайна).
MASTER_UI_LABELS: dict[str, str] = {
    "downloading": "загружается",
    "seeding": "раздаётся",
    "stopped": "остановлен",
    "error": "с ошибкой",
    "missing": "нет на master",
    "unavailable": "master недоступен",
}

_QB_DOWNLOADING = frozenset({
    "downloading",
    "metadl",
    "forceddl",
    "stalleddl",
    "queueddl",
    "checkingdl",
    "allocating",
    "moving",
})
_QB_SEEDING = frozenset({
    "uploading",
    "stalledup",
    "queuedup",
    "forcedup",
    "checkingup",
})
_QB_STOPPED = frozenset({
    "pauseddl",
    "pausedup",
    "stoppeddl",
    "stoppedup",
})
_QB_ERROR = frozenset({
    "error",
    "missingfiles",
    "unknown",
})


def map_qb_torrent_ui_state(state: str | None, progress: float | None = None) -> str:
    """qB state → downloading | seeding | stopped | error."""
    raw = str(state or "").strip().lower()
    prog = float(progress or 0.0)
    if raw in _QB_ERROR or "error" in raw:
        return "error"
    if raw in _QB_STOPPED:
        return "stopped"
    if raw in _QB_SEEDING:
        return "seeding"
    if raw in _QB_DOWNLOADING:
        return "downloading"
    if prog >= 1.0:
        return "seeding"
    if prog > 0:
        return "downloading"
    return "stopped"


def sanitize_info_hash(value: str) -> str:
    """Нормализует info_hash: только hex длиной 32–64, иначе ValueError."""
    cleaned = value.strip().lower()
    if not _INFO_HASH_RE.fullmatch(cleaned):
        raise ValueError(f"Некорректный info_hash: {value!r}")
    return cleaned


def _decode_bencode(data: bytes, start: int = 0) -> tuple[object, int]:
    token = data[start : start + 1]
    if token == b"i":
        end = data.index(b"e", start)
        return int(data[start + 1 : end]), end + 1
    if token == b"l":
        result: list[object] = []
        index = start + 1
        while data[index : index + 1] != b"e":
            value, index = _decode_bencode(data, index)
            result.append(value)
        return result, index + 1
    if token == b"d":
        result: dict[bytes, object] = {}
        index = start + 1
        while data[index : index + 1] != b"e":
            key, index = _decode_bencode(data, index)
            if not isinstance(key, bytes):
                raise ValueError("Bencode-словарь должен содержать byte-ключи")
            value, index = _decode_bencode(data, index)
            result[key] = value
        return result, index + 1
    if token.isdigit():
        length_end = data.index(b":", start)
        length = int(data[start:length_end])
        value_start = length_end + 1
        value_end = value_start + length
        return data[value_start:value_end], value_end
    raise ValueError("Некорректный bencode torrent-файла")


def _extract_info_slice(data: bytes) -> bytes:
    marker = b"4:info"
    marker_index = data.find(marker)
    if marker_index < 0:
        raise ValueError("В torrent-файле отсутствует секция info")
    info_start = marker_index + len(marker)
    _, info_end = _decode_bencode(data, info_start)
    return data[info_start:info_end]


def torrent_info_hash(torrent: str | Path | bytes) -> str:
    """SHA1 от bencode-секции info — совпадает с qBittorrent %I."""
    if isinstance(torrent, bytes):
        torrent_bytes = torrent
    else:
        torrent_bytes = Path(torrent).read_bytes()
    info_section = _extract_info_slice(torrent_bytes)
    return hashlib.sha1(info_section).hexdigest()


_LIBRIA_ANNOUNCE_BASE = "http://tr.libria.fun:2710/announce"


def ensure_announce_passkey(torrent_bytes: bytes, passkey: str | None) -> bytes:
    """Подставляет ?pk= в announce AniLibria без изменения info (info_hash сохраняется)."""
    pk = (passkey or "").strip()
    if not pk:
        return torrent_bytes
    if f"pk={pk}".encode() in torrent_bytes:
        return torrent_bytes

    result = torrent_bytes
    # Без pk
    bare = _LIBRIA_ANNOUNCE_BASE
    bare_field = f"{len(bare)}:{bare}".encode()
    with_pk = f"{bare}?pk={pk}"
    with_pk_field = f"{len(with_pk)}:{with_pk}".encode()
    if bare_field in result:
        result = result.replace(bare_field, with_pk_field)

    # Уже есть query без нашего pk — не трогаем другие параметры, только голый announce
    return result


def qb_add_torrent(
    client: qbittorrentapi.Client,
    torrent_bytes: bytes,
    *,
    rename: str | None = None,
    comment: str | None = None,
    category: str | None = None,
) -> bool:
    """Добавляет торрент в qB. True = новый, False = уже был (Conflict)."""
    add_kwargs: dict = {"torrent_files": [torrent_bytes]}
    if rename:
        add_kwargs["rename"] = rename
    if category:
        add_kwargs["category"] = category

    already_present = False
    try:
        client.torrents_add(**add_kwargs)
    except Exception as exc:
        if not is_qb_torrent_already_present(exc):
            raise
        already_present = True

    needs_post = bool(comment) or (bool(rename) and already_present)
    if needs_post:
        info_hash = torrent_info_hash(torrent_bytes)
        if rename and already_present:
            try:
                client.torrents_rename(torrent_hash=info_hash, new_torrent_name=rename)
            except Exception:
                pass
        if comment:
            try:
                client.torrents_set_comment(comment=comment, torrent_hashes=info_hash)
            except Exception:
                # Нужен qBittorrent ≥ 5.2 / WebAPI 2.12.1
                pass
    return not already_present


def qbittorrent_add(*args, **kwargs) -> dict:
    _ = (args, kwargs)
    return {"ok": True, "message": "Заглушка фазы 0"}


def test_qb_connection(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
) -> dict[str, str | int | bool]:
    """Проверка логина в WebUI API. Возвращает ok/version или raises RuntimeError."""
    host_value = host.strip()
    if not host_value:
        raise RuntimeError("Не указан хост qBittorrent")
    if port <= 0 or port > 65535:
        raise RuntimeError(f"Некорректный порт qBittorrent: {port}")

    client = qbittorrentapi.Client(
        host=host_value,
        port=port,
        username=username.strip(),
        password=password,
    )
    try:
        client.auth_log_in()
        version = str(client.app_version() or "-")
        try:
            webapi = str(client.app_web_api_version() or "-")
        except Exception:
            webapi = "-"
        torrent_count = len(client.torrents_info() or [])
    except qbittorrentapi.LoginFailed as exc:
        raise RuntimeError(f"Ошибка авторизации: {exc}") from exc
    except qbittorrentapi.APIConnectionError as exc:
        raise RuntimeError(f"Нет соединения с qBittorrent: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"Ошибка подключения к qBittorrent: {exc}") from exc
    finally:
        try:
            client.auth_log_out()
        except Exception:
            pass

    return {
        "ok": True,
        "host": host_value,
        "port": port,
        "version": version,
        "webapi": webapi,
        "torrents": torrent_count,
    }
