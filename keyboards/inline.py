"""Общие построители inline-клавиатур."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from keyboards.callbacks import CallbackData


def pages_count(total: int, page_size: int) -> int:
    return max(1, (total + page_size - 1) // page_size)


def clamp_page(page: int, total: int, page_size: int) -> int:
    return max(0, min(page, pages_count(total, page_size) - 1))


def pagination_row(page: int, total: int, page_size: int, make_cb) -> list[InlineKeyboardButton]:
    """make_cb(page) -> CallbackData. Возвращает пустой список, если страница одна."""
    pages = pages_count(total, page_size)
    if pages <= 1:
        return []
    row = []
    row.append(InlineKeyboardButton(
        text="⏮" if page > 0 else "·", callback_data=make_cb(0).pack() if page > 0 else make_cb(page).pack()
    ))
    row.append(InlineKeyboardButton(
        text="◀️" if page > 0 else "·", callback_data=make_cb(max(0, page - 1)).pack()
    ))
    row.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=make_cb(page).pack()))
    row.append(InlineKeyboardButton(
        text="▶️" if page < pages - 1 else "·", callback_data=make_cb(min(pages - 1, page + 1)).pack()
    ))
    row.append(InlineKeyboardButton(
        text="⏭" if page < pages - 1 else "·", callback_data=make_cb(pages - 1).pack()
    ))
    return row


def number_buttons(builder: InlineKeyboardBuilder, items: list[tuple[str, CallbackData]], per_row: int = 5) -> None:
    for i in range(0, len(items), per_row):
        builder.row(*[InlineKeyboardButton(text=t, callback_data=cb.pack()) for t, cb in items[i : i + per_row]])


def button(text: str, cb: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb.pack())
