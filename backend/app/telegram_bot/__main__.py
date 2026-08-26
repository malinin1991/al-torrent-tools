"""Сервис telegram-bot: polling команд + drain outbox + heartbeat."""

from __future__ import annotations

import asyncio
import argparse
import logging
import os
from pathlib import Path

from telegram import Update
from telegram.error import InvalidToken, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from app.db.session import SessionLocal
from app.services.runtime_settings import resolve_telegram_bot_settings
from app.services.telegram_notify import normalize_telegram_bot_api_base
from app.services.telegram_outbox import drain_outbox
from app.telegram_bot.handlers import add_alias, del_alias, list_aliases, start, update_tracked

logger = logging.getLogger(__name__)

OUTBOX_INTERVAL_SEC = float(os.environ.get("ALTT_TELEGRAM_OUTBOX_INTERVAL_SEC", "15"))
HEARTBEAT_INTERVAL_SEC = float(os.environ.get("ALTT_TELEGRAM_HEARTBEAT_INTERVAL_SEC", "30"))
CONFIG_RETRY_SEC = float(os.environ.get("ALTT_TELEGRAM_CONFIG_RETRY_SEC", "30"))


def _health_file(bot_key: str) -> Path:
    default = (
        "/tmp/altt_telegram_hevc_healthy"
        if bot_key == "hevc"
        else "/tmp/altt_telegram_healthy"
    )
    return Path(os.environ.get("ALTT_TELEGRAM_HEALTH_FILE", default))


def _touch_heartbeat(bot_key: str) -> None:
    health_file = _health_file(bot_key)
    health_file.parent.mkdir(parents=True, exist_ok=True)
    health_file.write_text("ok", encoding="utf-8")
    # Для страницы «Информация» (API в другом контейнере не видит /tmp бота).
    try:
        from app.utils.datetime_fmt import utcnow
        from app.db.models import Setting

        with SessionLocal() as db:
            config = resolve_telegram_bot_settings(db, bot_key)
            row = db.get(Setting, config.heartbeat_key)
            now = utcnow().replace(microsecond=0).isoformat() + "Z"
            if row is None:
                db.add(Setting(key=config.heartbeat_key, value=now))
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


def _load_bot_config(bot_key: str) -> tuple[str, str]:
    with SessionLocal() as db:
        config = resolve_telegram_bot_settings(db, bot_key)
    if not config.enabled:
        raise RuntimeError(f"Telegram {bot_key} выключен в настройках")
    if not config.token:
        raise RuntimeError(f"Не задан токен Telegram-профиля {bot_key}")
    return config.token, normalize_telegram_bot_api_base(config.api_base_url)


async def _outbox_loop(stop_event: asyncio.Event, bot_key: str) -> None:
    while not stop_event.is_set():
        try:
            with SessionLocal() as db:
                config = resolve_telegram_bot_settings(db, bot_key)
                if config.enabled:
                    stats = await drain_outbox(db, bot_key=bot_key)
                    if stats.get("sent") or stats.get("failed"):
                        logger.info("Outbox drain: %s", stats)
        except Exception:
            logger.exception("Ошибка drain outbox")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=OUTBOX_INTERVAL_SEC)
        except asyncio.TimeoutError:
            pass


async def _heartbeat_loop(stop_event: asyncio.Event, bot_key: str) -> None:
    while not stop_event.is_set():
        try:
            _touch_heartbeat(bot_key)
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


def _register_handlers(application: Application, bot_key: str) -> None:
    if bot_key == "hevc":
        from app.telegram_bot.hevc_handlers import (
            addressed_text,
            error,
            group_membership,
            overdue,
            start as hevc_start,
            status,
            status_callback,
            unknown_command,
        )

        application.add_handler(
            ChatMemberHandler(group_membership, ChatMemberHandler.MY_CHAT_MEMBER)
        )
        application.add_handler(CommandHandler("start", hevc_start))
        application.add_handler(CommandHandler("help", hevc_start))
        application.add_handler(CommandHandler("overdue", overdue))
        application.add_handler(CommandHandler("status", status))
        application.add_handler(CommandHandler("error", error))
        application.add_handler(
            CallbackQueryHandler(status_callback, pattern=r"^status:\d+$")
        )
        application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, addressed_text)
        )
        return
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("add", add_alias))
    application.add_handler(CommandHandler("del", del_alias))
    application.add_handler(CommandHandler("list", list_aliases))
    application.add_handler(CommandHandler("update", update_tracked))


async def run_bot(bot_key: str = "primary") -> None:
    from app.logging_filters import setup_redacted_logging

    setup_redacted_logging(level=logging.INFO)
    bot_key = bot_key.strip().lower()
    if bot_key not in {"primary", "hevc"}:
        raise ValueError(f"Неизвестный профиль Telegram-бота: {bot_key}")
    stop_event = asyncio.Event()
    heartbeat_task = asyncio.create_task(_heartbeat_loop(stop_event, bot_key))
    outbox_task = asyncio.create_task(_outbox_loop(stop_event, bot_key))

    application: Application | None = None
    try:
        while True:
            try:
                token, api_root = _load_bot_config(bot_key)
            except RuntimeError as exc:
                logger.warning("%s — повтор через %.0fс", exc, CONFIG_RETRY_SEC)
                await asyncio.sleep(CONFIG_RETRY_SEC)
                continue

            ptb_base = ptb_bot_api_base_url(api_root)
            builder = Application.builder().token(token).base_url(ptb_base)
            application = builder.build()
            _register_handlers(application, bot_key)

            logger.info(
                "Запуск Telegram polling (profile=%s, ptb_base_url=%s)",
                bot_key,
                ptb_base,
            )
            try:
                await application.initialize()
                await application.start()
                assert application.updater is not None
                await application.updater.start_polling(
                    drop_pending_updates=True,
                    allowed_updates=Update.ALL_TYPES if bot_key == "hevc" else None,
                )
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
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        choices=("primary", "hevc"),
        default=os.environ.get("ALTT_TELEGRAM_PROFILE", "primary"),
    )
    args = parser.parse_args()
    asyncio.run(run_bot(args.profile))


if __name__ == "__main__":
    main()
