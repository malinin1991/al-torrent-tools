import hashlib
import logging
import re
import time
from pathlib import Path

import qbittorrentapi
from qbittorrentapi import exceptions as qb_exc

_INFO_HASH_RE = re.compile(r"^[0-9a-fA-F]{32,64}$")
logger = logging.getLogger(__name__)
_comment_unsupported_warned = False


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
    tags: list[str] | None = None,
) -> tuple[bool, bool, bool]:
    """Добавляет торрент в qB.

    Returns:
        (added_new, comment_ok, tags_ok)
    """
    clean_tags = _normalize_tags(tags)
    add_kwargs: dict = {"torrent_files": [torrent_bytes]}
    if rename:
        add_kwargs["rename"] = rename
    if category:
        add_kwargs["category"] = category
    if clean_tags:
        add_kwargs["tags"] = clean_tags

    already_present = False
    try:
        client.torrents_add(**add_kwargs)
    except Exception as exc:
        if not is_qb_torrent_already_present(exc):
            raise
        already_present = True

    info_hash = torrent_info_hash(torrent_bytes)
    if rename and already_present:
        _apply_torrent_rename(client, info_hash, rename)

    comment_ok = True
    if comment:
        comment_ok = _set_torrent_comment_after_add(client, info_hash, comment)
        if not comment_ok:
            logger.warning(
                "Не удалось установить comment после add для %s (already_present=%s)",
                info_hash[:8],
                already_present,
            )

    tags_ok = True
    if clean_tags:
        tags_ok = _set_torrent_tags_after_add(client, info_hash, clean_tags)
        if not tags_ok:
            logger.warning(
                "Не удалось установить tags после add для %s (already_present=%s)",
                info_hash[:8],
                already_present,
            )
    return (not already_present), comment_ok, tags_ok


def _normalize_tags(tags: list[str] | None) -> list[str]:
    if not tags:
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        text = (raw or "").replace(",", " ").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    return cleaned


def _apply_torrent_rename(client: qbittorrentapi.Client, info_hash: str, rename: str) -> None:
    try:
        client.torrents_rename(torrent_hash=info_hash, new_torrent_name=rename)
    except Exception as exc:
        logger.warning("Не удалось переименовать торрент %s: %s", info_hash[:8], exc)


def _torrent_hash_fields(torrent: object) -> list[str]:
    """Все известные hash-поля записи torrents/info (v1/v2/hybrid)."""
    keys = ("hash", "infohash_v1", "infohash_v2")
    values: list[str] = []
    for key in keys:
        raw = getattr(torrent, key, None)
        if raw is None and isinstance(torrent, dict):
            raw = torrent.get(key)
        if raw is None:
            continue
        text = str(raw).strip().lower()
        if text and text not in values:
            values.append(text)
    return values


def collect_client_info_hashes(client: qbittorrentapi.Client) -> set[str]:
    """Множество info_hash, которые сейчас есть в qB (включая v1/v2)."""
    found: set[str] = set()
    torrents = client.torrents_info()
    for torrent in torrents or []:
        for value in _torrent_hash_fields(torrent):
            try:
                found.add(sanitize_info_hash(value))
            except ValueError:
                continue
    return found


def _client_hash_candidates(client: qbittorrentapi.Client, preferred: str) -> list[str]:
    """preferred + hash/infohash_v1/v2 из torrents/info, если торрент уже виден."""
    try:
        safe = sanitize_info_hash(preferred)
    except ValueError:
        return []
    ordered = [safe]
    try:
        items = list(client.torrents_info(torrent_hashes=safe) or [])
    except Exception:
        items = []
    for torrent in items:
        for value in _torrent_hash_fields(torrent):
            try:
                normalized = sanitize_info_hash(value)
            except ValueError:
                continue
            if normalized not in ordered:
                ordered.append(normalized)
    return ordered


def _wait_torrent_hash_candidates(
    client: qbittorrentapi.Client,
    info_hash: str,
    *,
    attempts: int = 8,
    delay_sec: float = 0.25,
) -> list[str]:
    """Ждёт появления торрента в qB после add и возвращает hash-кандидаты."""
    for attempt in range(1, attempts + 1):
        candidates = _client_hash_candidates(client, info_hash)
        try:
            present = bool(client.torrents_info(torrent_hashes=candidates[0] if candidates else info_hash))
        except Exception:
            present = False
        if present:
            return candidates
        if attempt < attempts:
            time.sleep(delay_sec * attempt)
    return _client_hash_candidates(client, info_hash)


def _set_torrent_comment_after_add(
    client: qbittorrentapi.Client,
    info_hash: str,
    comment: str,
) -> bool:
    """После add/Conflict ждёт торрент и ставит comment по всем известным hash."""
    candidates = _wait_torrent_hash_candidates(client, info_hash)
    if not candidates:
        try:
            candidates = [sanitize_info_hash(info_hash)]
        except ValueError:
            return False
    for candidate in candidates:
        if _ensure_torrent_comment(client, candidate, comment, attempts=8, delay_sec=0.3):
            return True
    return False


