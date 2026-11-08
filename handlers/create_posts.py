"""Создание публикаций по шаблону (заголовок + текст + список авторов) и управление партиями."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select

from app_context import App
from database import Batch, BatchItem
from handlers.common import answer, safe_edit
from handlers.states import CreatePosts
from handlers.youtube_groups import start_bind
from keyboards.callbacks import BatchCb, CreateCb, ExportCb, Nav, PubList
from keyboards.inline import button, clamp_page, number_buttons, pagination_row
from keyboards.navigation import cancel_button, cancel_kb
from services import publications as pubs
from services.batch_creator import BatchBusyError, BatchReport, BatchStateError, ItemResult
from services.content import nodes_to_text, prepare_content
from utils.formatting import esc, fmt_dt, fmt_int, link, plural, split_message, truncate
from utils.validators import ValidationError, parse_authors, validate_title

logger = logging.getLogger(__name__)
router = Router(name="create_posts")

TEXT_PARTS_LIMIT = 30
BATCH_STATUS = {
    "draft": "📝 черновик",
    "running": "⏳ выполняется",
    "completed": "✅ завершена",
    "partial": "⚠️ создана частично",
    "cancelled": "⏹ остановлена",
    "interrupted": "❗ прервана",
}
ITEM_STATUS = {
    "created": "✅", "failed": "❌", "pending": "⏳", "creating": "⏳", "uncertain": "❔", "cancelled": "⏹",
}


# ====================================================================== шаги мастера


async def start_create(message: Message, state: FSMContext, app: App) -> None:
    await state.clear()
    await state.set_state(CreatePosts.title)
    note = ""
    if app.creator.busy:
        note = "\n\n⏳ Сейчас выполняется другая партия — новую можно будет запустить после её завершения."
    await message.answer(
        "📝 <b>Создание публикаций — шаг 1 из 3</b>\n\nОтправьте <b>общий заголовок</b> публикаций "
        f"(до 256 символов).{note}",
        reply_markup=cancel_kb(),
    )


@router.callback_query(Nav.filter(F.to == "create"))
async def on_create_cb(cb: CallbackQuery, state: FSMContext, app: App) -> None:
    await start_create(cb.message, state, app)
    await answer(cb)


@router.message(CreatePosts.title, F.text)
async def on_title(message: Message, state: FSMContext) -> None:
    try:
        title = validate_title(message.text)
    except ValidationError as exc:
        await message.answer(f"❗ {exc}", reply_markup=cancel_kb())
        return
    await state.update_data(title=title, parts=[])
    await state.set_state(CreatePosts.text)
    await message.answer(
        f"✅ Заголовок: <b>{esc(title)}</b>\n\n"
        "📝 <b>Шаг 2 из 3</b>\nОтправьте <b>основной текст</b> публикации. Форматирование Telegram "
        "(жирный, курсив, ссылки, цитаты, код) сохранится.\n\n"
        "Длинный текст можно отправить несколькими сообщениями — затем нажмите «✅ Текст готов».",
        reply_markup=cancel_kb(),
    )


def _text_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(button("✅ Текст готов", CreateCb(act="textdone")))
    kb.row(button("↩️ Начать текст заново", CreateCb(act="textreset")), cancel_button())
    return kb.as_markup()


@router.message(CreatePosts.text, F.text)
async def on_text_part(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    parts: list[str] = list(data.get("parts", []))
    if len(parts) >= TEXT_PARTS_LIMIT:
        await message.answer("❗ Слишком много частей текста. Нажмите «✅ Текст готов».", reply_markup=_text_kb())
        return
    parts.append(message.html_text)
    await state.update_data(parts=parts)
    total = sum(len(p) for p in parts)
    await message.answer(
        f"Принята часть {len(parts)} · всего ≈{fmt_int(total)} символов.\n"
        "Отправьте продолжение или нажмите «✅ Текст готов».",
        reply_markup=_text_kb(),
    )


@router.callback_query(CreateCb.filter(F.act == "textreset"), CreatePosts.text)
async def on_text_reset(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(parts=[])
    await cb.message.answer("Текст очищен. Отправьте текст заново.", reply_markup=cancel_kb())
    await answer(cb)


@router.callback_query(CreateCb.filter(F.act == "textdone"), CreatePosts.text)
async def on_text_done(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    parts: list[str] = data.get("parts", [])
    if not parts:
        await answer(cb, "Сначала отправьте текст", alert=True)
        return
    html_text = "\n\n".join(parts)
    try:
        _, content_json = prepare_content(html_text)
    except ValidationError as exc:
        await cb.message.answer(f"❗ {exc}\nИзмените текст и отправьте заново.", reply_markup=_text_kb())
        await answer(cb)
        return
    await state.update_data(content_html=html_text, content_json=content_json, parts=[])
    await state.set_state(CreatePosts.authors)
    await cb.message.answer(
        "✅ Текст сохранён.\n\n📝 <b>Шаг 3 из 3</b>\nОтправьте <b>список авторов</b> — каждый с новой строки.\n\n"
        "<i>Пример:\nИван\nДмитрий\nАлександр</i>\n\nДля каждого автора будет создана отдельная публикация.",
        reply_markup=cancel_kb(),
    )
    await answer(cb)


@router.message(CreatePosts.authors, F.text)
@router.message(CreatePosts.confirm, F.text)
async def on_authors(message: Message, state: FSMContext, app: App) -> None:
    data = await state.get_data()
    if not data.get("content_json"):
        await state.clear()
        await message.answer("Данные мастера устарели. Начните заново: «📝 Создать публикации».")
        return
    parsed = parse_authors(message.text, int(app.settings["max_batch_size"]))
    if not parsed.authors or len(parsed.authors) > int(app.settings["max_batch_size"]):
        errors = "\n".join(f"• {esc(e)}" for e in parsed.errors) or "• список пуст"
        await message.answer(f"❗ Список авторов не принят:\n{errors}\n\nОтправьте список ещё раз.",
                             reply_markup=cancel_kb())
        return

    known = await pubs.author_pub_count(app, parsed.authors)
    preview = nodes_to_text(json.loads(data["content_json"]))
    lines = [
        "📋 <b>Проверьте перед созданием</b>",
        "",
        f"<b>Заголовок:</b> {esc(data['title'])}",
        f"<b>Текст:</b> {fmt_int(len(preview))} символов",
        f"<blockquote>{esc(truncate(preview, 300))}</blockquote>",
        f"<b>Авторов:</b> {len(parsed.authors)} → будет создано {len(parsed.authors)} "
        f"{plural(len(parsed.authors), 'публикация', 'публикации', 'публикаций')}",
        "",
    ]
    for i, name in enumerate(parsed.authors, start=1):
        line = f"{i}. {esc(name)}"
        if name in known:
            line += f" — <i>ℹ️ этот автор уже использовался ранее, публикаций: {known[name]}</i>"
        lines.append(line)
    if parsed.duplicates:
        lines += ["", "⚠️ Повторы в списке убраны: " + esc(", ".join(parsed.duplicates))]
    if parsed.errors:
        lines += ["", "❗ Пропущены строки:"] + [f"• {esc(e)}" for e in parsed.errors]

    await state.update_data(authors=parsed.authors)
    await state.set_state(CreatePosts.confirm)
    kb = InlineKeyboardBuilder()
    kb.row(button(f"✅ Создать {len(parsed.authors)} публ.", CreateCb(act="go")))
    kb.row(button("✏️ Изменить авторов", CreateCb(act="reauthors")), cancel_button())
    text = "\n".join(lines)
    chunks = split_message(text)
    for chunk in chunks[:-1]:
        await message.answer(chunk)
    await message.answer(chunks[-1], reply_markup=kb.as_markup())


@router.callback_query(CreateCb.filter(F.act == "reauthors"), CreatePosts.confirm)
async def on_reauthors(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CreatePosts.authors)
    await cb.message.answer("Отправьте новый список авторов (каждый с новой строки).", reply_markup=cancel_kb())
    await answer(cb)


@router.callback_query(CreateCb.filter(F.act == "go"))
async def on_go(cb: CallbackQuery, state: FSMContext, app: App, bot: Bot) -> None:
    # Защита от повторного нажатия: проверка и создание партии — под одной блокировкой,
    # данные мастера очищаются сразу, поэтому второе нажатие не найдёт черновика.
    async with app.create_guard:
        if app.creator.busy:
            await answer(cb, "⏳ Уже выполняется создание партии. Дождитесь завершения.", alert=True)
            return
        data = await state.get_data()
        if await state.get_state() != CreatePosts.confirm.state or not data.get("authors"):
            await answer(cb, "Эта партия уже запущена или данные устарели.", alert=True)
            return
        await state.clear()
        try:
            batch_id = await app.creator.create_draft(
                data["title"], data["content_html"], data["content_json"], data["authors"]
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Не удалось подготовить партию")
            await cb.message.answer(f"❗ Не удалось подготовить партию: {esc(exc)}")
            await answer(cb)
            return
    await answer(cb, "Запускаю создание…")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    progress = await cb.message.answer(f"⏳ Создание публикаций: 0 из {len(data['authors'])}",
                                       reply_markup=_stop_kb(batch_id))
    app.spawn(run_with_progress(app, bot, progress.chat.id, progress.message_id, batch_id), name=f"batch-{batch_id}")


def _stop_kb(batch_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[button("⏹ Остановить", CreateCb(act="stop", batch=batch_id))]])


async def run_with_progress(app: App, bot: Bot, chat_id: int, message_id: int, batch_id: int) -> None:
    last_edit = 0.0
    recent: list[str] = []

    async def progress(done: int, total: int, item: ItemResult) -> None:
        nonlocal last_edit
        mark = "✅" if item.status == "created" else "❌"
        recent.append(f"{mark} {esc(item.author)}" + (f" — {esc(item.error)}" if item.error and item.status != "created" else ""))
        now = time.monotonic()
        if now - last_edit < 1.2 and done < total:
            return
        last_edit = now
        text = f"⏳ Создание публикаций: {done} из {total}\n\n" + "\n".join(recent[-8:])
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=_stop_kb(batch_id))
        except Exception:  # noqa: BLE001
            pass

    try:
        report = await app.creator.run(batch_id, progress)
    except BatchBusyError:
        await bot.send_message(chat_id, "⏳ Сейчас выполняется другая партия. Повторите позже из раздела «📦 Партии».")
        return
    except BatchStateError as exc:
        await bot.send_message(chat_id, f"ℹ️ {esc(exc)}")
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Сбой партии #%s", batch_id)
        await app.creator._mark_interrupted(batch_id, str(exc))  # noqa: SLF001
        report = await app.creator.report(batch_id)
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    await send_report(bot, chat_id, report)


def format_report(report: BatchReport) -> str:
    created = report.created
    failed = report.not_created
    if not failed:
        head = (f"✅ <b>Готово! Успешно создано {len(created)} "
                f"{plural(len(created), 'публикация', 'публикации', 'публикаций')}</b>")
    elif report.status == "cancelled":
        head = f"⏹ <b>Создание остановлено.</b> Создано {len(created)} из {report.total}"
    else:
        head = f"⚠️ <b>Создано {len(created)} из {report.total}</b>"
    lines = [head, f"Партия #{report.batch_id}: «{esc(report.title)}»", ""]
    for item in created:
        lines.append(f"{item.position}. {esc(item.author)} — {link(item.url, item.url) if item.url else '—'}")
    if failed:
        lines += ["", "<b>Не созданы:</b>"]
        for item in failed:
            reason = item.error or {"pending": "не обработана", "cancelled": "остановлено"}.get(item.status, item.status)
            lines.append(f"{ITEM_STATUS.get(item.status, '•')} {item.position}. {esc(item.author)} — {esc(reason)}")
    if report.fatal_error:
        lines += ["", f"❗ Партия остановлена: {esc(report.fatal_error)}"]
    return "\n".join(lines)


def report_kb(report: BatchReport) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if report.created:
        kb.row(button("📋 Открыть партию", PubList(ctx="batch", arg=report.batch_id, sort="old")),
               button("📥 Excel", ExportCb(act="batch", arg=report.batch_id)))
        kb.row(button("🎬 Привязать YouTube ко всей партии", BatchCb(act="yt", id=report.batch_id)))
    if report.not_created and report.status in {"partial", "cancelled", "interrupted"}:
        kb.row(button("🔁 Создать оставшиеся", BatchCb(act="resume", id=report.batch_id)))
    kb.row(button("📦 Карточка партии", BatchCb(act="card", id=report.batch_id)))
    return kb.as_markup()


async def send_report(bot: Bot, chat_id: int, report: BatchReport) -> None:
    chunks = split_message(format_report(report))
    for chunk in chunks[:-1]:
        await bot.send_message(chat_id, chunk)
    await bot.send_message(chat_id, chunks[-1], reply_markup=report_kb(report))


@router.callback_query(CreateCb.filter(F.act == "stop"))
async def on_stop(cb: CallbackQuery, callback_data: CreateCb, app: App) -> None:
    if app.creator.current_batch != callback_data.batch:
        await answer(cb, "Эта партия уже не выполняется")
        return
    app.creator.request_cancel(callback_data.batch)
    await answer(cb, "⏹ Остановлю после текущей публикации", alert=True)


@router.message(CreatePosts.title)
@router.message(CreatePosts.text)
@router.message(CreatePosts.authors)
@router.message(CreatePosts.confirm)
async def on_not_text(message: Message) -> None:
    await message.answer("Пожалуйста, отправьте текстовое сообщение или нажмите «Отмена».", reply_markup=cancel_kb())


# ====================================================================== партии


async def render_batches(app: App, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    async with app.db() as session:
        batches = (await session.execute(select(Batch).order_by(Batch.id.desc()))).scalars().all()
        created = dict((await session.execute(
            select(BatchItem.batch_id, func.count(BatchItem.id)).where(BatchItem.status == "created")
            .group_by(BatchItem.batch_id))).all())
    size = int(app.settings["page_size"])
    page = clamp_page(page, len(batches), size)
    chunk = batches[page * size : (page + 1) * size]
    lines = ["📦 <b>Партии публикаций</b>", f"Всего: {len(batches)}", ""]
    if not batches:
        lines.append("Партий пока нет.")
    numbered = []
    for b in chunk:
        lines.append(f"<b>#{b.id}</b> {esc(truncate(b.title, 50))}\n    {BATCH_STATUS.get(b.status, b.status)} · "
                     f"создано {created.get(b.id, 0)} из {b.total} · {fmt_dt(b.created_at, app.config.timezone)}")
        numbered.append((f"#{b.id}", BatchCb(act="card", id=b.id)))
    kb = InlineKeyboardBuilder()
    number_buttons(kb, numbered)
    pag = pagination_row(page, len(batches), size, lambda p: BatchCb(act="list", page=p))
    if pag:
        kb.row(*pag)
    kb.row(button("📚 Все публикации", PubList(ctx="all")), button("🏠 Меню", Nav(to="main")))
    return "\n".join(lines), kb.as_markup()


@router.callback_query(BatchCb.filter(F.act == "list"))
async def on_batches(cb: CallbackQuery, callback_data: BatchCb, app: App) -> None:
    await safe_edit(cb.message, *(await render_batches(app, callback_data.page)))
    await answer(cb)


@router.callback_query(BatchCb.filter(F.act == "card"))
async def on_batch_card(cb: CallbackQuery, callback_data: BatchCb, app: App) -> None:
    try:
        report = await app.creator.report(callback_data.id)
    except LookupError:
        await answer(cb, "Партия не найдена", alert=True)
        return
    async with app.db() as session:
        batch = await session.get(Batch, callback_data.id)
    text = (f"📦 <b>Партия #{report.batch_id}</b> · {BATCH_STATUS.get(report.status, report.status)}\n"
            f"Создана: {fmt_dt(batch.created_at, app.config.timezone)}\n\n" + format_report(report))
    kb = report_kb(report)
    rows = list(kb.inline_keyboard)
    rows.append([button("⬅️ К партиям", BatchCb(act="list"))])
    chunks = split_message(text)
    if len(chunks) == 1:
        await safe_edit(cb.message, text, InlineKeyboardMarkup(inline_keyboard=rows))
    else:
        for chunk in chunks[:-1]:
            await cb.message.answer(chunk)
        await cb.message.answer(chunks[-1], reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await answer(cb)


@router.callback_query(BatchCb.filter(F.act == "resume"))
async def on_resume(cb: CallbackQuery, callback_data: BatchCb, app: App, bot: Bot) -> None:
    async with app.create_guard:
        if app.creator.busy:
            await answer(cb, "⏳ Уже выполняется создание партии", alert=True)
            return
        report = await app.creator.report(callback_data.id)
        remaining = len(report.not_created)
        if remaining == 0:
            await answer(cb, "Все публикации партии уже созданы", alert=True)
            return
        progress = await cb.message.answer(f"⏳ Продолжение партии #{callback_data.id}: осталось {remaining}",
                                           reply_markup=_stop_kb(callback_data.id))
        app.spawn(run_with_progress(app, bot, progress.chat.id, progress.message_id, callback_data.id),
                  name=f"batch-{callback_data.id}")
        # даём задаче захватить блокировку партии, чтобы повторное нажатие увидело busy
        await asyncio.sleep(0)
    await answer(cb)


@router.callback_query(BatchCb.filter(F.act == "yt"))
async def on_batch_yt(cb: CallbackQuery, callback_data: BatchCb, app: App, state: FSMContext) -> None:
    report = await app.creator.report(callback_data.id)
    ids = [i.publication_id for i in report.created if i.publication_id]
    if not ids:
        await answer(cb, "В партии нет созданных публикаций", alert=True)
        return
    await start_bind(cb.message, state, target="batch", ids=ids)
    await answer(cb)


__all__ = ["router", "start_create", "send_report", "render_batches"]
