"""Аналитика просмотров.

Источники данных (различаются явно):
* ФАКТ (Telegraph API getViews без даты) — общий счётчик просмотров страницы;
* ФАКТ (getViews с year/month/day) — просмотры за календарные сутки (таблица daily_views);
* РАСЧЁТ — прирост за скользящие окна 24 ч / 7 дн. / 30 дн. по сохранённым снимкам счётчика.

Telegraph API не умеет отдавать «последние 24 часа» или произвольный интервал, поэтому
скользящие окна считаются по снимкам: прирост = сумма положительных изменений счётчика
между снимком на начало окна и последним снимком. Уменьшения счётчика (корректировки
Telegraph) не вычитаются, а учитываются отдельно (drops) и показываются с пояснением.

Качество расчёта (Growth.quality):
* exact    — есть снимок на начало окна (погрешность не больше интервала сбора);
* creation — публикация создана внутри окна, база = 0 просмотров в момент создания;
* approx   — ближайший снимок до начала окна заметно раньше (бот был выключен): окно шире;
* partial  — снимков на начало окна нет, данные только за часть периода;
* nodata   — снимков ещё нет;
* future   — публикации в этот период ещё не существовало.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database import Author, DailyView, Publication, ViewSnapshot
from services.settings_store import SettingsStore

logger = logging.getLogger(__name__)

HOUR = 3600
DAY = 86400
PERIODS = {"24h": DAY, "7d": 7 * DAY, "30d": 30 * DAY}
PERIOD_LABELS = {"24h": "24 часа", "7d": "7 дней", "30d": "30 дней", "all": "всё время"}

USABLE = {"exact", "creation", "approx"}

_WINDOW_SQL = text(
    """
    WITH fs AS (
        SELECT publication_id AS pid, MIN(taken_at) AS t FROM view_snapshots GROUP BY publication_id
    ),
    bs AS (
        SELECT publication_id AS pid, MAX(taken_at) AS t FROM view_snapshots
        WHERE taken_at <= :start GROUP BY publication_id
    ),
    be AS (
        SELECT publication_id AS pid, MAX(taken_at) AS t FROM view_snapshots
        WHERE taken_at <= :end GROUP BY publication_id
    ),
    w AS (
        SELECT fs.pid AS pid, COALESCE(bs.t, fs.t) AS b0, be.t AS b1,
               CASE WHEN bs.t IS NULL THEN 1 ELSE 0 END AS partial
        FROM fs LEFT JOIN bs ON bs.pid = fs.pid LEFT JOIN be ON be.pid = fs.pid
    )
    SELECT w.pid, w.b0, w.b1, w.partial,
           COALESCE(SUM(CASE WHEN s.delta > 0 THEN s.delta ELSE 0 END), 0) AS g,
           COALESCE(SUM(CASE WHEN s.delta < 0 THEN -s.delta ELSE 0 END), 0) AS d
    FROM w
    LEFT JOIN view_snapshots s
        ON s.publication_id = w.pid AND s.taken_at > w.b0 AND w.b1 IS NOT NULL AND s.taken_at <= w.b1
    GROUP BY w.pid, w.b0, w.b1, w.partial
    """
)


@dataclass(slots=True)
class Growth:
    value: int = 0
    drops: int = 0
    quality: str = "nodata"
    start: int = 0
    end: int = 0
    baseline: int | None = None
    last: int | None = None

    @property
    def usable(self) -> bool:
        return self.quality in USABLE

    @property
    def span_seconds(self) -> int:
        if self.baseline is None or self.last is None:
            return 0
        return max(0, self.last - self.baseline)

    @property
    def coverage(self) -> float:
        if self.quality in USABLE:
            return 1.0
        if self.quality == "partial" and self.end > self.start:
            return min(1.0, self.span_seconds / (self.end - self.start))
        return 0.0

    @property
    def extra_seconds(self) -> int:
        """На сколько фактическое окно шире запрошенного (для approx)."""
        if self.baseline is None:
            return 0
        return max(0, self.start - self.baseline)


@dataclass(slots=True)
class PubStats:
    id: int
    author_id: int
    author_name: str
    title: str
    url: str
    path: str
    created_ts: int
    total: int
    updated_ts: int | None
    is_favorite: bool
    youtube_video_id: int | None
    label: str | None
    batch_id: int | None
    sync_error: str | None
    kind: str = "created"
    competitor_id: int | None = None
    tracked_ts: int | None = None
    g24: Growth = field(default_factory=Growth)
    p24: Growth = field(default_factory=Growth)
    g7: Growth = field(default_factory=Growth)
    p7: Growth = field(default_factory=Growth)
    g30: Growth = field(default_factory=Growth)
    status: str = "unknown"
    reference: float | None = None  # «обычная» скорость, просмотров в сутки
    accel: float | None = None  # g24 / reference
    velocity: float = 0.0  # просмотров в час за последние 24 ч
    trend_score: float = 0.0
    promise_score: float = 0.0
    computed_at: int = 0
    # Факт getViews по календарным суткам: сумма и число загруженных/ожидаемых дней за 7 и 30 дней
    day7: int = 0
    day7_n: int = 0
    day7_exp: int = 0
    day30: int = 0
    day30_n: int = 0
    day30_exp: int = 0

    @property
    def age_hours(self) -> float:
        return max(0.0, (self.computed_at - self.created_ts) / HOUR)

    @property
    def is_competitor(self) -> bool:
        return self.kind == "competitor"

    def growth(self, period: str) -> Growth:
        return {"24h": self.g24, "7d": self.g7, "30d": self.g30}[period]


@dataclass(slots=True)
class AuthorStats:
    id: int
    name: str
    count: int = 0
    total: int = 0
    g24: int = 0
    g7: int = 0
    g30: int = 0
    approx24: bool = False
    approx7: bool = False
    approx30: bool = False
    last_created_ts: int = 0

    @property
    def avg(self) -> float:
        return self.total / self.count if self.count else 0.0


@dataclass(slots=True)
class Overview:
    pubs: int = 0
    authors: int = 0
    authors_all: int = 0
    total: int = 0
    g24: int = 0
    g7: int = 0
    g30: int = 0
    not_exact24: int = 0
    not_exact7: int = 0
    not_exact30: int = 0
    growing: int = 0
    stable: int = 0
    declining: int = 0
    new: int = 0
    snapshots: int = 0
    first_snapshot_ts: int | None = None
    last_update_ts: int | None = None
    errors: int = 0


@dataclass(slots=True)
class DailySummary:
    today: int | None = None
    yesterday: int | None = None
    sum7: int = 0
    loaded7: int = 0
    expected7: int = 0
    sum30: int = 0
    loaded30: int = 0
    expected30: int = 0
    days: list[tuple[str, int, bool]] = field(default_factory=list)


SORT_LABELS = {
    "new": "по дате (новые)",
    "old": "по дате (старые)",
    "v": "по просмотрам",
    "d1": "по приросту за 24 ч",
    "d7": "по приросту за 7 дн.",
    "d30": "по приросту за 30 дн.",
    "au": "по автору",
    "fast": "⚡ по скорости набора",
    "prom": "🚀 перспективные",
    "trend": "🔥 набирают популярность",
    "low": "⚠️ низкая активность",
}
AUTHOR_SORT_LABELS = {
    "v": "по общему числу просмотров",
    "d1": "по приросту за 24 ч",
    "d7": "по приросту за 7 дн.",
    "d30": "по приросту за 30 дн.",
    "n": "по количеству публикаций",
}


def period_value(st: PubStats, period: str) -> tuple[int | None, bool]:
    """(значение, неточное ли) для короткой строки «24ч / 7д / 30д».

    Основа — прирост по снимкам. Если снимков за период ещё мало (публикация только добавлена),
    для 7 и 30 дней берётся факт getViews по календарным суткам — он доступен сразу.
    """
    g = st.growth(period)
    if g.usable:
        return g.value, g.quality == "approx"
    if g.quality == "future":
        return None, False
    if period in {"7d", "30d"}:
        total, loaded, expected = ((st.day7, st.day7_n, st.day7_exp) if period == "7d"
                                   else (st.day30, st.day30_n, st.day30_exp))
        if loaded:
            return max(total, g.value if g.quality == "partial" else 0), loaded < expected
    if g.quality == "nodata":
        return None, False
    return g.value, True


def sort_stats(items: list[PubStats], sort: str) -> list[PubStats]:
    if sort == "old":
        return sorted(items, key=lambda s: (s.created_ts, s.id))
    if sort == "v":
        return sorted(items, key=lambda s: (s.total, s.id), reverse=True)
    if sort in {"d1", "d7", "d30"}:
        period = {"d1": "24h", "d7": "7d", "d30": "30d"}[sort]
        return sorted(items, key=lambda s: (period_value(s, period)[0] or 0, s.total), reverse=True)
    if sort == "au":
        return sorted(items, key=lambda s: (s.author_name.casefold(), -s.created_ts))
    if sort == "fast":
        return sorted([s for s in items if s.velocity > 0], key=lambda s: s.velocity, reverse=True)
    if sort == "prom":
        return sorted([s for s in items if s.promise_score > 0], key=lambda s: s.promise_score, reverse=True)
    if sort == "trend":
        return sorted([s for s in items if s.trend_score > 0], key=lambda s: s.trend_score, reverse=True)
    if sort == "low":
        low = [s for s in items if s.status == "declining"]
        return sorted(low, key=lambda s: (s.accel if s.accel is not None else 0.0, s.g24.value))
    return sorted(items, key=lambda s: (s.created_ts, s.id), reverse=True)


def sort_authors(items: list[AuthorStats], sort: str) -> list[AuthorStats]:
    keys = {
        "v": lambda a: (a.total, a.count),
        "d1": lambda a: (a.g24, a.total),
        "d7": lambda a: (a.g7, a.total),
        "d30": lambda a: (a.g30, a.total),
        "n": lambda a: (a.count, a.total),
    }
    return sorted(items, key=keys.get(sort, keys["v"]), reverse=True)


def author_metric(a: AuthorStats, sort: str) -> int:
    return {"v": a.total, "d1": a.g24, "d7": a.g7, "d30": a.g30, "n": a.count}.get(sort, a.total)


class Analytics:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], settings: SettingsStore, ttl: int = 300):
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._ttl = ttl
        self._cache: dict[int, PubStats] | None = None
        self._cache_at = 0.0
        self._cache_version = -1
        self._version = 0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._version += 1

    async def _get_cached(self) -> tuple[dict[int, PubStats], dict[int, PubStats]]:
        """(свои публикации, публикации конкурентов)."""
        version = self._version
        if self._cache is not None and self._cache_version == version and time.time() - self._cache_at < self._ttl:
            return self._cache
        async with self._lock:
            if self._cache is not None and self._cache_version == self._version and time.time() - self._cache_at < self._ttl:
                return self._cache
            version = self._version
            data = await self._compute()
            own = {k: v for k, v in data.items() if not v.is_competitor}
            comp = {k: v for k, v in data.items() if v.is_competitor}
            self._cache = (own, comp)
            self._cache_version = version
            self._cache_at = time.time()
            return self._cache

    async def get_all(self) -> dict[int, PubStats]:
        """Только СВОИ публикации (созданные ботом и добавленные готовые). Конкуренты — get_competitors()."""
        return (await self._get_cached())[0]

    async def get_competitors(self) -> dict[int, PubStats]:
        return (await self._get_cached())[1]

    async def get(self, pub_id: int) -> PubStats | None:
        own, comp = await self._get_cached()
        return own.get(pub_id) or comp.get(pub_id)

    # ------------------------------------------------------------------ расчёт

    def tolerance(self) -> int:
        interval = int(self._settings["snapshot_interval_minutes"]) * 60
        return int(interval * 1.5) + 300

    async def _window(self, session: AsyncSession, start: int, end: int) -> dict[int, tuple]:
        rows = (await session.execute(_WINDOW_SQL, {"start": start, "end": end})).all()
        return {int(r[0]): (r[1], r[2], bool(r[3]), int(r[4] or 0), int(r[5] or 0)) for r in rows}

    @staticmethod
    def _growth(row: tuple | None, start: int, end: int, is_now: bool, created_ts: int, tol: int,
                tracked: bool = False) -> Growth:
        if created_ts > end:
            return Growth(quality="future", start=start, end=end)
        if row is None or row[1] is None:
            return Growth(quality="nodata", start=start, end=end)
        b0, b1, partial, g, d = row
        b0, b1 = int(b0), int(b1)
        if partial:
            # для добавленных готовых/чужих страниц история начинается с момента добавления,
            # а не с нуля при создании — поэтому «с момента создания» к ним не применяется
            quality = ("creation" if not tracked and created_ts >= start and b0 <= created_ts + tol
                       else "partial")
        else:
            quality = "exact" if start - b0 <= tol else "approx"
        if not is_now and quality == "exact" and end - b1 > tol:
            quality = "approx"
        return Growth(value=g, drops=d, quality=quality, start=start, end=end, baseline=b0, last=b1)

    async def _compute(self) -> dict[int, PubStats]:
        started = time.perf_counter()
        now = int(time.time())
        tol = self.tolerance()
        # Для окон «до текущего момента» верхняя граница с запасом: время снимков строго
        # возрастает и может опережать часы на 1–2 секунды.
        end_now = now + 120
        windows = {
            "g24": (now - DAY, end_now, True),
            "p24": (now - 2 * DAY, now - DAY, False),
            "g7": (now - 7 * DAY, end_now, True),
            "p7": (now - 14 * DAY, now - 7 * DAY, False),
            "g30": (now - 30 * DAY, end_now, True),
        }
        async with self._sessionmaker() as session:
            pubs = (
                await session.execute(
                    select(
                        Publication.id, Publication.author_id, Publication.author_name, Publication.title,
                        Publication.url, Publication.path, Publication.created_at, Publication.views,
                        Publication.views_updated_at, Publication.is_favorite, Publication.youtube_video_id,
                        Publication.label, Publication.batch_id, Publication.sync_error,
                        Publication.kind, Publication.competitor_id, Publication.tracked_since,
                    )
                )
            ).all()
            raw = {key: await self._window(session, s, e) for key, (s, e, _) in windows.items()}
            today = datetime.now(timezone.utc).date()
            d7_from, d30_from = (today - timedelta(days=6)).isoformat(), (today - timedelta(days=29)).isoformat()
            daily = {
                r[0]: (int(r[1] or 0), int(r[2] or 0), int(r[3] or 0), int(r[4] or 0))
                for r in (await session.execute(
                    select(
                        DailyView.publication_id,
                        func.sum(case((DailyView.day >= d7_from, DailyView.views), else_=0)),
                        func.sum(case((DailyView.day >= d7_from, 1), else_=0)),
                        func.sum(DailyView.views),
                        func.count(),
                    ).where(DailyView.day >= d30_from).group_by(DailyView.publication_id)
                )).all()
            }

        result: dict[int, PubStats] = {}
        for p in pubs:
            created_ts = int(p.created_at.timestamp())
            st = PubStats(
                id=p.id, author_id=p.author_id, author_name=p.author_name, title=p.title, url=p.url,
                path=p.path, created_ts=created_ts, total=int(p.views or 0),
                updated_ts=int(p.views_updated_at.timestamp()) if p.views_updated_at else None,
                is_favorite=bool(p.is_favorite), youtube_video_id=p.youtube_video_id, label=p.label,
                batch_id=p.batch_id, sync_error=p.sync_error, computed_at=now,
                kind=p.kind or "created", competitor_id=p.competitor_id,
                tracked_ts=int(p.tracked_since.timestamp()) if p.tracked_since else None,
            )
            for key, (s, e, is_now) in windows.items():
                setattr(st, key, self._growth(raw[key].get(p.id), s, e, is_now, created_ts, tol,
                                              tracked=st.tracked_ts is not None))
            if p.id in daily:
                st.day7, st.day7_n, st.day30, st.day30_n = daily[p.id]
            created_day = datetime.fromtimestamp(created_ts, tz=timezone.utc).date()
            age_days = (today - created_day).days + 1
            st.day7_exp, st.day30_exp = max(1, min(7, age_days)), max(1, min(30, age_days))
            self._classify(st, now)
            result[p.id] = st
        logger.debug("Аналитика пересчитана за %.3f с (%s публикаций)", time.perf_counter() - started, len(result))
        return result

    def _classify(self, st: PubStats, now: int) -> None:
        s = self._settings
        min_age = float(s["status_min_age_hours"])
        min_views = float(s["status_min_views"])
        growth_ratio = float(s["status_growth_ratio"])
        decline_ratio = float(s["status_decline_ratio"])
        trending_min = float(s["trending_min_views_24h"])

        span_h = st.g24.span_seconds / HOUR
        if st.g24.quality in USABLE or st.g24.quality == "partial":
            st.velocity = st.g24.value / max(span_h, 1.0) if st.g24.value > 0 else 0.0

        if st.g24.quality == "nodata":
            st.status = "unknown"
        elif st.age_hours < min_age or not (st.g24.usable and st.p24.usable):
            st.status = "new"
        else:
            g24 = st.g24.value
            reference = float(st.p24.value)
            if st.g7.usable:
                prev_start = max(st.created_ts, now - 7 * DAY)
                prev_days = (now - DAY - prev_start) / DAY
                if prev_days >= 1:
                    avg_prev = max(0, st.g7.value - g24) / prev_days
                    reference = (reference + avg_prev) / 2
            st.reference = reference
            st.accel = g24 / reference if reference > 0 else None
            if g24 >= min_views and (reference <= 0 or g24 >= reference * growth_ratio):
                st.status = "growing"
            elif reference >= min_views and g24 <= reference * decline_ratio:
                st.status = "declining"
            else:
                st.status = "stable"

        # 🔥 набирают популярность: учитывается и объём, и ускорение относительно обычной скорости
        g24 = st.g24.value
        if g24 >= trending_min:
            if st.status == "growing":
                factor = min(st.accel, 10.0) if st.accel is not None else 3.0
                st.trend_score = g24 * math.sqrt(factor)
            elif st.status == "new" and st.g24.quality == "creation":
                st.trend_score = g24 * math.sqrt(1.5)

        # 🚀 перспективные: устойчивый рост за неделю по сравнению с предыдущей неделей
        if st.g7.usable and st.g7.value >= max(min_views * 3, trending_min) and st.status != "declining":
            if st.p7.usable and st.p7.value > 0:
                ratio = st.g7.value / st.p7.value
                if ratio >= 1.0:
                    st.promise_score = st.g7.value * math.sqrt(min(ratio, 10.0))
            elif st.p7.quality in {"future", "creation"} or st.g7.quality == "creation":
                st.promise_score = st.g7.value * math.sqrt(1.5)

    # ------------------------------------------------------------------ агрегаты

    async def authors(self) -> list[AuthorStats]:
        stats = await self.get_all()
        async with self._sessionmaker() as session:
            authors = (await session.execute(select(Author.id, Author.name))).all()
        result: dict[int, AuthorStats] = {a.id: AuthorStats(id=a.id, name=a.name) for a in authors}
        for st in stats.values():
            a = result.setdefault(st.author_id, AuthorStats(id=st.author_id, name=st.author_name))
            a.count += 1
            a.total += st.total
            a.g24 += st.g24.value
            a.g7 += st.g7.value
            a.g30 += st.g30.value
            a.approx24 |= st.g24.quality not in {"exact", "creation", "future"}
            a.approx7 |= st.g7.quality not in {"exact", "creation", "future"}
            a.approx30 |= st.g30.quality not in {"exact", "creation", "future"}
            a.last_created_ts = max(a.last_created_ts, st.created_ts)
        return list(result.values())

    async def author(self, author_id: int) -> AuthorStats | None:
        for a in await self.authors():
            if a.id == author_id:
                return a
        return None

    async def overview(self) -> Overview:
        stats = await self.get_all()
        ov = Overview()
        ov.pubs = len(stats)
        ov.authors = len({s.author_id for s in stats.values()})
        for s in stats.values():
            ov.total += s.total
            ov.g24 += s.g24.value
            ov.g7 += s.g7.value
            ov.g30 += s.g30.value
            ov.not_exact24 += s.g24.quality not in {"exact", "creation", "future"}
            ov.not_exact7 += s.g7.quality not in {"exact", "creation", "future"}
            ov.not_exact30 += s.g30.quality not in {"exact", "creation", "future"}
            ov.errors += bool(s.sync_error)
            if s.status == "growing":
                ov.growing += 1
            elif s.status == "stable":
                ov.stable += 1
            elif s.status == "declining":
                ov.declining += 1
            else:
                ov.new += 1
            if s.updated_ts and (ov.last_update_ts is None or s.updated_ts > ov.last_update_ts):
                ov.last_update_ts = s.updated_ts
        async with self._sessionmaker() as session:
            # авторы, у которых есть только чужие (конкурентные) публикации, не считаются
            competitor_only = (
                select(Publication.author_id).group_by(Publication.author_id)
                .having(func.sum(case((Publication.kind == "competitor", 0), else_=1)) == 0)
            )
            ov.authors_all = int((await session.execute(
                select(func.count(Author.id)).where(Author.id.not_in(competitor_only)))).scalar() or 0)
            row = (await session.execute(
                select(func.count(ViewSnapshot.id), func.min(ViewSnapshot.taken_at))
                .join(Publication, Publication.id == ViewSnapshot.publication_id)
                .where(Publication.kind != "competitor")
            )).one()
            ov.snapshots = int(row[0] or 0)
            ov.first_snapshot_ts = int(row[1]) if row[1] is not None else None
        return ov

    async def snapshot_info(self, pub_id: int) -> tuple[int, int | None, int]:
        """(количество снимков, время первого снимка, число зафиксированных уменьшений счётчика)."""
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(
                        func.count(ViewSnapshot.id),
                        func.min(ViewSnapshot.taken_at),
                        func.sum(case((ViewSnapshot.delta < 0, 1), else_=0)),
                    ).where(ViewSnapshot.publication_id == pub_id)
                )
            ).one()
        return int(row[0] or 0), (int(row[1]) if row[1] is not None else None), int(row[2] or 0)

    async def daily_summary(self, pub_id: int, created_ts: int) -> DailySummary:
        today = datetime.now(timezone.utc).date()
        created_day = datetime.fromtimestamp(created_ts, tz=timezone.utc).date()
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(DailyView.day, DailyView.views, DailyView.is_final)
                    .where(DailyView.publication_id == pub_id)
                    .where(DailyView.day >= (today - timedelta(days=29)).isoformat())
                    .order_by(DailyView.day.desc())
                )
            ).all()
        by_day = {r.day: (int(r.views), bool(r.is_final)) for r in rows}
        summary = DailySummary(days=[(r.day, int(r.views), bool(r.is_final)) for r in rows])
        summary.today = by_day.get(today.isoformat(), (None,))[0]
        summary.yesterday = by_day.get((today - timedelta(days=1)).isoformat(), (None,))[0]
        for n, attr in ((7, "7"), (30, "30")):
            expected = loaded = total = 0
            for i in range(n):
                day = today - timedelta(days=i)
                if day < created_day:
                    break
                expected += 1
                if day.isoformat() in by_day:
                    loaded += 1
                    total += by_day[day.isoformat()][0]
            setattr(summary, f"sum{attr}", total)
            setattr(summary, f"loaded{attr}", loaded)
            setattr(summary, f"expected{attr}", expected)
        return summary
