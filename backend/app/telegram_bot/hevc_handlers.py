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


async def _bot_identity(
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[str | None, int | None]:
    username = getattr(context.bot, "username", None)
    bot_id = getattr(context.bot, "id", None)
    if (
        not isinstance(username, str)
        or not username.strip()
        or not isinstance(bot_id, int)
        or bot_id <= 0
    ):
        bot = await context.bot.get_me()
        username = getattr(bot, "username", None)
        bot_id = getattr(bot, "id", None)
    clean_username = (
        username.strip().lstrip("@").casefold()
        if isinstance(username, str)
        else None
    ) or None
    clean_id = bot_id if isinstance(bot_id, int) and bot_id > 0 else None
    return clean_username, clean_id


async def _bot_username(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    username, _ = await _bot_identity(context)
    return username


def _mentions_bot(text: str, username: str) -> bool:
    return bool(
        re.search(
            rf"(?<![\w@])@{re.escape(username)}(?!\w)",
            text,
            flags=re.IGNORECASE,
        )
    )


def _slice_utf16(text: str, offset: int, length: int) -> str:
    """Telegram entity offset/length считаются в UTF-16 code units."""
    encoded = text.encode("utf-16-le")
    start = offset * 2
    end = (offset + length) * 2
    if start < 0 or end < start:
        return ""
    return encoded[start:end].decode("utf-16-le", errors="ignore")


def _entity_type(entity: object) -> str:
    raw = getattr(entity, "type", "")
    value = getattr(raw, "value", raw)
    return str(value).rsplit(".", 1)[-1].casefold()


def _message_addresses_bot(
    message: object,
    username: str | None,
    bot_id: int | None,
) -> bool:
    """Явный тег в тексте/caption или text_mention по id. Reply сам по себе не считается."""
    text = getattr(message, "text", None) or ""
    caption = getattr(message, "caption", None) or ""
    if username:
        if _mentions_bot(text, username) or _mentions_bot(caption, username):
            return True

    for entity, source in (
        *((item, text) for item in getattr(message, "entities", None) or ()),
        *(
            (item, caption)
            for item in getattr(message, "caption_entities", None) or ()
        ),
    ):
        entity_type = _entity_type(entity)
        user = getattr(entity, "user", None)
        if user is not None:
            if bot_id is not None and getattr(user, "id", None) == bot_id:
                return True
            mention_username = getattr(user, "username", None)
            if (
                username
                and isinstance(mention_username, str)
                and mention_username.casefold() == username
            ):
                return True
        if entity_type == "mention" and username:
            offset = int(getattr(entity, "offset", 0) or 0)
            length = int(getattr(entity, "length", 0) or 0)
            mention = _slice_utf16(source, offset, length)
            if mention.lstrip("@").casefold() == username:
                return True
    return False


def _command_targets_bot(text: str, username: str | None) -> bool:
    command = text.split(maxsplit=1)[0]
    _, separator, target = command.partition("@")
    return not separator or (username is not None and target.casefold() == username)


def _is_private_chat(chat: object) -> bool:
    return getattr(chat, "type", None) == "private"


async def unknown_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Случайный ответ на неизвестную команду, адресованную боту.

    В группе — всем, кто тегнул бота (без ACL). В личке — только одобренным.
    """
    message = getattr(update, "effective_message", None) or update.message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    username = await _bot_username(context)
    if not _command_targets_bot(message.text or "", username):
        return
    if not await require_private_hevc_access(update):
        return
    await message.reply_text(random.choice(RANDOM_REPLIES))


async def addressed_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Случайный ответ на упоминание бота / любой текст в личке.

    В группе — всем, кто тегнул (без ACL). В личке — только одобренным.
    """
    message = getattr(update, "effective_message", None) or update.message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    if not _is_private_chat(chat):
        username, bot_id = await _bot_identity(context)
        if not _message_addresses_bot(message, username, bot_id):
            return
    if not await require_private_hevc_access(update):
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


async def require_private_hevc_access(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> bool:
    """В группе публичные команды доступны всем; ACL только в личке."""
    chat = update.effective_chat
    if chat is not None and not _is_private_chat(chat):
        return True
    return await check_hevc_access(update, context)


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
    if not await require_private_hevc_access(update) or update.message is None:
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
    if not await require_private_hevc_access(update) or update.message is None:
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
    if not await require_private_hevc_access(update) or update.message is None:
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
    if not await require_private_hevc_access(update) or update.message is None:
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
    if not await require_private_hevc_access(update) or update.callback_query is None:
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

