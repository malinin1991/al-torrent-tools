"""Клиент Video Kensetsu by GeeKaZ0iD (health / presets / encode)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.services.runtime_settings import get_setting_value, upsert_setting

HEALTH_CACHE_TTL_SEC = 60.0
SETTING_HEALTH_OK = "video_kensetsu_health_ok"
SETTING_HEALTH_AT = "video_kensetsu_health_checked_at"
ENCODE_TIMEOUT_BASE_SEC = 60.0
ENCODE_TIMEOUT_PER_PATH_SEC = 15.0
ENCODE_TIMEOUT_MAX_SEC = 300.0
ENCODE_BATCH_MAX_FILES = 100


class VideoKensetsuHttpError(RuntimeError):
    """HTTP-ошибка Video Kensetsu с исходным status_code и текстом тела."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = int(status_code)


def coerce_preset_available(value: Any, *, default: bool = True) -> bool:
    """Нормализует available пресета: falsey / \"false\" / 0 → недоступен."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in {"", "0", "false", "no", "off"}:
            return False
        if cleaned in {"1", "true", "yes", "on"}:
            return True
        return True
    return bool(value)


def _encoder_error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            for key in ("error", "detail", "message"):
                raw = data.get(key)
                if isinstance(raw, str) and raw.strip():
                    return raw.strip()
                if raw is not None and not isinstance(raw, (dict, list)):
                    return str(raw)
    except Exception:
        pass
    text = (response.text or "").strip()
    if text:
        return text[:500]
    return f"HTTP {response.status_code}"


def encode_timeout_for_paths(path_count: int, *, base_sec: float = ENCODE_TIMEOUT_BASE_SEC) -> float:
    """Таймаут encode: для batch растёт с числом путей, с потолком."""
    n = max(1, int(path_count))
    scaled = base_sec if n <= 1 else base_sec + ENCODE_TIMEOUT_PER_PATH_SEC * (n - 1)
    return float(min(ENCODE_TIMEOUT_MAX_SEC, max(base_sec, scaled)))


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
    paths: list[str],
    preset_id: str,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    """POST /api/internal/encode JSON: path/paths + preset_id.

    HTTP 200 и 207 считаются успешным ответом (207 — частичный batch).
    Прочие коды → ``VideoKensetsuHttpError`` с текстом ``{error}`` из тела.
    Возвращает ``{"status_code": int, "body": ...}``.
    """
    root = normalize_video_kensetsu_base_url(base_url)
    if not root:
        raise ValueError("Не задан URL Video Kensetsu")
    cleaned_id = (preset_id or "").strip()
    if not cleaned_id:
        raise ValueError("Не передан preset_id")
    cleaned_paths = [str(p).strip() for p in (paths or []) if str(p).strip()]
    if not cleaned_paths:
        raise ValueError("Не задан путь к файлу")

    if len(cleaned_paths) == 1:
        payload: dict[str, Any] = {"path": cleaned_paths[0], "preset_id": cleaned_id}
    else:
        payload = {"paths": cleaned_paths, "preset_id": cleaned_id}

    effective_timeout = (
        float(timeout_sec)
        if timeout_sec is not None
        else encode_timeout_for_paths(len(cleaned_paths))
    )

    async with httpx.AsyncClient(timeout=effective_timeout) as client:
        response = await client.post(
            f"{root}/api/internal/encode",
            json=payload,
            headers={"Accept": "application/json"},
        )
    if response.status_code not in (200, 207):
        raise VideoKensetsuHttpError(
            _encoder_error_message(response),
            status_code=response.status_code,
        )
    content_type = (response.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        body: Any = response.json()
    else:
        text = (response.text or "").strip()
        body = {"ok": True, "body": text or None}
    return {"status_code": response.status_code, "body": body}


def find_preset(presets: list[dict[str, Any]], preset_id: str) -> dict[str, Any] | None:
    wanted = (preset_id or "").strip()
    if not wanted:
        return None
    for item in presets:
        if str(item.get("id") or "") == wanted:
            return item
    return None
