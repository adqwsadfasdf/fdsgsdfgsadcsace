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
from utils.render import growth_short

router = Router(name="authors")

METRIC_SUFFIX = {"v": "просмотров", "d1": "за 24 ч", "d7": "за 7 дн.", "d30": "за 30 дн.", "n": "публикаций"}


async def render_rating(app: App, sort: str = "v", page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    authors = [a for a in await app.analytics.authors() if a.count > 0]
    ranked = sort_authors(authors, sort)
    size = int(app.settings["page_size"])
    page = clamp_page(page, len(ranked), size)
    chunk = ranked[page * size : (page + 1) * size]
    lines = [f"👥 <b>Рейтинг авторов</b> — {AUTHOR_SORT_LABELS.get(sort, sort)}", f"Авторов: {len(ranked)}", ""]
    if not ranked:
        lines.append("Пока нет авторов с публикациями.")
    numbered = []
    for i, a in enumerate(chunk, start=page * size + 1):
        value = author_metric(a, sort)
        value_text = fmt_delta(value) if sort in {"d1", "d7", "d30"} else fmt_int(value)
        approx = {"d1": a.approx24, "d7": a.approx7, "d30": a.approx30}.get(sort, False)
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(
            f"{medal} <b>{esc(a.name)}</b> — {'≈' if approx else ''}{value_text} {METRIC_SUFFIX[sort]}\n"
            f"    📄 {a.count} · 👁 {fmt_int(a.total)} · ср. {fmt_float(a.avg)} · "
            f"24ч {fmt_delta(a.g24)} · 7д {fmt_delta(a.g7)}"
        )
        numbered.append((str(i), AuthorCb(act="card", id=a.id, sort=sort, page=page)))
    if ranked:
        lines += ["", "<i>Прирост — расчёт по снимкам; ≈ — у части публикаций неполные данные.</i>"]
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
        f"👤 <b>{esc(a.name)}</b>",
        f"🏆 Место в рейтинге по просмотрам: {place}" if place else "🏆 В рейтинге не участвует (нет публикаций)",
        "",
        f"📄 Публикаций: <b>{a.count}</b>",
        f"👁 Просмотров всего: <b>{fmt_int(a.total)}</b> <i>(факт API)</i>",
        f"📊 В среднем на публикацию: {fmt_float(a.avg)}",
        "",
        "📈 Прирост <i>(расчёт по снимкам)</i>:",
        f"• 24 часа: {'≈' if a.approx24 else ''}{fmt_delta(a.g24)}",
        f"• 7 дней: {'≈' if a.approx7 else ''}{fmt_delta(a.g7)}",
        f"• 30 дней: {'≈' if a.approx30 else ''}{fmt_delta(a.g30)}",
    ]
    if stats:
        lines += ["", f"Первая публикация: {fmt_date(stats[-1].created_ts, app.config.timezone)} · "
                      f"последняя: {fmt_date(stats[0].created_ts, app.config.timezone)}", "",
                  "<b>Последние публикации:</b>"]
        for st in stats[:8]:
            lines.append(f"{STATUS_EMOJI.get(st.status, '')} {link(st.url, truncate(st.title, 45))} · "
                         f"{fmt_date(st.created_ts, app.config.timezone)}\n"
                         f"    👁 {fmt_int(st.total)} · 24ч {growth_short(st.g24)} · 7д {growth_short(st.g7)}")
        if len(stats) > 8:
            lines.append(f"… всего {len(stats)} — откройте полную историю")
    kb = InlineKeyboardBuilder()
    kb.row(button("📋 История публикаций", PubList(ctx="author", arg=author_id, sort="new")),
           button("👁 По просмотрам", PubList(ctx="author", arg=author_id, sort="v")))
    kb.row(button("📥 Excel автора", ExportCb(act="author", arg=author_id)))
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
