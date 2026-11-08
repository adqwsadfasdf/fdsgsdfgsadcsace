"""Раздел «Избранное» — список публикаций с отметкой ⭐ (добавление/удаление — в карточке или массово)."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from app_context import App
from handlers.publications import render_list


async def render_favorites(app: App) -> tuple[str, InlineKeyboardMarkup]:
    return await render_list(app, "fav", 0, "v", 0, 0)
