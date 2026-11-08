"""Умный поиск: автор, заголовок, дата, ссылка Telegraph, ссылка YouTube, метка — с комбинированием фильтров."""
from __future__ import annotations

from datetime import date

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app_context import App
from handlers.common import answer, safe_edit
from handlers.publications import render_list
from handlers.states import SearchInput
from keyboards.callbacks import ExportCb, Nav, PubList, SearchCb
from keyboards.inline import button
from keyboards.navigation import cancel_kb
from services import publications as pubs
from services.publications import SearchFilters
from services.youtube import parse_youtube
from utils.formatting import esc
from utils.validators import ValidationError, clean_line, parse_date_range, parse_telegraph_path

router = Router(name="search")

FIELD_PROMPTS = {
    "author": "👤 Отправьте имя автора (или часть имени).",
    "title": "📝 Отправьте слово или фразу из заголовка.",
    "date": "📅 Отправьте дату (08.11.2026) или период (01.11.2026-08.11.2026).",
    "tg": "🔗 Отправьте ссылку Telegraph.",
    "yt": "🎬 Отправьте ссылку YouTube.",
    "label": "🏷 Отправьте внутреннюю метку (или её часть).",
}


def _fmt_date(iso: str | None) -> str:
    return date.fromisoformat(iso).strftime("%d.%m.%Y") if iso else ""


def describe(f: SearchFilters) -> list[str]:
    out = []
    if f.text:
        out.append(f"🔤 Автор или заголовок содержит: «{esc(f.text)}»")
    if f.author:
        out.append(f"👤 Автор: «{esc(f.author)}»")
    if f.title:
        out.append(f"📝 Заголовок: «{esc(f.title)}»")
    if f.date_from:
        period = _fmt_date(f.date_from) if f.date_from == f.date_to else f"{_fmt_date(f.date_from)} — {_fmt_date(f.date_to)}"
        out.append(f"📅 Дата создания: {period}")
    if f.telegraph:
        out.append(f"🔗 Telegraph: {esc(f.telegraph)}")
    if f.youtube:
        out.append(f"🎬 YouTube: youtu.be/{esc(f.youtube)}")
    if f.label:
        out.append(f"🏷 Метка: «{esc(f.label)}»")
    if f.favorites:
        out.append("⭐ Только избранное")
    return out


async def render_menu(app: App) -> tuple[str, InlineKeyboardMarkup]:
    filters = await pubs.load_filters(app)
    found = len(await pubs.query(app, "search")) if not filters.is_empty() else 0
    lines = [
        "🔍 <b>Поиск</b>",
        "Отправьте автора, слово из заголовка, дату (08.11.2026) или ссылку Telegraph / YouTube.",
    ]
    desc = describe(filters)
    if desc:
        lines += ["", "<b>Фильтры:</b>"] + desc + [f"Найдено: <b>{found}</b>"]
    kb = InlineKeyboardBuilder()
    kb.row(button("👤 Автор", SearchCb(act="f_author")), button("📝 Заголовок", SearchCb(act="f_title")),
           button("📅 Дата", SearchCb(act="f_date")))
    kb.row(button("🔗 Telegraph", SearchCb(act="f_tg")), button("🎬 YouTube", SearchCb(act="f_yt")),
           button("🏷 Метка", SearchCb(act="f_label")))
    kb.row(button(("✅ " if filters.favorites else "") + "⭐ Только избранное", SearchCb(act="fav")))
    if desc:
        kb.row(button(f"📋 Показать результаты ({found})", PubList(ctx="search", sort="new")))
        kb.row(button("🧹 Сбросить", SearchCb(act="reset")))
    kb.row(button("🏠 Меню", Nav(to="main")))
    return "\n".join(lines), kb.as_markup()


async def open_search(message: Message, state: FSMContext, app: App, edit: bool = False) -> None:
    await state.set_state(SearchInput.query)
    text, kb = await render_menu(app)
    if edit:
        await safe_edit(message, text, kb)
    else:
        await message.answer(text, reply_markup=kb)


@router.callback_query(SearchCb.filter(F.act == "menu"))
async def on_menu(cb: CallbackQuery, state: FSMContext, app: App) -> None:
    await open_search(cb.message, state, app, edit=True)
    await answer(cb)


