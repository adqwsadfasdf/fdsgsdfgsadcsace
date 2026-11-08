"""Преобразование текста из Telegram (HTML с форматированием) в узлы Telegraph (Node)."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any

from services.telegraph import CONTENT_LIMIT_BYTES, content_size
from utils.validators import ValidationError

# Теги, которые принимает Telegraph
ALLOWED_TAGS = {
    "a", "aside", "b", "blockquote", "br", "code", "em", "figcaption", "figure", "h3", "h4",
    "hr", "i", "iframe", "img", "li", "ol", "p", "pre", "s", "strong", "u", "ul", "video",
}
# Telegram -> Telegraph
TAG_MAP = {"ins": "u", "del": "s", "strike": "s"}
# Теги Telegram, которые «разворачиваются» (остаётся только содержимое)
UNWRAP_TAGS = {"tg-spoiler", "span", "tg-emoji", "tg-time"}
BLOCK_TAGS = {"pre", "blockquote"}

Node = Any  # str | dict


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: dict[str, Any] = {"tag": None, "orig": None, "children": []}
        self.stack: list[dict[str, Any]] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "br":
            self.stack[-1]["children"].append({"tag": "br"})
            return
        mapped = TAG_MAP.get(tag, tag)
        if tag in UNWRAP_TAGS or mapped not in ALLOWED_TAGS:
            mapped = None
        node: dict[str, Any] = {"tag": mapped, "orig": tag, "children": []}
        if mapped == "a":
            href = dict(attrs).get("href")
            if href:
                node["attrs"] = {"href": href}
        self.stack[-1]["children"].append(node)
        self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "br":
            self.stack[-1]["children"].append({"tag": "br"})

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for idx in range(len(self.stack) - 1, 0, -1):
            if self.stack[idx].get("orig") == tag:
                del self.stack[idx:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1]["children"].append(data)


def _flatten(nodes: list[Node]) -> list[Node]:
    """Убирает «прозрачные» узлы (tag=None), оставляя их содержимое."""
    out: list[Node] = []
    for node in nodes:
        if isinstance(node, str):
            out.append(node)
        elif node.get("tag") is None:
            out.extend(_flatten(node.get("children", [])))
        else:
            clean: dict[str, Any] = {"tag": node["tag"]}
            if node.get("attrs"):
                clean["attrs"] = node["attrs"]
            if node["tag"] != "br":
                clean["children"] = _flatten(node.get("children", []))
            out.append(clean)
    return out


def _newlines_to_br(nodes: list[Node]) -> list[Node]:
    out: list[Node] = []
    for node in nodes:
        if isinstance(node, str):
            parts = node.split("\n")
            for i, part in enumerate(parts):
                if i > 0:
                    out.append({"tag": "br"})
                if part:
                    out.append(part)
        else:
            if node.get("tag") not in {"pre", "br"} and "children" in node:
                node = dict(node)
                node["children"] = _newlines_to_br(node["children"])
            out.append(node)
    return out


def _is_blank(nodes: list[Node]) -> bool:
    for node in nodes:
        if isinstance(node, str):
            if node.strip():
                return False
        elif node.get("tag") != "br":
            if node.get("tag") in {"hr", "img"} or not _is_blank(node.get("children", [])):
                return False
    return True


def _trim(nodes: list[Node]) -> list[Node]:
    nodes = list(nodes)
    while nodes and (
        (isinstance(nodes[0], dict) and nodes[0].get("tag") == "br")
        or (isinstance(nodes[0], str) and not nodes[0].strip())
    ):
        nodes.pop(0)
    while nodes and (
        (isinstance(nodes[-1], dict) and nodes[-1].get("tag") == "br")
        or (isinstance(nodes[-1], str) and not nodes[-1].strip())
    ):
        nodes.pop()
    if nodes and isinstance(nodes[0], str):
        nodes[0] = nodes[0].lstrip()
    if nodes and isinstance(nodes[-1], str):
        nodes[-1] = nodes[-1].rstrip()
    return [n for n in nodes if n != ""]


def _drop_empty_children(node: Node) -> Node:
    if isinstance(node, str):
        return node
    if "children" in node:
        children = [_drop_empty_children(c) for c in node["children"] if c != ""]
        if children:
            node["children"] = children
        else:
            node.pop("children")
    return node


def html_to_nodes(html_text: str) -> list[Node]:
    """HTML из Telegram (message.html_text) -> массив Node для Telegraph.

    Пустая строка между абзацами -> отдельный <p>; одиночный перевод строки -> <br>.
    """
    parser = _Parser()
    parser.feed(html_text or "")
    parser.close()
    nodes = _flatten(parser.root["children"])

    blocks: list[Node] = []
    current: list[Node] = []

    def flush() -> None:
        nonlocal current
        para = _trim(_newlines_to_br(current))
        if para and not _is_blank(para):
            blocks.append({"tag": "p", "children": para})
        current = []

    for node in nodes:
        if isinstance(node, str):
            pieces = re.split(r"\n[ \t]*\n+", node)
            for i, piece in enumerate(pieces):
                if i > 0:
                    flush()
                if piece:
                    current.append(piece)
        elif node.get("tag") in BLOCK_TAGS:
            flush()
            if node["tag"] == "blockquote":
                node = dict(node)
                node["children"] = _trim(_newlines_to_br(node.get("children", [])))
            if not _is_blank(node.get("children", [])):
                blocks.append(node)
        else:
            current.append(node)
    flush()
    return [_drop_empty_children(b) for b in blocks]


def plain_to_html(text: str) -> str:
    import html

    return html.escape(text or "", quote=False)


def prepare_content(html_text: str) -> tuple[list[Node], str]:
    """Проверяет текст и возвращает (узлы, JSON). Выбрасывает ValidationError."""
    nodes = html_to_nodes(html_text)
    if not nodes:
        raise ValidationError("Текст публикации пуст.")
    size = content_size(nodes)
    if size > CONTENT_LIMIT_BYTES:
        raise ValidationError(
            f"Текст слишком большой: {size // 1024} КБ после преобразования. "
            f"Лимит Telegraph — {CONTENT_LIMIT_BYTES // 1024} КБ."
        )
    return nodes, json.dumps(nodes, ensure_ascii=False)


def nodes_to_text(nodes: list[Node]) -> str:
    """Грубое текстовое представление узлов (для предпросмотра)."""
    out: list[str] = []

    def walk(items: list[Node]) -> None:
        for item in items:
            if isinstance(item, str):
                out.append(item)
            elif item.get("tag") == "br":
                out.append("\n")
            else:
                walk(item.get("children", []))
                if item.get("tag") in {"p", "pre", "blockquote", "h3", "h4", "li"}:
                    out.append("\n\n")

    walk(nodes)
    return "".join(out).strip()
