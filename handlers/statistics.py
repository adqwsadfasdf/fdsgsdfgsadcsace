"""Разделы «Статистика» (общая аналитика) и «Лучшие публикации»."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app_context import App
from handlers.common import answer, safe_edit
from keyboards.callbacks import Nav, PubList, StatCb
from keyboards.inline import button
from services.analytics import period_value, sort_stats
from utils.formatting import esc, fmt_ago, fmt_int, link, truncate
from utils.render import period_short, sum_line

router = Router(name="statistics")


async def render_overview(app: App) -> tuple[str, InlineKeyboardMarkup]:
    ov = await app.analytics.overview()
    items = list((await app.analytics.get_all()).values())
    lines = [
        f"📊 <b>Статистика</b> · {fmt_int(ov.pubs)} публ. · {fmt_int(ov.authors)} авт.",
        sum_line(items),
        f"📈 растут: {ov.growing}  |  ➖ стабильно: {ov.stable}  |  📉 падают: {ov.declining}",
        f"🕒 обновлено {fmt_ago(ov.last_update_ts)}" + (f" · ❗ ошибок: {ov.errors}" if ov.errors else ""),
    ]
    kb = InlineKeyboardBuilder()
    kb.row(button("👁 Всего", PubList(ctx="stat", sort="v")), button("24 ч", PubList(ctx="stat", sort="d1")),
           button("7 дн.", PubList(ctx="stat", sort="d7")), button("30 дн.", PubList(ctx="stat", sort="d30")))
    kb.row(button("🔄 Обновить", StatCb(act="refresh")), button("ℹ️ Точность", StatCb(act="info")),
           button("🏠 Меню", Nav(to="main")))
    return "\n".join(lines), kb.as_markup()


async def render_best(app: App) -> tuple[str, InlineKeyboardMarkup]:
    items = list((await app.analytics.get_all()).values())
    lines = ["🏆 <b>Лучшие</b>"]
    if not items:
        lines.append("Публикаций пока нет.")
    for label, sort, period in (("24ч", "d1", "24h"), ("7д", "d7", "7d"), ("30д", "d30", "30d"), ("Всего", "v", None)):
        if period:
            best = max(items, key=lambda s: (period_value(s, period)[0] or 0, s.total), default=None)
        else:
            best = max(items, key=lambda s: s.total, default=None)
        if best is None:
            continue
        if period:
            value, _ = period_value(best, period)
            if not value:
                lines.append(f"{label}: —")
                continue
            metric = period_short(best, period)
        else:
            metric = f"👁 {fmt_int(best.total)}"
        lines.append(f"<b>{label}:</b> <b>{metric}</b>  |  {link(best.url, truncate(best.title, 35))} · "
                     f"{esc(truncate(best.author_name, 15))}")
    kb = InlineKeyboardBuilder()
    kb.row(button("Топ 24 ч", PubList(ctx="best", sort="d1")), button("Топ 7 дн.", PubList(ctx="best", sort="d7")),
           button("Топ 30 дн.", PubList(ctx="best", sort="d30")))
    kb.row(button("Всё время", PubList(ctx="best", sort="v")), button("🔥 Набирают", PubList(ctx="best", sort="trend")),
           button("⚠️ Падают", PubList(ctx="best", sort="low")))
    kb.row(button("🏠 Меню", Nav(to="main")))
    return "\n".join(lines), kb.as_markup()


ACCURACY_TEXT = (
    "ℹ️ <b>Откуда цифры</b>\n"
    "👁 Всего — счётчик Telegraph (getViews), страницы бот не открывает.\n"
    "24ч / 7д / 30д — разница счётчика по снимкам, которые бот делает каждые N минут. "
    "Для только что добавленных 7д и 30д берутся из просмотров по дням (тоже getViews).\n"
    "Пока бот следит за постом меньше периода, цифра показывает прирост за то время, что есть."
)


@router.callback_query(StatCb.filter(F.act == "info"))
async def on_info(cb: CallbackQuery) -> None:
    kb = InlineKeyboardBuilder()
    kb.row(button("⬅️ Назад", Nav(to="stats")))
    await safe_edit(cb.message, ACCURACY_TEXT, kb.as_markup())
    await answer(cb)


@router.callback_query(StatCb.filter(F.act == "refresh"))
async def on_refresh(cb: CallbackQuery, app: App, bot: Bot) -> None:
    if app.collector.lock.locked():
        await answer(cb, "⏳ Сбор статистики уже выполняется", alert=True)
        return
    await answer(cb, "🔄 Запускаю сбор статистики…")
    chat_id, message_id = cb.message.chat.id, cb.message.message_id

    async def job() -> None:
        result = await app.collector.collect_all()
        if app.notifier is not None:
            await app.notifier.check()
        text, kb = await render_overview(app)
        summary = (f"✅ Обновлено: {result.ok} из {result.total}" + (f", ошибок: {result.failed}" if result.failed else "")
                   if not result.skipped else "⏳ Сбор уже выполнялся")
        try:
            await bot.edit_message_text(f"{summary}\n\n{text}", chat_id=chat_id, message_id=message_id, reply_markup=kb)
        except Exception:  # noqa: BLE001
            await bot.send_message(chat_id, f"{summary}\n\n{text}", reply_markup=kb)

    app.spawn(job(), name="manual-collect")


__all__ = ["router", "render_overview", "render_best"]