@router.callback_query(SearchCb.filter(F.act == "reset"))
async def on_reset(cb: CallbackQuery, state: FSMContext, app: App) -> None:
    await pubs.save_filters(app, SearchFilters())
    await open_search(cb.message, state, app, edit=True)
    await answer(cb, "Фильтры сброшены")


@router.callback_query(SearchCb.filter(F.act == "fav"))
async def on_fav(cb: CallbackQuery, state: FSMContext, app: App) -> None:
    filters = await pubs.load_filters(app)
    filters.favorites = not filters.favorites
    await pubs.save_filters(app, filters)
    await open_search(cb.message, state, app, edit=True)
    await answer(cb)


@router.callback_query(SearchCb.filter(F.act.startswith("f_")))
async def on_field(cb: CallbackQuery, callback_data: SearchCb, state: FSMContext) -> None:
    field = callback_data.act[2:]
    await state.set_state(SearchInput.field)
    await state.update_data(field=field)
    await cb.message.answer(FIELD_PROMPTS[field] + "\nЧтобы убрать этот фильтр — отправьте «-».", reply_markup=cancel_kb())
    await answer(cb)


def apply_field(filters: SearchFilters, field: str, raw: str) -> None:
    value = clean_line(raw)
    clear = value in {"-", "—", ""}
    if field == "author":
        filters.author = None if clear else value
    elif field == "title":
        filters.title = None if clear else value
    elif field == "label":
        filters.label = None if clear else value
    elif field == "date":
        if clear:
            filters.date_from = filters.date_to = None
        else:
            d1, d2 = parse_date_range(value)
            filters.date_from, filters.date_to = d1.isoformat(), d2.isoformat()
    elif field == "tg":
        if clear:
            filters.telegraph = None
        else:
            path = parse_telegraph_path(value)
            if not path:
                raise ValidationError("Это не ссылка Telegraph (ожидается telegra.ph/...)")
            filters.telegraph = path
    elif field == "yt":
        if clear:
            filters.youtube = None
        else:
            vid = parse_youtube(value)
            if not vid:
                raise ValidationError("Не удалось распознать ссылку YouTube")
            filters.youtube = vid


def detect_query(filters: SearchFilters, raw: str) -> str:
    """Определяет тип быстрого запроса и записывает в нужный фильтр. Возвращает описание."""
    value = clean_line(raw)
    if value in {"-", "—"}:
        filters.text = None
        return "текстовый фильтр снят"
    path = parse_telegraph_path(value)
    if path:
        filters.telegraph = path
        return "ссылка Telegraph"
    vid = parse_youtube(value, allow_bare_id=False)
    if vid:
        filters.youtube = vid
        return "ссылка YouTube"
    try:
        d1, d2 = parse_date_range(value)
        filters.date_from, filters.date_to = d1.isoformat(), d2.isoformat()
        return "дата"
    except ValidationError:
        pass
    filters.text = value
    return "автор или заголовок"


async def _show_results(message: Message, app: App, note: str) -> None:
    text, kb = await render_list(app, "search", 0, "new", 0, 0)
    await message.answer(f"🔍 Распознано: {note}\n\n{text}", reply_markup=kb)


@router.message(SearchInput.field, F.text)
async def on_field_input(message: Message, state: FSMContext, app: App) -> None:
    field = (await state.get_data()).get("field", "author")
    filters = await pubs.load_filters(app)
    try:
        apply_field(filters, field, message.text)
    except ValidationError as exc:
        await message.answer(f"❗ {exc}", reply_markup=cancel_kb())
        return
    await pubs.save_filters(app, filters)
    await state.set_state(SearchInput.query)
    if filters.is_empty():
        await open_search(message, state, app)
        return
    await _show_results(message, app, "фильтр обновлён")


@router.message(SearchInput.query, F.text)
async def on_query(message: Message, state: FSMContext, app: App) -> None:
    filters = await pubs.load_filters(app)
    note = detect_query(filters, message.text)
    await pubs.save_filters(app, filters)
    if filters.is_empty():
        await open_search(message, state, app)
        return
    await _show_results(message, app, note)
