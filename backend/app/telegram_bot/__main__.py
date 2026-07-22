"""Сервис telegram-bot: polling команд + drain outbox + heartbeat."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from telegram.error import InvalidToken, TelegramError
from telegram.ext import Application, CommandHandler

from app.db.session import SessionLocal
from app.services.runtime_settings import get_setting_value
from app.services.telegram_notify import is_telegram_enabled, resolve_telegram_bot_api_base
from app.services.telegram_outbox import drain_outbox
from app.telegram_bot.handlers import add_alias, del_alias, list_aliases, start, update_tracked

logger = logging.getLogger(__name__)

HEALTH_FILE = Path(os.environ.get("ALTT_TELEGRAM_HEALTH_FILE", "/tmp/altt_telegram_healthy"))
OUTBOX_INTERVAL_SEC = float(os.environ.get("ALTT_TELEGRAM_OUTBOX_INTERVAL_SEC", "15"))
HEARTBEAT_INTERVAL_SEC = float(os.environ.get("ALTT_TELEGRAM_HEARTBEAT_INTERVAL_SEC", "30"))
CONFIG_RETRY_SEC = float(os.environ.get("ALTT_TELEGRAM_CONFIG_RETRY_SEC", "30"))


def _touch_heartbeat() -> None:
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_FILE.write_text("ok", encoding="utf-8")
    # Для страницы «Информация» (API в другом контейнере не видит /tmp бота).
    try:
        from app.utils.datetime_fmt import utcnow
        from app.db.models import Setting

        with SessionLocal() as db:
            row = db.get(Setting, "telegram_bot_heartbeat_at")
            now = utcnow().replace(microsecond=0).isoformat() + "Z"
            if row is None:
                db.add(Setting(key="telegram_bot_heartbeat_at", value=now))
            else:
                row.value = now
            db.commit()
    except Exception:
        logger.exception("Не удалось записать telegram_bot_heartbeat_at")


def ptb_bot_api_base_url(api_root: str) -> str:
    """PTB склеивает base_url + token + /method → нужен суффикс /bot БЕЗ хвостового слэша.

    Иначе получится .../bot/TOKEN/... (404) вместо .../botTOKEN/...
    """
    root = (api_root or "").strip().rstrip("/")
    if not root:
        root = "https://api.telegram.org"
    if root.endswith("/bot"):
        return root
    return f"{root}/bot"


def _load_bot_config() -> tuple[str, str]:
    with SessionLocal() as db:
        token = get_setting_value(db, "telegram_bot_token", "").strip()
        base_url = resolve_telegram_bot_api_base(db)
        enabled = is_telegram_enabled(db)
    if not enabled:
        raise RuntimeError("Telegram выключен в настройках (telegram_enabled=false)")
    if not token:
        raise RuntimeError("Не задан telegram_bot_token в настройках")
    return token, base_url


async def _outbox_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            with SessionLocal() as db:
                if is_telegram_enabled(db):
                    stats = await drain_outbox(db)
                    if stats.get("sent") or stats.get("failed"):
                        logger.info("Outbox drain: %s", stats)
        except Exception:
            logger.exception("Ошибка drain outbox")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=OUTBOX_INTERVAL_SEC)
        except asyncio.TimeoutError:
            pass


async def _heartbeat_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            _touch_heartbeat()
        except Exception:
            logger.exception("Не удалось обновить heartbeat")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_INTERVAL_SEC)
        except asyncio.TimeoutError:
            pass


async def _shutdown_application(application: Application | None) -> None:
    if application is None:
        return
    try:
        updater = application.updater
        if updater is not None and updater.running:
            await updater.stop()
        if application.running:
            await application.stop()
        await application.shutdown()
    except Exception:
        logger.exception("Ошибка остановки Telegram application")


async def run_bot() -> None:
    from app.logging_filters import setup_redacted_logging

    setup_redacted_logging(level=logging.INFO)
    stop_event = asyncio.Event()
    heartbeat_task = asyncio.create_task(_heartbeat_loop(stop_event))
    outbox_task = asyncio.create_task(_outbox_loop(stop_event))

    application: Application | None = None
    try:
        while True:
            try:
                token, api_root = _load_bot_config()
            except RuntimeError as exc:
                logger.warning("%s — повтор через %.0fс", exc, CONFIG_RETRY_SEC)
                await asyncio.sleep(CONFIG_RETRY_SEC)
                continue

            ptb_base = ptb_bot_api_base_url(api_root)
            builder = Application.builder().token(token).base_url(ptb_base)
            application = builder.build()
            application.add_handler(CommandHandler("start", start))
            application.add_handler(CommandHandler("help", start))
            application.add_handler(CommandHandler("add", add_alias))
            application.add_handler(CommandHandler("del", del_alias))
            application.add_handler(CommandHandler("list", list_aliases))
            application.add_handler(CommandHandler("update", update_tracked))

            logger.info("Запуск Telegram polling (ptb_base_url=%s)", ptb_base)
            try:
                await application.initialize()
                await application.start()
                assert application.updater is not None
                await application.updater.start_polling(drop_pending_updates=True)
            except InvalidToken:
                logger.error(
                    "Telegram API отклонил токен (InvalidToken). "
                    "Проверьте токен в Настройках. Повтор через %.0fс.",
                    CONFIG_RETRY_SEC,
                )
                await _shutdown_application(application)
                application = None
                await asyncio.sleep(CONFIG_RETRY_SEC)
                continue
            except TelegramError as exc:
                logger.error(
                    "Telegram API отклонил подключение (%s). "
                    "Проверьте токен и Bot API base URL в Настройках. Повтор через %.0fс.",
                    type(exc).__name__,
                    CONFIG_RETRY_SEC,
                )
                await _shutdown_application(application)
                application = None
                await asyncio.sleep(CONFIG_RETRY_SEC)
                continue

            # Держим polling, пока не остановят процесс
            while True:
                await asyncio.sleep(3600)

    except asyncio.CancelledError:
        raise
    finally:
        stop_event.set()
        for task in (outbox_task, heartbeat_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await _shutdown_application(application)


def main() -> None:
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
