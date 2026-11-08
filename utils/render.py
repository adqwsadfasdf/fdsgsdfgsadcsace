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


NBSP = " "
SEP = "  |  "


def period_short(st: PubStats, period: str) -> str:
    value, _ = period_value(st, period)
    return "—" if value is None else fmt_delta(value)


def _seg(label: str, value: str) -> str:
    # неразрывный пробел: «7д: +62» не разрывается переносом строки
    return f"{label}:{NBSP}<b>{value}</b>"


def _line(total: int, values: dict[str, str]) -> str:
    return SEP.join([f"👁{NBSP}<b>{fmt_int(total)}</b>", _seg("24ч", values["24h"]), _seg("7д", values["7d"]),
                     _seg("30д", values["30d"])])


def stats_line(st: PubStats) -> str:
    """👁 всего | 24ч: +x | 7д: +y | 30д: +z"""
    return _line(st.total, {p: period_short(st, p) for p in ("24h", "7d", "30d")})


def sum_line(items: list[PubStats]) -> str:
    """То же для группы публикаций (конкурент, автор, видео)."""
    values = {}
    for period in ("24h", "7d", "30d"):
        known = [v for v, _ in (period_value(s, period) for s in items) if v is not None]
        values[period] = fmt_delta(sum(known)) if known else "—"
    return _line(sum(s.total for s in items), values)


def list_item(n: int, st: PubStats, sort: str, selected: bool | None = None, show_author: bool = True) -> str:
    """Две строки: «номер. заголовок · автор» и под ней цифры."""
    marks = ("⭐" if st.is_favorite else "") + ("🎬" if st.youtube_video_id else "") + ("❗" if st.sync_error else "")
    sel = ""
    if selected is not None:
        sel = "✅" if selected else "▫️"
    author = f" · {esc(truncate(st.author_name, 18))}" if show_author else ""
    head = f"{sel}<b>{n}.</b>{STATUS_EMOJI.get(st.status, '')}{marks} {link(st.url, truncate(st.title, 40))}{author}"
    extra = sort_metric(st, sort) if sort in {"fast", "trend", "prom", "low"} else None
    return f"{head}\n{stats_line(st)}" + (f"\n{extra}" if extra else "")
