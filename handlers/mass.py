"""Массовое управление: выбор публикаций и действия над выбранными."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app_context import App
from handlers.common import answer, safe_edit
from handlers.publications import render_list, start_mass_input
from handlers.youtube_groups import start_bind
from keyboards.callbacks import ExportCb, Nav, PubList, SelAct, SelToggle
from keyboards.inline import button
from keyboards.navigation import ListNav
from services import publications as pubs
from services.youtube import unbind
from utils.formatting import esc, fmt_int, truncate

router = Router(name="mass")


@router.callback_query(SelToggle.filter())
async def on_toggle(cb: CallbackQuery, callback_data: SelToggle, app: App) -> None:
    try:
        selected = await pubs.toggle_selected(app, callback_data.id)
    except LookupError:
        await answer(cb, "Публикация не найдена", alert=True)
        return
    nav = ListNav.decode(callback_data.nav) or ListNav(mode=1)
    text, kb = await render_list(app, nav.ctx, nav.arg, nav.sort, nav.page, nav.mode)
    await safe_edit(cb.message, text, kb)
    await answer(cb, "✅ Выбрано" if selected else "Снят выбор")


async def _rerender_list(cb: CallbackQuery, app: App, nav_raw: str, note: str) -> None:
    nav = ListNav.decode(nav_raw)
    if nav is None:
        await render_menu(cb, app, "")
        await answer(cb, note)
        return
    text, kb = await render_list(app, nav.ctx, nav.arg, nav.sort, nav.page, nav.mode)
    await safe_edit(cb.message, text, kb)
    await answer(cb, note)


@router.callback_query(SelAct.filter(F.act == "page"))
async def on_select_page(cb: CallbackQuery, callback_data: SelAct, app: App) -> None:
    nav = ListNav.decode(callback_data.nav) or ListNav()
    items = await pubs.query(app, nav.ctx, nav.arg, nav.sort)
    size = int(app.settings["page_size"])
    chunk = items[nav.page * size : (nav.page + 1) * size]
    added = await pubs.add_selected(app, [s.id for s in chunk])
    await _rerender_list(cb, app, callback_data.nav, f"Добавлено: {added}")


@router.callback_query(SelAct.filter(F.act == "all"))
async def on_select_all(cb: CallbackQuery, callback_data: SelAct, app: App) -> None:
    nav = ListNav.decode(callback_data.nav) or ListNav()
    items = await pubs.query(app, nav.ctx, nav.arg, nav.sort)
    added = await pubs.add_selected(app, [s.id for s in items])
    await _rerender_list(cb, app, callback_data.nav, f"Добавлено: {added}")


@router.callback_query(SelAct.filter(F.act == "clear"))
async def on_clear(cb: CallbackQuery, callback_data: SelAct, app: App) -> None:
    await pubs.clear_selected(app)
    await _rerender_list(cb, app, callback_data.nav, "Выбор снят")


async def render_menu(cb: CallbackQuery, app: App, nav_raw: str) -> None:
    ids = await pubs.selected_ids(app)
    stats = await app.analytics.get_all()
    chosen = [stats[i] for i in ids if i in stats]
    lines = [f"☑️ <b>Выбранные публикации: {len(chosen)}</b>", ""]
    if not chosen:
        lines.append("Ничего не выбрано. Откройте любой список и нажмите «☑️ Выбрать», "
                     "затем отмечайте публикации номерами.")
    for st in chosen[:15]:
        lines.append(f"• <b>{esc(st.author_name)}</b> — {esc(truncate(st.title, 40))} · 👁 {fmt_int(st.total)}")
    if len(chosen) > 15:
        lines.append(f"… и ещё {len(chosen) - 15}")
    if chosen:
        lines += ["", f"Суммарно просмотров: {fmt_int(sum(s.total for s in chosen))}"]

    kb = InlineKeyboardBuilder()
    if chosen:
        kb.row(button("📋 Показать список", PubList(ctx="sel", mode=0)))
        kb.row(button("🎬 Привязать YouTube", SelAct(act="yt", nav=nav_raw)),
               button("❌ Отвязать YouTube", SelAct(act="ytdel", nav=nav_raw)))
        kb.row(button("🏷 Изменить метку", SelAct(act="label", nav=nav_raw)))
        kb.row(button("⭐ В избранное", SelAct(act="favon", nav=nav_raw)),
               button("★ Из избранного", SelAct(act="favoff", nav=nav_raw)))
        kb.row(button("✏️ Заголовок в Telegraph", SelAct(act="et", nav=nav_raw)),
               button("✏️ Текст в Telegraph", SelAct(act="ex", nav=nav_raw)))
        kb.row(button("🗑 Удалить из базы", SelAct(act="del", nav=nav_raw)))
        kb.row(button("🧹 Снять выбор", SelAct(act="clear", nav=nav_raw)))
    nav = ListNav.decode(nav_raw)
    if nav:
        kb.row(button("⬅️ К списку", nav.to_list_cb()))
    else:
        kb.row(button("📚 Все публикации", PubList(ctx="all", mode=1)), button("🏠 Меню", Nav(to="main")))
    await safe_edit(cb.message, "\n".join(lines), kb.as_markup())


@router.callback_query(SelAct.filter(F.act == "menu"))
async def on_menu(cb: CallbackQuery, callback_data: SelAct, app: App) -> None:
    await render_menu(cb, app, callback_data.nav)
    await answer(cb)


async def _ids_or_alert(cb: CallbackQuery, app: App) -> list[int] | None:
    ids = sorted(await pubs.selected_ids(app))
    if not ids:
        await answer(cb, "Ничего не выбрано", alert=True)
        return None
    return ids


@router.callback_query(SelAct.filter(F.act.in_({"favon", "favoff"})))
async def on_fav(cb: CallbackQuery, callback_data: SelAct, app: App) -> None:
    ids = await _ids_or_alert(cb, app)
    if ids is None:
        return
    count = await pubs.set_favorite(app, ids, callback_data.act == "favon")
    await render_menu(cb, app, callback_data.nav)
    await answer(cb, f"{'Добавлено в избранное' if callback_data.act == 'favon' else 'Убрано из избранного'}: {count}")


@router.callback_query(SelAct.filter(F.act == "ytdel"))
async def on_ytdel(cb: CallbackQuery, callback_data: SelAct, app: App) -> None:
    ids = await _ids_or_alert(cb, app)
    if ids is None:
        return
    async with app.db() as session:
        count = await unbind(session, ids)
        await session.commit()
    app.analytics.invalidate()
    await render_menu(cb, app, callback_data.nav)
    await answer(cb, f"YouTube отвязан: {count}")


@router.callback_query(SelAct.filter(F.act == "yt"))
async def on_yt(cb: CallbackQuery, callback_data: SelAct, app: App, state: FSMContext) -> None:
    ids = await _ids_or_alert(cb, app)
    if ids is None:
        return
    await start_bind(cb.message, state, target="sel", ids=ids, nav=callback_data.nav)
    await answer(cb)


@router.callback_query(SelAct.filter(F.act.in_({"label", "et", "ex"})))
async def on_input(cb: CallbackQuery, callback_data: SelAct, app: App, state: FSMContext) -> None:
    ids = await _ids_or_alert(cb, app)
    if ids is None:
        return
    await start_mass_input(cb, state, callback_data.act, ids, callback_data.nav)
    await answer(cb)


@router.callback_query(SelAct.filter(F.act == "del"))
async def on_delete(cb: CallbackQuery, callback_data: SelAct, app: App) -> None:
    ids = await _ids_or_alert(cb, app)
    if ids is None:
        return
    kb = InlineKeyboardBuilder()
    kb.row(button(f"🗑 Да, удалить {len(ids)} записей", SelAct(act="dely", nav=callback_data.nav)))
    kb.row(button("Отмена", SelAct(act="menu", nav=callback_data.nav)))
    await safe_edit(
        cb.message,
        f"🗑 <b>Удалить {len(ids)} записей из базы бота?</b>\n\n"
        "• Страницы Telegraph <b>не удаляются</b> и остаются доступны по ссылкам.\n"
        "• История статистики этих записей в боте будет удалена.\n"
        "• Действие необратимо.",
        kb.as_markup(),
    )
    await answer(cb)


@router.callback_query(SelAct.filter(F.act == "dely"))
async def on_delete_yes(cb: CallbackQuery, callback_data: SelAct, app: App) -> None:
    ids = await _ids_or_alert(cb, app)
    if ids is None:
        return
    count = await pubs.delete_records(app, ids)
    await pubs.clear_selected(app)
    nav = ListNav.decode(callback_data.nav)
    if nav:
        text, kb = await render_list(app, nav.ctx, nav.arg, nav.sort, 0, 0)
        await safe_edit(cb.message, text, kb)
    else:
        await render_menu(cb, app, "")
    await answer(cb, f"Удалено записей: {count}. Страницы Telegraph не тронуты.", alert=True)
