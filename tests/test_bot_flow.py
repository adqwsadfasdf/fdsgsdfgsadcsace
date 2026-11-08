"""Сквозной тест обработчиков: обновления Telegram подаются в Dispatcher, ответы перехватываются.

Проверяется также, что вся HTML-разметка корректна (иначе Telegram отклонит сообщение)
и что callback_data всех кнопок укладывается в 64 байта.
"""
from __future__ import annotations

import asyncio
import itertools
from datetime import datetime
from html.parser import HTMLParser

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, GetMe, SendDocument, SendMessage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User
from sqlalchemy import func, select

from database import Publication, YouTubeVideo
from handlers import setup_routers
from keyboards import main_menu as mm
from keyboards.callbacks import (
    AuthorCb, BatchCb, CreateCb, ExportCb, PubAct, PubCard, PubList, SearchCb, SelAct, SelToggle, SetCb,
    SortMenu, StatCb, YtCb,
)
from keyboards.navigation import ListNav
from middlewares.access import DENIED_TEXT, AccessMiddleware

ADMIN = 111
STRANGER = 999
ALLOWED_TAGS = {"b", "i", "u", "s", "a", "code", "pre", "blockquote", "tg-spoiler"}


class _HtmlChecker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"недопустимый тег <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"несбалансированный </{tag}> (стек {self.stack})")
        else:
            self.stack.pop()


def check_html(text: str) -> None:
    checker = _HtmlChecker()
    checker.feed(text)
    assert not checker.errors and not checker.stack, f"{checker.errors} {checker.stack}\n{text[:500]}"
    assert len(text) <= 4096, f"сообщение длиннее 4096: {len(text)}"


def check_markup(markup) -> None:
    if isinstance(markup, InlineKeyboardMarkup):
        for row in markup.inline_keyboard:
            for btn in row:
                if btn.callback_data is not None:
                    assert len(btn.callback_data.encode()) <= 64, btn.callback_data


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests: list = []
        self._ids = itertools.count(10_000)

    async def close(self):
        return None

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        text = getattr(method, "text", None)
        if isinstance(text, str) and getattr(method, "parse_mode", None) is not None:
            check_html(text)
        check_markup(getattr(method, "reply_markup", None))
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="bot", username="bot")
        returning = getattr(method, "__returning__", bool)
        if returning is bool:
            return True
        chat_id = getattr(method, "chat_id", ADMIN) or ADMIN
        return Message(
            message_id=getattr(method, "message_id", None) or next(self._ids), date=datetime.now(),
            chat=Chat(id=int(chat_id), type="private"), text=text if isinstance(text, str) else None,
        ).as_(bot)

    # --- помощники
    def texts(self) -> list[str]:
        out = []
        for m in self.requests:
            if isinstance(m, AnswerCallbackQuery):
                continue
            t = getattr(m, "text", None)
            if isinstance(t, str):
                out.append(t)
        return out

    def last_text(self) -> str:
        return self.texts()[-1]

    def alerts(self) -> list[str]:
        return [m.text or "" for m in self.requests if isinstance(m, AnswerCallbackQuery)]


ROUTER = setup_routers()  # роутеры — синглтоны модулей, подключаем один раз
DP = Dispatcher(storage=MemoryStorage())
DP.update.outer_middleware(AccessMiddleware(frozenset({ADMIN})))
DP.include_router(ROUTER)

_upd = itertools.count(1)
_msg = itertools.count(1)


def user(uid: int) -> User:
    return User(id=uid, is_bot=False, first_name="Test")


def msg_update(text: str, uid: int = ADMIN) -> Update:
    return Update(update_id=next(_upd), message=Message(
        message_id=next(_msg), date=datetime.now(), chat=Chat(id=uid, type="private"), from_user=user(uid), text=text))


def cb_update(data: str, uid: int = ADMIN) -> Update:
    return Update(update_id=next(_upd), callback_query=CallbackQuery(
        id=str(next(_upd)), from_user=user(uid), chat_instance="ci", data=data,
        message=Message(message_id=next(_msg), date=datetime.now(), chat=Chat(id=uid, type="private"),
                        from_user=User(id=1, is_bot=True, first_name="bot"), text="old")))


