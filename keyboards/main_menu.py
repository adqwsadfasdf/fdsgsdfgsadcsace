"""Главное меню (постоянная ReplyKeyboard)."""
from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

BTN_CREATE = "📝 Создать публикации"
BTN_ALL = "📚 Все публикации"
BTN_STATS = "📊 Статистика"
BTN_BEST = "🏆 Лучшие публикации"
BTN_AUTHORS = "👥 Рейтинг авторов"
BTN_SEARCH = "🔍 Поиск"
BTN_YT = "🎬 Группы YouTube"
BTN_FAV = "⭐ Избранное"
BTN_EXPORT = "📥 Экспорт Excel"
BTN_SETTINGS = "⚙️ Настройки"
BTN_IMPORT = "📥 Добавить готовые"
BTN_COMP = "🕵️ Конкуренты"

MAIN_BUTTONS = [
    BTN_CREATE, BTN_IMPORT, BTN_ALL, BTN_STATS, BTN_BEST, BTN_AUTHORS,
    BTN_SEARCH, BTN_YT, BTN_FAV, BTN_EXPORT, BTN_COMP, BTN_SETTINGS,
]


def main_menu_kb() -> ReplyKeyboardMarkup:
    rows = [
        [BTN_CREATE, BTN_IMPORT],
        [BTN_ALL, BTN_STATS],
        [BTN_BEST, BTN_AUTHORS],
        [BTN_SEARCH, BTN_YT],
        [BTN_FAV, BTN_EXPORT],
        [BTN_COMP, BTN_SETTINGS],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите раздел",
    )
