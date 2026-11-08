"""Общие помощники обработчиков."""
from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from utils.formatting import split_message

logger = logging.getLogger(__name__)


def error_text(exc: BaseException) -> str:
    from services.telegraph import TelegraphError, describe_error

    if isinstance(exc, TelegraphError):
        return describe_error(exc)
    return str(exc) or type(exc).__name__


async def answer(cb: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        await cb.answer(text, show_alert=alert)
    except TelegramBadRequest:
        pass  # запрос устарел или уже отвечен


async def safe_edit(message: Message | None, text: str, markup: InlineKeyboardMarkup | None = None) -> Message | None:
    """Редактирует сообщение; если нельзя — отправляет новое."""
    if message is None:
        return None
    if len(text) > 4096:
        text = text[:4000] + "\n…"
    try:
        await message.edit_text(text, reply_markup=markup)
        return message
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return message
        logger.debug("edit_text не удался (%s), отправляем новое сообщение", exc)
        return await message.answer(text, reply_markup=markup)


async def send_long(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> Message:
    parts = split_message(text)
    last = message
    for i, part in enumerate(parts):
        last = await message.answer(part, reply_markup=markup if i == len(parts) - 1 else None)
    return last
