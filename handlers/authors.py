"""Раздел «Рейтинг авторов» и история публикаций автора."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app_context import App
from handlers.common import answer, safe_edit
from keyboards.callbacks import AuthorCb, ExportCb, Nav, PubList
from keyboards.inline import button, clamp_page, number_buttons, pagination_row
from services.analytics import AUTHOR_SORT_LABELS, author_metric, sort_authors
from utils.formatting import STATUS_EMOJI, esc, fmt_date, fmt_delta, fmt_float, fmt_int, link, truncate
from utils.render import list_item, sum_line

router = Router(name="authors")

METRIC_SUFFIX = {"v": "просмотров", "d1": "за 24 ч", "d7": "за 7 дн.", "d30": "за 30 дн.", "n": "публикаций"}


async def render_rating(app: App, sort: str = "v", page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    authors = [a for a in await app.analytics.authors() if a.count > 0]
    ranked = sort_authors(authors, sort)
    size = int(app.settings["page_size"])
    page = clamp_page(page, len(ranked), size)
    chunk = ranked[page * size : (page + 1) * size]
    lines = [f"👥 <b>Авторы</b> · {len(ranked)} · {AUTHOR_SORT_LABELS.get(sort, sort)}"]
    if not ranked:
        lines.append("Пока нет авторов с публикациями.")
    by_author: dict[int, list] = {}
    for st in (await app.analytics.get_all()).values():
        by_author.setdefault(st.author_id, []).append(st)
    numbered = []
    for i, a in enumerate(chunk, start=page * size + 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(f"{medal} <b>{esc(truncate(a.name, 25))}</b> · {a.count} публ.\n{sum_line(by_author.get(a.id, []))}")
        numbered.append((str(i), AuthorCb(act="card", id=a.id, sort=sort, page=page)))
    kb = InlineKeyboardBuilder()
    number_buttons(kb, numbered)
    pag = pagination_row(page, len(ranked), size, lambda p: AuthorCb(act="list", sort=sort, page=p))
    if pag:
        kb.row(*pag)
    kb.row(*[button(("• " if sort == s else "") + label, AuthorCb(act="list", sort=s))
             for s, label in (("v", "👁 Всего"), ("d1", "24 ч"), ("d7", "7 дн."))])
    kb.row(*[button(("• " if sort == s else "") + label, AuthorCb(act="list", sort=s))
             for s, label in (("d30", "30 дн."), ("n", "📄 Кол-во"))])
    kb.row(button("🏠 Меню", Nav(to="main")))
    return "\n".join(lines), kb.as_markup()


async def render_author(app: App, author_id: int, sort: str = "v", page: int = 0
                        ) -> tuple[str, InlineKeyboardMarkup] | None:
    a = await app.analytics.author(author_id)
    if a is None:
        return None
    stats = sorted([s for s in (await app.analytics.get_all()).values() if s.author_id == author_id],
                   key=lambda s: s.created_ts, reverse=True)
    authors = [x for x in await app.analytics.authors() if x.count > 0]
    place = next((i for i, x in enumerate(sort_authors(authors, "v"), start=1) if x.id == author_id), None)
    lines = [
        f"👤 <b>{esc(a.name)}</b> · {a.count} публ." + (f" · 🏆 {place} место" if place else ""),
        sum_line(stats),
    ]
    if stats:
        lines.append("")
        for i, st in enumerate(stats[:8], start=1):
            lines.append(list_item(i, st, "new", show_author=False))
        if len(stats) > 8:
            lines.append(f"… всего {len(stats)}")
    kb = InlineKeyboardBuilder()
    kb.row(button("📋 История публикаций", PubList(ctx="author", arg=author_id, sort="new")),
           button("👁 По просмотрам", PubList(ctx="author", arg=author_id, sort="v")))
    kb.row(button("⬅️ К рейтингу", AuthorCb(act="list", sort=sort, page=page)))
    return "\n".join(lines), kb.as_markup()


@router.callback_query(AuthorCb.filter(F.act == "list"))
async def on_rating(cb: CallbackQuery, callback_data: AuthorCb, app: App) -> None:
    await safe_edit(cb.message, *(await render_rating(app, callback_data.sort, callback_data.page)))
    await answer(cb)


@router.callback_query(AuthorCb.filter(F.act == "card"))
async def on_author(cb: CallbackQuery, callback_data: AuthorCb, app: App) -> None:
    rendered = await render_author(app, callback_data.id, callback_data.sort, callback_data.page)
    if rendered is None:
        await answer(cb, "Автор не найден", alert=True)
        return
    await safe_edit(cb.message, *rendered)
    await answer(cb)
