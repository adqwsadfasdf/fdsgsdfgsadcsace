"""Разделы «📥 Добавить готовые» (свои публикации, созданные не ботом) и «🕵️ Конкуренты».

Статистика по ним собирается так же, как по своим: только запросы к api.telegra.ph (getViews),
страницы telegra.ph не открываются — просмотров не накручивается.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from app_context import App
from database import Publication
from handlers.common import answer, error_text, safe_edit, send_long
from handlers.states import TrackInput
from keyboards.callbacks import CompCb, ExportCb, ImpCb, Nav, PubAct, PubCard, PubList
from keyboards.inline import button, clamp_page, number_buttons, pagination_row
from keyboards.navigation import ListNav, cancel_kb
from services import tracking
from services.analytics import PubStats
from services.statistics_collector import CollectorBusy
from utils.formatting import STATUS_LABELS, esc, fmt_ago, fmt_date, fmt_delta, fmt_dt, fmt_float, fmt_int, link, truncate
from utils.render import growth_text
from utils.validators import ValidationError

logger = logging.getLogger(__name__)
router = Router(name="tracking")

COMP_PAGE = 10
LINKS_HELP = (
    "Пришлите ссылки на страницы Telegraph — <b>каждую с новой строки</b> (можно сразу много, до "
    f"{tracking.MAX_LINKS}). Подходят telegra.ph и graph.org.\n\n"
    "<i>Бот не открывает страницы: данные берутся только через Telegraph API (getPage/getViews), "
    "поэтому добавление не добавляет просмотров.</i>"
)


def _busy_text() -> str:
    return "⏳ Уже выполняется другая массовая операция. Дождитесь её завершения."


async def _progress_factory(message: Message, title: str):
    progress_msg = await message.answer(f"⏳ {title}: 0")

    async def progress(done: int, total: int) -> None:
        if done == total or done % 5 == 0:
            await safe_edit(progress_msg, f"⏳ {title}: {done} из {total}")

    return progress_msg, progress


def report_text(result: tracking.TrackResult, competitor: bool, invalid: list[str] | None = None,
                duplicates: int = 0) -> str:
    who = " конкурента" if competitor else ""
    lines = [f"✅ <b>Добавлено публикаций{who}: {len(result.added)}</b>"]
    for _, title, views in result.added[:30]:
        lines.append(f"• {esc(truncate(title, 60))} — 👁 {fmt_int(views)}")
    if len(result.added) > 30:
        lines.append(f"… и ещё {len(result.added) - 30}")
    if result.moved:
        lines.append(f"\n🔀 Перенесено из конкурентов в ваши (история сохранена): {len(result.moved)}")
    if result.existing_own:
        lines.append(f"\n↩️ Уже есть среди ваших публикаций: {len(result.existing_own)}")
    if result.existing_comp:
        lines.append(f"↩️ Уже отслеживаются как публикации конкурентов: {len(result.existing_comp)}")
    if duplicates:
        lines.append(f"♻️ Повторы в списке пропущены: {duplicates}")
    if result.failed:
        lines.append(f"\n❗ Не добавлено: {len(result.failed)}")
        for path, err in result.failed[:15]:
            lines.append(f"• {esc(path)} — {esc(err)}")
    if invalid:
        lines.append(f"\n⚠️ Не похоже на ссылку Telegraph ({len(invalid)}): "
                     + ", ".join(esc(x) for x in invalid[:10]))
    if not competitor and result.not_owned:
        lines.append(
            f"\nℹ️ {result.not_owned} из них созданы в другом аккаунте Telegraph: статистика отслеживается, "
            "но редактировать их из бота нельзя. Чтобы можно было — подключите токен того аккаунта "
            "(«🔑 Страницы другого аккаунта»)."
        )
    if result.added:
        lines.append(
            "\n<i>Прирост считается с момента добавления (раньше бот за ними не следил). Фактические просмотры "
            "по дням за последние 30 дней подгружаются в фоне.</i>"
        )
    return "\n".join(lines)


# ====================================================================== свои готовые


async def render_import_menu(app: App) -> tuple[str, InlineKeyboardMarkup]:
    imported = sum(1 for s in (await app.analytics.get_all()).values() if s.kind == "imported")
    accounts = await app.accounts.list_accounts()
    text = (
        "📥 <b>Добавить готовые публикации</b>\n\n"
        "Бот будет следить за просмотрами ваших страниц, созданных не через него (вручную, другим ботом и т.п.). "
        "Они попадут во все разделы: статистику, рейтинг авторов, поиск, избранное, экспорт.\n\n"
        "• <b>По ссылкам</b> — пришлите ссылки, каждую с новой строки.\n"
        "• <b>Все страницы аккаунта</b> — бот сам возьмёт список страниц аккаунта Telegraph (getPageList).\n"
        "• <b>Другой аккаунт</b> — если страницы созданы под другим токеном Telegraph.\n\n"
        f"Уже добавлено готовых: <b>{imported}</b> · подключено аккаунтов Telegraph: {len(accounts)}"
    )
    kb = InlineKeyboardBuilder()
    kb.row(button("🔗 По ссылкам", ImpCb(act="links")))
    kb.row(button("🧾 Все страницы аккаунта", ImpCb(act="accs")))
    kb.row(button("🔑 Страницы другого аккаунта", ImpCb(act="token")))
    if imported:
        kb.row(button(f"📋 Добавленные ({imported})", PubList(ctx="imp", sort="v")))
    kb.row(button("🏠 Меню", Nav(to="main")))
    return text, kb.as_markup()


@router.callback_query(ImpCb.filter(F.act == "menu"))
async def imp_menu(cb: CallbackQuery, state: FSMContext, app: App) -> None:
    await state.clear()
    await safe_edit(cb.message, *(await render_import_menu(app)))
    await answer(cb)


@router.callback_query(ImpCb.filter(F.act == "links"))
async def imp_links_start(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TrackInput.own_links)
    await cb.message.answer("🔗 <b>Свои готовые публикации</b>\n\n" + LINKS_HELP, reply_markup=cancel_kb())
    await answer(cb)


async def _run_track(message: Message, state: FSMContext, app: App, *, competitor: bool,
                     competitor_id: int | None = None) -> None:
    try:
        parsed = tracking.parse_links(message.text or "")
    except ValidationError as exc:
        await message.answer(f"❗ {exc}", reply_markup=cancel_kb())
        return
    if not parsed.paths:
        await message.answer("❗ Не нашёл ни одной ссылки Telegraph. Пример: https://telegra.ph/Primer-11-08",
                             reply_markup=cancel_kb())
        return
    if app.mass_lock.locked():
        await message.answer(_busy_text())
        return
    await state.clear()
    async with app.mass_lock:
        progress_msg, progress = await _progress_factory(message, "Добавляю публикации")
        result = await tracking.track_paths(app, parsed.paths, competitor=competitor,
                                            competitor_id=competitor_id, progress=progress)
    text = report_text(result, competitor, parsed.invalid, parsed.duplicates)
    kb = InlineKeyboardBuilder()
    if competitor:
        kb.row(button("📋 Публикации конкурента", PubList(ctx="compc", arg=competitor_id or 0, sort="v")))
        kb.row(button("➕ Добавить ещё", CompCb(act="addto", id=competitor_id or 0)),
               button("🕵️ Конкуренты", CompCb(act="menu")))
    else:
        kb.row(button("📋 Добавленные публикации", PubList(ctx="imp", sort="v")))
        kb.row(button("➕ Добавить ещё", ImpCb(act="links")), button("📥 К разделу", ImpCb(act="menu")))
    await safe_edit(progress_msg, "✅ Готово")
    await send_long(message, text, kb.as_markup())


@router.message(TrackInput.own_links, F.text)
async def imp_links_input(message: Message, state: FSMContext, app: App) -> None:
    await _run_track(message, state, app, competitor=False)


@router.callback_query(ImpCb.filter(F.act == "accs"))
async def imp_accounts(cb: CallbackQuery, app: App) -> None:
    await answer(cb)
    accounts = await app.accounts.list_accounts()
    kb = InlineKeyboardBuilder()
    lines = ["🧾 <b>Выберите аккаунт Telegraph</b>", "",
             "Бот добавит все его страницы, которых ещё нет в базе.", ""]
    for acc in accounts:
        try:
            info = await app.telegraph.get_account_info(acc.access_token, ["short_name", "page_count"])
            count = info.page_count
        except Exception as exc:  # noqa: BLE001
            count = None
            logger.info("getAccountInfo #%s: %s", acc.id, error_text(exc))
        title = f"{acc.short_name}{' (основной)' if acc.is_active else ''}"
        lines.append(f"• {esc(title)} — страниц: {count if count is not None else '—'}")
        kb.row(button(f"{truncate(title, 30)} · {count if count is not None else '?'} стр.",
                      ImpCb(act="acc", id=acc.id)))
    kb.row(button("🔑 Подключить другой аккаунт", ImpCb(act="token")))
    kb.row(button("⬅️ Назад", ImpCb(act="menu")))
    await safe_edit(cb.message, "\n".join(lines), kb.as_markup())


@router.callback_query(ImpCb.filter(F.act == "acc"))
async def imp_account_confirm(cb: CallbackQuery, callback_data: ImpCb) -> None:
    kb = InlineKeyboardBuilder()
    kb.row(button("✅ Добавить все страницы", ImpCb(act="accy", id=callback_data.id)),
           button("Отмена", ImpCb(act="accs")))
    await safe_edit(cb.message, "Добавить в бота все страницы этого аккаунта Telegraph, которых ещё нет в базе?\n\n"
                                "<i>Уже добавленные пропускаются. Страницы не изменяются.</i>", kb.as_markup())
    await answer(cb)


@router.callback_query(ImpCb.filter(F.act == "accy"))
async def imp_account_run(cb: CallbackQuery, callback_data: ImpCb, app: App) -> None:
    if app.mass_lock.locked():
        await answer(cb, _busy_text(), alert=True)
        return
    await answer(cb, "Начинаю…")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    async with app.mass_lock:
        progress_msg, progress = await _progress_factory(cb.message, "Добавляю страницы аккаунта")
        try:
            result = await tracking.import_account(app, callback_data.id, progress=progress)
        except Exception as exc:  # noqa: BLE001
            await safe_edit(progress_msg, f"❗ Не удалось получить список страниц: {esc(error_text(exc))}")
            return
    await safe_edit(progress_msg, "✅ Готово")
    kb = InlineKeyboardBuilder()
    kb.row(button("📋 Добавленные публикации", PubList(ctx="imp", sort="v")))
    kb.row(button("📥 К разделу", ImpCb(act="menu")))
    await send_long(cb.message, report_text(result, False), kb.as_markup())


@router.callback_query(ImpCb.filter(F.act == "token"))
async def imp_token_start(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TrackInput.token)
    await cb.message.answer(
        "🔑 <b>Подключение другого аккаунта Telegraph</b>\n\n"
        "Пришлите <b>access_token</b> аккаунта, в котором созданы ваши страницы. Сообщение с токеном бот "
        "сразу удалит из чата, а сам токен сохранит только в своей базе.\n\n"
        "<i>Зачем: бот проверит, что страницы ваши, сможет добавить их все разом и редактировать их. "
        "Новые публикации по-прежнему создаются основным аккаунтом.</i>",
        reply_markup=cancel_kb(),
    )
    await answer(cb)


@router.message(TrackInput.token, F.text)
async def imp_token_input(message: Message, state: FSMContext, app: App) -> None:
    token = (message.text or "").strip()
    try:
        await message.delete()  # не оставляем токен в истории чата
    except Exception:  # noqa: BLE001
        pass
    try:
        account, info, new = await app.accounts.add_existing(token)
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"❗ Токен не принят Telegraph: {esc(error_text(exc))}. Проверьте и пришлите ещё раз.",
                             reply_markup=cancel_kb())
        return
    await state.clear()
    # страницы, добавленные раньше по ссылкам, теперь можно редактировать
    owners = await tracking.owned_paths(app)
    async with app.db() as session:
        rows = (await session.execute(
            select(Publication).where(Publication.kind == "imported").where(Publication.account_id.is_(None))
        )).scalars().all()
        fixed = 0
        for pub in rows:
            if pub.path in owners:
                pub.account_id = owners[pub.path]
                fixed += 1
        await session.commit()
    app.analytics.invalidate()
    kb = InlineKeyboardBuilder()
    kb.row(button(f"✅ Добавить все его страницы ({info.page_count or 0})", ImpCb(act="accy", id=account.id)))
    kb.row(button("📥 К разделу", ImpCb(act="menu")))
    await message.answer(
        f"✅ Аккаунт <b>{esc(account.short_name)}</b> {'подключён' if new else 'уже был подключён'}. "
        f"Страниц в нём: {info.page_count if info.page_count is not None else '—'}."
        + (f"\nРанее добавленных публикаций стало доступно для редактирования: {fixed}." if fixed else "")
        + "\n\n<i>Сообщение с токеном удалено из чата.</i>",
        reply_markup=kb.as_markup(),
    )


# ====================================================================== конкуренты


def _avg(items: list[PubStats], attr: str) -> float | None:
    if not items:
        return None
    if attr == "total":
        return sum(s.total for s in items) / len(items)
    usable = [s for s in items if s.g24.quality in {"exact", "creation", "approx"}]
    if not usable:
        return None
    return sum(s.g24.value for s in usable) / len(usable)


async def render_comp_menu(app: App, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    comps = sorted(await tracking.competitor_stats(app), key=lambda c: (c.total, c.count), reverse=True)
    comp_pubs = list((await app.analytics.get_competitors()).values())
    own_pubs = list((await app.analytics.get_all()).values())
    lines = [
        "🕵️ <b>Конкуренты</b>",
        "",
        "Добавьте ссылки на публикации конкурентов — бот будет следить за их просмотрами через Telegraph API "
        "(getViews). Сами страницы не открываются, просмотров им не добавляется. "
        "Конкуренты не попадают в вашу статистику, рейтинг авторов и уведомления.",
        "",
        f"Конкурентов: <b>{len([c for c in comps if c.id])}</b> · публикаций: <b>{len(comp_pubs)}</b>",
    ]
    if comp_pubs:
        total = sum(s.total for s in comp_pubs)
        g24 = sum(s.g24.value for s in comp_pubs)
        lines.append(f"👁 Всего просмотров: <b>{fmt_int(total)}</b> · за 24 ч: {fmt_delta(g24)}")
        lines += ["", "📊 <b>Сравнение — в среднем на одну публикацию</b>"]
        for title, items in (("Ваши", own_pubs), ("Конкуренты", comp_pubs)):
            avg_total, avg24 = _avg(items, "total"), _avg(items, "g24")
            lines.append(
                f"• {title}: 👁 {fmt_float(avg_total) if avg_total is not None else '—'} всего · "
                f"{('+' + fmt_float(avg24)) if avg24 is not None else '—'} за 24 ч"
            )
        lines.append("<i>Прирост за 24 ч считается по публикациям, у которых есть история за сутки.</i>")
    page = clamp_page(page, len(comps), COMP_PAGE)
    chunk = comps[page * COMP_PAGE : (page + 1) * COMP_PAGE]
    if chunk:
        lines += ["", "<b>Рейтинг конкурентов</b> (по просмотрам):"]
    numbered = []
    for i, c in enumerate(chunk, start=page * COMP_PAGE + 1):
        g24 = fmt_delta(c.g24) + ("*" if c.partial24 and c.count else "")
        lines.append(f"<b>{i}.</b> {esc(truncate(c.name, 40))} — {c.count} публ. · 👁 {fmt_int(c.total)} · 24ч {g24}")
        numbered.append((str(i), CompCb(act="card", id=c.id)))
    if any(c.partial24 for c in chunk):
        lines.append("<i>* — неполные данные: история меньше суток.</i>")
    kb = InlineKeyboardBuilder()
    number_buttons(kb, numbered)
    pag = pagination_row(page, len(comps), COMP_PAGE, lambda p: CompCb(act="menu", page=p))
    if pag:
        kb.row(*pag)
    kb.row(button("➕ Добавить публикации конкурента", CompCb(act="add")))
    if comp_pubs:
        kb.row(button(f"📋 Все публикации ({len(comp_pubs)})", PubList(ctx="comp", sort="v")),
               button("🔥 Растут сейчас", PubList(ctx="comp", sort="d1")))
        kb.row(button("🔄 Обновить просмотры", CompCb(act="refresh")),
               button("📥 Excel", ExportCb(act="Lcomp")))
    kb.row(button("🏠 Меню", Nav(to="main")))
    return "\n".join(lines), kb.as_markup()


@router.callback_query(CompCb.filter(F.act == "menu"))
async def comp_menu(cb: CallbackQuery, callback_data: CompCb, state: FSMContext, app: App) -> None:
    await state.clear()
    await safe_edit(cb.message, *(await render_comp_menu(app, callback_data.page)))
    await answer(cb)


async def render_comp_card(app: App, competitor_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    comps = {c.id: c for c in await tracking.competitor_stats(app)}
    c = comps.get(competitor_id)
    if c is None:
        if competitor_id:
            return None
        c = tracking.CompetitorStats(id=0, name="Без группы")
    items = [s for s in (await app.analytics.get_competitors()).values() if (s.competitor_id or 0) == competitor_id]
    lines = [
        f"🕵️ <b>{esc(c.name)}</b>",
        "",
        f"Публикаций: <b>{c.count}</b>",
        f"👁 Всего просмотров: <b>{fmt_int(c.total)}</b> <i>(факт getViews)</i>",
        f"В среднем на публикацию: {fmt_float(c.avg)}",
        f"📈 За 24 часа: {fmt_delta(c.g24)}{' <i>(неполные данные)</i>' if c.partial24 and c.count else ''}",
        f"📈 За 7 дней: {fmt_delta(c.g7)}{' <i>(неполные данные)</i>' if c.partial7 and c.count else ''}",
    ]
    if c.best_id:
        best = await app.analytics.get(c.best_id)
        if best:
            lines.append(f"🏆 Лучшая: {link(best.url, truncate(best.title, 60))} — 👁 {fmt_int(best.total)}")
    growing = sorted([s for s in items if s.g24.value > 0], key=lambda s: s.g24.value, reverse=True)[:3]
    if growing:
        lines += ["", "🔥 <b>Больше всего набрали за 24 ч:</b>"]
        for s in growing:
            lines.append(f"• {link(s.url, truncate(s.title, 50))} — {fmt_delta(s.g24.value)}")
    kb = InlineKeyboardBuilder()
    if c.count:
        kb.row(button("📋 Публикации", PubList(ctx="compc", arg=competitor_id, sort="v")),
               button("🆕 Новые", PubList(ctx="compc", arg=competitor_id, sort="new")))
    kb.row(button("➕ Добавить ссылки", CompCb(act="addto", id=competitor_id)))
    if competitor_id:
        kb.row(button("✏️ Переименовать", CompCb(act="ren", id=competitor_id)),
               button("🗑 Удалить", CompCb(act="del", id=competitor_id)))
    elif c.count:
        kb.row(button("🗑 Удалить все без группы", CompCb(act="del", id=0)))
    kb.row(button("⬅️ Конкуренты", CompCb(act="menu")))
    return "\n".join(lines), kb.as_markup()


@router.callback_query(CompCb.filter(F.act == "card"))
async def comp_card(cb: CallbackQuery, callback_data: CompCb, state: FSMContext, app: App) -> None:
    await state.clear()
    rendered = await render_comp_card(app, callback_data.id)
    if rendered is None:
        await answer(cb, "Конкурент не найден (возможно, удалён)", alert=True)
        return
    await safe_edit(cb.message, *rendered)
    await answer(cb)


@router.callback_query(CompCb.filter(F.act == "add"))
async def comp_add(cb: CallbackQuery, app: App) -> None:
    comps = [c for c in await tracking.competitor_stats(app) if c.id]
    kb = InlineKeyboardBuilder()
    kb.row(button("🆕 Новый конкурент", CompCb(act="new")))
    for c in comps[:40]:
        kb.button(text=truncate(c.name, 28), callback_data=CompCb(act="addto", id=c.id))
    kb.adjust(1, 2)
    kb.row(button("Без группы", CompCb(act="addto", id=0)))
    kb.row(button("⬅️ Назад", CompCb(act="menu")))
    await safe_edit(
        cb.message,
        "➕ <b>Чьи это публикации?</b>\n\nВыберите конкурента или создайте нового (например, название канала). "
        "Группировка нужна, чтобы сравнивать конкурентов между собой.",
        kb.as_markup(),
    )
    await answer(cb)


@router.callback_query(CompCb.filter(F.act == "new"))
async def comp_new(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TrackInput.comp_name)
    await cb.message.answer(f"🆕 Отправьте название конкурента (до {tracking.COMPETITOR_NAME_MAX} символов), "
                            "например «Канал Иван Петров».", reply_markup=cancel_kb())
    await answer(cb)


@router.message(TrackInput.comp_name, F.text)
async def comp_name_input(message: Message, state: FSMContext, app: App) -> None:
    try:
        competitor = await tracking.get_or_create_competitor(app, message.text)
    except ValidationError as exc:
        await message.answer(f"❗ {exc}", reply_markup=cancel_kb())
        return
    await state.set_state(TrackInput.comp_links)
    await state.update_data(competitor_id=competitor.id)
    await message.answer(f"🕵️ Конкурент: <b>{esc(competitor.name)}</b>\n\n" + LINKS_HELP, reply_markup=cancel_kb())


@router.callback_query(CompCb.filter(F.act == "addto"))
async def comp_add_to(cb: CallbackQuery, callback_data: CompCb, state: FSMContext, app: App) -> None:
    name = await tracking.competitor_name(app, callback_data.id)
    await state.set_state(TrackInput.comp_links)
    await state.update_data(competitor_id=callback_data.id)
    await cb.message.answer(f"🕵️ Конкурент: <b>{esc(name)}</b>\n\n" + LINKS_HELP, reply_markup=cancel_kb())
    await answer(cb)


@router.message(TrackInput.comp_links, F.text)
async def comp_links_input(message: Message, state: FSMContext, app: App) -> None:
    competitor_id = int((await state.get_data()).get("competitor_id") or 0)
    await _run_track(message, state, app, competitor=True, competitor_id=competitor_id or None)


@router.callback_query(CompCb.filter(F.act == "ren"))
async def comp_rename_start(cb: CallbackQuery, callback_data: CompCb, state: FSMContext) -> None:
    await state.set_state(TrackInput.comp_rename)
    await state.update_data(competitor_id=callback_data.id)
    await cb.message.answer("✏️ Отправьте новое название конкурента.", reply_markup=cancel_kb())
    await answer(cb)


@router.message(TrackInput.comp_rename, F.text)
async def comp_rename_input(message: Message, state: FSMContext, app: App) -> None:
    competitor_id = int((await state.get_data()).get("competitor_id") or 0)
    try:
        await tracking.rename_competitor(app, competitor_id, message.text)
    except (ValidationError, LookupError) as exc:
        await message.answer(f"❗ {exc}", reply_markup=cancel_kb())
        return
    await state.clear()
    rendered = await render_comp_card(app, competitor_id)
    if rendered:
        await message.answer("✅ Переименовано\n\n" + rendered[0], reply_markup=rendered[1])


@router.callback_query(CompCb.filter(F.act == "del"))
async def comp_delete(cb: CallbackQuery, callback_data: CompCb, app: App) -> None:
    name = await tracking.competitor_name(app, callback_data.id)
    count = sum(1 for s in (await app.analytics.get_competitors()).values()
                if (s.competitor_id or 0) == callback_data.id)
    kb = InlineKeyboardBuilder()
    kb.row(button("🗑 Да, удалить", CompCb(act="dely", id=callback_data.id)),
           button("Отмена", CompCb(act="card", id=callback_data.id)))
    await safe_edit(
        cb.message,
        f"🗑 Удалить {'конкурента «' + esc(name) + '» и ' if callback_data.id else ''}"
        f"его публикации ({count}) из отслеживания?\n\n"
        "• Удаляются только записи и история статистики в боте.\n"
        "• Сами страницы Telegraph никак не затрагиваются.\n"
        "• Действие необратимо.",
        kb.as_markup(),
    )
    await answer(cb)


@router.callback_query(CompCb.filter(F.act == "dely"))
async def comp_delete_yes(cb: CallbackQuery, callback_data: CompCb, app: App) -> None:
    count = await tracking.delete_competitor(app, callback_data.id)
    await safe_edit(cb.message, *(await render_comp_menu(app)))
    await answer(cb, f"Удалено из отслеживания: {count} публ.")


@router.callback_query(CompCb.filter(F.act == "refresh"))
async def comp_refresh(cb: CallbackQuery, app: App) -> None:
    ids = list((await app.analytics.get_competitors()).keys())
    if not ids:
        await answer(cb, "Публикаций конкурентов нет", alert=True)
        return
    await answer(cb, f"Запрашиваю getViews для {len(ids)} публикаций…")
    try:
        result = await app.collector.refresh_many(ids)
    except CollectorBusy as exc:
        await cb.message.answer(f"⏳ {esc(exc)}")
        return
    await safe_edit(cb.message, *(await render_comp_menu(app)))
    note = f"🔄 Обновлено: {result.ok} из {result.total}"
    if result.failed:
        note += f", ошибок: {result.failed}"
    if result.decreased:
        note += f"\nℹ️ У {result.decreased} публ. Telegraph уменьшил счётчик (корректировка, не вычитается)."
    await cb.message.answer(note)


# ---------------------------------------------------------------------- перенос публикации


@router.callback_query(CompCb.filter(F.act == "mv"))
async def comp_move(cb: CallbackQuery, callback_data: CompCb, app: App) -> None:
    comps = [c for c in await tracking.competitor_stats(app) if c.id]
    kb = InlineKeyboardBuilder()
    for c in comps[:40]:
        kb.button(text=truncate(c.name, 28), callback_data=CompCb(act="mvto", id=callback_data.id, to=c.id))
    kb.adjust(2)
    kb.row(button("Без группы", CompCb(act="mvto", id=callback_data.id, to=0)))
    kb.row(button("⬅️ Назад", PubCard(id=callback_data.id)))
    await safe_edit(cb.message, "🔀 К какому конкуренту отнести публикацию?", kb.as_markup())
    await answer(cb)


@router.callback_query(CompCb.filter(F.act == "mvto"))
async def comp_move_to(cb: CallbackQuery, callback_data: CompCb, app: App) -> None:
    try:
        await tracking.move_to_competitor(app, callback_data.id, callback_data.to)
    except LookupError as exc:
        await answer(cb, str(exc), alert=True)
        return
    rendered = await render_competitor_pub_card(app, callback_data.id, "")
    if rendered:
        await safe_edit(cb.message, *rendered)
    await answer(cb, "Перенесено")


# ---------------------------------------------------------------------- карточка публикации конкурента


async def render_competitor_pub_card(app: App, pub_id: int, nav_raw: str) -> tuple[str, InlineKeyboardMarkup] | None:
    st = await app.analytics.get(pub_id)
    if st is None or not st.is_competitor:
        return None
    tz = app.config.timezone
    daily = await app.analytics.daily_summary(pub_id, st.created_ts)
    name = await tracking.competitor_name(app, st.competitor_id)
    lines = [
        f"🕵️ <b>{esc(st.title)}</b>",
        f"Конкурент: <b>{esc(name)}</b>",
        f"👤 Автор на странице: {esc(st.author_name)}",
        f"🔗 {link(st.url, st.url)}",
        f"📅 Создана: {fmt_date(st.created_ts, tz)} <i>(по дате в ссылке)</i>",
        f"🔭 Отслеживается с: {fmt_dt(st.tracked_ts, tz) if st.tracked_ts else '—'}",
        f"Статус: {STATUS_LABELS.get(st.status, st.status)}",
        "",
        f"👁 <b>Всего просмотров: {fmt_int(st.total)}</b>",
        f"<i>факт Telegraph API (getViews), обновлено {fmt_ago(st.updated_ts)}</i>",
    ]
    if st.sync_error:
        lines.append(f"❗ Последняя ошибка обновления: {esc(st.sync_error)}")
    lines += [
        "",
        "📈 <b>Прирост</b> <i>(по снимкам счётчика с момента добавления)</i>",
        f"• За 24 часа: {growth_text(st.g24)}",
        f"• За 7 дней: {growth_text(st.g7)}",
        f"• За 30 дней: {growth_text(st.g30)}",
        "",
        "📅 <b>По календарным суткам</b> <i>(факт getViews по датам)</i>",
    ]
    if daily.days:
        lines.append(
            f"сегодня: {fmt_int(daily.today) if daily.today is not None else '—'} · "
            f"вчера: {fmt_int(daily.yesterday) if daily.yesterday is not None else '—'} · "
            f"7 дн.: {fmt_int(daily.sum7)} · 30 дн.: {fmt_int(daily.sum30)}"
        )
        for day, views, final in daily.days[:7]:
            _, m, d = day.split("-")
            lines.append(f"  {d}.{m}: {fmt_int(views)}{'' if final else ' (сутки не завершены)'}")
    else:
        lines.append("<i>ещё загружаются (это займёт несколько минут после добавления)</i>")
    nav = ListNav.decode(nav_raw)
    kb = InlineKeyboardBuilder()
    kb.row(button("🔄 Обновить сейчас", PubAct(act="refresh", id=pub_id, nav=nav_raw)),
           button("🔀 Другой конкурент", CompCb(act="mv", id=pub_id)))
    kb.row(button("🗑 Убрать из отслеживания", PubAct(act="del", id=pub_id, nav=nav_raw)))
    if nav:
        kb.row(button("⬅️ К списку", nav.to_list_cb()))
    kb.row(button("🕵️ К конкуренту", CompCb(act="card", id=st.competitor_id or 0)))
    return "\n".join(lines), kb.as_markup()


@router.message(TrackInput.own_links)
@router.message(TrackInput.token)
@router.message(TrackInput.comp_name)
@router.message(TrackInput.comp_links)
@router.message(TrackInput.comp_rename)
async def input_not_text(message: Message) -> None:
    await message.answer("Пожалуйста, отправьте текстовое сообщение или нажмите «Отмена».", reply_markup=cancel_kb())


__all__ = ["router", "render_import_menu", "render_comp_menu", "render_competitor_pub_card"]
