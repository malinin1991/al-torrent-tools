"""Сбор краткого статуса системы для страницы «Информация»."""

from __future__ import annotations

import asyncio
import platform
import sys
from importlib.metadata import PackageNotFoundError, version as pkg_version
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import QbClient
from app.services.qbittorrent import test_qb_connection
from app.services.runtime_settings import build_anilibria_client, get_setting_value, resolve_anilibria_settings
from app.services.torrent_archive import resolve_torrent_storage_root

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
    "pydantic-settings",
    "pydantic",
    "python-multipart",
)


def _pkg_version(name: str) -> str:
    try:
        return pkg_version(name)
    except PackageNotFoundError:
        return "—"


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

    storage = resolve_torrent_storage_root()
    return {
        "app": {
            "name": settings.app_name,
            "env": settings.app_env,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
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
        "storage": {
            "path": str(storage),
            "exists": storage.exists(),
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
