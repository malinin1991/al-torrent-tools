"""Метаданные для отображения торрента в qBittorrent."""

from typing import Any
from urllib.parse import urlparse

from app.core.config import settings
from app.services.torrent_archive import TorrentArchiveService


def resolve_anilibria_site_url(api_base_url: str | None = None) -> str:
    """Сайт релизов: aniliberty.top (не API host)."""
    configured = (getattr(settings, "anilibria_site_url", None) or "").strip()
    if configured:
        return configured.rstrip("/")
    base = (api_base_url or settings.anilibria_base_url or "").strip()
    if not base:
        return "https://aniliberty.top"
    parsed = urlparse(base)
    host = (parsed.hostname or "").lower()
    if host in {"anilibria.top", "www.anilibria.top"}:
        return "https://aniliberty.top"
    if host:
        scheme = parsed.scheme or "https"
        return f"{scheme}://{host}"
    return "https://aniliberty.top"


def build_release_torrents_url(release_alias: str | None, *, site_url: str | None = None) -> str | None:
    alias = (release_alias or "").strip().strip("/")
    if not alias:
        return None
    root = (site_url or resolve_anilibria_site_url()).rstrip("/")
    return f"{root}/anime/releases/release/{alias}/torrents"


def build_release_admin_url(release_id: int, template: str | None) -> str | None:
    """URL релиза в админке из шаблона с плейсхолдером ``{release_id}``.

    Пустой шаблон или отсутствие плейсхолдера → ссылку не строить.
    """
    tpl = (template or "").strip()
    if not tpl or "{release_id}" not in tpl:
        return None
    return tpl.replace("{release_id}", str(int(release_id)))


def first_release_torrents_url(
    *aliases: str | None,
    site_url: str | None = None,
) -> str | None:
    """Первый валидный URL релиза из списка alias-кандидатов."""
    for alias in aliases:
        url = build_release_torrents_url(alias, site_url=site_url)
        if url:
            return url
    return None


