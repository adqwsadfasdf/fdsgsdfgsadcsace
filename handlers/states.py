"""Состояния FSM."""
from aiogram.fsm.state import State, StatesGroup


class CreatePosts(StatesGroup):
    template = State()  # ждём ссылку на пост-образец
    names = State()  # ждём имена для копий образца
    title = State()
    text = State()
    authors = State()
    confirm = State()


class PubInput(StatesGroup):
    label = State()
    title = State()
    text = State()
    author = State()


class YtInput(StatesGroup):
    link = State()
    find = State()
    rename = State()
    change = State()


class SearchInput(StatesGroup):
    query = State()
    field = State()


class SettingInput(StatesGroup):
    value = State()


class TrackInput(StatesGroup):
    own_links = State()  # ссылки на свои готовые публикации
    token = State()  # access_token другого аккаунта Telegraph
    comp_name = State()  # название нового конкурента
    comp_links = State()  # ссылки на публикации конкурента
    comp_rename = State()
