"""Старт, главное меню, навигация между разделами и отмена операций."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app_context import App
from handlers import authors, favorites, publications, search, settings, statistics, tracking, youtube_groups
from handlers.common import answer, safe_edit
from handlers.create_posts import start_create
from keyboards import main_menu as mm
from keyboards.callbacks import Nav
from keyboards.main_menu import MENU_TEXT, main_menu_inline, main_menu_kb

router = Router(name="start")

WELCOME = "👋 Бот готов. Кнопка «🏠 Меню» внизу открывает разделы, /cancel — отмена."

SECTIONS = {"pubs", "stats", "best", "authors", "yt", "fav", "settings", "import", "comp"}


async def render_section(app: App, to: str) -> tuple[str, InlineKeyboardMarkup] | None:
    if to == "pubs":
        return await publications.render_list(app, "all", 0, "new", 0, 0)
    if to == "stats":
        return await statistics.render_overview(app)
    if to == "best":
        return await statistics.render_best(app)
    if to == "authors":
        return await authors.render_rating(app)
    if to == "yt":
        return await youtube_groups.render_groups(app)
    if to == "fav":
        return await favorites.render_favorites(app)
    if to == "settings":
        return await settings.render_menu(app)
    if to == "import":
        return await tracking.render_import_menu(app)
    if to == "comp":
        return await tracking.render_comp_menu(app)
    return None


BUTTON_TO_SECTION = {
    mm.BTN_ALL: "pubs",
    mm.BTN_STATS: "stats",
    mm.BTN_BEST: "best",
    mm.BTN_AUTHORS: "authors",
    mm.BTN_YT: "yt",
    mm.BTN_FAV: "fav",
    mm.BTN_SETTINGS: "settings",
    mm.BTN_IMPORT: "import",
    mm.BTN_COMP: "comp",
}


async def send_menu(message: Message) -> None:
    await message.answer(MENU_TEXT, reply_markup=main_menu_inline())


@router.message(CommandStart(), StateFilter("*"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    # первое сообщение ставит маленькую кнопку «🏠 Меню» вместо старой большой клавиатуры
    await message.answer(WELCOME, reply_markup=main_menu_kb())
    await send_menu(message)


@router.message(Command("menu"), StateFilter("*"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_menu(message)


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    await state.clear()
    await message.answer("❌ Отменено." if current else "Нет активного действия.", reply_markup=main_menu_inline())


@router.message(F.text.in_(set(mm.MAIN_BUTTONS)), StateFilter("*"))
async def on_main_button(message: Message, state: FSMContext, app: App) -> None:
    # Нажатие кнопки меню всегда прерывает текущий мастер (корректная отмена).
    await state.clear()
    if message.text == mm.BTN_MENU:
        await send_menu(message)
        return
    if message.text in mm.LEGACY_BUTTONS:
        # у пользователя осталась старая большая клавиатура — заменяем её маленькой
        await message.answer("Меню стало компактным 👇", reply_markup=main_menu_kb())
    if message.text == mm.BTN_CREATE:
        await start_create(message, state, app)
        return
    if message.text == mm.BTN_SEARCH:
        await search.open_search(message, state, app)
        return
    rendered = await render_section(app, BUTTON_TO_SECTION[message.text])
    if rendered:
        await message.answer(rendered[0], reply_markup=rendered[1])


@router.callback_query(Nav.filter(F.to == "cancel"), StateFilter("*"))
async def on_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    current = await state.get_state()
    await state.clear()
    await safe_edit(cb.message, ("❌ Отменено.\n\n" if current else "") + MENU_TEXT, main_menu_inline())
    await answer(cb)


@router.callback_query(Nav.filter(F.to == "main"), StateFilter("*"))
async def on_main(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(cb.message, MENU_TEXT, main_menu_inline())
    await answer(cb)


@router.callback_query(Nav.filter(F.to == "search"), StateFilter("*"))
async def on_nav_search(cb: CallbackQuery, state: FSMContext, app: App) -> None:
    await search.open_search(cb.message, state, app, edit=True)
    await answer(cb)


@router.callback_query(Nav.filter(F.to.in_(SECTIONS)), StateFilter("*"))
async def on_nav(cb: CallbackQuery, callback_data: Nav, state: FSMContext, app: App) -> None:
    await state.clear()
    rendered = await render_section(app, callback_data.to)
    if rendered:
        await safe_edit(cb.message, *rendered)
    await answer(cb)


fallback_router = Router(name="fallback")


@fallback_router.message(StateFilter(None))
async def on_unknown_message(message: Message) -> None:
    await send_menu(message)


@fallback_router.callback_query()
async def on_unknown_callback(cb: CallbackQuery) -> None:
    await answer(cb, "Кнопка устарела. Откройте раздел заново из меню.", alert=True)
