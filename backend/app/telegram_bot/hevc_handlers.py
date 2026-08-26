"""Handlers второго Telegram-бота: ACL, HEVC-списки и детали."""

from __future__ import annotations

import logging
import random
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.db.session import SessionLocal
from app.services.hevc_bot import (
    format_release_detail,
    query_hevc_statuses,
    query_release_detail,
    split_release_list,
    truncate_telegram_text,
)
from app.services.telegram_access import HEVC_BOT_KEY, check_access

logger = logging.getLogger(__name__)

RANDOM_REPLIES = (
    "Ну привет, мой хороший 🥰 Соскучился? 😊",
    "О? 😏 Решил со мной заговорить? А я думала, ты вечно будешь прятаться по углам 🤭",
    "Эй! 😾 Ты что, пальцем меня тыкаешь? Хочешь, чтобы я ответила тем же? 😼",
    "Чего тебе? 😒 Только не говори, что опять забыл про дедлайн 😩",
    "Опять ты? 😅 Ну давай, рассказывай, что натворил на этот раз 🫣",
    "Я всегда рада тебя видеть 💕 Даже если ты пришёл с очередной проблемой 😌",
    "О, какая неожиданность 😳 Ты сам ко мне подошёл? Солнце с запада встало? 🌞",
    "Ай! 😣 Ну и манеры. Хочешь, чтобы я тебя тоже пощекотала? 🪶",
    "Ты опять отвлекаешься? 🙄 А кто видео кодировать будет? 🎬",
    "Не переживай, я рядом 🤗 Даже если ты снова всё запорол 😅",
    "Отстань, я занята 😤 Наблюдаю за твоими провалами 🍿",
    "Хм? 🤨 Ты что-то сказал? Я слишком увлеклась мыслью, как тебя подколоть 😈",
    "Такой серьёзный пришёл 😼 Неужели соскучился по моим шуткам? 😏",
    "Прекрати тыкать, а то укушу 😾 Шучу... или нет 😼",
    "Ну? 😬 Говори уже, а то я от нетерпения начну придумывать, что ты хочешь сказать 😜",
    "О, ты снова существуешь? 🙃 А я уж думала, ты пропал навсегда 🕵️‍♀️",
    "Ты сегодня какой-то тихий 🤔 Что-то случилось? Или просто боишься меня? 👀",
    "Ты покраснел? 😳 От одного моего взгляда? Это так мило 🥰",
    "Эй, полегче! 😠 Я не подушка, чтобы в меня тыкать 🛏️",
    "Чего надо? 😼 Если по делу — говори, а если просто поболтать — тогда тем более 💬",
    "Я скучала 🥺 Ты долго не появлялся. Наверное, опять что-то натворил? 😅",
    "Ты пришёл за советом или чтобы я снова угадала твои мысли? 🔮",
    "Ой, щекотно! 😆 Ну всё, ты сам напросился 😈",
    "Ты же знаешь, что я всё равно окажусь на шаг впереди, так зачем начинаешь? 😏👣",
    "Мой дорогой неудачник 🥴 Я так рада тебя видеть 💖",
    "Хм, 48x48? Уже достаёшь ту самую аудиодорожку? Смело... и очень самоуверенно 😼",
    "Фокси, тебе сейчас перепадёт. И не потому что я злая, а потому что ты слишком милый, когда бесишься 😈",
)


