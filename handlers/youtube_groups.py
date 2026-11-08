"""Раздел «Группы YouTube»: привязка ссылок (только во внутренней базе), группы по видео."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from app_context import App
from database import Publication, YouTubeVideo
from handlers.common import answer, safe_edit
from handlers.states import YtInput
from keyboards.callbacks import ExportCb, Nav, PubAct, PubCard, PubList, SelAct, YtCb
from keyboards.inline import button, clamp_page, number_buttons, pagination_row
from keyboards.navigation import cancel_kb
from services import youtube as yt
from utils.formatting import STATUS_EMOJI, esc, fmt_delta, fmt_int, link, truncate
from utils.render import growth_short, list_item, sum_line

router = Router(name="youtube")

BIND_HINT = "🎬 Пришлите ссылку на YouTube-видео. В саму публикацию Telegraph она не добавляется."


async def start_bind(message: Message, state: FSMContext, *, target: str, ids: list[int], nav: str = "") -> None:
    await state.set_state(YtInput.link)
    await state.update_data(target=target, ids=ids, nav=nav)
    suffix = f"\n\nБудет привязано к публикациям: {len(ids)}." if len(ids) > 1 else ""
    await message.answer(BIND_HINT + suffix, reply_markup=cancel_kb())


@router.callback_query(PubAct.filter(F.act == "yt"))
async def on_card_bind(cb: CallbackQuery, callback_data: PubAct, state: FSMContext) -> None:
    await start_bind(cb.message, state, target="pub", ids=[callback_data.id], nav=callback_data.nav)
    await answer(cb)


@router.message(YtInput.link, F.text)
async def on_link(message: Message, state: FSMContext, app: App) -> None:
    video_id = yt.parse_youtube(message.text)
    if not video_id:
        await message.answer("❗ Это не похоже на ссылку YouTube. Попробуйте ещё раз.", reply_markup=cancel_kb())
        return
    data = await state.get_data()
    ids: list[int] = data.get("ids", [])
    await state.clear()
    async with app.db() as session:
        video = await yt.get_or_create_video(session, video_id, app.http, bool(app.settings["fetch_youtube_titles"]))
        count = await yt.bind(session, ids, video)
        await session.commit()
        total = len((await session.execute(
            select(Publication.id).where(Publication.youtube_video_id == video.id))).all())
    app.analytics.invalidate()
    kb = InlineKeyboardBuilder()
    if data.get("target") == "pub" and ids:
        kb.row(button("📄 К публикации", PubCard(id=ids[0], nav=data.get("nav", ""))))
    elif data.get("target") == "sel":
        kb.row(button("☑️ К выбранным", SelAct(act="menu", nav=data.get("nav", ""))))
    else:
        kb.row(button("🎬 Открыть группу", YtCb(act="card", id=video.id)))
    await message.answer(f"✅ Видео привязано к {count} публ. (всего с этим видео: {total})",
                         reply_markup=kb.as_markup())


# ====================================================================== группы


async def render_groups(app: App, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    async with app.db() as session:
        groups = await yt.list_groups(session)
    stats = await app.analytics.get_all()
    by_group: dict[int, list] = {}
    no_link = 0
    for st in stats.values():
        if st.youtube_video_id:
            by_group.setdefault(st.youtube_video_id, []).append(st)
        else:
            no_link += 1
    size = int(app.settings["page_size"])
    page = clamp_page(page, len(groups), size)
    chunk = groups[page * size : (page + 1) * size]
    lines = [f"🎬 <b>Группы YouTube</b> · {len(groups)} · без видео: {no_link}", ""]
    if not groups:
        lines.append("Групп нет. Привяжите ссылку YouTube в карточке публикации.")
    numbered = []
    for i, g in enumerate(chunk, start=page * size + 1):
        items = by_group.get(g.id, [])
        lines.append(f"<b>{i}.</b> {link(g.url, truncate(g.display, 30))} · {g.count} публ.\n{sum_line(items)}")
        numbered.append((str(i), YtCb(act="card", id=g.id)))
    kb = InlineKeyboardBuilder()
    number_buttons(kb, numbered)
    pag = pagination_row(page, len(groups), size, lambda p: YtCb(act="list", page=p))
    if pag:
        kb.row(*pag)
    kb.row(button("🔍 Найти по ссылке", YtCb(act="find")),
           button(f"📄 Без YouTube ({no_link})", PubList(ctx="nolink")))
    kb.row(button("➕ Привязать к нескольким", PubList(ctx="nolink", mode=1)))
    kb.row(button("🏠 Меню", Nav(to="main")))
    return "\n".join(lines), kb.as_markup()


async def render_group(app: App, group_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    async with app.db() as session:
        video = await session.get(YouTubeVideo, group_id)
    if video is None:
        return None
    items = sorted(
        [s for s in (await app.analytics.get_all()).values() if s.youtube_video_id == group_id],
        key=lambda s: s.total, reverse=True,
    )
    lines = [
        f"🎬 {link(video.url, truncate(video.title or 'Видео ' + video.video_id, 60))} · {len(items)} публ.",
        sum_line(items),
        "",
    ]
    for i, st in enumerate(items[:10], start=1):
        lines.append(list_item(i, st, "v"))
    if len(items) > 10:
        lines.append(f"… ещё {len(items) - 10}")
    kb = InlineKeyboardBuilder()
    kb.row(button("📋 Все публикации группы", PubList(ctx="yt", arg=group_id, sort="v")))
    kb.row(button("➕ Добавить публикации", YtCb(act="add", id=group_id)))
    kb.row(button("✏️ Изменить ссылку", YtCb(act="chg", id=group_id)),
           button("✏️ Название", YtCb(act="ren", id=group_id)))
    kb.row(button("🗑 Расформировать группу", YtCb(act="del", id=group_id)))
    kb.row(button("⬅️ К группам", YtCb(act="list")))
    return "\n".join(lines), kb.as_markup()


@router.callback_query(YtCb.filter(F.act == "list"))
async def on_groups(cb: CallbackQuery, callback_data: YtCb, app: App) -> None:
    await safe_edit(cb.message, *(await render_groups(app, callback_data.page)))
    await answer(cb)


@router.callback_query(YtCb.filter(F.act == "card"))
async def on_group(cb: CallbackQuery, callback_data: YtCb, app: App) -> None:
    rendered = await render_group(app, callback_data.id)
    if rendered is None:
        await answer(cb, "Группа не найдена", alert=True)
        return
    await safe_edit(cb.message, *rendered)
    await answer(cb)


@router.callback_query(YtCb.filter(F.act == "add"))
async def on_add(cb: CallbackQuery, callback_data: YtCb, app: App, state: FSMContext) -> None:
    async with app.db() as session:
        video = await session.get(YouTubeVideo, callback_data.id)
    if video is None:
        await answer(cb, "Группа не найдена", alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.row(button("☑️ Выбрать публикации без видео", PubList(ctx="nolink", mode=1)))
    kb.row(button("☑️ Выбрать из всех публикаций", PubList(ctx="all", mode=1)))
    kb.row(button("⬅️ Назад", YtCb(act="card", id=callback_data.id)))
    await safe_edit(
        cb.message,
        "➕ <b>Добавление публикаций в группу</b>\n\n"
        "1. Откройте список и отметьте нужные публикации номерами.\n"
        "2. Нажмите «⚙️ Действия» → «🎬 Привязать YouTube».\n"
        f"3. Отправьте ссылку этого видео:\n<code>{esc(video.url)}</code>",
        kb.as_markup(),
    )
    await answer(cb)


@router.callback_query(YtCb.filter(F.act == "find"))
async def on_find(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(YtInput.find)
    await cb.message.answer("🔍 Отправьте YouTube-ссылку — покажу все связанные публикации.", reply_markup=cancel_kb())
    await answer(cb)


@router.message(YtInput.find, F.text)
async def on_find_input(message: Message, state: FSMContext, app: App) -> None:
    video_id = yt.parse_youtube(message.text)
    if not video_id:
        await message.answer("❗ Не удалось распознать ссылку YouTube.", reply_markup=cancel_kb())
        return
    await state.clear()
    async with app.db() as session:
        video = (await session.execute(select(YouTubeVideo).where(YouTubeVideo.video_id == video_id))).scalar_one_or_none()
    if video is None:
        await message.answer("Публикаций с этим видео нет.")
        return
    rendered = await render_group(app, video.id)
    if rendered:
        await message.answer(rendered[0], reply_markup=rendered[1])


@router.callback_query(YtCb.filter(F.act == "chg"))
async def on_change(cb: CallbackQuery, callback_data: YtCb, state: FSMContext) -> None:
    await state.set_state(YtInput.change)
    await state.update_data(group_id=callback_data.id)
    await cb.message.answer(
        "✏️ Отправьте новую YouTube-ссылку для всей группы. Если такое видео уже есть в другой группе — "
        "группы будут объединены.", reply_markup=cancel_kb())
    await answer(cb)


@router.message(YtInput.change, F.text)
async def on_change_input(message: Message, state: FSMContext, app: App) -> None:
    video_id = yt.parse_youtube(message.text)
    if not video_id:
        await message.answer("❗ Не удалось распознать ссылку YouTube.", reply_markup=cancel_kb())
        return
    group_id = (await state.get_data()).get("group_id")
    await state.clear()
    async with app.db() as session:
        group = await session.get(YouTubeVideo, group_id)
        if group is None:
            await message.answer("Группа не найдена.")
            return
        group, merged = await yt.change_group_video(
            session, group, video_id, app.http, bool(app.settings["fetch_youtube_titles"]))
        await session.commit()
        new_id = group.id
    app.analytics.invalidate()
    await message.answer("✅ Группы объединены." if merged else "✅ Ссылка группы изменена.")
    rendered = await render_group(app, new_id)
    if rendered:
        await message.answer(rendered[0], reply_markup=rendered[1])


@router.callback_query(YtCb.filter(F.act == "ren"))
async def on_rename(cb: CallbackQuery, callback_data: YtCb, state: FSMContext) -> None:
    await state.set_state(YtInput.rename)
    await state.update_data(group_id=callback_data.id)
    await cb.message.answer("✏️ Отправьте название группы (для удобства, до 256 символов).", reply_markup=cancel_kb())
    await answer(cb)


@router.message(YtInput.rename, F.text)
async def on_rename_input(message: Message, state: FSMContext, app: App) -> None:
    title = (message.text or "").strip()[:256]
    group_id = (await state.get_data()).get("group_id")
    await state.clear()
    async with app.db() as session:
        group = await session.get(YouTubeVideo, group_id)
        if group is None:
            await message.answer("Группа не найдена.")
            return
        group.title = title or None
        await session.commit()
    rendered = await render_group(app, group_id)
    if rendered:
        await message.answer(rendered[0], reply_markup=rendered[1])


@router.callback_query(YtCb.filter(F.act == "del"))
async def on_delete(cb: CallbackQuery, callback_data: YtCb) -> None:
    kb = InlineKeyboardBuilder()
    kb.row(button("🗑 Да, расформировать", YtCb(act="dely", id=callback_data.id)),
           button("Отмена", YtCb(act="card", id=callback_data.id)))
    await safe_edit(cb.message, "Расформировать группу? У всех её публикаций будет удалена привязка к видео. "
                                "Сами публикации и их статистика останутся.", kb.as_markup())
    await answer(cb)


@router.callback_query(YtCb.filter(F.act == "dely"))
async def on_delete_yes(cb: CallbackQuery, callback_data: YtCb, app: App) -> None:
    async with app.db() as session:
        group = await session.get(YouTubeVideo, callback_data.id)
        if group is not None:
            ids = list((await session.execute(
                select(Publication.id).where(Publication.youtube_video_id == group.id))).scalars())
            await yt.unbind(session, ids)
            await session.delete(group)
            await session.commit()
    app.analytics.invalidate()
    await safe_edit(cb.message, *(await render_groups(app)))
    await answer(cb, "Группа расформирована")


@router.message(YtInput.link)
@router.message(YtInput.find)
@router.message(YtInput.change)
@router.message(YtInput.rename)
async def on_not_text(message: Message) -> None:
    await message.answer("Отправьте текстовое сообщение со ссылкой или нажмите «Отмена».", reply_markup=cancel_kb())
