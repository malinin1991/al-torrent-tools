"""Сбор краткого статуса системы для страницы «Информация»."""

from __future__ import annotations

import asyncio
import os
import platform
import sys
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import QbClient, TelegramOutbox, TrackedRelease
from app.services.qbittorrent import test_qb_connection
from app.services.runtime_settings import build_anilibria_client, get_setting_value, resolve_anilibria_settings
from app.services.telegram_notify import (
    OUTBOX_PENDING,
    get_telegram_bot_token,
    get_telegram_chat_id,
    is_telegram_enabled,
    resolve_telegram_bot_api_base,
    test_telegram_get_me,
)
from app.services.torrent_archive import resolve_torrent_storage_root
from app.services.video_kensetsu import (
    health as video_kensetsu_health,
    is_video_kensetsu_enabled,
    resolve_video_kensetsu_base_url,
    store_health_cache as video_kensetsu_store_health_cache,
)

_APP_PACKAGES = (
    "fastapi",
    "uvicorn",
    "sqlalchemy",
    "alembic",
    "psycopg",
    "httpx",
    "jinja2",
    "apscheduler",
    "qbittorrent-api",
    "python-telegram-bot",
    "pydantic-settings",
    "pydantic",
    "python-multipart",
)

_TELEGRAM_HEARTBEAT_STALE_SEC = 120
_BUILD_TIME_FILE = Path(os.environ.get("APP_BUILD_TIME_FILE", "/etc/altt_build_time"))
_GIT_SHA_FILE = Path(os.environ.get("APP_GIT_SHA_FILE", "/etc/altt_git_sha"))


def _pkg_version(name: str) -> str:
    try:
        return pkg_version(name)
    except PackageNotFoundError:
        return "—"


def _read_build_stamp(path: Path, *, env_key: str) -> str | None:
    """ENV (override) → файл из Docker-образа → None."""
    raw = (os.environ.get(env_key) or "").strip()
    if raw:
        return raw
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def resolve_build_info() -> dict[str, str | None]:
    """Дата/время сборки образа и опциональный git SHA."""
    return {
        "time": _read_build_stamp(_BUILD_TIME_FILE, env_key="APP_BUILD_TIME"),
        "git_sha": _read_build_stamp(_GIT_SHA_FILE, env_key="APP_GIT_SHA"),
    }


async def collect_system_status(db: Session) -> dict[str, Any]:
    al_settings = resolve_anilibria_settings(db)
    has_token = bool(al_settings.bearer_token.strip())
    has_passkey = bool(al_settings.passkey.strip())
    has_login = bool(get_setting_value(db, "anilibria_login", "").strip())
    has_password = bool(get_setting_value(db, "anilibria_password", "").strip())

    anilibria = await _probe_anilibria(db, has_token=has_token)
    database = _probe_database(db)
    master_creds = _qb_credentials(db, "master")
    slave_creds = _qb_credentials(db, "slave")
    master_status, slave_status = await asyncio.gather(
        asyncio.to_thread(_probe_qb_creds, master_creds),
        asyncio.to_thread(_probe_qb_creds, slave_creds),
    )
    qb = {"master": master_status, "slave": slave_status}
    telegram = await _probe_telegram(db)
    video_kensetsu = await _probe_video_kensetsu(db)

    storage = resolve_torrent_storage_root()
    from app.jobs.orphan_cleanup import media_root_writable_status
    from app.services.torrent_files_meta import resolve_media_root

    media_root = resolve_media_root()
    media_ok, media_detail = media_root_writable_status(media_root)
    build = resolve_build_info()
    return {
        "app": {
            "name": settings.app_name,
            "env": settings.app_env,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "build_time": build["time"],
            "git_sha": build["git_sha"],
        },
        "anilibria": {
            "base_url": al_settings.base_url,
            "fallback_base_url": al_settings.fallback_base_url,
            "has_login": has_login,
            "has_password": has_password,
            "has_token": has_token,
            "has_passkey": has_passkey,
            **anilibria,
        },
        "database": database,
        "qb": qb,
        "telegram": telegram,
        "video_kensetsu": video_kensetsu,
        "storage": {
            "path": str(storage),
            "exists": storage.exists(),
        },
        "media": {
            "path": str(media_root),
            "exists": Path(media_root).is_dir(),
            "writable": media_ok,
            "detail": media_detail,
        },
        "libraries": [{"name": name, "version": _pkg_version(name)} for name in _APP_PACKAGES],
    }