def _read_torrent_tags(client: qbittorrentapi.Client, info_hash: str) -> set[str]:
    try:
        items = list(client.torrents_info(torrent_hashes=info_hash) or [])
    except Exception:
        return set()
    if not items:
        return set()
    raw = getattr(items[0], "tags", None)
    if raw is None and isinstance(items[0], dict):
        raw = items[0].get("tags")
    if not isinstance(raw, str) or not raw.strip():
        return set()
    return {part.strip().casefold() for part in raw.split(",") if part.strip()}


def _ensure_torrent_tags(
    client: qbittorrentapi.Client,
    info_hash: str,
    tags: list[str],
    *,
    attempts: int = 6,
    delay_sec: float = 0.3,
    require_present: bool = False,
) -> bool:
    """Добавляет tags (addTags), с ретраями; успех если все desired есть у торрента."""
    desired = _normalize_tags(tags)
    if not desired:
        return True
    try:
        safe_hash = sanitize_info_hash(info_hash)
    except ValueError:
        return False

    if require_present:
        try:
            present = bool(client.torrents_info(torrent_hashes=safe_hash))
        except Exception:
            present = True
        if not present:
            return False

    desired_keys = {t.casefold() for t in desired}
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            client.torrents_add_tags(tags=desired, torrent_hashes=safe_hash)
            current = _read_torrent_tags(client, safe_hash)
            if desired_keys.issubset(current):
                return True
            last_exc = RuntimeError(f"tags не обновились (сейчас={sorted(current)!r})")
        except (
            qb_exc.UnsupportedQbittorrentVersion,
            qb_exc.NotFound404Error,
            qb_exc.HTTP404Error,
            AttributeError,
        ) as exc:
            last_exc = exc
            if isinstance(exc, (qb_exc.UnsupportedQbittorrentVersion, AttributeError)):
                logger.warning("Tags торрента недоступны в этой версии qBittorrent/WebAPI")
                return False
            if require_present and isinstance(exc, (qb_exc.NotFound404Error, qb_exc.HTTP404Error)):
                return False
        except Exception as exc:
            last_exc = exc

        if attempt < attempts:
            time.sleep(delay_sec * attempt)

    logger.warning(
        "Не удалось установить tags для %s после %s попыток: %s",
        safe_hash[:8],
        attempts,
        last_exc,
    )
    return False


def _set_torrent_tags_after_add(
    client: qbittorrentapi.Client,
    info_hash: str,
    tags: list[str],
) -> bool:
    """После add/Conflict ждёт торрент и ставит genre tags."""
    desired = _normalize_tags(tags)
    if not desired:
        return True
    candidates = _wait_torrent_hash_candidates(client, info_hash)
    if not candidates:
        try:
            candidates = [sanitize_info_hash(info_hash)]
        except ValueError:
            return False
    for candidate in candidates:
        if _ensure_torrent_tags(client, candidate, desired, attempts=6, delay_sec=0.25):
            return True
    return False


def _ensure_torrent_comment(
    client: qbittorrentapi.Client,
    info_hash: str,
    comment: str,
    *,
    attempts: int = 6,
    delay_sec: float = 0.35,
    require_present: bool = False,
) -> bool:
    """Принудительно ставит comment (перезапись), с ретраями на 404 сразу после add.

    Успех только если properties.comment совпал с desired.
    require_present=True — сразу False, если торрента нет в клиенте (для массового backfill).
    """
    global _comment_unsupported_warned
    desired = (comment or "").strip()
    if not desired:
        return False

    try:
        safe_hash = sanitize_info_hash(info_hash)
    except ValueError:
        return False

    if require_present:
        try:
            present = bool(client.torrents_info(torrent_hashes=safe_hash))
        except Exception:
            present = True
        if not present:
            return False

    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            client.torrents_set_comment(comment=desired, torrent_hashes=safe_hash)
            current = _read_torrent_comment(client, safe_hash)
            if current == desired:
                return True
            if current is None:
                # Торрент ещё не готов / properties 404 — не считаем успехом.
                last_exc = RuntimeError("comment не прочитан после setComment")
            else:
                last_exc = RuntimeError(f"comment не обновился (сейчас={current!r})")
        except (
            qb_exc.UnsupportedQbittorrentVersion,
            qb_exc.NotFound404Error,
            qb_exc.HTTP404Error,
            AttributeError,
        ) as exc:
            last_exc = exc
            if isinstance(exc, (qb_exc.UnsupportedQbittorrentVersion, AttributeError)):
                if not _comment_unsupported_warned:
                    _comment_unsupported_warned = True
                    logger.warning(
                        "Комментарий торрента недоступен (нужен qBittorrent ≥ 5.2 / WebAPI 2.12.1)"
                    )
                return False
            if require_present and isinstance(exc, (qb_exc.NotFound404Error, qb_exc.HTTP404Error)):
                return False
        except Exception as exc:
            last_exc = exc

        if attempt < attempts:
            time.sleep(delay_sec * attempt)

    logger.warning(
        "Не удалось установить comment для %s после %s попыток: %s",
        safe_hash[:8],
        attempts,
        last_exc,
    )
    return False


def _read_torrent_comment(client: qbittorrentapi.Client, info_hash: str) -> str | None:
    try:
        props = client.torrents_properties(torrent_hash=info_hash)
    except Exception:
        return None
    raw = getattr(props, "comment", None)
    if isinstance(props, dict):
        raw = props.get("comment", raw)
    if not isinstance(raw, str):
        return None
    return raw.strip()


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
