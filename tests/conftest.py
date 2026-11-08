"""Фикстуры тестов: локальный mock Telegraph API и приложение на временной БД.

Реальный telegra.ph в тестах не используется — публичные страницы не создаются.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("ADMIN_ID", "111")


def _translit(title: str) -> str:
    table = str.maketrans({
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
        "й": "j", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
        "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
        "э": "eh", "ю": "yu", "я": "ya",
    })
    slug = title.lower().translate(table)
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return "-".join(w.capitalize() for w in slug.split("-")) or "Page"


class MockTelegraph:
    """Эмулирует api.telegra.ph с возможностью внедрять сбои."""

    def __init__(self) -> None:
        self.accounts: dict[str, dict] = {}
        self.pages: dict[str, dict] = {}
        self.order: list[str] = []
        self.views: dict[str, int] = {}
        self.daily_views: dict[tuple[str, int, int, int], int] = {}
        self.year_views: dict[tuple[str, int], int] = {}
        self.calls: list[str] = []
        # внедрение сбоев
        self.slow_create_once = False  # страница создаётся, но ответ задерживается (тайм-аут клиента)
        self.flood_once = False
        self.fail_author: str | None = None  # для этого автора createPage возвращает ошибку
        self.delay = 0.0

    def _page_dict(self, path: str) -> dict:
        p = self.pages[path]
        return {**p, "views": self.views.get(path, 0)}

    async def handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        path = request.match_info.get("path", "")
        form = dict(await request.post())
        self.calls.append(method)
        if method == "createAccount":
            token = f"tok{len(self.accounts) + 1:04d}abcdefgh"
            self.accounts[token] = {"short_name": form.get("short_name"), "author_name": form.get("author_name")}
            return web.json_response({"ok": True, "result": {**self.accounts[token], "access_token": token,
                                                              "auth_url": "https://edit.telegra.ph/auth/x"}})
        if method == "getAccountInfo":
            token = form.get("access_token")
            if token not in self.accounts:
                return web.json_response({"ok": False, "error": "ACCESS_TOKEN_INVALID"})
            count = sum(1 for p in self.pages.values() if p["_token"] == token)
            return web.json_response({"ok": True, "result": {**self.accounts[token], "page_count": count,
                                                              "auth_url": "https://edit.telegra.ph/auth/x"}})
        if method == "createPage":
            if self.flood_once:
                self.flood_once = False
                return web.json_response({"ok": False, "error": "FLOOD_WAIT_1"})
            token = form.get("access_token")
            if token not in self.accounts:
                return web.json_response({"ok": False, "error": "ACCESS_TOKEN_INVALID"})
            if self.fail_author and form.get("author_name") == self.fail_author:
                return web.json_response({"ok": False, "error": "PAGE_SAVE_FAILED"})
            content = json.loads(form["content"])
            assert isinstance(content, list) and content
            base = f"{_translit(form['title'])}-{datetime.now(timezone.utc):%m-%d}"
            new_path, n = base, 1
            while new_path in self.pages:
                n += 1
                new_path = f"{base}-{n}"
            self.pages[new_path] = {
                "path": new_path, "url": f"https://telegra.ph/{new_path}", "title": form["title"],
                "description": "", "author_name": form.get("author_name"), "_token": token, "content": content,
            }
            self.order.insert(0, new_path)
            self.views[new_path] = 0
            if self.slow_create_once:
                self.slow_create_once = False
                await asyncio.sleep(3)  # клиент с тайм-аутом 1 с не дождётся ответа
            return web.json_response({"ok": True, "result": self._public(new_path)})
        if method == "editPage":
            if path not in self.pages:
                return web.json_response({"ok": False, "error": "PAGE_NOT_FOUND"})
            page = self.pages[path]
            if page["_token"] != form.get("access_token"):
                return web.json_response({"ok": False, "error": "PAGE_ACCESS_DENIED"})
            page["title"] = form["title"]
            page["author_name"] = form.get("author_name")
            page["content"] = json.loads(form["content"])
            return web.json_response({"ok": True, "result": self._public(path)})
        if method == "getPage":
            if path not in self.pages:
                return web.json_response({"ok": False, "error": "PAGE_NOT_FOUND"})
            result = self._public(path)
            if form.get("return_content") == "true":
                result["content"] = self.pages[path]["content"]
            return web.json_response({"ok": True, "result": result})
        if method == "getPageList":
            token = form.get("access_token")
            mine = [p for p in self.order if self.pages[p]["_token"] == token]
            offset, limit = int(form.get("offset", 0)), int(form.get("limit", 50))
            return web.json_response({"ok": True, "result": {
                "total_count": len(mine), "pages": [self._public(p) for p in mine[offset : offset + limit]]}})
        if method == "getViews":
            if path not in self.pages:
                return web.json_response({"ok": False, "error": "PAGE_NOT_FOUND"})
            if "day" in form:
                key = (path, int(form["year"]), int(form["month"]), int(form["day"]))
                return web.json_response({"ok": True, "result": {"views": self.daily_views.get(key, 0)}})
            if "year" in form:
                year = int(form["year"])
                default = self.views.get(path, 0) if year == datetime.now(timezone.utc).year else 0
                return web.json_response({"ok": True, "result": {"views": self.year_views.get((path, year), default)}})
            return web.json_response({"ok": True, "result": {"views": self.views.get(path, 0)}})
        return web.json_response({"ok": False, "error": "METHOD_NOT_FOUND"})

    def add_page(self, path: str, title: str, author: str | None, views: int = 0, token: str = "foreign") -> str:
        """Страница, созданная «не через бота» (вручную или другим аккаунтом)."""
        self.accounts.setdefault(token, {"short_name": token, "author_name": None})
        self.pages[path] = {
            "path": path, "url": f"https://telegra.ph/{path}", "title": title, "description": "",
            "author_name": author, "_token": token, "content": [{"tag": "p", "children": ["Исходный текст"]}],
        }
        self.order.insert(0, path)
        self.views[path] = views
        return f"https://telegra.ph/{path}"

    def _public(self, path: str) -> dict:
        return {k: v for k, v in self._page_dict(path).items() if not k.startswith("_") and k != "content"}


@pytest_asyncio.fixture
async def mock_telegraph():
    mock = MockTelegraph()
    web_app = web.Application()
    web_app.router.add_post("/{method}/{path:.+}", mock.handle)
    web_app.router.add_post("/{method}", mock.handle)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    mock.url = f"http://127.0.0.1:{port}"
    yield mock
    await runner.cleanup()


@pytest_asyncio.fixture
async def app(tmp_path, mock_telegraph, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TELEGRAPH_API_URL", mock_telegraph.url)
    monkeypatch.setenv("TELEGRAPH_TIMEOUT", "3")
    monkeypatch.setenv("TELEGRAPH_MIN_INTERVAL", "0")
    monkeypatch.setenv("TELEGRAPH_TOKEN", "")
    import config as config_module
    from bot import build_app

    cfg = config_module.load_config()
    # тайм-аут меньше задержки mock-сервера, чтобы проверить «неопределённый» createPage
    object.__setattr__(cfg, "telegraph_timeout", 1.0)
    application = await build_app(cfg)
    application.telegraph.timeout = 1.0
    await application.telegraph.close()
    await application.telegraph.start()
    await application.settings.set("creation_delay_seconds", 0)
    await application.settings.set("fetch_youtube_titles", False)  # без обращений к YouTube в тестах
    await application.accounts.ensure_account()
    yield application
    await application.shutdown_tasks()
    await application.telegraph.close()
    if application.http:
        await application.http.close()
    await application.engine.dispose()
