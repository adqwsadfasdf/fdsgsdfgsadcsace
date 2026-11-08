"""Пост-образец: бот копирует заголовок и текст (со всем форматированием) готовой страницы Telegraph,
а при создании меняет только имя автора — маленькую строчку под заголовком.

Образец загружается один раз через Telegraph API (getPage с содержимым) и хранится в базе бота.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from services.telegraph import TelegraphAPIError, content_size, CONTENT_LIMIT_BYTES
from utils.validators import ValidationError, parse_telegraph_path

if TYPE_CHECKING:
    from app_context import App

STATE_KEY = "post_template"
TELEGRAPH_ORIGIN = "https://telegra.ph"


@dataclass
class Template:
    path: str
    url: str
    title: str
    content_json: str
    loaded_at: int

    @property
    def nodes(self) -> list[Any]:
        return json.loads(self.content_json)


def absolutize(nodes: list[Any]) -> list[Any]:
    """Относительные ссылки Telegraph (/file/….jpg) → полные, чтобы картинки и ссылки работали в копии."""
    out: list[Any] = []
    for node in nodes or []:
        if isinstance(node, dict):
            node = dict(node)
            attrs = dict(node.get("attrs") or {})
            for key in ("src", "href"):
                value = attrs.get(key)
                if isinstance(value, str) and value.startswith("/") and not value.startswith("//"):
                    attrs[key] = TELEGRAPH_ORIGIN + value
            if attrs:
                node["attrs"] = attrs
            if node.get("children"):
                node["children"] = absolutize(node["children"])
        out.append(node)
    return out


async def get_template(app: "App") -> Template | None:
    data = await app.settings.get_state(STATE_KEY)
    if not data:
        return None
    try:
        return Template(**data)
    except TypeError:
        return None


async def load_template(app: "App", text: str) -> Template:
    """Загружает образец по ссылке Telegraph и сохраняет его как текущий."""
    path = parse_telegraph_path(text or "")
    if not path:
        raise ValidationError("Это не ссылка Telegraph. Пример: https://telegra.ph/Primer-11-08")
    try:
        page = await app.telegraph.get_page(path, return_content=True)
    except TelegraphAPIError as exc:
        if exc.code == "PAGE_NOT_FOUND":
            raise ValidationError("Страница не найдена в Telegraph — проверьте ссылку.") from exc
        raise
    nodes = absolutize(page.content or [])
    if not nodes:
        raise ValidationError("У этой страницы пустой текст — выберите другой образец.")
    if content_size(nodes) > CONTENT_LIMIT_BYTES:
        raise ValidationError("Текст образца слишком большой для Telegraph (лимит 64 КБ).")
    template = Template(path=page.path, url=page.url, title=page.title[:256] or page.path,
                        content_json=json.dumps(nodes, ensure_ascii=False), loaded_at=int(time.time()))
    await app.settings.set_state(STATE_KEY, template.__dict__)
    return template
