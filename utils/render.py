"""Тексты статистики для сообщений: прирост с пометками точности, строки списков."""
from __future__ import annotations

from services.analytics import Growth, PubStats
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


def list_item(n: int, st: PubStats, sort: str, selected: bool | None = None) -> str:
    marks = ""
    if st.is_favorite:
        marks += "⭐"
    if st.youtube_video_id:
        marks += "🎬"
    if st.label:
        marks += f" 🏷{esc(truncate(st.label, 15))}"
    if st.sync_error:
        marks += " ❗"
    sel = ""
    if selected is not None:
        sel = "✅ " if selected else "▫️ "
    head = (
        f"{sel}<b>{n}.</b> {STATUS_EMOJI.get(st.status, '')} <b>{esc(truncate(st.author_name, 40))}</b> — "
        f"{link(st.url, truncate(st.title, 50))} {marks}".rstrip()
    )
    line2 = f"    👁 {fmt_int(st.total)} · 24ч {growth_short(st.g24)} · 7д {growth_short(st.g7)}"
    extra = sort_metric(st, sort)
    if extra:
        line2 += f"\n    {extra}"
    return f"{head}\n{line2}"


LEGEND = "<i>* — неполные данные, ≈ — приблизительно. 📈 растёт · ➖ стабильно · 📉 спад · 🆕 мало данных</i>"
