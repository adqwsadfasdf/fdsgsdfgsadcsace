"""Главное меню.

Внизу экрана — одна маленькая кнопка «🏠 Меню» (не занимает место), сами разделы —
inline-кнопки под сообщением.
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from keyboards.callbacks import Nav

BTN_MENU = "🏠 Меню"

# Старые подписи большой клавиатуры — продолжают работать, если она ещё осталась у пользователя
BTN_CREATE = "📝 Создать публикации"
BTN_ALL = "📚 Все публикации"
BTN_STATS = "📊 Статистика"
BTN_BEST = "🏆 Лучшие публикации"
BTN_AUTHORS = "👥 Рейтинг авторов"
BTN_SEARCH = "🔍 Поиск"
BTN_YT = "🎬 Группы YouTube"
BTN_FAV = "⭐ Избранное"
BTN_SETTINGS = "⚙️ Настройки"
BTN_IMPORT = "📥 Добавить готовые"
BTN_COMP = "🕵️ Конкуренты"
BTN_EXPORT = "📥 Экспорт Excel"

LEGACY_BUTTONS = [
    BTN_CREATE, BTN_IMPORT, BTN_ALL, BTN_STATS, BTN_BEST, BTN_AUTHORS,
    BTN_SEARCH, BTN_YT, BTN_FAV, BTN_COMP, BTN_SETTINGS,
]
MAIN_BUTTONS = [BTN_MENU, *LEGACY_BUTTONS]

MENU_ITEMS = [
    [("📝 Создать", "create"), ("📥 Свои готовые", "import")],
    [("📚 Публикации", "pubs"), ("🕵️ Конкуренты", "comp")],
    [("📊 Статистика", "stats"), ("🏆 Лучшие", "best"), ("👥 Авторы", "authors")],
    [("🔍 Поиск", "search"), ("⭐ Избранное", "fav"), ("🎬 YouTube", "yt")],
    [("⚙️ Настройки", "settings")],
]
MENU_TEXT = "🏠 <b>Меню</b>"


def main_menu_kb() -> ReplyKeyboardMarkup:
    """Одна маленькая кнопка внизу экрана."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_MENU)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_menu_inline() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for row in MENU_ITEMS:
        kb.row(*[InlineKeyboardButton(text=text, callback_data=Nav(to=to).pack()) for text, to in row])
    return kb.as_markup()