async def _probe_anilibria(db: Session, *, has_token: bool) -> dict[str, Any]:
    client = build_anilibria_client(db)
    result: dict[str, Any] = {
        "ok": False,
        "detail": "",
        "api_payload": None,
        "token_valid": None,
    }
    try:
        payload = await client.health()
        result["ok"] = True
        result["detail"] = "API отвечает"
        result["api_payload"] = payload if isinstance(payload, (dict, list, str, int, float, bool)) else str(payload)
    except Exception as exc:
        result["detail"] = str(exc)
        return result

    if has_token:
        try:
            profile = await client.get_my_profile(include=["id", "login"])
            result["token_valid"] = True
            if isinstance(profile, dict):
                login = profile.get("login") or (profile.get("user") or {}).get("login")
                if login:
                    result["profile_login"] = str(login)
        except Exception as exc:
            result["token_valid"] = False
            result["token_error"] = str(exc)
    return result


async def _probe_telegram(db: Session) -> dict[str, Any]:
    enabled = is_telegram_enabled(db)
    token = get_telegram_bot_token(db)
    chat_id = get_telegram_chat_id(db)
    base_url = resolve_telegram_bot_api_base(db)
    has_token = bool(token)
    has_chat_id = bool(chat_id)

    tracked_count = db.scalar(
        select(func.count()).select_from(TrackedRelease).where(TrackedRelease.enabled.is_(True))
    ) or 0
    pending_outbox = db.scalar(
        select(func.count()).select_from(TelegramOutbox).where(TelegramOutbox.status == OUTBOX_PENDING)
    ) or 0

    bot_process = _telegram_bot_process_status(db)
    api: dict[str, Any] = {
        "ok": False,
        "detail": "Токен не задан",
        "username": None,
        "bot_id": None,
    }
    if has_token:
        try:
            me = await test_telegram_get_me(token=token, base_url=base_url)
            username = me.get("username")
            api = {
                "ok": True,
                "detail": "API отвечает",
                "username": f"@{username}" if username else None,
                "bot_id": me.get("id"),
            }
        except Exception as exc:
            api = {
                "ok": False,
                "detail": str(exc),
                "username": None,
                "bot_id": None,
            }

    configured = has_token and has_chat_id
    if not enabled:
        bot_detail = "Выключен в настройках"
        bot_ok = False
    elif not configured:
        missing = []
        if not has_token:
            missing.append("токен")
        if not has_chat_id:
            missing.append("chat_id")
        bot_detail = "Не настроен: " + ", ".join(missing)
        bot_ok = False
    elif bot_process["ok"] is True:
        bot_detail = "Сервис работает"
        bot_ok = True
    elif bot_process["ok"] is False:
        bot_detail = bot_process["detail"]
        bot_ok = False
    else:
        bot_detail = "Настроен (heartbeat ещё не получен)"
        bot_ok = False

    return {
        "enabled": enabled,
        "configured": configured,
        "has_token": has_token,
        "has_chat_id": has_chat_id,
        "base_url": base_url,
        "tracked_count": int(tracked_count),
        "pending_outbox": int(pending_outbox),
        "bot": {
            "ok": bot_ok,
            "detail": bot_detail,
            "heartbeat_at": bot_process.get("heartbeat_at"),
            "heartbeat_age_sec": bot_process.get("age_sec"),
        },
        "api": api,
    }