def _clean(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def extract_release_genres(release_payload: dict[str, Any]) -> list[str]:
    """Имена жанров релиза для qBittorrent Tags (порядок как в API, без дублей)."""
    raw = release_payload.get("genres")
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name: str | None = None
        if isinstance(item, dict):
            name = _clean(item.get("name"))
        elif isinstance(item, str):
            name = _clean(item)
        if not name:
            continue
        # Запятая в qB — разделитель тегов.
        name = name.replace(",", " ").strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def genres_from_quality_json(quality_json: dict[str, Any] | None) -> list[str]:
    """Жанры, сохранённые в torrent_archive.quality_json."""
    if not isinstance(quality_json, dict):
        return []
    raw = quality_json.get("genres")
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = _clean(item) if isinstance(item, str) else None
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


_KNOWN_MEMBER_ROLES = frozenset(
    {"poster", "timing", "voicing", "editing", "decorating", "translating"}
)


def extract_release_members(release_payload: dict[str, Any]) -> list[dict[str, str]]:
    """Участники релиза: [{role, role_label, nickname, api_id?}, ...] в порядке API."""
    raw = release_payload.get("members")
    if not isinstance(raw, list):
        return []
    members: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        nickname = _clean(item.get("nickname"))
        if not nickname:
            continue
        role_obj = item.get("role")
        role = ""
        role_label = ""
        if isinstance(role_obj, dict):
            role = (_clean(role_obj.get("value")) or "").casefold()
            role_label = _clean(role_obj.get("description")) or ""
        elif isinstance(role_obj, str):
            role = (_clean(role_obj) or "").casefold()
        if role not in _KNOWN_MEMBER_ROLES:
            role = "unknown"
        if not role_label:
            role_label = role
        key = (role, nickname.casefold())
        if key in seen:
            continue
        seen.add(key)
        entry: dict[str, str] = {
            "role": role,
            "role_label": role_label,
            "nickname": nickname,
        }
        api_id = item.get("id")
        if isinstance(api_id, str) and api_id.strip():
            entry["api_id"] = api_id.strip()
        elif isinstance(api_id, (int, float)):
            entry["api_id"] = str(int(api_id))
        members.append(entry)
    return members


def members_from_quality_json(quality_json: dict[str, Any] | None) -> list[dict[str, str]]:
    """Участники, сохранённые в torrent_archive.quality_json."""
    if not isinstance(quality_json, dict):
        return []
    raw = quality_json.get("members")
    if not isinstance(raw, list):
        return []
    members: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        nickname = _clean(item.get("nickname"))
        if not nickname:
            continue
        role = (_clean(item.get("role")) or "unknown").casefold()
        if role not in _KNOWN_MEMBER_ROLES:
            role = "unknown"
        role_label = _clean(item.get("role_label")) or role
        key = (role, nickname.casefold())
        if key in seen:
            continue
        seen.add(key)
        members.append({"role": role, "role_label": role_label, "nickname": nickname})
    return members


def parse_api_bool(value: Any) -> bool | None:
    """Нормализация bool из API/JSONB; None если значение нераспознано."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
    return None


def extract_release_block_flags(release_payload: dict[str, Any]) -> tuple[bool, bool]:
    """(is_blocked_by_geo, is_blocked_by_copyrights) из payload AniLibria.

    Ключ отсутствует → False (для вызовов, где наличие ключа уже проверено снаружи).
    """
    geo = parse_api_bool(release_payload.get("is_blocked_by_geo"))
    copy = parse_api_bool(release_payload.get("is_blocked_by_copyrights"))
    return (bool(geo), bool(copy))


def block_flags_from_quality_json(quality_json: dict[str, Any] | None) -> tuple[bool, bool]:
    """Флаги блокировок из torrent_archive.quality_json."""
    if not isinstance(quality_json, dict):
        return False, False
    geo = parse_api_bool(quality_json.get("is_blocked_by_geo"))
    copy = parse_api_bool(quality_json.get("is_blocked_by_copyrights"))
    return (bool(geo), bool(copy))


def extract_release_names(release_payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """(русское main, оригинальное english)."""
    name = release_payload.get("name")
    if not isinstance(name, dict):
        return None, None
    main = _clean(name.get("main"))
    original = _clean(name.get("english"))
    if original is None:
        original = _clean(name.get("alternative"))
    return main, original


def build_qb_torrent_name(
    *,
    main_name: str | None,
    original_name: str | None,
    episodes: str | None,
    torrent_type: str | None,
) -> str:
    """Шаблон: name / jpn_name (серии) [тип]."""
    main = _clean(main_name)
    original = _clean(original_name)
    episodes_text = _clean(episodes)
    type_text = _clean(torrent_type)

    if main and original and main.casefold() != original.casefold():
        title = f"{main} / {original}"
    else:
        title = main or original or "torrent"

    if episodes_text:
        title = f"{title} ({episodes_text})"
    if type_text:
        title = f"{title} [{type_text}]"
    return title


def build_qb_torrent_name_from_payloads(
    release_payload: dict[str, Any],
    torrent_payload: dict[str, Any],
) -> str:
    main, original = extract_release_names(release_payload)
    episodes = TorrentArchiveService._extract_torrent_description(torrent_payload)
    torrent_type = TorrentArchiveService.extract_torrent_type(torrent_payload)
    return build_qb_torrent_name(
        main_name=main,
        original_name=original,
        episodes=episodes,
        torrent_type=torrent_type,
    )


def build_qb_torrent_name_from_archive(
    *,
    anime_name: str | None,
    torrent_description: str | None,
    torrent_type: str | None,
    quality_json: dict[str, Any] | None = None,
) -> str:
    original = None
    if isinstance(quality_json, dict):
        names = quality_json.get("names")
        if isinstance(names, dict):
            original = _clean(names.get("english")) or _clean(names.get("original"))
            if not anime_name:
                anime_name = _clean(names.get("main"))
    return build_qb_torrent_name(
        main_name=anime_name,
        original_name=original,
        episodes=torrent_description,
        torrent_type=torrent_type,
    )
