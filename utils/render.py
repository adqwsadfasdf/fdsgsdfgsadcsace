"""Тексты статистики для сообщений: прирост с пометками точности, строки списков."""
from __future__ import annotations

from services.analytics import Growth, PubStats, period_value
from utils.formatting import (
    STATUS_EMOJI,
    esc,
    fmt_delta,
    fmt_duration,
    fmt_float,
    fmt_int,
    link,
    truncate,
)


def growth_text(g: Growth) -> str:
    """Подробная строка прироста с честной пометкой о точности."""
    q = g.quality
    if q == "nodata":
        return "нет данных (снимков ещё нет)"
    if q == "future":
        return "— (публикации ещё не было)"
    value = fmt_delta(g.value)
    if q == "exact":
        text = value
    elif q == "creation":
        text = f"{value} <i>(с момента создания)</i>"
    elif q == "approx":
        text = f"≈{value} <i>(приблизительно: окно шире на {fmt_duration(g.extra_seconds)} из-за пропуска снимков)</i>"
    else:  # partial
        if g.span_seconds < 3600:
            return "недостаточно данных <i>(история меньше 1 ч)</i>"
        text = f"{value} ⚠️ <i>данные только за {fmt_duration(g.span_seconds)} из периода</i>"
    if g.drops:
        text += f" <i>ℹ️ счётчик Telegraph уменьшался на {fmt_int(g.drops)} — не вычитается</i>"
    return text


def growth_short(g: Growth) -> str:
    q = g.quality
    if q in {"nodata"}:
        return "—"
    if q == "future":
        return "—"
    value = fmt_delta(g.value)
    if q == "approx":
        return f"≈{value}"
    if q == "partial":
        return f"{value}*"
    return value


def sort_metric(st: PubStats, sort: str) -> str | None:
    if sort == "fast":
        return f"⚡ {fmt_float(st.velocity)}/ч"
    if sort == "trend":
        accel = f" (×{fmt_float(st.accel)})" if st.accel else " (новая)"
        return f"🔥 {fmt_delta(st.g24.value)} за сутки{accel}"
    if sort == "prom":
        ratio = f" (×{fmt_float(st.g7.value / st.p7.value)} к пред. неделе)" if st.p7.usable and st.p7.value else ""
        return f"🚀 {fmt_delta(st.g7.value)} за 7 дн.{ratio}"
    if sort == "low":
        ref = f", обычно ≈{fmt_int(st.reference)}/сутки" if st.reference else ""
        return f"⚠️ всего {fmt_delta(st.g24.value)} за 24 ч{ref}"
    if sort == "d30":
        return f"30 дн.: {growth_short(st.g30)}"
    return None


def period_short(st: PubStats, period: str) -> str:
    value, inexact = period_value(st, period)
    if value is None:
        return "—"
    return fmt_delta(value) + ("*" if inexact else "")


def stats_line(st: PubStats) -> str:
    """Одна строка: всего · 24ч · 7д · 30д."""
    return (f"👁 <b>{fmt_int(st.total)}</b> · 24ч {period_short(st, '24h')} · "
            f"7д {period_short(st, '7d')} · 30д {period_short(st, '30d')}")


def sum_line(items: list[PubStats]) -> str:
    """Одна строка для группы публикаций (конкурент, автор, видео): всего · 24ч · 7д · 30д."""
    parts = [f"👁 <b>{fmt_int(sum(s.total for s in items))}</b>"]
    for period, label in (("24h", "24ч"), ("7d", "7д"), ("30d", "30д")):
        values = [period_value(s, period) for s in items]
        known = [v for v, _ in values if v is not None]
        if not known:
            parts.append(f"{label} —")
            continue
        inexact = any(x for _, x in values) or len(known) < len(values)
        parts.append(f"{label} {fmt_delta(sum(known))}{'*' if inexact else ''}")
    return " · ".join(parts)


def list_item(n: int, st: PubStats, sort: str, selected: bool | None = None, show_author: bool = True) -> str:
    marks = ("⭐" if st.is_favorite else "") + ("🎬" if st.youtube_video_id else "") + ("❗" if st.sync_error else "")
    sel = ""
    if selected is not None:
        sel = "✅" if selected else "▫️"
    author = f" · {esc(truncate(st.author_name, 18))}" if show_author else ""
    head = f"{sel}<b>{n}.</b>{STATUS_EMOJI.get(st.status, '')}{marks} {link(st.url, truncate(st.title, 32))}{author}"
    extra = sort_metric(st, sort) if sort in {"fast", "trend", "prom", "low"} else None
    return f"{head} — {stats_line(st)}" + (f" · {extra}" if extra else "")


LEGEND = "<i>* — неполные данные</i>"
