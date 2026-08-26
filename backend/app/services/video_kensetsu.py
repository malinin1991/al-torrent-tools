"""Клиент Video Kensetsu by GeeKaZ0iD (health / presets / encode)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.services.runtime_settings import get_setting_value, upsert_setting

DEFAULT_METADATA_NICKNAME = "GeeKaZ0iD"

_SKIP_PRESET_KEYS = frozenset({"id", "name", "description", "audio_settings"})
_AUDIO_FIELD_MAP = {
    "codec": "audio_codec",
    "bitrate": "audio_bitrate",
    "sample_rate": "audio_sample_rate",
    "vbr": "audio_vbr",
}

HEALTH_CACHE_TTL_SEC = 60.0
SETTING_HEALTH_OK = "video_kensetsu_health_ok"
SETTING_HEALTH_AT = "video_kensetsu_health_checked_at"


def normalize_video_kensetsu_base_url(base_url: str | None) -> str:
    return (base_url or "").strip().rstrip("/")


def resolve_video_kensetsu_base_url(db: Session | None) -> str:
    return normalize_video_kensetsu_base_url(
        get_setting_value(db, "video_kensetsu_base_url", "")
    )


def is_video_kensetsu_enabled(db: Session | None) -> bool:
    value = get_setting_value(db, "video_kensetsu_enabled", "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def store_health_cache(db: Session | None, ok: bool, *, commit: bool = False) -> None:
    """Сохраняет результат health-probe для UI (ссылка «Кодировщик»)."""
    if db is None:
        return
    upsert_setting(db, SETTING_HEALTH_OK, "true" if ok else "false")
    upsert_setting(db, SETTING_HEALTH_AT, datetime.now(timezone.utc).isoformat())
    if commit:
        db.commit()


def read_health_cache(db: Session | None) -> tuple[bool | None, float | None]:
    """Возвращает (ok | None, age_sec | None). None — кэша нет / битый."""
    if db is None:
        return None, None
    raw_ok = get_setting_value(db, SETTING_HEALTH_OK, "").strip().lower()
    raw_at = get_setting_value(db, SETTING_HEALTH_AT, "").strip()
    if not raw_ok or not raw_at:
        return None, None
    ok = raw_ok in {"1", "true", "yes", "on"}
    try:
        cleaned = raw_at.rstrip("Z")
        checked = datetime.fromisoformat(cleaned)
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - checked).total_seconds()
    except Exception:
        return ok, None
    return ok, age


def health_sync(base_url: str, *, timeout_sec: float = 2.0) -> dict[str, Any]:
    """Синхронный GET {base_url} — ok при HTTP 200 (для SSR / settings save)."""
    root = normalize_video_kensetsu_base_url(base_url)
    if not root:
        raise ValueError("Не задан URL Video Kensetsu")
    with httpx.Client(timeout=timeout_sec) as client:
        response = client.get(root)
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    return {"ok": True, "status_code": response.status_code, "base_url": root}


def probe_and_store_health(
    db: Session | None,
    *,
    base_url: str | None = None,
    timeout_sec: float = 2.0,
    commit: bool = True,
) -> bool:
    """Проверяет health и пишет кэш. False если выключено / нет URL / ошибка."""
    enabled = is_video_kensetsu_enabled(db)
    root = normalize_video_kensetsu_base_url(base_url) or resolve_video_kensetsu_base_url(db)
    if not enabled or not root:
        store_health_cache(db, False, commit=commit)
        return False
    try:
        health_sync(root, timeout_sec=timeout_sec)
        store_health_cache(db, True, commit=commit)
        return True
    except Exception:
        store_health_cache(db, False, commit=commit)
        return False


def video_kensetsu_ui_context(
    db: Session | None,
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    """Флаги для шаблонов.

    ``video_kensetsu_ok`` = включено + URL + последний успешный health (200).
    ``refresh=True`` (полный SSR): при протухшем/пустом кэше — короткий sync-probe.
    ``refresh=False`` (SSE live): только кэш, без сетевого запроса.
    """
    enabled = is_video_kensetsu_enabled(db)
    base_url = resolve_video_kensetsu_base_url(db)
    if not enabled or not base_url:
        ok = False
    elif refresh:
        cached_ok, age = read_health_cache(db)
        if cached_ok is not None and age is not None and 0 <= age <= HEALTH_CACHE_TTL_SEC:
            ok = cached_ok
        else:
            ok = probe_and_store_health(db, timeout_sec=2.0, commit=True)
    else:
        cached_ok, _age = read_health_cache(db)
        ok = cached_ok is True
    return {
        "video_kensetsu_enabled": enabled,
        "video_kensetsu_base_url": base_url,
        "video_kensetsu_ok": ok,
    }


def _form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build_encode_form_fields(path: str, preset: dict[str, Any]) -> dict[str, str]:
    """Multipart-поля для POST /api/internal/encode (без None)."""
    fields: dict[str, str] = {
        "path": path,
        "preset_id": str(preset.get("id") or ""),
        "preset_modified": "false",
        "cpu_threads": "0",
        "thread_queue_size": "0",
        "x265_pools": "0",
        "update_metadata": "true",
        "metadata_nickname": DEFAULT_METADATA_NICKNAME,
    }
    for key, value in preset.items():
        if key in _SKIP_PRESET_KEYS or value is None:
            continue
        if isinstance(value, (dict, list)):
            continue
        fields[key] = _form_value(value)

    audio = preset.get("audio_settings")
    if isinstance(audio, dict):
        for src, dst in _AUDIO_FIELD_MAP.items():
            raw = audio.get(src)
            if raw is None:
                continue
            fields[dst] = _form_value(raw)
    return fields


async def health(base_url: str, *, timeout_sec: float = 10.0) -> dict[str, Any]:
    """GET {base_url} — ok при HTTP 200."""
    root = normalize_video_kensetsu_base_url(base_url)
    if not root:
        raise ValueError("Не задан URL Video Kensetsu")
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        response = await client.get(root)
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    return {"ok": True, "status_code": response.status_code, "base_url": root}


async def list_presets(base_url: str, *, timeout_sec: float = 15.0) -> list[dict[str, Any]]:
    root = normalize_video_kensetsu_base_url(base_url)
    if not root:
        raise ValueError("Не задан URL Video Kensetsu")
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        response = await client.get(f"{root}/api/presets")
        response.raise_for_status()
        payload = response.json()
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("presets", "items", "data"):
            raw = payload.get(key)
            if isinstance(raw, list):
                return [item for item in raw if isinstance(item, dict)]
    raise RuntimeError("Некорректный ответ /api/presets")


async def encode(
    base_url: str,
    *,
    path: str,
    preset: dict[str, Any],
    timeout_sec: float = 60.0,
) -> Any:
    root = normalize_video_kensetsu_base_url(base_url)
    if not root:
        raise ValueError("Не задан URL Video Kensetsu")
    cleaned_path = (path or "").strip()
    if not cleaned_path:
        raise ValueError("Не задан путь к файлу")
    if not isinstance(preset, dict) or not preset.get("id"):
        raise ValueError("Некорректный пресет")

    form = build_encode_form_fields(cleaned_path, preset)
    # multipart/form-data как в UI кодировщика (поля без файлов).
    multipart = {key: (None, value) for key, value in form.items()}
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        response = await client.post(f"{root}/api/internal/encode", files=multipart)
        response.raise_for_status()
        content_type = (response.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            return response.json()
        text = (response.text or "").strip()
        return {"ok": True, "status_code": response.status_code, "body": text or None}


def find_preset(presets: list[dict[str, Any]], preset_id: str) -> dict[str, Any] | None:
    wanted = (preset_id or "").strip()
    if not wanted:
        return None
    for item in presets:
        if str(item.get("id") or "") == wanted:
            return item
    return None
