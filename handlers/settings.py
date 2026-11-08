"""Раздел «Настройки»: интервалы, пороги, уведомления, аккаунт Telegraph, резервная копия."""
from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app_context import App
from handlers.common import answer, error_text, safe_edit
from handlers.states import SettingInput
from keyboards.callbacks import Nav, SetCb, StatCb
from keyboards.inline import button
from keyboards.navigation import cancel_kb
from services.accounts import mask_token
from services.settings_store import GROUPS, METRIC_LABELS, SPEC_BY_KEY, SPECS, display_value
from utils.backup import backup_sqlite
from utils.formatting import esc, fmt_ago, fmt_dt, link

logger = logging.getLogger(__name__)
router = Router(name="settings")

TELEGRAM_UPLOAD_LIMIT = 49 * 1024 * 1024


async def render_menu(app: App) -> tuple[str, InlineKeyboardMarkup]:
    last = await app.settings.get_state("last_collect")
    daily = await app.settings.get_state("last_daily_sync")
    lines = [
        "⚙️ <b>Настройки</b>",
        "",
        f"⏱ Снимки просмотров: каждые {app.settings['snapshot_interval_minutes']} мин · "
        f"последний сбор: {fmt_ago(last.get('ts')) if last else 'ещё не было'}",
        f"📅 Данные по дням: каждые {app.settings['daily_sync_hours']} ч · "
        f"последняя синхронизация: {fmt_ago(daily.get('ts')) if daily else 'ещё не было'}",
        f"🔔 Уведомления включены: {sum(1 for s in SPECS if s.group == 'notify' and s.type is bool and app.settings[s.key])}",
    ]
    kb = InlineKeyboardBuilder()
    for code, title in GROUPS.items():
        kb.row(button(title, SetCb(act="grp", key=code)))
    kb.row(button("🧾 Аккаунт Telegraph", SetCb(act="acc")), button("💾 Резервная копия", SetCb(act="backup")))
    kb.row(button("ℹ️ О точности данных", StatCb(act="info")))
    kb.row(button("🏠 Меню", Nav(to="main")))
    return "\n".join(lines), kb.as_markup()


async def render_group(app: App, group: str) -> tuple[str, InlineKeyboardMarkup]:
    specs = [s for s in SPECS if s.group == group]
    lines = [f"<b>{GROUPS.get(group, group)}</b>", ""]
    kb = InlineKeyboardBuilder()
    for spec in specs:
        value = app.settings[spec.key]
        lines.append(f"• {esc(spec.label)}: <b>{esc(display_value(spec, value))}</b>")
        if spec.help:
            lines.append(f"  <i>{esc(spec.help)}</i>")
        if spec.type is bool:
            kb.row(button(f"{'✅' if value else '❌'} {spec.label}", SetCb(act="tog", key=spec.key)))
        else:
            kb.row(button(f"✏️ {spec.label}: {display_value(spec, value)}", SetCb(act="ch", key=spec.key)))
    if group == "notify":
        lines += ["", "<i>Одинаковые уведомления не повторяются чаще заданного интервала. "
                      "Проверка выполняется после каждого сбора статистики.</i>"]
    if group == "status":
        lines += ["", "<i>Статус считается по приросту за 24 ч относительно «обычной скорости» публикации "
                      "(предыдущие сутки и среднее за неделю). Новые публикации получают «Мало данных».</i>"]
    kb.row(button("⬅️ Назад", SetCb(act="menu")))
    return "\n".join(lines), kb.as_markup()


@router.callback_query(SetCb.filter(F.act == "menu"))
async def on_menu(cb: CallbackQuery, app: App) -> None:
    await safe_edit(cb.message, *(await render_menu(app)))
    await answer(cb)


@router.callback_query(SetCb.filter(F.act == "grp"))
async def on_group(cb: CallbackQuery, callback_data: SetCb, app: App) -> None:
    await safe_edit(cb.message, *(await render_group(app, callback_data.key)))
    await answer(cb)


