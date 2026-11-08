"""Списки публикаций, карточка публикации и действия с ней."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app_context import App
from database import Publication, YouTubeVideo
from handlers.common import answer, error_text, safe_edit
from handlers.states import PubInput
from keyboards.callbacks import AuthorCb, BatchCb, ExportCb, PubAct, PubCard, PubList, SelAct, SelToggle, SortMenu, YtCb
from keyboards.inline import button, clamp_page, number_buttons, pagination_row
from keyboards.navigation import ListNav, cancel_kb, list_parent_button
from services import publications as pubs
from services.analytics import PERIOD_LABELS, SORT_LABELS, PubStats
from services.content import prepare_content
from utils.formatting import STATUS_LABELS, esc, fmt_ago, fmt_date, fmt_dt, fmt_float, fmt_int, fmt_pct, link, truncate
from utils.render import LEGEND, growth_text, list_item, stats_line
from utils.validators import ValidationError, validate_author, validate_label, validate_title

logger = logging.getLogger(__name__)
router = Router(name="publications")

LIST_SORTS = ["new", "old", "v", "d1", "d7", "d30", "au", "fast", "prom", "trend", "low"]
EMPTY_TEXTS = {
    "trend": "Сейчас нет публикаций, которые заметно ускорились. Для оценки нужна история минимум за 48 часов.",
    "low": "Публикаций с заметным снижением активности нет (или пока мало данных для оценки).",
    "prom": "Пока нет публикаций с устойчивым ростом за неделю.",
    "fast": "Нет данных о приросте за последние 24 часа.",
}


# ====================================================================== список


async def render_list(app: App, ctx: str, arg: int = 0, sort: str = "new", page: int = 0, mode: int = 0
                      ) -> tuple[str, InlineKeyboardMarkup]:
    items = await pubs.query(app, ctx, arg, sort)
    size = int(app.settings["page_size"])
    page = clamp_page(page, len(items), size)
    chunk = items[page * size : (page + 1) * size]
    nav = ListNav(ctx, arg, sort, page, mode)
    chosen = await pubs.selected_ids(app) if (mode or ctx == "sel") else set()

    title = await pubs.context_title(app, ctx, arg)
    head = [f"<b>{esc(title)}</b> · {len(items)} · {SORT_LABELS.get(sort, sort)}"]
    if mode:
        head.append(f"☑️ Нажимайте номера, чтобы отметить. Выбрано: {len(chosen)}")
    lines = head
    if not items:
        lines.append(EMPTY_TEXTS.get(sort, "Публикаций нет."))
    shown = 0
    show_author = ctx not in pubs.COMPETITOR_CTXS and ctx != "author"
    for i, st in enumerate(chunk, start=page * size + 1):
        item = list_item(i, st, sort, selected=(st.id in chosen) if mode else None, show_author=show_author)
        if sum(len(x) + 1 for x in lines) + len(item) > 3700:
            lines.append("… (уменьшите число публикаций на странице в настройках)")
            break
        lines.append(item)
        shown += 1
    if items:
        lines.append(LEGEND)

    kb = InlineKeyboardBuilder()
    numbered = []
    for i, st in enumerate(chunk[:shown], start=page * size + 1):
        if mode:
            mark = "✅" if st.id in chosen else ""
            numbered.append((f"{mark}{i}", SelToggle(id=st.id, nav=nav.encode())))
        else:
            numbered.append((str(i), PubCard(id=st.id, per="", nav=nav.encode())))
    number_buttons(kb, numbered)
    pag = pagination_row(page, len(items), size, lambda p: nav.to_list_cb(page=p))
    if pag:
        kb.row(*pag)
    competitor_ctx = ctx in pubs.COMPETITOR_CTXS
    if competitor_ctx:
        mode = 0  # массовые операции (правка, YouTube) к чужим публикациям не применяются
        kb.row(button("↕️ Сортировка", SortMenu(ctx=ctx, arg=arg, mode=0)))
    else:
        kb.row(
            button("↕️ Сортировка", SortMenu(ctx=ctx, arg=arg, mode=mode)),
            button("✖️ Выйти из выбора" if mode else "☑️ Выбрать", nav.to_list_cb(mode=0 if mode else 1)),
        )
    if mode:
        kb.row(
            button("☑️ Вся страница", SelAct(act="page", nav=nav.encode())),
            button(f"☑️ Все ({len(items)})", SelAct(act="all", nav=nav.encode())),
        )
        kb.row(
            button("🧹 Снять выбор", SelAct(act="clear", nav=nav.encode())),
            button(f"⚙️ Действия ({len(chosen)})", SelAct(act="menu", nav=nav.encode())),
        )
    if ctx == "all" and not mode:
        kb.row(button("📦 Партии", BatchCb(act="list")), list_parent_button(nav))
    else:
        kb.row(list_parent_button(nav))
    return "\n".join(lines), kb.as_markup()


@router.callback_query(PubList.filter())
async def on_list(cb: CallbackQuery, callback_data: PubList, app: App) -> None:
    text, kb = await render_list(app, callback_data.ctx, callback_data.arg, callback_data.sort,
                                 callback_data.page, callback_data.mode)
    await safe_edit(cb.message, text, kb)
    await answer(cb)


@router.callback_query(SortMenu.filter())
async def on_sort_menu(cb: CallbackQuery, callback_data: SortMenu, app: App) -> None:
    kb = InlineKeyboardBuilder()
    for code in LIST_SORTS:
        kb.row(button(SORT_LABELS[code], PubList(ctx=callback_data.ctx, arg=callback_data.arg, sort=code,
                                                 page=0, mode=callback_data.mode)))
    kb.row(button("⬅️ Назад", PubList(ctx=callback_data.ctx, arg=callback_data.arg, mode=callback_data.mode)))
    await safe_edit(cb.message, "↕️ <b>Выберите сортировку</b>\n\n"
                                "<i>«Набирают популярность», «Перспективные», «Низкая активность» и «По скорости» "
                                "показывают только подходящие публикации.</i>", kb.as_markup())
    await answer(cb)


# ====================================================================== карточка


def _period_block(st: PubStats, per: str, snap_info: tuple[int, int | None, int], daily, tz) -> list[str]:
    lines = [f"🔎 <b>Подробно за период: {PERIOD_LABELS.get(per, per)}</b>"]
    if per == "24h":
        lines.append(f"Предыдущие 24 часа: {growth_text(st.p24)}")
        if st.g24.usable and st.p24.usable and st.p24.value > 0:
            lines.append(f"Изменение к предыдущим суткам: {fmt_pct(st.g24.value / st.p24.value)}")
        if st.velocity:
            lines.append(f"Средняя скорость: {fmt_float(st.velocity)} просмотров/ч")
        if st.reference is not None:
            lines.append(f"Обычная скорость (база для статуса): ≈{fmt_float(st.reference)} в сутки")
        if st.g24.baseline:
            lines.append(f"<i>Расчёт: снимок {fmt_dt(st.g24.baseline, tz, False)} → {fmt_dt(st.g24.last, tz, False)}</i>")
    elif per == "7d":
        lines.append(f"Предыдущие 7 дней: {growth_text(st.p7)}")
        if st.g7.usable and st.p7.usable and st.p7.value > 0:
            lines.append(f"Изменение к предыдущей неделе: {fmt_pct(st.g7.value / st.p7.value)}")
        days = max(1.0, min(7.0, st.age_hours / 24))
        if st.g7.usable:
            lines.append(f"В среднем за сутки: {fmt_float(st.g7.value / days)}")
        if daily.days:
            lines.append("<i>По календарным суткам (факт API):</i>")
            for day, views, final in daily.days[:7]:
                y, m, d = day.split("-")
                lines.append(f"  {d}.{m}: {fmt_int(views)}{'' if final else ' (сутки не завершены)'}")
    elif per == "30d":
        days = max(1.0, min(30.0, st.age_hours / 24))
        if st.g30.usable:
            lines.append(f"В среднем за сутки: {fmt_float(st.g30.value / days)}")
        if daily.expected30:
            lines.append(
                f"Сумма по календарным суткам (факт API): {fmt_int(daily.sum30)} "
                f"<i>(загружено {daily.loaded30} из {daily.expected30} дн.)</i>"
            )
    else:
        age_days = max(st.age_hours / 24, 1 / 24)
        lines.append(f"Всего просмотров (факт API): <b>{fmt_int(st.total)}</b>")
        lines.append(f"Возраст публикации: {fmt_float(age_days)} дн.")
        lines.append(f"В среднем в сутки за всё время: {fmt_float(st.total / max(age_days, 1.0))}")
        count, first_ts, drops = snap_info
        lines.append(f"Снимков счётчика в истории: {count}" + (f", первый — {fmt_dt(first_ts, tz)}" if first_ts else ""))
        if drops:
            lines.append(f"ℹ️ Счётчик Telegraph уменьшался {drops} раз(а) — это корректировки на стороне Telegraph.")
    return lines


async def render_card(app: App, pub_id: int, per: str = "", nav_raw: str = ""
                      ) -> tuple[str, InlineKeyboardMarkup] | None:
    st = await app.analytics.get(pub_id)
    if st is None:
        return None
    if st.is_competitor:
        from handlers.tracking import render_competitor_pub_card

        return await render_competitor_pub_card(app, pub_id, nav_raw)
    tz = app.config.timezone
    async with app.db() as session:
        pub = await session.get(Publication, pub_id)
        if pub is None:
            return None
        video = await session.get(YouTubeVideo, pub.youtube_video_id) if pub.youtube_video_id else None
    group_size = 0
    if video is not None:
        group_size = sum(1 for s in (await app.analytics.get_all()).values() if s.youtube_video_id == video.id)
    selected = pub_id in await pubs.selected_ids(app)

    created = fmt_date(st.created_ts, tz)
    extra = []
    if video:
        extra.append(f"🎬 {link(video.url, truncate(video.title or 'YouTube', 30))} ({group_size})")
    if st.label:
        extra.append(f"🏷 {esc(st.label)}")
    if st.is_favorite:
        extra.append("⭐")
    if st.kind == "imported":
        extra.append("📥 своя готовая" + ("" if pub.account_id else ", без правки"))
    lines = [
        f"📄 {link(st.url, truncate(st.title, 60))}",
        f"👤 {esc(truncate(st.author_name, 30))} · {created} · {STATUS_LABELS.get(st.status, st.status)}",
        stats_line(st),
    ]
    if extra:
        lines.append(" · ".join(extra))
    if st.sync_error:
        lines.append(f"❗ {esc(st.sync_error)}")

    if per:  # подробности по запросу
        snap_info = await app.analytics.snapshot_info(pub_id)
        daily = await app.analytics.daily_summary(pub_id, st.created_ts)
        lines += [
            "",
            f"<i>Обновлено {fmt_ago(st.updated_ts)}. Прирост — по снимкам счётчика:</i>",
            f"24 ч: {growth_text(st.g24)}",
            f"7 дн.: {growth_text(st.g7)}",
            f"30 дн.: {growth_text(st.g30)}",
            "",
        ]
        lines += _period_block(st, per, snap_info, daily, tz)

    nav = ListNav.decode(nav_raw)
    kb = InlineKeyboardBuilder()
    if per:
        kb.row(*[
            button(("• " if per == p else "") + label, PubCard(id=pub_id, per=p, nav=nav_raw))
            for p, label in (("24h", "24 ч"), ("7d", "7 дн."), ("30d", "30 дн."), ("all", "Всё"))
        ])
    kb.row(button("🔄 Обновить", PubAct(act="refresh", id=pub_id, nav=nav_raw)),
           button("★ Из избр." if st.is_favorite else "⭐ В избр.", PubAct(act="fav", id=pub_id, nav=nav_raw)),
           button("✖️ Кратко" if per else "📊 Подробнее", PubCard(id=pub_id, per="" if per else "24h", nav=nav_raw)))
    row = [button("🎬 YouTube", PubAct(act="yt", id=pub_id, nav=nav_raw)),
           button("🏷 Метка", PubAct(act="label", id=pub_id, nav=nav_raw))]
    if pub.account_id is not None:
        row.append(button("✏️ Правка", PubAct(act="edit", id=pub_id, nav=nav_raw)))
    kb.row(*row)
    if video:
        kb.row(button("🎬 Группа видео", YtCb(act="card", id=video.id)),
               button("❌ Отвязать YouTube", PubAct(act="ytdel", id=pub_id, nav=nav_raw)))
    kb.row(button("👤 Автор", AuthorCb(act="card", id=st.author_id)),
           button("✅ Выбрано" if selected else "☑️ Выбрать", PubAct(act="sel", id=pub_id, nav=nav_raw)),
           button("🗑 Удалить", PubAct(act="del", id=pub_id, nav=nav_raw)))
    kb.row(button("⬅️ Назад", nav.to_list_cb()) if nav else button("📚 Все публикации", PubList(ctx="all")))
    return "\n".join(lines), kb.as_markup()


async def show_card_message(message: Message, app: App, pub_id: int, nav_raw: str = "", edit: bool = False) -> None:
    rendered = await render_card(app, pub_id, "", nav_raw)
    if rendered is None:
        await message.answer("Публикация не найдена (возможно, удалена из базы).")
        return
    if edit:
        await safe_edit(message, *rendered)
    else:
        await message.answer(rendered[0], reply_markup=rendered[1])


@router.callback_query(PubCard.filter())
async def on_card(cb: CallbackQuery, callback_data: PubCard, app: App) -> None:
    rendered = await render_card(app, callback_data.id, callback_data.per, callback_data.nav)
    if rendered is None:
        await answer(cb, "Публикация не найдена (возможно, удалена из базы)", alert=True)
        return
    await safe_edit(cb.message, *rendered)
    await answer(cb)


# ====================================================================== действия карточки


async def _rerender(cb: CallbackQuery, app: App, pub_id: int, nav: str, note: str | None = None) -> None:
    rendered = await render_card(app, pub_id, "", nav)
    if rendered is None:
        await answer(cb, "Публикация не найдена", alert=True)
        return
    await safe_edit(cb.message, *rendered)
    await answer(cb, note)


@router.callback_query(PubAct.filter(F.act == "fav"))
async def act_fav(cb: CallbackQuery, callback_data: PubAct, app: App) -> None:
    try:
        value = await pubs.toggle_favorite(app, callback_data.id)
    except LookupError:
        await answer(cb, "Публикация не найдена", alert=True)
        return
    await _rerender(cb, app, callback_data.id, callback_data.nav, "⭐ Добавлено в избранное" if value else "Убрано из избранного")


@router.callback_query(PubAct.filter(F.act == "sel"))
async def act_sel(cb: CallbackQuery, callback_data: PubAct, app: App) -> None:
    try:
        value = await pubs.toggle_selected(app, callback_data.id)
    except LookupError:
        await answer(cb, "Публикация не найдена", alert=True)
        return
    await _rerender(cb, app, callback_data.id, callback_data.nav, "Добавлено в выбор" if value else "Убрано из выбора")


@router.callback_query(PubAct.filter(F.act == "refresh"))
async def act_refresh(cb: CallbackQuery, callback_data: PubAct, app: App) -> None:
    await answer(cb, "Запрашиваю getViews…")
    try:
        old, new = await app.collector.refresh_one(callback_data.id)
    except Exception as exc:  # noqa: BLE001
        await cb.message.answer(f"❗ Не удалось обновить: {esc(error_text(exc))}")
        return
    rendered = await render_card(app, callback_data.id, "", callback_data.nav)
    if rendered:
        await safe_edit(cb.message, *rendered)
    if new < old:
        await cb.message.answer(
            f"ℹ️ Telegraph уменьшил счётчик: {fmt_int(old)} → {fmt_int(new)}. Это корректировка на стороне "
            "Telegraph, в приросте она не вычитается."
        )


@router.callback_query(PubAct.filter(F.act == "check"))
async def act_check(cb: CallbackQuery, callback_data: PubAct, app: App) -> None:
    async with app.db() as session:
        pub = await session.get(Publication, callback_data.id)
    if pub is None:
        await answer(cb, "Публикация не найдена", alert=True)
        return
    try:
        page = await app.telegraph.get_page(pub.path, return_content=False)
    except Exception as exc:  # noqa: BLE001
        await answer(cb, f"Ошибка API: {error_text(exc)}"[:190], alert=True)
        return
    same = page.title == pub.title and (page.author_name or "") == pub.author_name
    await answer(
        cb,
        (f"✅ Страница доступна (getPage).\nЗаголовок: {page.title[:60]}\nАвтор: {page.author_name or '—'}\n"
         f"Просмотров: {page.views}\n" + ("Данные совпадают с базой." if same else "⚠️ Данные отличаются от базы!"))[:195],
        alert=True,
    )


@router.callback_query(PubAct.filter(F.act == "ytdel"))
async def act_ytdel(cb: CallbackQuery, callback_data: PubAct, app: App) -> None:
    from services.youtube import unbind

    async with app.db() as session:
        await unbind(session, [callback_data.id])
        await session.commit()
    app.analytics.invalidate()
    await _rerender(cb, app, callback_data.id, callback_data.nav, "YouTube-ссылка отвязана")


@router.callback_query(PubAct.filter(F.act == "del"))
async def act_del(cb: CallbackQuery, callback_data: PubAct, app: App) -> None:
    st = await app.analytics.get(callback_data.id)
    if st is None:
        await answer(cb, "Публикация не найдена", alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.row(button("🗑 Да, удалить", PubAct(act="dely", id=callback_data.id, nav=callback_data.nav)),
           button("Отмена", PubCard(id=callback_data.id, nav=callback_data.nav)))
    await safe_edit(
        cb.message,
        f"🗑 Удалить «{esc(truncate(st.title, 50))}» из бота вместе с историей просмотров?\n"
        "Сама страница Telegraph останется.",
        kb.as_markup(),
    )
    await answer(cb)


@router.callback_query(PubAct.filter(F.act == "dely"))
async def act_del_yes(cb: CallbackQuery, callback_data: PubAct, app: App) -> None:
    st = await app.analytics.get(callback_data.id)
    count = await pubs.delete_records(app, [callback_data.id])
    nav = ListNav.decode(callback_data.nav)
    if st and st.is_competitor and (nav is None or nav.ctx == "compc"):
        from handlers.tracking import render_comp_card

        rendered = await render_comp_card(app, st.competitor_id or 0, nav.page if nav else 0)
        text, kb = rendered if rendered else await render_list(app, "comp", 0, "v")
    else:
        nav = nav or ListNav()
        text, kb = await render_list(app, nav.ctx, nav.arg, nav.sort, nav.page, nav.mode)
    await safe_edit(cb.message, text, kb)
    await answer(cb, "Запись удалена из базы (страница Telegraph не тронута)" if count else "Запись уже удалена")


@router.callback_query(PubAct.filter(F.act == "edit"))
async def act_edit_menu(cb: CallbackQuery, callback_data: PubAct, app: App) -> None:
    kb = InlineKeyboardBuilder()
    kb.row(button("✏️ Заголовок", PubAct(act="et", id=callback_data.id, nav=callback_data.nav)))
    kb.row(button("✏️ Текст", PubAct(act="ex", id=callback_data.id, nav=callback_data.nav)))
    kb.row(button("✏️ Имя автора", PubAct(act="ea", id=callback_data.id, nav=callback_data.nav)))
    kb.row(button("⬅️ Назад", PubCard(id=callback_data.id, nav=callback_data.nav)))
    await safe_edit(
        cb.message,
        "✏️ Что изменить в Telegraph? Ссылка и просмотры сохранятся.",
        kb.as_markup(),
    )
    await answer(cb)


PROMPTS = {
    "label": "🏷 Отправьте внутреннюю метку (до 64 символов). Чтобы убрать метку — отправьте «-».",
    "et": "✏️ Отправьте новый заголовок (до 256 символов).",
    "ex": "✏️ Отправьте новый текст публикации одним сообщением (форматирование Telegram сохранится).",
    "ea": "✏️ Отправьте новое имя автора (до 128 символов).",
}
STATE_BY_ACT = {"label": PubInput.label, "et": PubInput.title, "ex": PubInput.text, "ea": PubInput.author}


@router.callback_query(PubAct.filter(F.act.in_({"label", "et", "ex", "ea"})))
async def act_input_start(cb: CallbackQuery, callback_data: PubAct, state: FSMContext) -> None:
    await state.set_state(STATE_BY_ACT[callback_data.act])
    await state.update_data(target="pub", ids=[callback_data.id], nav=callback_data.nav)
    await cb.message.answer(PROMPTS[callback_data.act], reply_markup=cancel_kb())
    await answer(cb)


async def start_mass_input(cb: CallbackQuery, state: FSMContext, act: str, ids: list[int], nav: str) -> None:
    await state.set_state(STATE_BY_ACT[act])
    await state.update_data(target="sel", ids=ids, nav=nav)
    note = f"\n\nБудет применено к выбранным публикациям: {len(ids)}."
    await cb.message.answer(PROMPTS[act] + note, reply_markup=cancel_kb())


async def _finish_input(message: Message, state: FSMContext, app: App, text: str) -> None:
    data = await state.get_data()
    await state.clear()
    ids: list[int] = data.get("ids", [])
    if data.get("target") == "pub" and ids:
        await message.answer(text)
        await show_card_message(message, app, ids[0], data.get("nav", ""))
    else:
        kb = InlineKeyboardBuilder()
        kb.row(button("☑️ К выбранным", SelAct(act="menu", nav=data.get("nav", ""))))
        await message.answer(text, reply_markup=kb.as_markup())


@router.message(PubInput.label, F.text)
async def input_label(message: Message, state: FSMContext, app: App) -> None:
    try:
        label = validate_label(message.text)
    except ValidationError as exc:
        await message.answer(f"❗ {exc}", reply_markup=cancel_kb())
        return
    ids = (await state.get_data()).get("ids", [])
    count = await pubs.set_label(app, ids, label)
    await _finish_input(message, state, app, f"🏷 Метка {'установлена' if label else 'убрана'}: {count} публ.")


async def _run_edit(message: Message, state: FSMContext, app: App, **kwargs) -> None:
    ids = (await state.get_data()).get("ids", [])
    if app.mass_lock.locked():
        await message.answer("⏳ Уже выполняется другая массовая операция. Дождитесь её завершения.")
        return
    async with app.mass_lock:
        progress_msg = await message.answer(f"⏳ Редактирование в Telegraph: 0 из {len(ids)}")

        async def progress(done: int, total: int) -> None:
            if done == total or done % 3 == 0:
                await safe_edit(progress_msg, f"⏳ Редактирование в Telegraph: {done} из {total}")

        result = await pubs.edit_in_telegraph(app, ids, progress=progress, **kwargs)
    text = f"✅ Изменено в Telegraph: {len(result.ok)} из {len(ids)}."
    if result.failed:
        text += "\n\n❗ Не изменены:\n" + "\n".join(f"• #{pid}: {esc(err)}" for pid, err in result.failed[:20])
    await _finish_input(message, state, app, text)


@router.message(PubInput.title, F.text)
async def input_title(message: Message, state: FSMContext, app: App) -> None:
    try:
        title = validate_title(message.text)
    except ValidationError as exc:
        await message.answer(f"❗ {exc}", reply_markup=cancel_kb())
        return
    await _run_edit(message, state, app, title=title)


@router.message(PubInput.text, F.text)
async def input_text(message: Message, state: FSMContext, app: App) -> None:
    try:
        nodes, content_json = prepare_content(message.html_text)
    except ValidationError as exc:
        await message.answer(f"❗ {exc}", reply_markup=cancel_kb())
        return
    await _run_edit(message, state, app, content=(message.html_text, content_json))


@router.message(PubInput.author, F.text)
async def input_author(message: Message, state: FSMContext, app: App) -> None:
    try:
        author = validate_author(message.text)
    except ValidationError as exc:
        await message.answer(f"❗ {exc}", reply_markup=cancel_kb())
        return
    await _run_edit(message, state, app, author=author)


@router.message(PubInput.label)
@router.message(PubInput.title)
@router.message(PubInput.text)
@router.message(PubInput.author)
async def input_not_text(message: Message) -> None:
    await message.answer("Пожалуйста, отправьте текстовое сообщение или нажмите «Отмена».", reply_markup=cancel_kb())


__all__ = ["router", "render_list", "render_card", "show_card_message", "start_mass_input"]
