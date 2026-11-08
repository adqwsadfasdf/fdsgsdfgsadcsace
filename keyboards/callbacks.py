"""Фабрики callback_data (aiogram 3). Все значения короткие: лимит Telegram — 64 байта."""
from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class Nav(CallbackData, prefix="n"):
    """Переход в раздел: main, pubs, stats, best, authors, search, yt, fav, export, settings, cancel."""

    to: str


class PubList(CallbackData, prefix="pl"):
    ctx: str
    arg: int = 0
    sort: str = "new"
    page: int = 0
    mode: int = 0  # 1 — режим выбора


class SortMenu(CallbackData, prefix="ps"):
    ctx: str
    arg: int = 0
    mode: int = 0


class PubCard(CallbackData, prefix="pc"):
    id: int
    per: str = ""  # "" — короткая карточка; 24h / 7d / 30d / all — подробности за период
    nav: str = ""  # закодированный контекст списка для кнопки «Назад»


class PubAct(CallbackData, prefix="pa"):
    act: str
    id: int
    nav: str = ""


class SelToggle(CallbackData, prefix="st"):
    id: int
    nav: str


class SelAct(CallbackData, prefix="sa"):
    act: str
    nav: str = ""


class AuthorCb(CallbackData, prefix="au"):
    act: str  # list | card
    id: int = 0
    sort: str = "v"
    page: int = 0


class YtCb(CallbackData, prefix="yt"):
    act: str
    id: int = 0
    page: int = 0


class StatCb(CallbackData, prefix="sc"):
    act: str


class SearchCb(CallbackData, prefix="sr"):
    act: str


class ExportCb(CallbackData, prefix="ex"):
    act: str
    arg: int = 0
    page: int = 0


class SetCb(CallbackData, prefix="se"):
    act: str
    key: str = ""
    val: str = ""


class CreateCb(CallbackData, prefix="cr"):
    act: str
    batch: int = 0


class BatchCb(CallbackData, prefix="bt"):
    act: str
    id: int = 0
    page: int = 0


class ImpCb(CallbackData, prefix="im"):
    """Добавление готовых своих публикаций."""

    act: str
    id: int = 0


class CompCb(CallbackData, prefix="cp"):
    """Раздел «Конкуренты». id — конкурент (0 — без группы) или публикация (для act=mv/mvto)."""

    act: str
    id: int = 0
    page: int = 0
    to: int = 0
