"""Разделы «📥 Свои готовые» (свои публикации, созданные не ботом) и «🕵️ Конкуренты».

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
from keyboards.callbacks import CompCb, ImpCb, Nav, PubAct, PubCard, PubList
from keyboards.inline import button, clamp_page, number_buttons, pagination_row
from keyboards.navigation import ListNav, cancel_kb
from services import tracking
from services.analytics import sort_stats
from services.statistics_collector import CollectorBusy
from utils.formatting import esc, fmt_ago, fmt_date, fmt_int, link, truncate
from utils.render import list_item, stats_line, sum_line
from utils.validators import ValidationError

logger = logging.getLogger(__name__)
router = Router(name="tracking")

PAGE = 10
LINKS_HELP = "Пришлите ссылки Telegraph — каждую с новой строки."


def _busy_text() -> str:
    return "⏳ Уже выполняется другая операция, подождите."


def report_text(result: tracking.TrackResult, competitor: bool, invalid: list[str] | None = None,
                duplicates: int = 0) -> str:
    lines = [f"✅ Добавлено: <b>{len(result.added)}</b>"]
    for _, title, views in result.added[:10]:
        lines.append(f"• {esc(truncate(title, 40))} — 👁 {fmt_int(views)}")
    if len(result.added) > 10:
        lines.append(f"… ещё {len(result.added) - 10}")
    if result.moved:
        where = "в конкуренты из ваших" if competitor else "в ваши из конкурентов"
        lines.append(f"🔀 Перенесено {where}: {len(result.moved)}")
    if result.existing_own:
        lines.append(
            f"↩️ Это ваши публикации (созданы ботом), в конкуренты не добавлены: {len(result.existing_own)}"
            if competitor else f"↩️ Уже есть в ваших: {len(result.existing_own)}"
        )
    if result.existing_comp:
        lines.append(f"↩️ Уже есть в конкурентах: {len(result.existing_comp)}")
    if duplicates:
        lines.append(f"♻️ Повторы пропущены: {duplicates}")
    for path, err in result.failed[:5]:
        lines.append(f"❗ {esc(truncate(path, 40))} — {esc(err)}")
    if len(result.failed) > 5:
        lines.append(f"❗ … и ещё ошибок: {len(result.failed) - 5}")
    if invalid:
        lines.append(f"⚠️ Не ссылки Telegraph: {', '.join(esc(truncate(x, 25)) for x in invalid[:5])}")
    if not competitor and result.not_owned:
        lines.append(f"ℹ️ {result.not_owned} — из другого аккаунта Telegraph: статистика есть, правка недоступна.")
    return "\n".join(lines)


async def _run_track(message: Message, state: FSMContext, app: App, *, competitor: bool,
                     competitor_id: int | None = None) -> None:
    try:
        parsed = tracking.parse_links(message.text or "")
    except ValidationError as exc:
        await message.answer(f"❗ {exc}", reply_markup=cancel_kb())
        return
    if not parsed.paths:
        await message.answer("❗ Не нашёл ссылок Telegraph. Пример: https://telegra.ph/Primer-11-08",
                             reply_markup=cancel_kb())
        return
    if app.mass_lock.locked():
        await message.answer(_busy_text())
        return
    await state.clear()
    async with app.mass_lock:
        progress_msg = await message.answer(f"⏳ Добавляю: 0 из {len(parsed.paths)}")

        async def progress(done: int, total: int) -> None:
            if done < total and done % 5 == 0:
                await safe_edit(progress_msg, f"⏳ Добавляю: {done} из {total}")

        result = await tracking.track_paths(app, parsed.paths, competitor=competitor,
                                            competitor_id=competitor_id, progress=progress)
    kb = InlineKeyboardBuilder()
    if competitor:
        kb.row(button("🕵️ Открыть", CompCb(act="card", id=competitor_id or 0)),
               button("➕ Ещё", CompCb(act="addto", id=competitor_id or 0)))
    else:
        kb.row(button("📋 Открыть", PubList(ctx="imp", sort="v")), button("➕ Ещё", ImpCb(act="links")))
    # одно сообщение: прогресс превращается в отчёт
    await safe_edit(progress_msg, report_text(result, competitor, parsed.invalid, parsed.duplicates), kb.as_markup())


# ====================================================================== свои готовые


async def render_import_menu(app: App) -> tuple[str, InlineKeyboardMarkup]:
    imported = sum(1 for s in (await app.analytics.get_all()).values() if s.kind == "imported")
    text = f"📥 <b>Свои готовые публикации</b> · добавлено: {imported}\nПосты, созданные не через бота."
    kb = InlineKeyboardBuilder()
    kb.row(button("🔗 По ссылкам", ImpCb(act="links")), button("🧾 Весь аккаунт", ImpCb(act="accs")))
    kb.row(button("🔑 Другой аккаунт (токен)", ImpCb(act="token")))
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
    await cb.message.answer("🔗 " + LINKS_HELP, reply_markup=cancel_kb())
    await answer(cb)


@router.message(TrackInput.own_links, F.text)
async def imp_links_input(message: Message, state: FSMContext, app: App) -> None:
    await _run_track(message, state, app, competitor=False)


@router.callback_query(ImpCb.filter(F.act == "accs"))
async def imp_accounts(cb: CallbackQuery, app: App) -> None:
    await answer(cb)
    kb = InlineKeyboardBuilder()
    for acc in await app.accounts.list_accounts():
        try:
            count = (await app.telegraph.get_account_info(acc.access_token, ["short_name", "page_count"])).page_count
        except Exception as exc:  # noqa: BLE001
            count = None
            logger.info("getAccountInfo #%s: %s", acc.id, error_text(exc))
        title = f"{acc.short_name}{' (основной)' if acc.is_active else ''}"
        kb.row(button(f"{truncate(title, 30)} · {count if count is not None else '?'} стр.",
                      ImpCb(act="accy", id=acc.id)))
    kb.row(button("⬅️ Назад", ImpCb(act="menu")))
    await safe_edit(cb.message, "🧾 Какой аккаунт добавить целиком? Уже добавленные страницы пропускаются.",
                    kb.as_markup())


@router.callback_query(ImpCb.filter(F.act == "accy"))
async def imp_account_run(cb: CallbackQuery, callback_data: ImpCb, app: App) -> None:
    if app.mass_lock.locked():
        await answer(cb, _busy_text(), alert=True)
        return
    await answer(cb, "Начинаю…")
    async with app.mass_lock:
        await safe_edit(cb.message, "⏳ Добавляю страницы аккаунта…")
        try:
            result = await tracking.import_account(app, callback_data.id)
        except Exception as exc:  # noqa: BLE001
            await safe_edit(cb.message, f"❗ Не удалось получить список страниц: {esc(error_text(exc))}")
            return
    kb = InlineKeyboardBuilder()
    kb.row(button("📋 Открыть", PubList(ctx="imp", sort="v")), button("⬅️ Назад", ImpCb(act="menu")))
    await safe_edit(cb.message, report_text(result, False), kb.as_markup())


@router.callback_query(ImpCb.filter(F.act == "token"))
async def imp_token_start(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TrackInput.token)
    await cb.message.answer("🔑 Пришлите access_token аккаунта Telegraph. Сообщение с токеном бот сразу удалит.",
                            reply_markup=cancel_kb())
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
        await message.answer(f"❗ Токен не принят: {esc(error_text(exc))}", reply_markup=cancel_kb())
        return
    await state.clear()
    # страницы, добавленные раньше по ссылкам, теперь можно редактировать
    owners = await tracking.owned_paths(app)
    async with app.db() as session:
        rows = (await session.execute(
            select(Publication).where(Publication.kind == "imported").where(Publication.account_id.is_(None))
        )).scalars().all()
        for pub in rows:
            if pub.path in owners:
                pub.account_id = owners[pub.path]
        await session.commit()
    app.analytics.invalidate()
    kb = InlineKeyboardBuilder()
    kb.row(button(f"✅ Добавить все страницы ({info.page_count or 0})", ImpCb(act="accy", id=account.id)))
    kb.row(button("⬅️ Назад", ImpCb(act="menu")))
    await message.answer(f"✅ Аккаунт <b>{esc(account.short_name)}</b> {'подключён' if new else 'уже подключён'}.",
                         reply_markup=kb.as_markup())


# ====================================================================== конкуренты


def _competitor_items(stats: dict, competitor_id: int) -> list:
    return [s for s in stats.values() if (s.competitor_id or 0) == competitor_id]


async def render_comp_menu(app: App, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    comp_stats = await app.analytics.get_competitors()
    comps = await tracking.competitor_stats(app)
    groups = sorted(((c, _competitor_items(comp_stats, c.id)) for c in comps),
                    key=lambda x: sum(s.total for s in x[1]), reverse=True)
    page = clamp_page(page, len(groups), PAGE)
    lines = [f"🕵️ <b>Конкуренты</b> · {len([c for c in comps if c.id])}"]
    if not groups:
        lines.append("Пока пусто. Нажмите «➕ Добавить».")
    numbered = []
    for i, (c, items) in enumerate(groups[page * PAGE : (page + 1) * PAGE], start=page * PAGE + 1):
        lines.append(f"<b>{i}.</b> {esc(truncate(c.name, 25))} · {len(items)} публ.\n{sum_line(items)}")
        numbered.append((str(i), CompCb(act="card", id=c.id)))
    kb = InlineKeyboardBuilder()
    number_buttons(kb, numbered)
    pag = pagination_row(page, len(groups), PAGE, lambda p: CompCb(act="menu", page=p))
    if pag:
        kb.row(*pag)
    kb.row(button("➕ Добавить", CompCb(act="add")), button("🔄 Обновить", CompCb(act="refresh")))
    kb.row(button("🏠 Меню", Nav(to="main")))
    return "\n".join(lines), kb.as_markup()


@router.callback_query(CompCb.filter(F.act == "menu"))
async def comp_menu(cb: CallbackQuery, callback_data: CompCb, state: FSMContext, app: App) -> None:
    await state.clear()
    await safe_edit(cb.message, *(await render_comp_menu(app, callback_data.page)))
    await answer(cb)


async def render_comp_card(app: App, competitor_id: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup] | None:
    name = await tracking.competitor_name(app, competitor_id)
    if competitor_id and name == "без группы":
        return None
    items = sort_stats(_competitor_items(await app.analytics.get_competitors(), competitor_id), "v")
    page = clamp_page(page, len(items), PAGE)
    nav = ListNav("compc", competitor_id, "v", page, 0).encode()
    lines = [f"🕵️ <b>{esc(name if competitor_id else 'Без группы')}</b> · {len(items)} публ.\n{sum_line(items)}", ""]
    if not items:
        lines.append("Публикаций нет. Нажмите «➕ Ссылки».")
    numbered = []
    for i, st in enumerate(items[page * PAGE : (page + 1) * PAGE], start=page * PAGE + 1):
        lines.append(list_item(i, st, "v", show_author=False))
        numbered.append((str(i), PubCard(id=st.id, per="", nav=nav)))
    kb = InlineKeyboardBuilder()
    number_buttons(kb, numbered)
    pag = pagination_row(page, len(items), PAGE, lambda p: CompCb(act="card", id=competitor_id, page=p))
    if pag:
        kb.row(*pag)
    row = [button("➕ Ссылки", CompCb(act="addto", id=competitor_id))]
    if competitor_id:
        row.append(button("✏️ Имя", CompCb(act="ren", id=competitor_id)))
    if competitor_id or items:
        row.append(button("🗑 Удалить", CompCb(act="del", id=competitor_id)))
    kb.row(*row)
    kb.row(button("⬅️ Конкуренты", CompCb(act="menu")))
    return "\n".join(lines), kb.as_markup()


@router.callback_query(CompCb.filter(F.act == "card"))
async def comp_card(cb: CallbackQuery, callback_data: CompCb, state: FSMContext, app: App) -> None:
    await state.clear()
    rendered = await render_comp_card(app, callback_data.id, callback_data.page)
    if rendered is None:
        await answer(cb, "Конкурент не найден (возможно, удалён)", alert=True)
        return
    await safe_edit(cb.message, *rendered)
    await answer(cb)


@router.callback_query(CompCb.filter(F.act == "add"))
async def comp_add(cb: CallbackQuery, app: App) -> None:
    comps = [c for c in await tracking.competitor_stats(app) if c.id]
    kb = InlineKeyboardBuilder()
    kb.row(button("🆕 Новый", CompCb(act="new")))
    for c in comps[:40]:
        kb.button(text=truncate(c.name, 28), callback_data=CompCb(act="addto", id=c.id))
    kb.adjust(1, 2)
    kb.row(button("⬅️ Назад", CompCb(act="menu")))
    await safe_edit(cb.message, "➕ Чьи публикации? Выберите конкурента или создайте нового.", kb.as_markup())
    await answer(cb)


@router.callback_query(CompCb.filter(F.act == "new"))
async def comp_new(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TrackInput.comp_name)
    await cb.message.answer("🆕 Как назвать конкурента? (например, имя или канал)", reply_markup=cancel_kb())
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
    await message.answer(f"🕵️ <b>{esc(competitor.name)}</b>: {LINKS_HELP}", reply_markup=cancel_kb())


@router.callback_query(CompCb.filter(F.act == "addto"))
async def comp_add_to(cb: CallbackQuery, callback_data: CompCb, state: FSMContext, app: App) -> None:
    name = await tracking.competitor_name(app, callback_data.id)
    await state.set_state(TrackInput.comp_links)
    await state.update_data(competitor_id=callback_data.id)
    await cb.message.answer(f"🕵️ <b>{esc(name)}</b>: {LINKS_HELP}", reply_markup=cancel_kb())
    await answer(cb)


@router.message(TrackInput.comp_links, F.text)
async def comp_links_input(message: Message, state: FSMContext, app: App) -> None:
    competitor_id = int((await state.get_data()).get("competitor_id") or 0)
    await _run_track(message, state, app, competitor=True, competitor_id=competitor_id or None)


@router.callback_query(CompCb.filter(F.act == "ren"))
async def comp_rename_start(cb: CallbackQuery, callback_data: CompCb, state: FSMContext) -> None:
    await state.set_state(TrackInput.comp_rename)
    await state.update_data(competitor_id=callback_data.id)
    await cb.message.answer("✏️ Новое имя конкурента:", reply_markup=cancel_kb())
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
        await message.answer(rendered[0], reply_markup=rendered[1])


@router.callback_query(CompCb.filter(F.act == "del"))
async def comp_delete(cb: CallbackQuery, callback_data: CompCb, app: App) -> None:
    name = await tracking.competitor_name(app, callback_data.id)
    count = len(_competitor_items(await app.analytics.get_competitors(), callback_data.id))
    kb = InlineKeyboardBuilder()
    kb.row(button("🗑 Да, удалить", CompCb(act="dely", id=callback_data.id)),
           button("Отмена", CompCb(act="card", id=callback_data.id)))
    await safe_edit(cb.message, f"🗑 Удалить «{esc(name)}» и {count} публ. из отслеживания? "
                                "Страницы Telegraph не затрагиваются.", kb.as_markup())
    await answer(cb)


@router.callback_query(CompCb.filter(F.act == "dely"))
async def comp_delete_yes(cb: CallbackQuery, callback_data: CompCb, app: App) -> None:
    count = await tracking.delete_competitor(app, callback_data.id)
    await safe_edit(cb.message, *(await render_comp_menu(app)))
    await answer(cb, f"Удалено: {count} публ.")


@router.callback_query(CompCb.filter(F.act == "refresh"))
async def comp_refresh(cb: CallbackQuery, app: App) -> None:
    ids = list((await app.analytics.get_competitors()).keys())
    if not ids:
        await answer(cb, "Публикаций конкурентов нет", alert=True)
        return
    await answer(cb, f"Обновляю {len(ids)} публ.…")
    try:
        result = await app.collector.refresh_many(ids)
    except CollectorBusy as exc:
        await answer(cb, str(exc), alert=True)
        return
    text, kb = await render_comp_menu(app)
    note = f"🔄 Обновлено: {result.ok} из {result.total}" + (f", ошибок: {result.failed}" if result.failed else "")
    await safe_edit(cb.message, f"{note}\n{text}", kb)


# ---------------------------------------------------------------------- перенос публикации


@router.callback_query(CompCb.filter(F.act == "mv"))
async def comp_move(cb: CallbackQuery, callback_data: CompCb, app: App) -> None:
    comps = [c for c in await tracking.competitor_stats(app) if c.id]
    kb = InlineKeyboardBuilder()
    for c in comps[:40]:
        kb.button(text=truncate(c.name, 28), callback_data=CompCb(act="mvto", id=callback_data.id, to=c.id))
    kb.adjust(2)
    kb.row(button("Без группы", CompCb(act="mvto", id=callback_data.id, to=0)))
    kb.row(button("⬅️ Назад", PubCard(id=callback_data.id, per="")))
    await safe_edit(cb.message, "🔀 К какому конкуренту отнести?", kb.as_markup())
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
    name = await tracking.competitor_name(app, st.competitor_id)
    lines = [
        f"🕵️ {link(st.url, truncate(st.title, 60))}",
        f"{esc(name)} · создана {fmt_date(st.created_ts, tz)} · обновлено {fmt_ago(st.updated_ts)}",
        stats_line(st),
    ]
    if st.sync_error:
        lines.append(f"❗ {esc(st.sync_error)}")
    nav = ListNav.decode(nav_raw)
    kb = InlineKeyboardBuilder()
    kb.row(button("🔄 Обновить", PubAct(act="refresh", id=pub_id, nav=nav_raw)),
           button("🔀 Перенести", CompCb(act="mv", id=pub_id)),
           button("🗑 Убрать", PubAct(act="del", id=pub_id, nav=nav_raw)))
    if nav and nav.ctx in {"comp", "compc"}:
        kb.row(button("⬅️ Назад", CompCb(act="card", id=nav.arg, page=nav.page) if nav.ctx == "compc"
                      else nav.to_list_cb()))
    else:
        kb.row(button("⬅️ Назад", CompCb(act="card", id=st.competitor_id or 0)))
    return "\n".join(lines), kb.as_markup()


@router.message(TrackInput.own_links)
@router.message(TrackInput.token)
@router.message(TrackInput.comp_name)
@router.message(TrackInput.comp_links)
@router.message(TrackInput.comp_rename)
async def input_not_text(message: Message) -> None:
    await message.answer("Отправьте текст или нажмите «Отмена».", reply_markup=cancel_kb())


__all__ = ["router", "render_import_menu", "render_comp_menu", "render_competitor_pub_card", "send_long"]