@router.callback_query(SetCb.filter(F.act == "tog"))
async def on_toggle(cb: CallbackQuery, callback_data: SetCb, app: App) -> None:
    spec = SPEC_BY_KEY.get(callback_data.key)
    if spec is None or spec.type is not bool:
        await answer(cb, "Неизвестная настройка", alert=True)
        return
    await app.settings.set(spec.key, not app.settings[spec.key])
    await safe_edit(cb.message, *(await render_group(app, spec.group)))
    await answer(cb, "Сохранено")


@router.callback_query(SetCb.filter(F.act == "ch"))
async def on_choices(cb: CallbackQuery, callback_data: SetCb, app: App) -> None:
    spec = SPEC_BY_KEY.get(callback_data.key)
    if spec is None:
        await answer(cb, "Неизвестная настройка", alert=True)
        return
    kb = InlineKeyboardBuilder()
    current = app.settings[spec.key]
    for choice in spec.choices or ():
        label = METRIC_LABELS.get(choice, f"{choice:g}" if isinstance(choice, float) else str(choice))
        kb.button(text=("• " if choice == current else "") + label,
                  callback_data=SetCb(act="set", key=spec.key, val=str(choice)))
    kb.adjust(3)
    if spec.type in (int, float):
        kb.row(button("⌨️ Ввести значение", SetCb(act="inp", key=spec.key)))
    kb.row(button("⬅️ Назад", SetCb(act="grp", key=spec.group)))
    limits = ""
    if spec.min is not None or spec.max is not None:
        limits = f"\nДопустимо: {spec.min:g} … {spec.max:g}" if spec.max is not None else f"\nМинимум: {spec.min:g}"
    await safe_edit(cb.message, f"<b>{esc(spec.label)}</b>\nСейчас: <b>{esc(display_value(spec, current))}</b>"
                                f"{limits}\n\n{esc(spec.help)}", kb.as_markup())
    await answer(cb)


@router.callback_query(SetCb.filter(F.act == "set"))
async def on_set(cb: CallbackQuery, callback_data: SetCb, app: App) -> None:
    spec = SPEC_BY_KEY.get(callback_data.key)
    if spec is None:
        await answer(cb, "Неизвестная настройка", alert=True)
        return
    try:
        await app.settings.set(spec.key, callback_data.val)
    except ValueError as exc:
        await answer(cb, f"❗ {exc}", alert=True)
        return
    await safe_edit(cb.message, *(await render_group(app, spec.group)))
    await answer(cb, "Сохранено")


@router.callback_query(SetCb.filter(F.act == "inp"))
async def on_input_start(cb: CallbackQuery, callback_data: SetCb, state: FSMContext) -> None:
    spec = SPEC_BY_KEY.get(callback_data.key)
    if spec is None:
        await answer(cb, "Неизвестная настройка", alert=True)
        return
    await state.set_state(SettingInput.value)
    await state.update_data(key=spec.key)
    await cb.message.answer(f"⌨️ Отправьте новое значение для «{esc(spec.label)}».", reply_markup=cancel_kb())
    await answer(cb)


@router.message(SettingInput.value, F.text)
async def on_input(message: Message, state: FSMContext, app: App) -> None:
    key = (await state.get_data()).get("key")
    spec = SPEC_BY_KEY.get(key or "")
    if spec is None:
        await state.clear()
        await message.answer("Настройка не найдена.")
        return
    try:
        await app.settings.set(spec.key, message.text)
    except ValueError as exc:
        await message.answer(f"❗ {exc}. Попробуйте ещё раз.", reply_markup=cancel_kb())
        return
    await state.clear()
    text, kb = await render_group(app, spec.group)
    await message.answer("✅ Сохранено\n\n" + text, reply_markup=kb)


# ---------------------------------------------------------------------- аккаунт Telegraph