async def _bot_username(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    username = getattr(context.bot, "username", None)
    if not isinstance(username, str) or not username.strip():
        bot = await context.bot.get_me()
        username = getattr(bot, "username", None)
    if not isinstance(username, str):
        return None
    return username.strip().lstrip("@").casefold() or None


def _mentions_bot(text: str, username: str) -> bool:
    return bool(
        re.search(
            rf"(?<![\w@])@{re.escape(username)}(?!\w)",
            text,
            flags=re.IGNORECASE,
        )
    )


def _command_targets_bot(text: str, username: str | None) -> bool:
    command = text.split(maxsplit=1)[0]
    _, separator, target = command.partition("@")
    return not separator or (username is not None and target.casefold() == username)


async def unknown_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    username = await _bot_username(context)
    if not _command_targets_bot(message.text or "", username):
        return
    if not await check_hevc_access(update):
        return
    await message.reply_text(random.choice(RANDOM_REPLIES))


async def addressed_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    if chat.type != "private":
        username = await _bot_username(context)
        if username is None or not _mentions_bot(message.text or "", username):
            return
    if not await check_hevc_access(update):
        return
    await message.reply_text(random.choice(RANDOM_REPLIES))


async def check_hevc_access(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return False
    is_private = chat.type == "private"
    with SessionLocal() as db:
        decision = check_access(
            db,
            bot_key=HEVC_BOT_KEY,
            user_id=int(user.id),
            username=user.username,
            user_title=" ".join(
                part for part in (user.first_name, user.last_name) if part
            ),
            chat_id=None if is_private else int(chat.id),
            chat_title=chat.title,
            is_private=is_private,
        )
    if decision.allowed:
        return True
    text = (
        "⏳ Заявка на доступ зарегистрирована и ожидает одобрения."
        if decision.waiting_for_approval
        else "⛔ Доступ запрещён администратором."
    )
    if update.callback_query is not None:
        await update.callback_query.answer(text, show_alert=True)
    elif update.message is not None:
        await update.message.reply_text(text)
    elif context is not None and not is_private:
        await context.bot.send_message(chat_id=chat.id, text=text)
    return False


async def group_membership(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Регистрирует pending сразу при добавлении HEVC-бота в группу."""
    membership = update.my_chat_member
    if membership is None:
        return
    if membership.new_chat_member.status not in {"member", "administrator"}:
        return
    await check_hevc_access(update, context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_hevc_access(update) or update.message is None:
        return
    await update.message.reply_text(
        "🎞 HEVC Status Bot\n\n"
        "/overdue — просроченные AVC\n"
        "/overdue <nickname> — просрочка точного исполнителя\n"
        "/status — ожидающие HEVC\n"
        "/status <release_id> — детали релиза\n"
        "/error — расхождения типов AVC/HEVC\n"
        "/help — помощь"
    )


def _details_keyboard(rows: list[object]) -> InlineKeyboardMarkup | None:
    buttons = [
        [
            InlineKeyboardButton(
                truncate_telegram_text(
                    f"Детали · {getattr(row, 'title', getattr(row, 'release_id', ''))}",
                    64,
                ),
                callback_data=f"status:{getattr(row, 'release_id')}",
            )
        ]
        for row in rows
    ]
    return InlineKeyboardMarkup(buttons) if buttons else None


async def overdue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_hevc_access(update) or update.message is None:
        return
    nickname = " ".join(context.args).strip() or None
    try:
        with SessionLocal() as db:
            rows = query_hevc_statuses(db, kind="overdue", nickname=nickname)
        for text, chunk_rows in split_release_list(rows, kind="overdue"):
            await update.message.reply_text(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_details_keyboard(list(chunk_rows)),
            )
    except Exception:
        logger.exception("Ошибка /overdue")
        await update.message.reply_text("❌ Не удалось получить список просрочек")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_hevc_access(update) or update.message is None:
        return
    if context.args:
        try:
            release_id = int(context.args[0])
        except (TypeError, ValueError):
            await update.message.reply_text("❌ release_id должен быть числом")
            return
        await _send_detail(update, release_id)
        return
    try:
        with SessionLocal() as db:
            rows = query_hevc_statuses(db, kind="waiting")
        for text, chunk_rows in split_release_list(rows, kind="waiting"):
            await update.message.reply_text(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_details_keyboard(list(chunk_rows)),
            )
    except Exception:
        logger.exception("Ошибка /status")
        await update.message.reply_text("❌ Не удалось получить статусы")


async def error(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_hevc_access(update) or update.message is None:
        return
    try:
        with SessionLocal() as db:
            rows = query_hevc_statuses(db, kind="error")
        for text, chunk_rows in split_release_list(rows, kind="error"):
            await update.message.reply_text(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_details_keyboard(list(chunk_rows)),
            )
    except Exception:
        logger.exception("Ошибка /error")
        await update.message.reply_text("❌ Не удалось получить список ошибок")


async def _send_detail(update: Update, release_id: int) -> None:
    with SessionLocal() as db:
        row = query_release_detail(db, release_id)
    if row is None:
        text = f"❌ Релиз {release_id} не найден"
    else:
        text = format_release_detail(row)
    if update.callback_query is not None:
        await update.callback_query.answer()
        message = update.callback_query.message
        if message is not None:
            await message.reply_text(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
    elif update.message is not None:
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def status_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await check_hevc_access(update) or update.callback_query is None:
        return
    data = update.callback_query.data or ""
    try:
        release_id = int(data.partition(":")[2])
    except ValueError:
        await update.callback_query.answer("Некорректный release_id", show_alert=True)
        return
    try:
        await _send_detail(update, release_id)
    except Exception:
        logger.exception("Ошибка callback status:%s", release_id)
        await update.callback_query.answer("Не удалось получить детали", show_alert=True)

