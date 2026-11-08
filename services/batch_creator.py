"""Массовое создание публикаций Telegraph.

Защита от дублей и восстановление:
1. Партия и все её элементы сохраняются в БД ДО первого запроса к Telegraph.
2. Перед каждым createPage элемент помечается «creating» (фиксируется в БД).
3. Успешный результат и запись публикации сохраняются одной транзакцией.
4. Если ответ не получен (тайм-аут/обрыв) — элемент «uncertain»; бот сверяется со
   списком страниц аккаунта (getPageList) и повторяет createPage только если страницы нет.
5. После перезапуска незавершённые партии помечаются «interrupted», неопределённые
   элементы сверяются через getPageList, пользователь получает отчёт и может продолжить.
6. Глобальная блокировка не даёт запустить две партии одновременно; переход статуса
   партии draft/interrupted/partial -> running атомарен, поэтому повторное нажатие
   кнопки не создаёт вторую копию.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database import Batch, BatchItem, Publication, ViewSnapshot, get_or_create_author, utcnow
from services.accounts import AccountManager
from services.analytics import Analytics
from services.settings_store import SettingsStore
from services.telegraph import (
    FATAL_TOKEN_ERRORS,
    Page,
    TelegraphAmbiguousError,
    TelegraphAPIError,
    TelegraphClient,
    TelegraphError,
    describe_error,
)

logger = logging.getLogger(__name__)

MAX_ITEM_ATTEMPTS = 3
_PATH_DATE_RE = re.compile(r"-(\d{2})-(\d{2})(?:-\d+)?$")
RESUMABLE = ("draft", "interrupted", "partial", "cancelled")

ProgressCallback = Callable[[int, int, "ItemResult"], Awaitable[None]]


class BatchBusyError(RuntimeError):
    """Уже выполняется другая партия."""


class BatchStateError(RuntimeError):
    """Партия уже запущена или завершена."""


@dataclass(slots=True)
class ItemResult:
    position: int
    author: str
    status: str
    url: str | None = None
    publication_id: int | None = None
    error: str | None = None


@dataclass(slots=True)
class BatchReport:
    batch_id: int
    title: str
    status: str
    total: int
    items: list[ItemResult] = field(default_factory=list)
    fatal_error: str | None = None

    @property
    def created(self) -> list[ItemResult]:
        return [i for i in self.items if i.status == "created"]

    @property
    def not_created(self) -> list[ItemResult]:
        return [i for i in self.items if i.status != "created"]


class BatchCreator:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        client: TelegraphClient,
        accounts: AccountManager,
        analytics: Analytics,
        settings: SettingsStore,
        author_url: str | None = None,
    ):
        self._sessionmaker = sessionmaker
        self._client = client
        self._accounts = accounts
        self._analytics = analytics
        self._settings = settings
        self._author_url = author_url
        self._lock = asyncio.Lock()
        self._cancel: set[int] = set()
        self.current_batch: int | None = None

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def request_cancel(self, batch_id: int) -> None:
        self._cancel.add(batch_id)

    # ------------------------------------------------------------------ подготовка

    async def create_draft(self, title: str, content_html: str, content_json: str, authors: list[str]) -> int:
        account = await self._accounts.active()
        async with self._sessionmaker() as session:
            batch = Batch(
                title=title, content_html=content_html, content_json=content_json,
                account_id=account.id, status="draft", total=len(authors),
            )
            session.add(batch)
            await session.flush()
            for pos, name in enumerate(authors, start=1):
                session.add(BatchItem(batch_id=batch.id, position=pos, author_name=name, status="pending"))
            await session.commit()
            logger.info("Создана партия #%s: «%s», авторов: %s", batch.id, title, len(authors))
            return batch.id

    # ------------------------------------------------------------------ выполнение

    async def run(self, batch_id: int, progress: ProgressCallback | None = None) -> BatchReport:
        if self._lock.locked():
            raise BatchBusyError("Уже выполняется создание другой партии")
        async with self._lock:
            async with self._sessionmaker() as session:
                res = await session.execute(
                    update(Batch)
                    .where(Batch.id == batch_id, Batch.status.in_(RESUMABLE))
                    .values(status="running", started_at=utcnow(), last_error=None)
                )
                await session.commit()
                if not res.rowcount:
                    raise BatchStateError("Партия уже выполняется или полностью завершена")
            self.current_batch = batch_id
            self._cancel.discard(batch_id)
            try:
                return await self._run_locked(batch_id, progress)
            except asyncio.CancelledError:
                await self._mark_interrupted(batch_id, "работа бота была остановлена")
                raise
            finally:
                self.current_batch = None
                self._analytics.invalidate()

    async def _run_locked(self, batch_id: int, progress: ProgressCallback | None) -> BatchReport:
        async with self._sessionmaker() as session:
            batch = await session.get(Batch, batch_id)
            assert batch is not None
            items = (
                await session.execute(
                    select(BatchItem).where(BatchItem.batch_id == batch_id).order_by(BatchItem.position)
                )
            ).scalars().all()
        token = await self._accounts.token_for(batch.account_id)
        nodes = json.loads(batch.content_json)
        delay = float(self._settings["creation_delay_seconds"])

        total = len(items)
        done = sum(1 for i in items if i.status == "created")
        fatal: str | None = None
        cancelled = False
        first_request = True

        for item in items:
            if item.status == "created":
                continue
            if batch_id in self._cancel:
                cancelled = True
                break
            if fatal:
                break
            if not first_request and delay > 0:
                await asyncio.sleep(delay)
            first_request = False
            result = await self._process_item(batch, item, token, nodes)
            if result.status == "created":
                done += 1
            elif result.error and result.error.startswith("FATAL:"):
                fatal = result.error.removeprefix("FATAL:").strip()
            if progress is not None:
                try:
                    await progress(done, total, result)
                except Exception:  # noqa: BLE001 — сбой отображения не должен ломать партию
                    logger.exception("Ошибка обновления прогресса")

        async with self._sessionmaker() as session:
            if cancelled:
                await session.execute(
                    update(BatchItem)
                    .where(BatchItem.batch_id == batch_id, BatchItem.status.in_(("pending", "failed")))
                    .values(status="cancelled", error="остановлено пользователем")
                )
            items = (
                await session.execute(
                    select(BatchItem).where(BatchItem.batch_id == batch_id).order_by(BatchItem.position)
                )
            ).scalars().all()
            created = sum(1 for i in items if i.status == "created")
            if created == len(items):
                status = "completed"
            elif cancelled:
                status = "cancelled"
            else:
                status = "partial"
            await session.execute(
                update(Batch).where(Batch.id == batch_id).values(
                    status=status, finished_at=utcnow(), last_error=fatal
                )
            )
            await session.commit()
        self._cancel.discard(batch_id)
        logger.info("Партия #%s завершена: %s, создано %s из %s", batch_id, status, created, total)
        return await self.report(batch_id, fatal_error=fatal)

    async def _process_item(self, batch: Batch, item: BatchItem, token: str, nodes: list) -> ItemResult:
        async with self._sessionmaker() as session:
            db_item = await session.get(BatchItem, item.id)
            assert db_item is not None
            prev_status = db_item.status
            db_item.status = "creating"
            await session.commit()

        # Ранее статус был неопределённым — сначала проверяем, не создана ли страница.
        if prev_status in ("uncertain", "creating"):
            page = await self._find_existing(token, batch.title, item.author_name, ref=item.updated_at)
            if page is not None:
                return await self._record(batch, item, page)

        attempts = item.attempts
        last_error = "неизвестная ошибка"
        while attempts < MAX_ITEM_ATTEMPTS + item.attempts:
            attempts += 1
            await self._set_item(item.id, attempts=attempts)
            try:
                page = await self._client.create_page(
                    token, batch.title, nodes, author_name=item.author_name, author_url=self._author_url
                )
                page = await self._verify(page, batch.title, item.author_name)
                return await self._record(batch, item, page)
            except TelegraphAmbiguousError as exc:
                last_error = describe_error(exc)
                logger.warning("createPage для «%s»: ответ не получен (%s), сверяемся", item.author_name, exc)
                await self._set_item(item.id, status="uncertain", error=last_error)
                await asyncio.sleep(3)
                page = await self._find_existing(token, batch.title, item.author_name)
                if page is not None:
                    return await self._record(batch, item, page)
                await self._set_item(item.id, status="creating")
                continue
            except TelegraphAPIError as exc:
                last_error = describe_error(exc)
                if exc.code in FATAL_TOKEN_ERRORS:
                    await self._set_item(item.id, status="failed", error=last_error)
                    return ItemResult(item.position, item.author_name, "failed", error=f"FATAL: {last_error}")
                break  # ошибка в данных — повтор не поможет
            except TelegraphError as exc:
                last_error = describe_error(exc)
                await asyncio.sleep(2)
                continue
            except Exception as exc:  # noqa: BLE001
                logger.exception("Неожиданная ошибка при создании страницы для «%s»", item.author_name)
                last_error = f"{type(exc).__name__}: {exc}"
                break

        # Последняя сверка: не создана ли страница при последней неопределённой попытке.
        page = await self._find_existing(token, batch.title, item.author_name)
        if page is not None:
            return await self._record(batch, item, page)
        await self._set_item(item.id, status="failed", error=last_error)
        return ItemResult(item.position, item.author_name, "failed", error=last_error)

    async def _verify(self, page: Page, title: str, author: str) -> Page:
        if not page.path or not page.url:
            raise TelegraphAPIError("Telegraph не вернул ссылку на страницу", "BAD_RESPONSE")
        if not self._settings["verify_after_create"]:
            return page
        try:
            checked = await self._client.get_page(page.path, return_content=False)
        except Exception as exc:  # noqa: BLE001 — страница создана, проверка лишь дополнительная
            logger.warning("Проверка getPage %s не удалась: %s", page.path, exc)
            return page
        if checked.title != title or (checked.author_name or "") != author:
            logger.warning("getPage %s: данные отличаются (%r / %r)", page.path, checked.title, checked.author_name)
        return checked

    @staticmethod
    def _path_matches_date(path: str, ref: datetime | None) -> bool:
        """Telegraph добавляет к path дату создания «-ММ-ДД». Отсекаем старые страницы с тем же заголовком."""
        match = _PATH_DATE_RE.search(path)
        if not match:
            return False
        ref = ref or datetime.now(timezone.utc)
        allowed = {((ref + timedelta(days=d)).month, (ref + timedelta(days=d)).day) for d in (-1, 0, 1)}
        return (int(match.group(1)), int(match.group(2))) in allowed

    async def _find_existing(self, token: str, title: str, author: str, ref: datetime | None = None) -> Page | None:
        """Ищет страницу с таким заголовком и автором среди последних страниц аккаунта, ещё не записанную в БД."""
        try:
            async with self._sessionmaker() as session:
                known = set((await session.execute(select(Publication.path))).scalars().all())
            for offset in (0, 100):
                page_list = await self._client.get_page_list(token, offset=offset, limit=100)
                for page in page_list.pages:
                    if page.path in known or not self._path_matches_date(page.path, ref):
                        continue
                    if page.title.strip() == title.strip() and (page.author_name or "").strip() == author.strip():
                        logger.info("Найдена ранее созданная страница %s для «%s»", page.path, author)
                        return page
                if offset + 100 >= page_list.total_count:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось сверить список страниц: %s", exc)
        return None

    async def _record(self, batch: Batch, item: BatchItem, page: Page) -> ItemResult:
        async with self._sessionmaker() as session:
            existing = (
                await session.execute(select(Publication).where(Publication.path == page.path))
            ).scalar_one_or_none()
            if existing is None:
                author = await get_or_create_author(session, item.author_name)
                now = datetime.now(timezone.utc)
                pub = Publication(
                    batch_id=batch.id, account_id=batch.account_id, title=batch.title,
                    author_id=author.id, author_name=item.author_name,
                    content_html=batch.content_html, content_json=batch.content_json,
                    url=page.url, path=page.path, created_at=now,
                    views=int(page.views or 0), views_updated_at=now,
                )
                session.add(pub)
                await session.flush()
                # Нулевая точка отсчёта: новая страница начинает с page.views (обычно 0)
                session.add(ViewSnapshot(publication_id=pub.id, taken_at=int(now.timestamp()),
                                         views=int(page.views or 0), delta=0))
                pub_id = pub.id
            else:
                pub_id = existing.id
            db_item = await session.get(BatchItem, item.id)
            if db_item is not None:
                db_item.status = "created"
                db_item.error = None
                db_item.publication_id = pub_id
            await session.commit()
        self._analytics.invalidate()
        return ItemResult(item.position, item.author_name, "created", url=page.url, publication_id=pub_id)

    async def _set_item(self, item_id: int, **values) -> None:
        async with self._sessionmaker() as session:
            await session.execute(update(BatchItem).where(BatchItem.id == item_id).values(**values))
            await session.commit()

    async def _mark_interrupted(self, batch_id: int, reason: str) -> None:
        try:
            async with self._sessionmaker() as session:
                await session.execute(
                    update(Batch).where(Batch.id == batch_id).values(status="interrupted", last_error=reason)
                )
                await session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось пометить партию #%s прерванной", batch_id)

    # ------------------------------------------------------------------ восстановление

    async def recover_on_startup(self) -> list[BatchReport]:
        """Партии, прерванные аварийной остановкой: сверка неопределённых элементов и отчёт."""
        async with self._sessionmaker() as session:
            batches = (
                await session.execute(select(Batch).where(Batch.status == "running"))
            ).scalars().all()
        reports: list[BatchReport] = []
        for batch in batches:
            logger.warning("Партия #%s была прервана, выполняется восстановление", batch.id)
            token = await self._accounts.token_for(batch.account_id)
            async with self._sessionmaker() as session:
                items = (
                    await session.execute(
                        select(BatchItem).where(
                            BatchItem.batch_id == batch.id,
                            BatchItem.status.in_(("creating", "uncertain")),
                        )
                    )
                ).scalars().all()
            for item in items:
                page = await self._find_existing(token, batch.title, item.author_name, ref=item.updated_at)
                if page is not None:
                    await self._record(batch, item, page)
                else:
                    await self._set_item(item.id, status="pending", error="прервано (страница не была создана)")
            await self._mark_interrupted(batch.id, "бот был перезапущен во время создания")
            reports.append(await self.report(batch.id))
        return reports

    # ------------------------------------------------------------------ отчёты

    async def report(self, batch_id: int, fatal_error: str | None = None) -> BatchReport:
        async with self._sessionmaker() as session:
            batch = await session.get(Batch, batch_id)
            if batch is None:
                raise LookupError("Партия не найдена")
            rows = (
                await session.execute(
                    select(BatchItem, Publication.url)
                    .join(Publication, Publication.id == BatchItem.publication_id, isouter=True)
                    .where(BatchItem.batch_id == batch_id)
                    .order_by(BatchItem.position)
                )
            ).all()
        items = [
            ItemResult(
                position=item.position, author=item.author_name, status=item.status, url=url,
                publication_id=item.publication_id, error=item.error,
            )
            for item, url in rows
        ]
        return BatchReport(
            batch_id=batch.id, title=batch.title, status=batch.status, total=batch.total,
            items=items, fatal_error=fatal_error or batch.last_error,
        )
