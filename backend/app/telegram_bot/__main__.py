"""Сервис telegram-bot: polling команд + drain outbox + heartbeat."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from telegram.ext import Application, CommandHandler

from app.db.session import SessionLocal
from app.services.runtime_settings import get_setting_value
from app.services.telegram_notify import is_telegram_enabled, resolve_telegram_bot_api_base
from app.services.telegram_outbox import drain_outbox
from app.telegram_bot.handlers import add_alias, del_alias, list_aliases, start

logger = logging.getLogger(__name__)

HEALTH_FILE = Path(os.environ.get("ALTT_TELEGRAM_HEALTH_FILE", "/tmp/altt_telegram_healthy"))
OUTBOX_INTERVAL_SEC = float(os.environ.get("ALTT_TELEGRAM_OUTBOX_INTERVAL_SEC", "15"))
HEARTBEAT_INTERVAL_SEC = float(os.environ.get("ALTT_TELEGRAM_HEARTBEAT_INTERVAL_SEC", "30"))


def _touch_heartbeat() -> None:
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_FILE.write_text("ok", encoding="utf-8")


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


async def run_bot() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        level=logging.INFO,
    )
    stop_event = asyncio.Event()
    heartbeat_task = asyncio.create_task(_heartbeat_loop(stop_event))
    outbox_task = asyncio.create_task(_outbox_loop(stop_event))

    application: Application | None = None
    try:
        while True:
            try:
                token, base_url = _load_bot_config()
            except RuntimeError as exc:
                logger.warning("%s — повтор через 30с", exc)
                await asyncio.sleep(30)
                continue

            builder = Application.builder().token(token)
            # PTB: base_url должен заканчиваться на /bot (библиотека добавляет token/...)
            # Стандарт: https://api.telegram.org/bot
            if base_url.rstrip("/").endswith("/bot"):
                builder = builder.base_url(base_url if base_url.endswith("/") else base_url + "/")
            else:
                api_root = base_url.rstrip("/") + "/bot"
                builder = builder.base_url(api_root if api_root.endswith("/") else api_root + "/")

            application = builder.build()
            application.add_handler(CommandHandler("start", start))
            application.add_handler(CommandHandler("help", start))
            application.add_handler(CommandHandler("add", add_alias))
            application.add_handler(CommandHandler("del", del_alias))
            application.add_handler(CommandHandler("list", list_aliases))

            logger.info("Запуск Telegram polling (base_url=%s)", base_url)
            await application.initialize()
            await application.start()
            assert application.updater is not None
            await application.updater.start_polling(drop_pending_updates=True)

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
        if application is not None:
            try:
                if application.updater is not None:
                    await application.updater.stop()
                await application.stop()
                await application.shutdown()
            except Exception:
                logger.exception("Ошибка остановки Telegram application")


def main() -> None:
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
