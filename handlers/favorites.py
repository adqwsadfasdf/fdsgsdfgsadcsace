"""Раздел «Избранное» — список публикаций с отметкой ⭐ (добавление/удаление — в карточке или массово)."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from app_context import App
from handlers.publications import render_list


async def render_favorites(app: App) -> tuple[str, InlineKeyboardMarkup]:
    text, kb = await render_list(app, "fav", 0, "v", 0, 0)
    hint = ("\n\n<i>Добавить в избранное: кнопка «⭐ Добавить в избранное» в карточке публикации "
            "или массово через «☑️ Выбрать». Убрать — той же кнопкой в карточке.</i>")
    return text + hint, kb
