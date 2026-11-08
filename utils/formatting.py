"""Форматирование чисел, дат и текста для сообщений Telegram (HTML)."""
from __future__ import annotations

import html
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

NBSP = " "

STATUS_LABELS = {
    "growing": "📈 Растёт",
    "stable": "➖ Стабильно",
    "declining": "📉 Активность снижается",
    "new": "🆕 Мало данных",
    "unknown": "⏳ Нет данных",
}
STATUS_EMOJI = {"growing": "📈", "stable": "➖", "declining": "📉", "new": "🆕", "unknown": "⏳"}


def esc(text: object) -> str:
    return html.escape(str(text) if text is not None else "", quote=False)


def esc_attr(text: object) -> str:
    return html.escape(str(text) if text is not None else "", quote=True)


def fmt_int(value: int | float | None) -> str:
    if value is None:
        return "—"
    return f"{int(round(value)):,}".replace(",", NBSP)


def fmt_float(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}".replace(",", NBSP).replace(".", ",")


def fmt_delta(value: int | None) -> str:
    if value is None:
        return "—"
    return f"+{fmt_int(value)}" if value >= 0 else f"−{fmt_int(-value)}"


def fmt_pct(ratio: float | None) -> str:
    if ratio is None:
        return "—"
    pct = (ratio - 1) * 100
    sign = "+" if pct >= 0 else "−"
    return f"{sign}{abs(pct):.0f}%"


def to_local(dt: datetime | int | float | None, tz: ZoneInfo) -> datetime | None:
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        dt = datetime.fromtimestamp(dt, tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def fmt_dt(dt: datetime | int | float | None, tz: ZoneInfo, with_year: bool = True) -> str:
    local = to_local(dt, tz)
    if local is None:
        return "—"
    return local.strftime("%d.%m.%Y %H:%M" if with_year else "%d.%m %H:%M")


def fmt_date(dt: datetime | int | float | None, tz: ZoneInfo) -> str:
    local = to_local(dt, tz)
    return local.strftime("%d.%m.%Y") if local else "—"


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}{NBSP}д")
    if hours:
        parts.append(f"{hours}{NBSP}ч")
    if minutes and not days:
        parts.append(f"{minutes}{NBSP}мин")
    if not parts:
        return f"{seconds}{NBSP}с" if seconds < 60 else f"{seconds // 60}{NBSP}мин"
    return " ".join(parts)


def fmt_ago(ts: datetime | int | float | None) -> str:
    if ts is None:
        return "никогда"
    if isinstance(ts, datetime):
        ts = ts.timestamp()
    diff = time.time() - ts
    if diff < 60:
        return "только что"
    return f"{fmt_duration(diff)} назад"


def truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def link(url: str, text: str) -> str:
    return f'<a href="{esc_attr(url)}">{esc(text)}</a>'


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Делит длинный текст на части по строкам, не разрывая HTML-строки."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