@pytest.fixture
def bot():
    session = FakeSession()
    b = Bot(token="123456:TEST", session=session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    return b


async def feed(bot, app, update):
    await DP.feed_update(bot, update, app=app)


async def send(bot, app, text, uid=ADMIN):
    await feed(bot, app, msg_update(text, uid))
    return bot.session.last_text()


async def press(bot, app, data, uid=ADMIN):
    if not isinstance(data, str):
        data = data.pack()
    await feed(bot, app, cb_update(data, uid))


async def wait_tasks(app):
    for _ in range(100):
        if not app.tasks:
            return
        await asyncio.gather(*list(app.tasks), return_exceptions=True)


async def test_access_denied(app, bot):
    await feed(bot, app, msg_update("/start", STRANGER))
    assert bot.session.last_text() == DENIED_TEXT
    await press(bot, app, PubList(ctx="all"), STRANGER)
    assert DENIED_TEXT in bot.session.alerts()[-1]
    async with app.db() as s:
        assert (await s.execute(select(func.count(Publication.id)))).scalar() == 0


async def test_full_flow(app, bot, mock_telegraph):
    s = bot.session
    assert "Telegraph-менеджер" in await send(bot, app, "/start")

    # ---- создание партии
    assert "шаг 1 из 3" in await send(bot, app, mm.BTN_CREATE)
    assert "Шаг 2 из 3" in await send(bot, app, "Новости технологий")
    assert "Принята часть 1" in await send(bot, app, "Здесь находится общий текст публикации.")
    await press(bot, app, CreateCb(act="textdone"))
    assert "Шаг 3 из 3" in s.last_text()
    preview = await send(bot, app, "Иван\nДмитрий\nАлександр\nМаксим\nСергей")
    assert "будет создано 5 публикаций" in preview
    await press(bot, app, CreateCb(act="go"))
    await press(bot, app, CreateCb(act="go"))  # повторное нажатие
    await wait_tasks(app)
    assert any("Готово! Успешно создано 5 публикаций" in t for t in s.texts())
    assert any("уже запущена" in a or "Уже выполняется" in a for a in s.alerts())
    assert len(mock_telegraph.pages) == 5

    # повторный автор — уведомление при подготовке
    await send(bot, app, mm.BTN_CREATE)
    await send(bot, app, "Ещё новости")
    await send(bot, app, "Текст")
    await press(bot, app, CreateCb(act="textdone"))
    assert "уже использовался ранее, публикаций: 1" in await send(bot, app, "Иван")
    await press(bot, app, "n:cancel")
    assert "отменено" in s.last_text()

    # ---- списки, карточка, периоды
    assert "Иван" in await send(bot, app, mm.BTN_ALL)
    nav = ListNav("all", 0, "new", 0, 0).encode()
    for per in ("24h", "7d", "30d", "all"):
        await press(bot, app, PubCard(id=1, per=per, nav=nav))
        assert "Всего просмотров" in s.last_text()
    await press(bot, app, SortMenu(ctx="all"))
    for sort in ("new", "old", "v", "d1", "d7", "d30", "au", "fast", "prom", "trend", "low"):
        await press(bot, app, PubList(ctx="all", sort=sort))
    await press(bot, app, PubList(ctx="all", page=5))  # за пределами — зажимается

    # ---- избранное
    await press(bot, app, PubAct(act="fav", id=1, nav=nav))
    assert "В избранном" in s.last_text()
    assert "Иван" in await send(bot, app, mm.BTN_FAV)

    # ---- обновление и проверка через API
    mock_telegraph.views[next(iter(mock_telegraph.pages))] = 7
    await press(bot, app, PubAct(act="refresh", id=1, nav=nav))
    await press(bot, app, PubAct(act="check", id=1, nav=nav))
    assert "Страница доступна" in s.alerts()[-1]

    # ---- YouTube из карточки
    await press(bot, app, PubAct(act="yt", id=1, nav=nav))
    assert "привязана к 1 публ." in await send(bot, app, "https://youtu.be/dQw4w9WgXcQ")
    async with app.db() as session:
        video = (await session.execute(select(YouTubeVideo))).scalar_one()
    await press(bot, app, YtCb(act="card", id=video.id))
    assert "Публикаций: 1" in s.last_text()

    # ---- массовые операции
    sel_nav = ListNav("all", 0, "new", 0, 1).encode()
    await press(bot, app, PubList(ctx="all", mode=1))
    await press(bot, app, SelToggle(id=2, nav=sel_nav))
    await press(bot, app, SelToggle(id=3, nav=sel_nav))
    await press(bot, app, SelAct(act="menu", nav=sel_nav))
    assert "Выбранные публикации: 2" in s.last_text()
    await press(bot, app, SelAct(act="yt", nav=sel_nav))
    await send(bot, app, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    await press(bot, app, YtCb(act="card", id=video.id))
    assert "Публикаций: 3" in s.last_text()
    await press(bot, app, SelAct(act="label", nav=sel_nav))
    assert "Метка установлена: 2" in await send(bot, app, "реклама")
    await press(bot, app, SelAct(act="favon", nav=sel_nav))
    await press(bot, app, SelAct(act="et", nav=sel_nav))
    assert "Изменено в Telegraph: 2 из 2" in await send(bot, app, "Исправленный заголовок")
    await press(bot, app, ExportCb(act="Lsel"))
    assert any(isinstance(m, SendDocument) for m in s.requests)
    await press(bot, app, SelAct(act="del", nav=sel_nav))
    assert "не удаляются" in s.last_text()
    await press(bot, app, SelAct(act="dely", nav=sel_nav))
    async with app.db() as session:
        assert (await session.execute(select(func.count(Publication.id)))).scalar() == 3
    assert len(mock_telegraph.pages) == 5

    # ---- поиск
    await send(bot, app, mm.BTN_SEARCH)
    assert "Иван" in await send(bot, app, "иван")
    await press(bot, app, SearchCb(act="f_yt"))
    result = await send(bot, app, "https://youtu.be/dQw4w9WgXcQ")
    assert "найдено: 1" in result
    await press(bot, app, ExportCb(act="Lsearch"))
    await press(bot, app, SearchCb(act="reset"))
    await send(bot, app, mm.BTN_SEARCH)
    assert "найдено: 3" in await send(bot, app, datetime.now(app.config.timezone).strftime("%d.%m.%Y"))

    # ---- разделы аналитики
    assert "Общая статистика" in await send(bot, app, mm.BTN_STATS)
    await press(bot, app, StatCb(act="info"))
    await press(bot, app, StatCb(act="refresh"))
    await wait_tasks(app)
    assert "Обновлено: 3 из 3" in s.last_text()
    assert "Лучшие публикации" in await send(bot, app, mm.BTN_BEST)
    assert "Рейтинг авторов" in await send(bot, app, mm.BTN_AUTHORS)
    for sort in ("v", "d1", "d7", "d30", "n"):
        await press(bot, app, AuthorCb(act="list", sort=sort))
    await press(bot, app, AuthorCb(act="card", id=1))
    assert "Иван" in s.last_text()
    await press(bot, app, PubList(ctx="author", arg=1))
    await press(bot, app, ExportCb(act="author", arg=1))

    # ---- группы, экспорт, партии, настройки
    assert "Группы YouTube" in await send(bot, app, mm.BTN_YT)
    await press(bot, app, PubList(ctx="nolink"))
    await press(bot, app, YtCb(act="ren", id=video.id))
    await send(bot, app, "Обзор")
    assert "Обзор" in s.last_text()
    await press(bot, app, ExportCb(act="yt", arg=video.id))
    assert "Экспорт в Excel" in await send(bot, app, mm.BTN_EXPORT)
    await press(bot, app, ExportCb(act="authors"))
    await press(bot, app, ExportCb(act="groups"))
    await press(bot, app, ExportCb(act="Lall"))
    await press(bot, app, BatchCb(act="list"))
    await press(bot, app, BatchCb(act="card", id=1))
    assert "Партия #1" in s.last_text()
    assert "Настройки" in await send(bot, app, mm.BTN_SETTINGS)
    for group in ("collect", "create", "status", "notify", "ui"):
        await press(bot, app, SetCb(act="grp", key=group))
    await press(bot, app, SetCb(act="tog", key="notify_daily_enabled"))
    assert app.settings["notify_daily_enabled"] is True
    await press(bot, app, SetCb(act="ch", key="snapshot_interval_minutes"))
    await press(bot, app, SetCb(act="set", key="snapshot_interval_minutes", val="60"))
    assert app.settings["snapshot_interval_minutes"] == 60
    await press(bot, app, SetCb(act="inp", key="status_growth_ratio"))
    await send(bot, app, "0.5")  # меньше минимума — отклоняется
    assert "Минимум" in s.last_text()
    await send(bot, app, "2,5")
    assert app.settings["status_growth_ratio"] == 2.5
    await press(bot, app, SetCb(act="acc"))
    assert "Аккаунт Telegraph" in s.last_text()
    await press(bot, app, SetCb(act="backup"))
    docs = [m for m in s.requests if isinstance(m, SendDocument)]
    assert any("bot_" in str(getattr(m.document, "path", "")) for m in docs), "копия БД не отправлена"

    # ---- удаление одной записи из карточки
    await press(bot, app, PubAct(act="del", id=1, nav=nav))
    await press(bot, app, PubAct(act="dely", id=1, nav=nav))
    async with app.db() as session:
        assert (await session.execute(select(func.count(Publication.id)))).scalar() == 2

    # ---- устаревшая кнопка и неизвестный текст
    await press(bot, app, PubCard(id=999, nav=nav))
    assert "не найдена" in s.alerts()[-1]
    await send(bot, app, "какой-то текст")
    assert "Выберите раздел" in s.last_text()

    # все исходящие сообщения не содержат включённого предпросмотра ссылок
    for m in s.requests:
        if isinstance(m, SendMessage):
            assert m.link_preview_options is None or getattr(m.link_preview_options, "is_disabled", True) is not False


async def test_import_and_competitors_flow(app, bot, mock_telegraph):
    from datetime import timezone

    from keyboards.callbacks import CompCb, ImpCb

    s = bot.session
    suffix = datetime.now(timezone.utc).strftime("%m-%d")
    own_url = mock_telegraph.add_page(f"Gotovyj-{suffix}", "Готовый пост", "Олег", views=40)
    rival_urls = [mock_telegraph.add_page(f"Rival-{i}-{suffix}", f"Пост конкурента {i}", "Соперник", views=10 * i)
                  for i in (1, 2, 3)]

    # ---- свои готовые по ссылкам
    assert "Добавить готовые публикации" in await send(bot, app, mm.BTN_IMPORT)
    await press(bot, app, ImpCb(act="links"))
    assert "каждую с новой строки" in s.last_text()
    await send(bot, app, "это не ссылка")
    assert "Не нашёл ни одной ссылки" in s.last_text()
    await send(bot, app, f"{own_url}\nмусор")
    report = s.last_text()
    assert "Добавлено публикаций: 1" in report and "другом аккаунте" in report and "мусор" in report
    await wait_tasks(app)
    await press(bot, app, PubList(ctx="imp", sort="v"))
    assert "Готовый пост" in s.last_text()
    own_id = (await pubs_ids(app, "imported"))[0]
    await press(bot, app, PubCard(id=own_id))
    card = s.last_text()
    assert "Добавлена готовой" in card and "без редактирования" in card
    await press(bot, app, ImpCb(act="accs"))
    assert "Выберите аккаунт" in s.last_text()
    await press(bot, app, ImpCb(act="token"))
    await send(bot, app, "bad token")
    assert "Токен не принят" in s.last_text()
    await press(bot, app, "n:cancel")

    # ---- конкуренты
    assert "Конкуренты" in await send(bot, app, mm.BTN_COMP)
    await press(bot, app, CompCb(act="add"))
    await press(bot, app, CompCb(act="new"))
    await send(bot, app, "Канал Соперника")
    assert "Конкурент: <b>Канал Соперника</b>" in s.last_text()
    await send(bot, app, "\n".join(rival_urls + [own_url]))
    report = s.last_text()
    assert "Добавлено публикаций конкурента: 3" in report and "Уже есть среди ваших публикаций: 1" in report
    await wait_tasks(app)
    await send(bot, app, mm.BTN_COMP)
    menu = s.last_text()
    assert "Канал Соперника — 3 публ." in menu and "Сравнение" in menu
    rival_id = (await app.analytics.get_competitors())
    comp_id = next(iter(rival_id.values())).competitor_id
    await press(bot, app, CompCb(act="card", id=comp_id))
    assert "Всего просмотров: <b>60</b>" in s.last_text()
    await press(bot, app, PubList(ctx="compc", arg=comp_id, sort="v"))
    listing = s.last_text()
    assert "Пост конкурента 3" in listing and "Готовый пост" not in listing
    nav = ListNav("compc", comp_id, "v", 0, 0).encode()
    pub_id = max(rival_id)
    await press(bot, app, PubCard(id=pub_id, nav=nav))
    assert "Отслеживается с" in s.last_text() and "Конкурент: <b>Канал Соперника</b>" in s.last_text()
    await press(bot, app, PubAct(act="refresh", id=pub_id, nav=nav))
    await press(bot, app, CompCb(act="refresh"))
    assert "Обновлено: 3 из 3" in s.last_text()
    await press(bot, app, CompCb(act="mv", id=pub_id))
    await press(bot, app, CompCb(act="mvto", id=pub_id, to=0))
    assert "без группы" in s.last_text()
    await press(bot, app, CompCb(act="card", id=0))
    assert "Без группы" in s.last_text()
    await press(bot, app, ExportCb(act="Lcomp"))
    assert any(isinstance(m, SendDocument) for m in s.requests)
    await press(bot, app, CompCb(act="ren", id=comp_id))
    await send(bot, app, "Переименованный")
    assert "Переименованный" in s.last_text()
    # свой раздел не видит конкурентов
    assert "Пост конкурента" not in await send(bot, app, mm.BTN_ALL)
    await press(bot, app, CompCb(act="del", id=comp_id))
    await press(bot, app, CompCb(act="dely", id=comp_id))
    assert len(await app.analytics.get_competitors()) == 1  # осталась одна «без группы»
    await press(bot, app, PubAct(act="del", id=pub_id))
    await press(bot, app, PubAct(act="dely", id=pub_id))
    assert not await app.analytics.get_competitors()


async def pubs_ids(app, kind: str) -> list[int]:
    async with app.db() as session:
        return list((await session.execute(select(Publication.id).where(Publication.kind == kind))).scalars())
