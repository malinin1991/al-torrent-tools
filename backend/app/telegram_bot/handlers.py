"""Обработчики команд Telegram-бота: /start /add /del /list."""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram import Update
from telegram.ext import ContextTypes

from app.db.models import TrackedRelease
from app.db.session import SessionLocal
from app.services.runtime_settings import build_anilibria_client, get_setting_value
from app.services.telegram_notify import (
    SOURCE_BOT,
    disable_tracked_by_alias,
    escape_markdown_v2,
    upsert_tracked_release,
)

logger = logging.getLogger(__name__)


def _allowed_chat_id(db: Session) -> str:
    return get_setting_value(db, "telegram_chat_id", "").strip()


async def check_access(update: Update) -> bool:
    if update.effective_chat is None or update.message is None:
        return False
    with SessionLocal() as db:
        allowed = _allowed_chat_id(db)
    if not allowed:
        await update.message.reply_text("⛔ Chat ID не настроен в al-torrent-tools")
        return False
    try:
        allowed_id = int(allowed)
    except ValueError:
        await update.message.reply_text("⛔ Некорректный telegram_chat_id в настройках")
        return False
    if update.effective_chat.id != allowed_id:
        await update.message.reply_text("⛔ Доступ запрещён!")
        logger.warning("Unauthorized access from chat_id=%s", update.effective_chat.id)
        return False
    return True


def extract_alias(text: str) -> str:
    match = re.search(r"/release/([^/\s?#]+)", text or "")
    if not match:
        return (text or "").strip().lower()
    return match.group(1).strip().lower()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update) or update.message is None:
        return
    await update.message.reply_text(
        "🚀 AL Torrent Tools Bot\n\n"
        "Команды:\n"
        "/add <url|alias> — добавить релиз в отслеживание\n"
        "/del <url|alias> — отключить отслеживание\n"
        "/list — список отслеживаемых\n"
        "/help — помощь"
    )


async def add_alias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update) or update.message is None:
        return
    if not context.args:
        await update.message.reply_text("❌ Не указан URL или alias")
        return

    alias = extract_alias(" ".join(context.args))
    if not alias:
        await update.message.reply_text("❌ Не удалось извлечь alias")
        return

    try:
        with SessionLocal() as db:
            existing = db.scalar(
                select(TrackedRelease).where(TrackedRelease.release_alias.ilike(alias)).limit(1)
            )
            if existing is not None and existing.enabled:
                title = existing.title or existing.release_alias
                await update.message.reply_text(
                    f"⚠️ [{escape_markdown_v2(title)}]"
                    f"(https://anilibria\\.top/anime/releases/release/{escape_markdown_v2(existing.release_alias)}) "
                    "уже отслеживается\\!",
                    parse_mode="MarkdownV2",
                    disable_web_page_preview=True,
                )
                return

            client = build_anilibria_client(db)
            data = await client.get_release(alias, include=["id", "alias", "name"])
            if not isinstance(data, dict) or not isinstance(data.get("id"), int):
                await update.message.reply_text("❌ Релиз не найден в AniLibria API")
                return

            release_id = int(data["id"])
            api_alias = data.get("alias") if isinstance(data.get("alias"), str) else alias
            title = _title_from_release(data) or api_alias or alias
            row = upsert_tracked_release(
                db,
                release_id=release_id,
                release_alias=str(api_alias or alias),
                title=title,
                source=SOURCE_BOT,
                enabled=True,
            )
            await update.message.reply_text(f"✅ Добавлен: {row.title}")
    except Exception as exc:
        logger.exception("Ошибка /add")
        await update.message.reply_text(f"❌ Ошибка: {exc}")


async def del_alias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update) or update.message is None:
        return
    if not context.args:
        await update.message.reply_text("❌ Не указан URL или alias")
        return

    alias = extract_alias(" ".join(context.args))
    try:
        with SessionLocal() as db:
            row = disable_tracked_by_alias(db, alias)
            if row is None:
                await update.message.reply_text("❌ Релиз не найден в отслеживании")
                return
            await update.message.reply_text(f"✅ Отключен: {row.title or row.release_alias}")
    except Exception as exc:
        logger.exception("Ошибка /del")
        await update.message.reply_text(f"❌ Ошибка: {exc}")


async def list_aliases(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update) or update.message is None:
        return
    try:
        with SessionLocal() as db:
            rows = list(
                db.scalars(
                    select(TrackedRelease)
                    .where(TrackedRelease.enabled.is_(True))
                    .order_by(TrackedRelease.title.asc())
                ).all()
            )
            if not rows:
                await update.message.reply_text("📭 Список пуст")
                return
            lines = [
                f"▫️ [{escape_markdown_v2(a.title or a.release_alias)}]"
                f"(https://anilibria\\.top/anime/releases/release/{escape_markdown_v2(a.release_alias)})"
                for a in rows
            ]
            await update.message.reply_text(
                "📚 Отслеживаемые релизы:\n\n" + "\n".join(lines),
                parse_mode="MarkdownV2",
                disable_web_page_preview=True,
            )
    except Exception as exc:
        logger.exception("Ошибка /list")
        await update.message.reply_text(f"❌ Ошибка: {exc}")


def _title_from_release(data: dict[str, Any]) -> str:
    name = data.get("name")
    if isinstance(name, dict):
        main = name.get("main")
        if isinstance(main, str) and main.strip():
            return main.strip()
    if isinstance(name, str) and name.strip():
        return name.strip()
    return ""
