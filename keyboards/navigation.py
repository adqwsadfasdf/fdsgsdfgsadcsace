"""Навигация: кодирование контекста списка и кнопки «Назад» / «Отмена»."""
from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from keyboards.callbacks import AuthorCb, BatchCb, CompCb, ImpCb, Nav, PubList, SearchCb, SelAct, YtCb


@dataclass(slots=True)
class ListNav:
    ctx: str = "all"
    arg: int = 0
    sort: str = "new"
    page: int = 0
    mode: int = 0

    def encode(self) -> str:
        return f"{self.ctx}.{self.arg}.{self.sort}.{self.page}.{self.mode}"

    @classmethod
    def decode(cls, raw: str | None) -> "ListNav | None":
        if not raw:
            return None
        parts = raw.split(".")
        if len(parts) != 5:
            return None
        try:
            return cls(parts[0], int(parts[1]), parts[2], int(parts[3]), int(parts[4]))
        except ValueError:
            return None

    def to_list_cb(self, **changes) -> PubList:
        data = {"ctx": self.ctx, "arg": self.arg, "sort": self.sort, "page": self.page, "mode": self.mode}
        data.update(changes)
        return PubList(**data)


def back_button(text: str = "⬅️ Назад", to: str = "main") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=Nav(to=to).pack())


def cancel_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="❌ Отмена", callback_data=Nav(to="cancel").pack())


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[cancel_button()]])


def list_parent_button(nav: ListNav) -> InlineKeyboardButton:
    """Куда ведёт «Назад» из списка публикаций в зависимости от контекста."""
    text = "⬅️ Назад"
    ctx = nav.ctx
    if ctx == "author":
        return InlineKeyboardButton(text=text, callback_data=AuthorCb(act="card", id=nav.arg).pack())
    if ctx == "yt":
        return InlineKeyboardButton(text=text, callback_data=YtCb(act="card", id=nav.arg).pack())
    if ctx == "nolink":
        return InlineKeyboardButton(text=text, callback_data=YtCb(act="list").pack())
    if ctx == "search":
        return InlineKeyboardButton(text=text, callback_data=SearchCb(act="menu").pack())
    if ctx == "sel":
        return InlineKeyboardButton(text=text, callback_data=SelAct(act="menu", nav="").pack())
    if ctx == "batch":
        return InlineKeyboardButton(text=text, callback_data=BatchCb(act="card", id=nav.arg).pack())
    if ctx == "best":
        return InlineKeyboardButton(text=text, callback_data=Nav(to="best").pack())
    if ctx == "stat":
        return InlineKeyboardButton(text=text, callback_data=Nav(to="stats").pack())
    if ctx == "imp":
        return InlineKeyboardButton(text=text, callback_data=ImpCb(act="menu").pack())
    if ctx == "comp":
        return InlineKeyboardButton(text=text, callback_data=CompCb(act="menu").pack())
    if ctx == "compc":
        return InlineKeyboardButton(text=text, callback_data=CompCb(act="card", id=nav.arg).pack())
    return InlineKeyboardButton(text="🏠 Меню", callback_data=Nav(to="main").pack())