async def _probe_video_kensetsu(db: Session) -> dict[str, Any]:
    enabled = is_video_kensetsu_enabled(db)
    base_url = resolve_video_kensetsu_base_url(db)
    configured = bool(base_url)
    result: dict[str, Any] = {
        "enabled": enabled,
        "configured": configured,
        "base_url": base_url or None,
        "ok": False,
        "detail": "",
    }
    if not enabled:
        result["detail"] = "Выключен в настройках"
        video_kensetsu_store_health_cache(db, False, commit=True)
        return result
    if not configured:
        result["detail"] = "URL не задан"
        video_kensetsu_store_health_cache(db, False, commit=True)
        return result
    try:
        await video_kensetsu_health(base_url, timeout_sec=5.0)
        result["ok"] = True
        result["detail"] = "HTTP 200"
        video_kensetsu_store_health_cache(db, True, commit=True)
    except Exception as exc:
        result["detail"] = str(exc)
        video_kensetsu_store_health_cache(db, False, commit=True)
    return result


def _telegram_bot_process_status(db: Session) -> dict[str, Any]:
    from datetime import datetime, timezone

    raw = get_setting_value(db, "telegram_bot_heartbeat_at", "").strip()
    if not raw:
        return {"ok": None, "detail": "Нет heartbeat", "heartbeat_at": None, "age_sec": None}
    try:
        cleaned = raw.rstrip("Z")
        hb = datetime.fromisoformat(cleaned)
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - hb).total_seconds()
    except Exception:
        return {"ok": False, "detail": f"Битый heartbeat: {raw}", "heartbeat_at": raw, "age_sec": None}
    if age <= _TELEGRAM_HEARTBEAT_STALE_SEC:
        return {
            "ok": True,
            "detail": "Heartbeat свежий",
            "heartbeat_at": raw,
            "age_sec": int(age),
        }
    return {
        "ok": False,
        "detail": f"Heartbeat устарел ({int(age)}с)",
        "heartbeat_at": raw,
        "age_sec": int(age),
    }


def _probe_database(db: Session) -> dict[str, Any]:
    try:
        db.execute(text("SELECT 1"))
        alembic = db.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar()
        return {
            "ok": True,
            "detail": "Подключено",
            "alembic_revision": alembic or "—",
        }
    except Exception as exc:
        return {
            "ok": False,
            "detail": str(exc),
            "alembic_revision": "—",
        }


def _qb_credentials(db: Session, role: str) -> dict[str, Any] | None:
    """Собирает креды на текущем потоке (без сетевых вызовов)."""
    client = db.scalar(select(QbClient).where(QbClient.role == role, QbClient.enabled.is_(True)).limit(1))
    if client is not None:
        return {
            "configured": True,
            "name": client.name,
            "host": client.host,
            "port": client.port,
            "username": client.username,
            "password": client.password_encrypted,
        }

    host = get_setting_value(db, f"qb_{role}_host", "")
    port_raw = get_setting_value(db, f"qb_{role}_port", "8080")
    username = get_setting_value(db, f"qb_{role}_username", "")
    password = get_setting_value(db, f"qb_{role}_password", "")
    if not host.strip():
        return None
    try:
        port = int(port_raw or "8080")
    except ValueError:
        port = 8080
    return {
        "configured": True,
        "name": None,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
    }


def _probe_qb_creds(creds: dict[str, Any] | None) -> dict[str, Any]:
    if creds is None:
        return {
            "configured": False,
            "ok": False,
            "detail": "Не настроен",
            "host": "",
            "port": None,
            "version": "—",
            "webapi": "—",
            "torrents": None,
        }
    return _run_qb_test(
        host=creds["host"],
        port=int(creds["port"]),
        username=creds["username"],
        password=creds["password"],
        configured=True,
        name=creds.get("name"),
    )


def _run_qb_test(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    configured: bool,
    name: str | None = None,
) -> dict[str, Any]:
    base = {
        "configured": configured,
        "name": name,
        "host": host,
        "port": port,
        "ok": False,
        "detail": "",
        "version": "—",
        "webapi": "—",
        "torrents": None,
    }
    try:
        result = test_qb_connection(host=host, port=port, username=username, password=password)
        base.update(
            {
                "ok": True,
                "detail": "Подключено",
                "version": str(result.get("version") or "—"),
                "webapi": str(result.get("webapi") or "—"),
                "torrents": result.get("torrents"),
            }
        )
    except Exception as exc:
        base["detail"] = str(exc)
    return base
