"""Планировщик (APScheduler): снимки просмотров, данные по дням, обслуживание базы."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

if TYPE_CHECKING:
    from app_context import App

logger = logging.getLogger(__name__)

JOB_COLLECT = "collect_views"
JOB_DAILY = "sync_daily"
JOB_COMPACT = "compact"


async def collect_job(app: "App") -> None:
    try:
        result = await app.collector.collect_all()
        if result.skipped:
            logger.info("Сбор пропущен: предыдущий ещё выполняется")
            return
        if app.notifier is not None:
            await app.notifier.check()
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка фонового сбора статистики")


async def daily_job(app: "App") -> None:
    try:
        await app.collector.sync_daily()
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка синхронизации данных по дням")


async def compact_job(app: "App") -> None:
    try:
        await app.collector.compact()
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка обслуживания базы")


def setup_scheduler(app: "App") -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=timezone.utc)
    common: dict[str, Any] = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 600, "replace_existing": True}
    now = datetime.now(timezone.utc)
    scheduler.add_job(
        collect_job, IntervalTrigger(minutes=int(app.settings["snapshot_interval_minutes"])),
        args=[app], id=JOB_COLLECT, next_run_time=now + timedelta(seconds=20), **common,
    )
    scheduler.add_job(
        daily_job, IntervalTrigger(hours=int(app.settings["daily_sync_hours"])),
        args=[app], id=JOB_DAILY, next_run_time=now + timedelta(minutes=2), **common,
    )
    scheduler.add_job(compact_job, CronTrigger(hour=3, minute=30), args=[app], id=JOB_COMPACT, **common)

    async def on_setting_change(key: str, value: Any) -> None:
        if key == "snapshot_interval_minutes":
            scheduler.reschedule_job(JOB_COLLECT, trigger=IntervalTrigger(minutes=int(value)))
            logger.info("Интервал сбора статистики изменён: %s мин", value)
        elif key == "daily_sync_hours":
            scheduler.reschedule_job(JOB_DAILY, trigger=IntervalTrigger(hours=int(value)))
            logger.info("Интервал синхронизации по дням изменён: %s ч", value)

    app.settings.on_change(on_setting_change)
    return scheduler
