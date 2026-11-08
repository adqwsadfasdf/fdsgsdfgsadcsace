"""Асинхронный клиент официального Telegraph API (https://telegra.ph/api).

Важно: клиент обращается ТОЛЬКО к api.telegra.ph. Сами страницы telegra.ph никогда
не открываются, поэтому проверка статистики не создаёт просмотров.

Поддерживаемые методы: createAccount, getAccountInfo, createPage, editPage, getPage,
getPageList, getViews.

Политика повторов:
* «безопасные» (идемпотентные) запросы повторяются при сетевых сбоях, тайм-аутах и 5xx;
* createPage НЕ повторяется вслепую при тайм-ауте/обрыве после отправки — выбрасывается
  TelegraphAmbiguousError, а вызывающий код сверяется со списком страниц аккаунта
  (getPageList), чтобы не создать дубликат;
* FLOOD_WAIT_N — запрос гарантированно не выполнен, ждём N секунд и повторяем.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

CONTENT_LIMIT_BYTES = 64 * 1024
_FLOOD_RE = re.compile(r"FLOOD_WAIT_(\d+)")


class TelegraphError(Exception):
    """Базовая ошибка Telegraph."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code or message


class TelegraphAPIError(TelegraphError):
    """API вернул ok=false (ошибка в запросе: неверный токен, страница не найдена и т.п.)."""


class TelegraphFloodError(TelegraphAPIError):
    def __init__(self, retry_after: int):
        super().__init__(f"FLOOD_WAIT_{retry_after}", "FLOOD_WAIT")
        self.retry_after = retry_after


class TelegraphTransientError(TelegraphError):
    """Временный сбой (сеть, тайм-аут, 5xx) — повторы исчерпаны."""


class TelegraphAmbiguousError(TelegraphError):
    """Неизвестно, выполнен ли неидемпотентный запрос (например, тайм-аут после отправки createPage)."""


FATAL_TOKEN_ERRORS = {"ACCESS_TOKEN_INVALID", "ACCESS_TOKEN_REQUIRED"}

ERROR_TEXTS = {
    "ACCESS_TOKEN_INVALID": "недействительный токен Telegraph",
    "ACCESS_TOKEN_REQUIRED": "не указан токен Telegraph",
    "PAGE_NOT_FOUND": "страница не найдена в Telegraph",
    "CONTENT_TOO_BIG": "текст слишком большой (лимит Telegraph — 64 КБ)",
    "TITLE_REQUIRED": "не указан заголовок",
    "TITLE_TOO_LONG": "слишком длинный заголовок",
    "AUTHOR_NAME_TOO_LONG": "слишком длинное имя автора",
    "CONTENT_REQUIRED": "пустой текст",
    "CONTENT_TEXT_REQUIRED": "пустой текст",
    "PAGE_ACCESS_DENIED": "страница принадлежит другому аккаунту Telegraph",
    "PAGE_SAVE_FAILED": "Telegraph не смог сохранить страницу",
    "SHORT_NAME_REQUIRED": "не указано короткое имя аккаунта",
}


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, TelegraphFloodError):
        return f"Telegraph ограничил частоту запросов (ожидание {exc.retry_after} с)"
    if isinstance(exc, TelegraphAPIError):
        return ERROR_TEXTS.get(exc.code, exc.code)
    if isinstance(exc, TelegraphAmbiguousError):
        return "нет ответа от Telegraph (результат неизвестен)"
    if isinstance(exc, TelegraphTransientError):
        return f"временный сбой Telegraph: {exc}"
    return f"{type(exc).__name__}: {exc}"


@dataclass(slots=True)
class Page:
    path: str
    url: str
    title: str
    description: str = ""
    author_name: str | None = None
    author_url: str | None = None
    views: int = 0
    can_edit: bool | None = None
    content: list[Any] | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Page":
        if not isinstance(data, dict) or not data.get("path") or not data.get("url"):
            raise TelegraphAPIError("Некорректный ответ Telegraph: нет path/url", "BAD_RESPONSE")
        return cls(
            path=str(data["path"]),
            url=str(data["url"]),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            author_name=data.get("author_name"),
            author_url=data.get("author_url"),
            views=int(data.get("views", 0) or 0),
            can_edit=data.get("can_edit"),
            content=data.get("content"),
        )


