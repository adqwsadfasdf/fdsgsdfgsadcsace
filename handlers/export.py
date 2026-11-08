"""Раздел «Экспорт Excel»: все, по автору, по YouTube-группе, избранное, поиск, выбранные, партия."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from app_context import App
from database import YouTubeVideo
from handlers.common import answer, safe_edit
from keyboards.callbacks import ExportCb, Nav
from keyboards.inline import button, clamp_page, pagination_row
from services import publications as pubs
from services import youtube as yt
from services.analytics import sort_authors
from services.export import build_workbook_async, safe_filename
from utils.formatting import truncate

logger = logging.getLogger(__name__)
router = Router(name="export")

CHOOSER_PAGE = 12


async def render_menu(app: App) -> tuple[str, InlineKeyboardMarkup]:
    stats = await app.analytics.get_all()
    selected = await pubs.selected_ids(app)
    favs = sum(1 for s in stats.values() if s.is_favorite)
    kb = InlineKeyboardBuilder()
    kb.row(button(f"📚 Все публикации ({len(stats)})", ExportCb(act="Lall")))
    kb.row(button("👤 Публикации автора…", ExportCb(act="authors")),
           button("🎬 YouTube-группа…", ExportCb(act="groups")))
    kb.row(button(f"⭐ Избранное ({favs})", ExportCb(act="Lfav")),
           button("🔍 Результаты поиска", ExportCb(act="Lsearch")))
    kb.row(button(f"☑️ Выбранные ({len(selected)})", ExportCb(act="Lsel")))
    comp = await app.analytics.get_competitors()
    if comp:
        kb.row(button(f"🕵️ Публикации конкурентов ({len(comp)})", ExportCb(act="Lcomp")))
    kb.row(button("🏠 Меню", Nav(to="main")))
    text = ("📥 <b>Экспорт в Excel</b>\n\nВыберите, что выгрузить. Файл .xlsx придёт сюда же.\n\n"
            "<i>Колонки: ID, заголовок, автор, Telegraph, YouTube, дата создания, просмотры (факт API), "
            "прирост 24 ч / 7 / 30 дн. (расчёт), точность расчёта, статус активности.</i>")
    return text, kb.as_markup()


@router.callback_query(ExportCb.filter(F.act == "menu"))
async def on_menu(cb: CallbackQuery, app: App) -> None:
    await safe_edit(cb.message, *(await render_menu(app)))
    await answer(cb)


@router.callback_query(ExportCb.filter(F.act == "authors"))
async def on_choose_author(cb: CallbackQuery, callback_data: ExportCb, app: App) -> None:
    authors = sort_authors([a for a in await app.analytics.authors() if a.count > 0], "v")
    page = clamp_page(callback_data.page, len(authors), CHOOSER_PAGE)
    kb = InlineKeyboardBuilder()
    for a in authors[page * CHOOSER_PAGE : (page + 1) * CHOOSER_PAGE]:
        kb.row(button(f"{truncate(a.name, 30)} ({a.count})", ExportCb(act="author", arg=a.id)))
    pag = pagination_row(page, len(authors), CHOOSER_PAGE, lambda p: ExportCb(act="authors", page=p))
    if pag:
        kb.row(*pag)
    kb.row(button("⬅️ Назад", ExportCb(act="menu")))
    await safe_edit(cb.message, "👤 Выберите автора для выгрузки:" if authors else "Авторов пока нет.", kb.as_markup())
    await answer(cb)


@router.callback_query(ExportCb.filter(F.act == "groups"))
async def on_choose_group(cb: CallbackQuery, callback_data: ExportCb, app: App) -> None:
    async with app.db() as session:
        groups = [g for g in await yt.list_groups(session) if g.count > 0]
    page = clamp_page(callback_data.page, len(groups), CHOOSER_PAGE)
    kb = InlineKeyboardBuilder()
    for g in groups[page * CHOOSER_PAGE : (page + 1) * CHOOSER_PAGE]:
        kb.row(button(f"{truncate(g.display, 32)} ({g.count})", ExportCb(act="yt", arg=g.id)))
    pag = pagination_row(page, len(groups), CHOOSER_PAGE, lambda p: ExportCb(act="groups", page=p))
    if pag:
        kb.row(*pag)
    kb.row(button("⬅️ Назад", ExportCb(act="menu")))
    await safe_edit(cb.message, "🎬 Выберите YouTube-группу:" if groups else "Групп с публикациями пока нет.",
                    kb.as_markup())
    await answer(cb)


async def send_export(cb: CallbackQuery, app: App, ctx: str, arg: int = 0) -> None:
    if ctx in {"best", "stat"}:
        ctx = "all"
    items = await pubs.query(app, ctx, arg, "new")
    if not items:
        await answer(cb, "Нет публикаций для выгрузки", alert=True)
        return
    await answer(cb, "📥 Формирую файл…")
    title = await pubs.context_title(app, ctx, arg)
    async with app.db() as session:
        videos = {v.id: (v.url, v.title) for v in (await session.execute(select(YouTubeVideo))).scalars()}
    try:
        data = await build_workbook_async(items, videos, title, app.config.timezone, app.config.tz_name)
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка формирования Excel")
        await cb.message.answer("❗ Не удалось сформировать файл. Подробности — в логе.")
        return
    name = safe_filename({"all": "publications", "fav": "favorites", "search": "search", "sel": "selected",
                          "comp": "competitors", "imp": "imported"}.get(ctx, f"{ctx}_{arg}"))
    await cb.message.answer_document(
        BufferedInputFile(data, filename=name),
        caption=f"📥 {title}\nПубликаций: {len(items)}",
    )
    logger.info("Экспорт %s/%s: %s строк", ctx, arg, len(items))


@router.callback_query(ExportCb.filter(F.act.startswith("L")))
async def on_export_list(cb: CallbackQuery, callback_data: ExportCb, app: App) -> None:
    await send_export(cb, app, callback_data.act[1:], callback_data.arg)


@router.callback_query(ExportCb.filter(F.act.in_({"author", "yt", "batch"})))
async def on_export_entity(cb: CallbackQuery, callback_data: ExportCb, app: App) -> None:
    await send_export(cb, app, callback_data.act, callback_data.arg)
