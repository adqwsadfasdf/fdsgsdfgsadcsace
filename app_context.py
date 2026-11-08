"""Контейнер зависимостей приложения (передаётся в обработчики как параметр `app`)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Coroutine

import aiohttp
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from config import Config
from services.accounts import AccountManager
from services.analytics import Analytics
from services.batch_creator import BatchCreator
from services.settings_store import SettingsStore
from services.statistics_collector import StatisticsCollector
from services.telegraph import TelegraphClient

if TYPE_CHECKING:
    from aiogram import Bot
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from services.notifications import Notifier

logger = logging.getLogger(__name__)


@dataclass
class App:
    config: Config
    engine: AsyncEngine
    db: async_sessionmaker[AsyncSession]
    settings: SettingsStore
    telegraph: TelegraphClient
    accounts: AccountManager
    analytics: Analytics
    collector: StatisticsCollector
    creator: BatchCreator
    http: aiohttp.ClientSession | None = None
    notifier: "Notifier | None" = None
    bot: "Bot | None" = None
    scheduler: "AsyncIOScheduler | None" = None
    tasks: set[asyncio.Task] = field(default_factory=set)
    create_guard: asyncio.Lock = field(default_factory=asyncio.Lock)
    mass_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def spawn(self, coro: Coroutine[Any, Any, Any], name: str | None = None) -> asyncio.Task:
        """Фоновая задача с логированием ошибок и учётом для корректной остановки."""
        task = asyncio.create_task(coro, name=name)
        self.tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self.tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.error("Фоновая задача %s завершилась с ошибкой", t.get_name(), exc_info=t.exception())

        task.add_done_callback(_done)
        return task

    async def shutdown_tasks(self, timeout: float = 10.0) -> None:
        if not self.tasks:
            return
        for task in list(self.tasks):
            task.cancel()
        await asyncio.wait(list(self.tasks), timeout=timeout)