@dataclass(slots=True)
class Account:
    short_name: str
    author_name: str | None = None
    author_url: str | None = None
    access_token: str | None = None
    auth_url: str | None = None
    page_count: int | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Account":
        return cls(
            short_name=str(data.get("short_name", "")),
            author_name=data.get("author_name"),
            author_url=data.get("author_url"),
            access_token=data.get("access_token"),
            auth_url=data.get("auth_url"),
            page_count=data.get("page_count"),
        )


@dataclass(slots=True)
class PageList:
    total_count: int
    pages: list[Page] = field(default_factory=list)


class RateLimiter:
    """Минимальный интервал между запросами + глобальная пауза при FLOOD_WAIT."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._next_at = 0.0
        self._blocked_until = 0.0

    async def wait(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            target = max(self._next_at, self._blocked_until)
            now = loop.time()
            if target > now:
                await asyncio.sleep(target - now)
            self._next_at = loop.time() + self.min_interval

    def block(self, seconds: float) -> None:
        loop = asyncio.get_running_loop()
        self._blocked_until = max(self._blocked_until, loop.time() + seconds)


def content_size(nodes: list[Any]) -> int:
    return len(json.dumps(nodes, ensure_ascii=False).encode("utf-8"))


class TelegraphClient:
    def __init__(
        self,
        api_url: str = "https://api.telegra.ph",
        timeout: float = 20.0,
        min_interval: float = 0.35,
        max_retries: int = 4,
        concurrency: int = 3,
        max_flood_wait: int = 600,
    ):
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_flood_wait = max_flood_wait
        self.limiter = RateLimiter(min_interval)
        self._semaphore = asyncio.Semaphore(concurrency)
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={"User-Agent": "TelegraphStatsBot/1.0 (+aiohttp)"},
            )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------ транспорт

    @staticmethod
    def _encode(params: dict[str, Any]) -> dict[str, str]:
        form: dict[str, str] = {}
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, bool):
                form[key] = "true" if value else "false"
            elif isinstance(value, (list, dict)):
                form[key] = json.dumps(value, ensure_ascii=False)
            else:
                form[key] = str(value)
        return form

    async def _request(self, method: str, params: dict[str, Any], *, safe_to_retry: bool) -> Any:
        await self.start()
        assert self._session is not None
        form = self._encode(params)
        url = f"{self.api_url}/{method}"
        attempt = 0
        while True:
            attempt += 1
            await self.limiter.wait()
            sent = False
            try:
                async with self._semaphore:
                    async with self._session.post(url, data=form) as resp:
                        sent = True
                        if resp.status == 429:
                            retry_after = int(resp.headers.get("Retry-After", "5") or 5)
                            raise TelegraphFloodError(retry_after)
                        if resp.status >= 500:
                            raise _ServerError(f"HTTP {resp.status}")
                        try:
                            data = await resp.json(content_type=None)
                        except (aiohttp.ContentTypeError, ValueError) as exc:
                            raise _ServerError(f"некорректный JSON (HTTP {resp.status})") from exc
            except TelegraphFloodError as exc:
                if await self._handle_flood(exc.retry_after, attempt, method):
                    continue
                raise
            except aiohttp.ClientConnectorError as exc:
                # Соединение не установлено — запрос точно не был отправлен.
                if attempt <= self.max_retries:
                    await self._backoff(attempt, method, exc)
                    continue
                raise TelegraphTransientError(f"нет соединения с Telegraph: {exc}") from exc
            except (asyncio.TimeoutError, aiohttp.ClientError, _ServerError) as exc:
                if not safe_to_retry:
                    raise TelegraphAmbiguousError(f"{method}: {type(exc).__name__}: {exc}") from exc
                if attempt <= self.max_retries:
                    await self._backoff(attempt, method, exc)
                    continue
                raise TelegraphTransientError(f"{method}: {type(exc).__name__}: {exc}") from exc

            if not isinstance(data, dict):
                raise TelegraphAPIError("Некорректный ответ Telegraph", "BAD_RESPONSE")
            if data.get("ok"):
                return data.get("result")
            error = str(data.get("error") or "UNKNOWN_ERROR")
            flood = _FLOOD_RE.search(error)
            if flood:
                if await self._handle_flood(int(flood.group(1)), attempt, method):
                    continue
                raise TelegraphFloodError(int(flood.group(1)))
            raise TelegraphAPIError(f"{method}: {error}", error)

    async def _handle_flood(self, seconds: int, attempt: int, method: str) -> bool:
        seconds = max(1, seconds)
        self.limiter.block(seconds + 0.5)
        if seconds > self.max_flood_wait or attempt > self.max_retries + 3:
            return False
        logger.warning("Telegraph FLOOD_WAIT %s с для %s, ждём", seconds, method)
        await asyncio.sleep(seconds + 0.5)
        return True

    async def _backoff(self, attempt: int, method: str, exc: BaseException) -> None:
        delay = min(30.0, 1.5 * (2 ** (attempt - 1))) + random.uniform(0, 0.5)
        logger.warning("Telegraph %s: %s — повтор %s через %.1f с", method, exc, attempt, delay)
        await asyncio.sleep(delay)

    # ------------------------------------------------------------------ методы API

    async def create_account(
        self, short_name: str, author_name: str | None = None, author_url: str | None = None
    ) -> Account:
        # Повторное создание аккаунта при сбое безвредно (лишний аккаунт без страниц).
        result = await self._request(
            "createAccount",
            {"short_name": short_name, "author_name": author_name, "author_url": author_url},
            safe_to_retry=True,
        )
        account = Account.from_api(result)
        if not account.access_token:
            raise TelegraphAPIError("Telegraph не вернул access_token", "BAD_RESPONSE")
        return account

    async def get_account_info(self, access_token: str, fields: list[str] | None = None) -> Account:
        fields = fields or ["short_name", "author_name", "author_url", "auth_url", "page_count"]
        result = await self._request(
            "getAccountInfo", {"access_token": access_token, "fields": fields}, safe_to_retry=True
        )
        return Account.from_api(result)

    async def create_page(
        self,
        access_token: str,
        title: str,
        content: list[Any],
        author_name: str | None = None,
        author_url: str | None = None,
    ) -> Page:
        if content_size(content) > CONTENT_LIMIT_BYTES:
            raise TelegraphAPIError("CONTENT_TOO_BIG", "CONTENT_TOO_BIG")
        result = await self._request(
            "createPage",
            {
                "access_token": access_token,
                "title": title,
                "author_name": author_name,
                "author_url": author_url,
                "content": content,
                "return_content": False,
            },
            safe_to_retry=False,
        )
        return Page.from_api(result)

    async def edit_page(
        self,
        access_token: str,
        path: str,
        title: str,
        content: list[Any],
        author_name: str | None = None,
        author_url: str | None = None,
    ) -> Page:
        if content_size(content) > CONTENT_LIMIT_BYTES:
            raise TelegraphAPIError("CONTENT_TOO_BIG", "CONTENT_TOO_BIG")
        # editPage идемпотентен: повтор с тем же содержимым даёт тот же результат.
        result = await self._request(
            f"editPage/{path}",
            {
                "access_token": access_token,
                "title": title,
                "author_name": author_name,
                "author_url": author_url,
                "content": content,
                "return_content": False,
            },
            safe_to_retry=True,
        )
        return Page.from_api(result)

    async def get_page(self, path: str, return_content: bool = False) -> Page:
        result = await self._request(
            f"getPage/{path}", {"return_content": return_content}, safe_to_retry=True
        )
        return Page.from_api(result)

    async def get_page_list(self, access_token: str, offset: int = 0, limit: int = 50) -> PageList:
        result = await self._request(
            "getPageList",
            {"access_token": access_token, "offset": offset, "limit": max(1, min(200, limit))},
            safe_to_retry=True,
        )
        pages = [Page.from_api(p) for p in (result or {}).get("pages", [])]
        return PageList(total_count=int((result or {}).get("total_count", 0)), pages=pages)

    async def get_views(
        self,
        path: str,
        year: int | None = None,
        month: int | None = None,
        day: int | None = None,
        hour: int | None = None,
    ) -> int:
        """Счётчик просмотров. Без даты — всего; с year/month/day[/hour] — за этот день/час.

        Ограничения API: hour требует day, day требует month, month требует year;
        произвольных интервалов («последние 24 часа») API не поддерживает.
        """
        if hour is not None and day is None or day is not None and month is None or month is not None and year is None:
            raise ValueError("getViews: hour требует day, day — month, month — year")
        result = await self._request(
            f"getViews/{path}",
            {"year": year, "month": month, "day": day, "hour": hour},
            safe_to_retry=True,
        )
        if not isinstance(result, dict) or "views" not in result:
            raise TelegraphAPIError("Некорректный ответ getViews", "BAD_RESPONSE")
        return int(result["views"])


class _ServerError(Exception):
    """Внутренний маркер: сервер ответил 5xx или мусором."""