@router.callback_query(SetCb.filter(F.act == "acc"))
async def on_account(cb: CallbackQuery, app: App) -> None:
    await answer(cb)
    account, info, err = await app.accounts.info()
    lines = [
        "🧾 <b>Аккаунт Telegraph</b>",
        "",
        f"Имя аккаунта: <b>{esc(info.short_name if info else account.short_name)}</b>",
        f"Токен: <code>{esc(mask_token(account.access_token))}</code> "
        f"<i>({'из .env' if app.config.telegraph_token == account.access_token else 'создан ботом, хранится в БД'})</i>",
        f"Подключён: {fmt_dt(account.created_at, app.config.timezone)}",
    ]
    if info is not None:
        lines.append(f"Страниц в аккаунте: {info.page_count if info.page_count is not None else '—'}")
    else:
        lines.append(f"❗ Не удалось получить данные аккаунта: {esc(err)}")
    lines += ["", "<i>Каждая публикация помнит аккаунт, которым создана, — поэтому редактирование старых "
                  "страниц работает и после смены аккаунта.</i>"]
    kb = InlineKeyboardBuilder()
    kb.row(button("🔑 Ссылка для входа в Telegraph (браузер)", SetCb(act="auth")))
    if not app.config.telegraph_token:
        kb.row(button("➕ Создать новый аккаунт", SetCb(act="accnew")))
    kb.row(button("⬅️ Назад", SetCb(act="menu")))
    await safe_edit(cb.message, "\n".join(lines), kb.as_markup())


@router.callback_query(SetCb.filter(F.act == "auth"))
async def on_auth_url(cb: CallbackQuery, app: App) -> None:
    account = await app.accounts.active()
    try:
        info = await app.telegraph.get_account_info(account.access_token, ["auth_url"])
    except Exception as exc:  # noqa: BLE001
        await answer(cb, f"Ошибка: {error_text(exc)}"[:190], alert=True)
        return
    await answer(cb)
    if info.auth_url:
        await cb.message.answer(
            f"🔑 Одноразовая ссылка для входа в аккаунт Telegraph в браузере (действует ~5 минут):\n"
            f"{link(info.auth_url, 'Войти в Telegraph')}\n\n"
            "<i>Не пересылайте её никому — она даёт доступ к редактированию ваших страниц.</i>"
        )


@router.callback_query(SetCb.filter(F.act == "accnew"))
async def on_account_new(cb: CallbackQuery) -> None:
    kb = InlineKeyboardBuilder()
    kb.row(button("✅ Да, создать", SetCb(act="accnewy")), button("Отмена", SetCb(act="acc")))
    await safe_edit(cb.message, "Создать новый аккаунт Telegraph (createAccount) и использовать его для новых "
                                "публикаций? Старые публикации останутся привязаны к прежнему аккаунту.",
                    kb.as_markup())
    await answer(cb)


@router.callback_query(SetCb.filter(F.act == "accnewy"))
async def on_account_new_yes(cb: CallbackQuery, app: App) -> None:
    if app.creator.busy:
        await answer(cb, "Дождитесь окончания создания партии", alert=True)
        return
    try:
        account = await app.accounts.create_new_account()
    except Exception as exc:  # noqa: BLE001
        await answer(cb, f"Ошибка: {error_text(exc)}"[:190], alert=True)
        return
    await answer(cb, f"Создан аккаунт {account.short_name}", alert=True)
    await on_account(cb, app)


# ---------------------------------------------------------------------- резервная копия


@router.callback_query(SetCb.filter(F.act == "backup"))
async def on_backup(cb: CallbackQuery, app: App) -> None:
    await answer(cb, "💾 Создаю резервную копию…")
    try:
        path = await asyncio.to_thread(backup_sqlite, app.config.database_path, app.config.backup_dir)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка резервного копирования")
        await cb.message.answer(f"❗ Не удалось создать копию: {esc(exc)}")
        return
    size = path.stat().st_size
    if size <= TELEGRAM_UPLOAD_LIMIT:
        await cb.message.answer_document(FSInputFile(path), caption=f"💾 Резервная копия базы ({size // 1024} КБ). "
                                                                   f"Также сохранена на сервере: {path.name}")
    else:
        await cb.message.answer(f"💾 Копия создана на сервере: <code>{esc(path)}</code> "
                                "(слишком большая для отправки в Telegram).")
