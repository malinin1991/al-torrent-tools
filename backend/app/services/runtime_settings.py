from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Setting
from app.providers.anilibria.client import AniLibriaClient

# Секреты не отдаём наружу через API в открытом виде.
SECRET_SETTING_KEYS = frozenset(
    {
        "anilibria_bearer_token",
        "anilibria_passkey",
        "anilibria_password",
        "qb_master_password",
        "qb_slave_password",
        "telegram_bot_token",
        "telegram_hevc_bot_token",
    }
)


@dataclass(frozen=True, slots=True)
class AniLibriaRuntimeSettings:
    base_url: str
    fallback_base_url: str
    bearer_token: str
    passkey: str
    request_retries: int
    retry_delay_ms: int


@dataclass(frozen=True, slots=True)
class TelegramBotRuntimeSettings:
    bot_key: str
    enabled: bool
    token: str
    api_base_url: str
    heartbeat_key: str


def get_setting_value(
    db: Session | None, key: str, default: str = "", *, allow_empty: bool = False
) -> str:
    """Значение из БД с fallback на default.

    По умолчанию пустая строка в БД не перекрывает default (удобно для URL API).
    ``allow_empty=True`` — пустое значение из БД считается валидным (например шаблон,
    где пусто = «выключено»).
    """
    if db is None:
        return default
    row = db.get(Setting, key)
    if row is None:
        return default
    value = row.value if isinstance(row.value, str) else str(row.value)
    if not value.strip():
        return "" if allow_empty else default
    return value


def resolve_anilibria_settings(db: Session | None = None) -> AniLibriaRuntimeSettings:
    """DB override → env/default из Settings."""
    return AniLibriaRuntimeSettings(
        base_url=get_setting_value(db, "anilibria_base_url", settings.anilibria_base_url),
        fallback_base_url=get_setting_value(
            db, "anilibria_fallback_base_url", settings.anilibria_fallback_base_url
        ),
        bearer_token=get_setting_value(db, "anilibria_bearer_token", settings.anilibria_bearer_token),
        passkey=get_setting_value(db, "anilibria_passkey", settings.anilibria_passkey),
        request_retries=settings.anilibria_request_retries,
        retry_delay_ms=settings.anilibria_retry_delay_ms,
    )


def build_anilibria_client(db: Session | None = None) -> AniLibriaClient:
    resolved = resolve_anilibria_settings(db)
    return AniLibriaClient(
        base_url=resolved.base_url,
        fallback_base_url=resolved.fallback_base_url,
        bearer_token=resolved.bearer_token,
        passkey=resolved.passkey,
        request_retries=resolved.request_retries,
        retry_delay_ms=resolved.retry_delay_ms,
    )


def resolve_telegram_bot_settings(
    db: Session | None,
    bot_key: str,
) -> TelegramBotRuntimeSettings:
    """Настройки primary/hevc без смешивания токенов и heartbeat."""
    normalized = (bot_key or "primary").strip().lower()
    if normalized == "primary":
        prefix = "telegram"
        heartbeat_key = "telegram_bot_heartbeat_at"
    elif normalized == "hevc":
        prefix = "telegram_hevc"
        heartbeat_key = "telegram_hevc_bot_heartbeat_at"
    else:
        raise ValueError(f"Неизвестный профиль Telegram-бота: {bot_key}")
    enabled = get_setting_value(db, f"{prefix}_enabled", "false").strip().lower()
    return TelegramBotRuntimeSettings(
        bot_key=normalized,
        enabled=enabled in {"1", "true", "yes", "on"},
        token=get_setting_value(db, f"{prefix}_bot_token", "").strip(),
        api_base_url=get_setting_value(db, f"{prefix}_bot_api_base_url", "").strip(),
        heartbeat_key=heartbeat_key,
    )


def upsert_setting(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def mask_settings_dict(raw: dict[str, str]) -> dict[str, str]:
    masked: dict[str, str] = {}
    for key, value in raw.items():
        if key in SECRET_SETTING_KEYS:
            masked[key] = "***" if value else ""
        else:
            masked[key] = value
    return masked
