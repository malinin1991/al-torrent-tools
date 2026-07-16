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


def _clean(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


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
