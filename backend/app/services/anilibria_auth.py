from sqlalchemy.orm import Session

from app.db.models import Setting
from app.services.runtime_settings import build_anilibria_client, upsert_setting


async def login_and_store_token(db: Session, login: str, password: str) -> str:
    """Вход в AniLibria API и сохранение bearer token + passkey в settings."""
    login_value = login.strip()
    if not login_value:
        raise ValueError("Укажите логин AniLibria")
    if not password.strip():
        raise ValueError("Укажите пароль AniLibria")

    client = build_anilibria_client(db)
    token = await client.auth_login(login_value, password)

    for key, value in (
        ("anilibria_login", login_value),
        ("anilibria_password", password),
        ("anilibria_bearer_token", token),
    ):
        upsert_setting(db, key, value)

    passkey = await client.get_my_passkey()
    if passkey:
        upsert_setting(db, "anilibria_passkey", passkey)

    db.commit()
    return token


def resolve_anilibria_password(db: Session | None, form_password: str = "") -> str:
    if form_password.strip():
        return form_password
    if db is None:
        return ""
    from app.services.runtime_settings import get_setting_value

    return get_setting_value(db, "anilibria_password", "")


async def ensure_passkey_stored(db: Session) -> str | None:
    """Если passkey нет в БД, но есть token — подтянуть из профиля и сохранить."""
    from app.services.runtime_settings import get_setting_value

    existing = get_setting_value(db, "anilibria_passkey", "")
    if existing.strip():
        return existing.strip()

    client = build_anilibria_client(db)
    if not client.bearer_token:
        return None
    passkey = await client.get_my_passkey()
    if passkey:
        upsert_setting(db, "anilibria_passkey", passkey)
        db.commit()
    return passkey
