"""Фоновый сбор статистики через Telegraph API getViews.

Страницы telegra.ph НЕ открываются: используются только запросы к api.telegra.ph,
которые не засчитываются как просмотры.

* collect_all  — снимок общего счётчика каждой страницы (getViews без даты);
* sync_daily   — фактические просмотры за календарные сутки (getViews с year/month/day);
* compact      — прореживание старых снимков (после срока хранения — один снимок в сутки).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database import DailyView, Publication, ViewSnapshot
from services.analytics import DAY, Analytics
from services.settings_store import SettingsStore
from services.telegraph import TelegraphAPIError, TelegraphClient, describe_error

logger = logging.getLogger(__name__)

CHUNK = 40
# Сутки считаются окончательными, если данные получены спустя 14 ч после конца суток UTC
# (граница суток Telegraph не документирована — запас покрывает любые часовые пояса).
FINALIZE_AFTER = 14 * 3600


@dataclass(slots=True)
class CollectResult:
    skipped: bool = False
    total: int = 0
    ok: int = 0
    failed: int = 0
    decreased: int = 0
    duration: float = 0.0
    errors: list[str] = field(default_factory=list)


class CollectorBusy(RuntimeError):
    pass


class StatisticsCollector:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        client: TelegraphClient,
        analytics: Analytics,
        settings: SettingsStore,
    ):
        self._sessionmaker = sessionmaker
        self._client = client
        self._analytics = analytics
        self._settings = settings
        self.lock = asyncio.Lock()
        self.daily_lock = asyncio.Lock()

    # ------------------------------------------------------------------ снимки

    async def collect_all(self) -> CollectResult:
        if self.lock.locked():
            return CollectResult(skipped=True)
        async with self.lock:
            started = time.perf_counter()
            result = CollectResult()
            async with self._sessionmaker() as session:
                pubs = (await session.execute(select(Publication.id, Publication.path))).all()
            result.total = len(pubs)
            for i in range(0, len(pubs), CHUNK):
                chunk = pubs[i : i + CHUNK]
                fetched = await asyncio.gather(*(self._fetch(p.id, p.path) for p in chunk))
                await self._store(fetched, result)
            result.duration = time.perf_counter() - started
            await self._settings.set_state(
                "last_collect",
                {"ts": int(time.time()), "ok": result.ok, "failed": result.failed, "total": result.total,
                 "duration": round(result.duration, 1)},
            )
            self._analytics.invalidate()
            logger.info(
                "Сбор статистики: %s/%s успешно, ошибок %s, уменьшений счётчика %s, %.1f с",
                result.ok, result.total, result.failed, result.decreased, result.duration,
            )
            return result

    async def _fetch(self, pub_id: int, path: str) -> tuple[int, int, int | None, str | None]:
        ts = int(time.time())
        try:
            views = await self._client.get_views(path)
            return pub_id, int(time.time()), views, None
        except TelegraphAPIError as exc:
            return pub_id, ts, None, describe_error(exc)
        except Exception as exc:  # noqa: BLE001 — одна страница не должна ломать весь сбор
            return pub_id, ts, None, describe_error(exc)

    async def _store(self, fetched: list[tuple[int, int, int | None, str | None]], result: CollectResult) -> None:
        async with self._sessionmaker() as session:
            for pub_id, ts, views, error in fetched:
                pub = await session.get(Publication, pub_id)
                if pub is None:  # удалена из базы во время сбора
                    continue
                if views is None:
                    result.failed += 1
                    pub.sync_error = (error or "ошибка")[:250]
                    if len(result.errors) < 10:
                        result.errors.append(f"{pub.path}: {error}")
                    continue
                # время снимков строго возрастает (иначе снимок в ту же секунду, что и предыдущий,
                # выпал бы из окна расчёта)
                if pub.views_updated_at is not None:
                    ts = max(ts, int(pub.views_updated_at.timestamp()) + 1)
                delta = views - int(pub.views or 0)
                if delta < 0:
                    result.decreased += 1
                    logger.warning(
                        "Счётчик просмотров %s уменьшился: %s -> %s (корректировка Telegraph)",
                        pub.path, pub.views, views,
                    )
                session.add(ViewSnapshot(publication_id=pub_id, taken_at=ts, views=views, delta=delta))
                pub.views = views
                pub.views_updated_at = datetime.fromtimestamp(ts, tz=timezone.utc)
                pub.sync_error = None
                result.ok += 1
            await session.commit()

    async def refresh_one(self, pub_id: int) -> tuple[int, int]:
        """Немедленно обновляет счётчик одной публикации. Возвращает (было, стало)."""
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=90)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise CollectorBusy("Идёт сбор статистики, попробуйте через минуту") from exc
        try:
            async with self._sessionmaker() as session:
                pub = await session.get(Publication, pub_id)
                if pub is None:
                    raise LookupError("Публикация не найдена")
                old, path = int(pub.views or 0), pub.path
            fetched = await self._fetch(pub_id, path)
            if fetched[3] is not None:
                raise RuntimeError(fetched[3])
            await self._store([fetched], CollectResult())
            self._analytics.invalidate()
            return old, int(fetched[2] or 0)
        finally:
            self.lock.release()

    async def refresh_many(self, pub_ids: list[int]) -> CollectResult:
        """Внеочередной снимок счётчика для набора публикаций (например, всех конкурентов)."""
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=90)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise CollectorBusy("Идёт сбор статистики, попробуйте через минуту") from exc
        try:
            started = time.perf_counter()
            result = CollectResult()
            async with self._sessionmaker() as session:
                pubs = (await session.execute(
                    select(Publication.id, Publication.path).where(Publication.id.in_(pub_ids)))).all()
            result.total = len(pubs)
            for i in range(0, len(pubs), CHUNK):
                chunk = pubs[i : i + CHUNK]
                fetched = await asyncio.gather(*(self._fetch(p.id, p.path) for p in chunk))
                await self._store(fetched, result)
            result.duration = time.perf_counter() - started
            self._analytics.invalidate()
            return result
        finally:
            self.lock.release()

    # ------------------------------------------------------------------ по дням

    async def sync_daily(self, pub_ids: list[int] | None = None) -> int:
        """Загружает фактические просмотры за календарные сутки (последние 30 дней)."""
        if self.daily_lock.locked():
            return 0
        async with self.daily_lock:
            now = int(time.time())
            today = datetime.now(timezone.utc).date()
            first_day = today - timedelta(days=29)
            async with self._sessionmaker() as session:
                query = select(Publication.id, Publication.path, Publication.created_at)
                if pub_ids:
                    query = query.where(Publication.id.in_(pub_ids))
                pubs = (await session.execute(query)).all()
                finals = {
                    (r.publication_id, r.day)
                    for r in (
                        await session.execute(
                            select(DailyView.publication_id, DailyView.day)
                            .where(DailyView.is_final.is_(True))
                            .where(DailyView.day >= first_day.isoformat())
                        )
                    ).all()
                }
            jobs: list[tuple[int, str, date]] = []
            for p in pubs:
                created_day = p.created_at.astimezone(timezone.utc).date()
                day = max(created_day, first_day)
                while day <= today:
                    if (p.id, day.isoformat()) not in finals:
                        jobs.append((p.id, p.path, day))
                    day += timedelta(days=1)

            stored = 0
            for i in range(0, len(jobs), CHUNK):
                chunk = jobs[i : i + CHUNK]
                values = await asyncio.gather(*(self._fetch_day(path, day) for _, path, day in chunk))
                rows = []
                fetched_at = int(time.time())
                for (pub_id, _, day), views in zip(chunk, values):
                    if views is None:
                        continue
                    day_end = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()) + DAY
                    rows.append({
                        "publication_id": pub_id, "day": day.isoformat(), "views": views,
                        "is_final": fetched_at >= day_end + FINALIZE_AFTER, "fetched_at": fetched_at,
                    })
                if rows:
                    async with self._sessionmaker() as session:
                        existing = {
                            r[0] for r in (await session.execute(select(Publication.id).where(
                                Publication.id.in_({r["publication_id"] for r in rows})))).all()
                        }
                        rows = [r for r in rows if r["publication_id"] in existing]
                        if rows:
                            stmt = sqlite_insert(DailyView).values(rows)
                            stmt = stmt.on_conflict_do_update(
                                index_elements=[DailyView.publication_id, DailyView.day],
                                set_={"views": stmt.excluded.views, "is_final": stmt.excluded.is_final,
                                      "fetched_at": stmt.excluded.fetched_at},
                            )
                            await session.execute(stmt)
                            await session.commit()
                            stored += len(rows)
            await self._settings.set_state("last_daily_sync", {"ts": now, "stored": stored, "jobs": len(jobs)})
            logger.info("Синхронизация по дням: запросов %s, сохранено %s", len(jobs), stored)
            return stored

    async def _fetch_day(self, path: str, day: date) -> int | None:
        try:
            return await self._client.get_views(path, year=day.year, month=day.month, day=day.day)
        except Exception as exc:  # noqa: BLE001
            logger.info("getViews %s за %s не получен: %s", path, day, describe_error(exc))
            return None

    # ------------------------------------------------------------------ обслуживание

    async def compact(self) -> int:
        retention = int(self._settings["snapshot_retention_days"])
        cutoff = int(time.time()) - retention * DAY
        async with self.lock:
            async with self._sessionmaker() as session:
                deleted = await session.execute(
                    text(
                        """
                        DELETE FROM view_snapshots
                        WHERE taken_at < :cutoff AND id NOT IN (
                            SELECT MAX(id) FROM view_snapshots WHERE taken_at < :cutoff
                            GROUP BY publication_id, taken_at / 86400
                        )
                        """
                    ),
                    {"cutoff": cutoff},
                )
                await session.execute(
                    text(
                        """
                        UPDATE view_snapshots SET delta = views - COALESCE((
                            SELECT p.views FROM view_snapshots p
                            WHERE p.publication_id = view_snapshots.publication_id
                              AND (p.taken_at < view_snapshots.taken_at
                                   OR (p.taken_at = view_snapshots.taken_at AND p.id < view_snapshots.id))
                            ORDER BY p.taken_at DESC, p.id DESC LIMIT 1
                        ), views - delta)
                        WHERE taken_at < :until
                        """
                    ),
                    {"until": cutoff + DAY},
                )
                await session.execute(
                    text("DELETE FROM notification_log WHERE sent_at < :old"),
                    {"old": (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")},
                )
                await session.commit()
        self._analytics.invalidate()
        count = deleted.rowcount or 0
        logger.info("Прореживание снимков старше %s дн.: удалено %s", retention, count)
        return count
