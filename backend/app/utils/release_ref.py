"""Разбор URL / alias / id релиза AniLiberty для джобов и UI."""

from __future__ import annotations

import re
from dataclasses import dataclass


class ReleaseRefParseError(ValueError):
    """Ввод не удалось разобрать как release_id или alias."""


@dataclass(frozen=True, slots=True)
class ReleaseRef:
    """Либо числовой id, либо alias (ровно одно из двух)."""

    release_id: int | None = None
    alias: str | None = None

    def __post_init__(self) -> None:
        has_id = self.release_id is not None
        has_alias = bool(self.alias)
        if has_id == has_alias:
            raise ValueError("ReleaseRef: нужен ровно один из release_id / alias")


_RELEASE_SEGMENT_RE = re.compile(r"/release/([^/\s?#]+)", re.IGNORECASE)
_HAS_LETTER_RE = re.compile(r"[A-Za-zА-Яа-яЁё]")
_URLISH_RE = re.compile(r"^https?://", re.IGNORECASE)


def _strip_html_suffix(segment: str) -> str:
    text = segment.strip()
    if text.lower().endswith(".html"):
        return text[:-5].strip()
    return text


def _ref_from_token(token: str) -> ReleaseRef:
    cleaned = _strip_html_suffix(token).strip().lower()
    if not cleaned:
        raise ReleaseRefParseError("Пустой alias/id после разбора")
    if cleaned.isdigit():
        release_id = int(cleaned)
        if release_id <= 0:
            raise ReleaseRefParseError("release_id должен быть положительным числом")
        return ReleaseRef(release_id=release_id)
    if _HAS_LETTER_RE.search(cleaned):
        return ReleaseRef(alias=cleaned)
    raise ReleaseRefParseError(
        f"Не удалось разобрать «{token}»: нужен числовой id или alias с буквой"
    )


def parse_release_ref(raw: str | int | None) -> ReleaseRef:
    """Принимает URL AniLiberty/AniLibria, alias или числовой release_id.

    Примеры:
    - ``https://aniliberty.top/anime/releases/release/re-creators``
    - ``https://aniliberty.top/.../release/re-creators/torrents``
    - ``https://www.anilibria.tv/release/reiwa-no-dara-san.html``
    - ``3993`` / ``3993``
    - ``re-creators``
    """
    if isinstance(raw, int):
        if raw <= 0:
            raise ReleaseRefParseError("release_id должен быть положительным числом")
        return ReleaseRef(release_id=raw)

    text = str(raw or "").strip()
    if not text:
        raise ReleaseRefParseError("Укажите URL, alias или id релиза")

    match = _RELEASE_SEGMENT_RE.search(text)
    if match:
        return _ref_from_token(match.group(1))

    looks_like_url = bool(_URLISH_RE.match(text)) or ("/" in text and "." in text.split("/", 1)[0])
    if looks_like_url or "/release/" in text.lower():
        raise ReleaseRefParseError(
            "URL не содержит сегмент /release/<alias>: не удалось разобрать"
        )

    return _ref_from_token(text)
