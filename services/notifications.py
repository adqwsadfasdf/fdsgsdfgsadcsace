"""Уведомления об изменении динамики просмотров (с защитой от повторов)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, update

from database import NotificationLog, Publication, utcnow
from keyboards.callbacks import AuthorCb, PubCard
from services.analytics import PubStats, author_metric, sort_authors
from services.settings_store import METRIC_LABELS
from utils.formatting import esc, fmt_delta, fmt_int, truncate

if TYPE_CHECKING:
    from app_context import App

logger = logging.getLogger(__name__)

MAX_MESSAGES_PER_RUN = 10


@dataclass(slots=True)
class Notice:
    kind: str
    entity: str
    text: str
    markup: InlineKeyboardMarkup | None = None
    value: int | None = None


def _card_kb(pub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📄 Открыть публикацию", callback_data=PubCard(id=pub_id, per="24h", nav="").pack())
    ]])


def _pub_line(st: PubStats) -> str:
    return f"<b>{esc(truncate(st.title, 60))}</b> — {esc(st.author_name)}"


class Notifier:
    def __init__(self, app: "App"):
        self.app = app

    async def check(self) -> int:
        stats = await self.app.analytics.get_all()
        transitions = await self._update_statuses(stats)
        s = self.app.settings
        cooldown = int(s["notify_cooldown_hours"])
        notices: list[Notice] = []

        if s["notify_daily_enabled"]:
            threshold = int(s["notify_daily_threshold"])
            for st in sorted(stats.values(), key=lambda x: x.g24.value, reverse=True):
                if st.g24.value < threshold or st.g24.quality not in {"exact", "creation", "approx"}:
                    continue
                notices.append(Notice(
                    "daily", str(st.id),
                    f"🎯 Публикация набрала {fmt_int(st.g24.value)} просмотров за сутки "
                    f"(порог {fmt_int(threshold)})\n{_pub_line(st)}\n👁 Всего: {fmt_int(st.total)}",
                    _card_kb(st.id), st.g24.value,
                ))

        leader_notice = await self._leader(stats)
        if leader_notice and s["notify_leader_enabled"]:
            notices.append(leader_notice)

        author_notice = await self._author_top()
        if author_notice and s["notify_author_top_enabled"]:
            notices.append(author_notice)

        if s["notify_trending_enabled"]:
            for pub_id, old, new in transitions:
                st = stats[pub_id]
                if new == "growing" and old in {"stable", "declining"}:
                    accel = f" (×{st.accel:.1f} к обычной скорости)" if st.accel else ""
                    notices.append(Notice(
                        "trending", str(pub_id),
                        f"🔥 Публикация начала быстро набирать популярность{accel}\n{_pub_line(st)}\n"
                        f"За 24 ч: {fmt_delta(st.g24.value)} · за предыдущие 24 ч: {fmt_delta(st.p24.value)}",
                        _card_kb(pub_id), st.g24.value,
                    ))

        if s["notify_stalled_enabled"]:
            dead = int(s["dead_views_24h"])
            for pub_id, old, new in transitions:
                st = stats[pub_id]
                if new == "declining" and st.g24.value <= dead:
                    notices.append(Notice(
                        "stalled", str(pub_id),
                        f"⚠️ Публикация практически перестала набирать просмотры\n{_pub_line(st)}\n"
                        f"За 24 ч: {fmt_delta(st.g24.value)} · обычно ≈{fmt_int(st.reference or 0)} в сутки",
                        _card_kb(pub_id), st.g24.value,
                    ))

        sent = 0
        skipped_extra = 0
        for notice in notices:
            if not await self._allowed(notice.kind, notice.entity, cooldown):
                continue
            if sent >= MAX_MESSAGES_PER_RUN:
                skipped_extra += 1
                continue
            if await self._send(notice.text, notice.markup):
                await self._log(notice)
                sent += 1
        if skipped_extra:
            await self._send(f"ℹ️ Ещё {skipped_extra} уведомлений не отправлено, чтобы не засорять чат. "
                             "Подробности — в разделе «📊 Статистика».", None)
        return sent

    async def _update_statuses(self, stats: dict[int, PubStats]) -> list[tuple[int, str, str]]:
        transitions: list[tuple[int, str, str]] = []
        async with self.app.db() as session:
            rows = (await session.execute(select(Publication.id, Publication.activity_status))).all()
            now = utcnow()
            for pub_id, old in rows:
                st = stats.get(pub_id)
                if st is None or st.status == old:
                    continue
                transitions.append((pub_id, old, st.status))
                await session.execute(
                    update(Publication).where(Publication.id == pub_id)
                    .values(activity_status=st.status, status_changed_at=now)
                )
            await session.commit()
        return transitions

    async def _leader(self, stats: dict[int, PubStats]) -> Notice | None:
        min_views = int(self.app.settings["trending_min_views_24h"])
        candidates = [s for s in stats.values() if s.g24.usable and s.g24.value >= min_views]
        if not candidates:
            return None
        top = max(candidates, key=lambda s: s.g24.value)
        previous = await self.app.settings.get_state("leader_pub")
        if previous == top.id:
            return None
        await self.app.settings.set_state("leader_pub", top.id)
        if previous is None:  # первый запуск — запоминаем без уведомления
            return None
        return Notice(
            "leader", str(top.id),
            f"🥇 Новый лидер по приросту за 24 ч: {fmt_delta(top.g24.value)}\n{_pub_line(top)}\n"
            f"👁 Всего: {fmt_int(top.total)}",
            _card_kb(top.id), top.g24.value,
        )

    async def _author_top(self) -> Notice | None:
        metric = str(self.app.settings["author_top_metric"])
        authors = [a for a in await self.app.analytics.authors() if a.count > 0]
        if not authors:
            return None
        ranked = sort_authors(authors, metric)
        top = ranked[0]
        if author_metric(top, metric) <= 0:
            return None
        key = f"leader_author_{metric}"
        previous = await self.app.settings.get_state(key)
        if previous == top.id:
            return None
        await self.app.settings.set_state(key, top.id)
        if previous is None:
            return None
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="👤 Открыть автора", callback_data=AuthorCb(act="card", id=top.id).pack())
        ]])
        return Notice(
            "author_top", str(top.id),
            f"🏆 Автор <b>{esc(top.name)}</b> вышел на 1-е место рейтинга "
            f"({METRIC_LABELS.get(metric, metric)}: {fmt_int(author_metric(top, metric))})",
            markup, author_metric(top, metric),
        )

    async def _allowed(self, kind: str, entity: str, cooldown_hours: int) -> bool:
        since = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
        async with self.app.db() as session:
            row = (
                await session.execute(
                    select(NotificationLog.id)
                    .where(NotificationLog.kind == kind, NotificationLog.entity == entity,
                           NotificationLog.sent_at >= since)
                    .limit(1)
                )
            ).first()
        return row is None

    async def _log(self, notice: Notice) -> None:
        async with self.app.db() as session:
            session.add(NotificationLog(kind=notice.kind, entity=notice.entity, value=notice.value))
            await session.commit()

    async def _send(self, text: str, markup: InlineKeyboardMarkup | None) -> bool:
        if self.app.bot is None:
            return False
        ok = False
        for admin_id in self.app.config.admin_ids:
            try:
                await self.app.bot.send_message(admin_id, text, reply_markup=markup)
                ok = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("Не удалось отправить уведомление %s: %s", admin_id, exc)
        return ok
