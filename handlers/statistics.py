"""Разделы «Статистика» (общая аналитика) и «Лучшие публикации»."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app_context import App
from handlers.common import answer, safe_edit
from keyboards.callbacks import Nav, PubList, StatCb
from keyboards.inline import button
from services.analytics import PubStats, sort_stats
from utils.formatting import esc, fmt_ago, fmt_dt, fmt_float, fmt_int, link, truncate
from utils.render import growth_short, sort_metric

router = Router(name="statistics")


async def render_overview(app: App) -> tuple[str, InlineKeyboardMarkup]:
    ov = await app.analytics.overview()
    last = await app.settings.get_state("last_collect")
    tz = app.config.timezone

    def note(n: int) -> str:
        return f" <i>(≈, у {n} публ. неполные данные)</i>" if n else ""

    lines = [
        "📊 <b>Общая статистика</b>",
        "",
        f"📄 Публикаций: <b>{fmt_int(ov.pubs)}</b>",
        f"👥 Авторов с публикациями: <b>{fmt_int(ov.authors)}</b> <i>(всего в истории: {fmt_int(ov.authors_all)})</i>",
        f"👁 Просмотров всего: <b>{fmt_int(ov.total)}</b> <i>(факт Telegraph API)</i>",
        "",
        "📈 <b>Суммарный прирост</b> <i>(расчёт по снимкам)</i>",
        f"• за 24 часа: +{fmt_int(ov.g24)}{note(ov.not_exact24)}",
        f"• за 7 дней: +{fmt_int(ov.g7)}{note(ov.not_exact7)}",
        f"• за 30 дней: +{fmt_int(ov.g30)}{note(ov.not_exact30)}",
        "",
        "🚦 <b>Активность публикаций</b>",
        f"📈 Растут: {ov.growing} · ➖ Стабильно: {ov.stable} · 📉 Активность снижается: {ov.declining}",
        f"🆕 Мало данных для оценки: {ov.new}",
        "",
        f"🕒 Последнее обновление: {fmt_ago(ov.last_update_ts)} · интервал сбора "
        f"{app.settings['snapshot_interval_minutes']} мин",
    ]
    if last:
        lines.append(f"<i>Последний сбор: {last.get('ok', 0)}/{last.get('total', 0)} успешно, "
                     f"{fmt_float(last.get('duration', 0))} с</i>")
    if ov.first_snapshot_ts:
        lines.append(f"🗂 История снимков с {fmt_dt(ov.first_snapshot_ts, tz)} · снимков: {fmt_int(ov.snapshots)}")
    if ov.errors:
        lines.append(f"❗ Ошибки обновления у {ov.errors} публ. (см. карточки, отмечены ❗)")

    kb = InlineKeyboardBuilder()
    kb.row(button("👁 По просмотрам", PubList(ctx="stat", sort="v")),
           button("📈 За 24 ч", PubList(ctx="stat", sort="d1")))
    kb.row(button("📈 За 7 дн.", PubList(ctx="stat", sort="d7")),
           button("📈 За 30 дн.", PubList(ctx="stat", sort="d30")))
    kb.row(button("📅 По дате", PubList(ctx="stat", sort="new")),
           button("👤 По автору", PubList(ctx="stat", sort="au")))
    kb.row(button("🔥 Набирают популярность", PubList(ctx="stat", sort="trend")))
    kb.row(button("⚠️ Низкая активность", PubList(ctx="stat", sort="low")))
    kb.row(button("🔄 Обновить сейчас", StatCb(act="refresh")), button("ℹ️ О точности", StatCb(act="info")))
    kb.row(button("🏠 Меню", Nav(to="main")))
    return "\n".join(lines), kb.as_markup()


def _best_line(st: PubStats, value: str) -> str:
    return (f"<b>{esc(truncate(st.title, 45))}</b> — {esc(st.author_name)}\n"
            f"    {link(st.url, 'ссылка')} · 👁 {fmt_int(st.total)} · {value}")


async def render_best(app: App) -> tuple[str, InlineKeyboardMarkup]:
    items = list((await app.analytics.get_all()).values())
    lines = ["🏆 <b>Лучшие публикации</b>", ""]
    if not items:
        lines.append("Публикаций пока нет.")

    def top(sort: str, n: int = 1) -> list[PubStats]:
        return sort_stats(items, sort)[:n]

    sections = [
        ("1️⃣ Больше всего просмотров за 24 часа", "d1", lambda s: f"за 24 ч {growth_short(s.g24)}", 1),
        ("2️⃣ Больше всего за 7 дней", "d7", lambda s: f"за 7 дн. {growth_short(s.g7)}", 1),
        ("3️⃣ Больше всего за 30 дней", "d30", lambda s: f"за 30 дн. {growth_short(s.g30)}", 1),
        ("4️⃣ Больше всего просмотров за всё время", "v", lambda s: f"за 24 ч {growth_short(s.g24)}", 1),
        ("5️⃣ ⚡ Самый быстрый прирост (просмотров в час)", "fast", lambda s: sort_metric(s, "fast"), 2),
        ("6️⃣ 🚀 Самые перспективные (рост к прошлой неделе)", "prom", lambda s: sort_metric(s, "prom"), 2),
        ("🔥 Сейчас набирают популярность", "trend", lambda s: sort_metric(s, "trend"), 3),
        ("⚠️ Публикации с низкой активностью", "low", lambda s: sort_metric(s, "low"), 3),
    ]
    if items:
        for title, sort, fmt, n in sections:
            best = top(sort, n)
            if sort in {"d1", "d7", "d30"}:
                best = [b for b in best if b.growth({"d1": "24h", "d7": "7d", "d30": "30d"}[sort]).value > 0]
            lines.append(f"<b>{title}</b>")
            if not best:
                lines.append("    <i>нет данных или подходящих публикаций</i>")
            for st in best:
                lines.append(_best_line(st, fmt(st) or ""))
            lines.append("")
        lines.append("<i>Прирост — расчёт по снимкам счётчика getViews; ≈ и * — ограниченная точность.\n"
                     "«Набирают популярность» учитывает ускорение относительно обычной скорости публикации, "
                     "а не только объём просмотров.</i>")
    kb = InlineKeyboardBuilder()
    kb.row(button("Топ 24 ч", PubList(ctx="best", sort="d1")), button("Топ 7 дн.", PubList(ctx="best", sort="d7")),
           button("Топ 30 дн.", PubList(ctx="best", sort="d30")))
    kb.row(button("Топ за всё время", PubList(ctx="best", sort="v")),
           button("⚡ Быстрые", PubList(ctx="best", sort="fast")))
    kb.row(button("🚀 Перспективные", PubList(ctx="best", sort="prom")),
           button("🔥 Набирают", PubList(ctx="best", sort="trend")))
    kb.row(button("⚠️ Низкая активность", PubList(ctx="best", sort="low")))
    kb.row(button("🏠 Меню", Nav(to="main")))
    return "\n".join(lines), kb.as_markup()


ACCURACY_TEXT = (
    "ℹ️ <b>Откуда берутся цифры</b>\n\n"
    "<b>Факт (Telegraph API):</b>\n"
    "• «Всего просмотров» — метод getViews без даты.\n"
    "• «По календарным суткам» — getViews с year/month/day. Границы суток определяет Telegraph "
    "(часовой пояс сервиса не документирован).\n\n"
    "<b>Расчёт (по снимкам):</b>\n"
    "Telegraph не умеет отдавать «последние 24 часа / 7 / 30 дней». Бот каждые N минут сохраняет "
    "значение счётчика, а прирост за период = сумма изменений счётчика между снимком на начало периода "
    "и последним снимком. Погрешность — не больше интервала сбора.\n\n"
    "<b>Пометки:</b>\n"
    "• «с момента создания» — публикация моложе периода, расчёт от нуля просмотров;\n"
    "• ≈ — между снимками был перерыв (бот был выключен), окно расчёта шире периода;\n"
    "• * / «данные только за …» — история короче периода;\n"
    "• если Telegraph уменьшил счётчик (корректировка), уменьшение не вычитается из прироста и показывается отдельно.\n\n"
    "<b>Статусы:</b> сравнивается прирост за последние 24 ч с «обычной скоростью» — средним из прироста за "
    "предыдущие 24 ч и среднесуточного прироста за остальные дни недели. Новые публикации (младше порога) "
    "получают статус «Мало данных», а не «спад». Пороги — в Настройках.\n\n"
    "<b>Без накрутки:</b> бот никогда не открывает страницы telegra.ph — только запросы к api.telegra.ph, "
    "они не засчитываются как просмотры. Предпросмотр ссылок в сообщениях бота отключён.\n\n"
    "Telegraph API не предоставляет географию просмотров — такие данные не показываются."
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
