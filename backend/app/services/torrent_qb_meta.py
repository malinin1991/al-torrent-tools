"""Метаданные для отображения торрента в qBittorrent."""

from typing import Any
from urllib.parse import urlparse

from app.core.config import settings
from app.services.torrent_archive import TorrentArchiveService


def resolve_anilibria_site_url(api_base_url: str | None = None) -> str:
    """Сайт релизов: www.anilibria.top (не API host)."""
    configured = (getattr(settings, "anilibria_site_url", None) or "").strip()
    if configured:
        return configured.rstrip("/")
    base = (api_base_url or settings.anilibria_base_url or "").strip()
    if not base:
        return "https://www.anilibria.top"
    parsed = urlparse(base)
    host = (parsed.hostname or "").lower()
    if host in {"anilibria.top", "www.anilibria.top"}:
        return "https://www.anilibria.top"
    if host:
        scheme = parsed.scheme or "https"
        return f"{scheme}://{host}"
    return "https://www.anilibria.top"


def build_release_torrents_url(release_alias: str | None, *, site_url: str | None = None) -> str | None:
    alias = (release_alias or "").strip().strip("/")
    if not alias:
        return None
    root = (site_url or resolve_anilibria_site_url()).rstrip("/")
    return f"{root}/anime/releases/release/{alias}/torrents"


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
    """Участники релиза: [{role, role_label, nickname}, ...] в порядке API."""
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
            role = "unknown" if role else "unknown"
        if not role_label:
            role_label = role
        key = (role, nickname.casefold())
        if key in seen:
            continue
        seen.add(key)
        members.append({"role": role, "role_label": role_label, "nickname": nickname})
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


def extract_release_block_flags(release_payload: dict[str, Any]) -> tuple[bool, bool]:
    """(is_blocked_by_geo, is_blocked_by_copyrights) из payload AniLibria."""
    return (
        bool(release_payload.get("is_blocked_by_geo")),
        bool(release_payload.get("is_blocked_by_copyrights")),
    )


def block_flags_from_quality_json(quality_json: dict[str, Any] | None) -> tuple[bool, bool]:
    """Флаги блокировок из torrent_archive.quality_json."""
    if not isinstance(quality_json, dict):
        return False, False
    return (
        bool(quality_json.get("is_blocked_by_geo")),
        bool(quality_json.get("is_blocked_by_copyrights")),
    )


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
