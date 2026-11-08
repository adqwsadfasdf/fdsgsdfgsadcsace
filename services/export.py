"""Экспорт публикаций в Excel (.xlsx, openpyxl)."""
from __future__ import annotations

import asyncio
import io
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from services.analytics import Growth, PubStats
from utils.formatting import STATUS_LABELS

QUALITY_TEXT = {
    "exact": "расчёт по снимкам",
    "creation": "с момента создания",
    "approx": "приблизительно (окно шире из-за пропуска снимков)",
    "partial": "неполные данные",
    "nodata": "нет данных",
    "future": "публикации ещё не было",
}

COLUMNS = [
    ("ID", 8), ("Заголовок", 40), ("Автор", 22), ("Telegraph", 45), ("YouTube", 45),
    ("Группа YouTube", 30), ("Метка", 16), ("Избранное", 10), ("Дата создания", 18),
    ("Просмотров всего (факт API)", 16), ("Обновлено", 18), ("Прирост 24 ч", 13),
    ("Прирост 7 дн.", 13), ("Прирост 30 дн.", 13), ("Точность 24 ч", 22), ("Точность 7 дн.", 22),
    ("Точность 30 дн.", 22), ("Статус активности", 22), ("Источник", 22), ("Отслеживается с", 18),
]
KIND_TEXT = {"created": "создана ботом", "imported": "своя, добавлена готовой", "competitor": "конкурент"}


def _growth_value(g: Growth) -> int | None:
    if g.quality in {"nodata"}:
        return None
    return g.value


def _quality(g: Growth) -> str:
    text = QUALITY_TEXT.get(g.quality, g.quality)
    if g.drops:
        text += f"; счётчик уменьшался на {g.drops}"
    if g.quality == "partial":
        text += f" ({int(g.coverage * 100)}% периода)"
    return text


def _local(ts: int | None, tz: ZoneInfo) -> datetime | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz).replace(tzinfo=None)


def build_workbook(
    items: list[PubStats],
    videos: dict[int, tuple[str, str | None]],
    title: str,
    tz: ZoneInfo,
    tz_name: str,
) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Публикации"
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="2F5597")
    for col, (name, width) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[1].height = 32

    for row, st in enumerate(items, start=2):
        video = videos.get(st.youtube_video_id) if st.youtube_video_id else None
        values = [
            st.id, st.title, st.author_name, st.url, video[0] if video else None,
            (video[1] or "") if video else None, st.label, "да" if st.is_favorite else "",
            _local(st.created_ts, tz), st.total, _local(st.updated_ts, tz),
            _growth_value(st.g24), _growth_value(st.g7), _growth_value(st.g30),
            _quality(st.g24), _quality(st.g7), _quality(st.g30),
            STATUS_LABELS.get(st.status, st.status),
            KIND_TEXT.get(st.kind, st.kind), _local(st.tracked_ts, tz),
        ]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=row, column=col, value=value)
            if col in (9, 11, 20) and value is not None:
                cell.number_format = "DD.MM.YYYY HH:MM"
            elif col in (10, 12, 13, 14) and value is not None:
                cell.number_format = "#,##0"
        if st.url:
            ws.cell(row=row, column=4).hyperlink = st.url
            ws.cell(row=row, column=4).style = "Hyperlink"
        if video:
            ws.cell(row=row, column=5).hyperlink = video[0]
            ws.cell(row=row, column=5).style = "Hyperlink"

    ws.freeze_panes = "B2"
    if items:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{len(items) + 1}"

    info = wb.create_sheet("Пояснения")
    info.column_dimensions["A"].width = 32
    info.column_dimensions["B"].width = 100
    now_local = datetime.now(tz).replace(tzinfo=None)
    rows = [
        ("Выгрузка", title),
        ("Сформировано", now_local.strftime("%d.%m.%Y %H:%M") + f" ({tz_name})"),
        ("Публикаций", len(items)),
        ("", ""),
        ("Просмотров всего", "Фактическое значение счётчика Telegraph (метод getViews) на момент последнего обновления."),
        ("Прирост 24 ч / 7 / 30 дн.", "Расчёт по сохранённым снимкам счётчика (Telegraph API не отдаёт скользящие периоды)."),
        ("Точность", "«расчёт по снимкам» — погрешность не больше интервала сбора; «приблизительно» — "
                     "был пропуск снимков; «неполные данные» — история короче периода."),
        ("Уменьшение счётчика", "Если Telegraph уменьшил счётчик (корректировка), уменьшение не вычитается из прироста, "
                                "а указывается в колонке точности."),
        ("Даты", f"Указаны в часовом поясе {tz_name}."),
        ("Добавленные готовые и конкуренты", "Прирост считается с момента добавления в бота («Отслеживается с»). "
                                             "Дата создания взята из ссылки Telegraph (-ММ-ДД), год уточнён по getViews."),
        ("География", "Telegraph API не предоставляет данных о странах и городах — такие поля не выгружаются."),
    ]
    for r, (k, v) in enumerate(rows, start=1):
        info.cell(row=r, column=1, value=k).font = Font(bold=True)
        info.cell(row=r, column=2, value=v).alignment = Alignment(wrap_text=True)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def build_workbook_async(*args, **kwargs) -> bytes:
    return await asyncio.to_thread(build_workbook, *args, **kwargs)


def safe_filename(name: str) -> str:
    name = re.sub(r"[^\w\-. ]+", "_", name, flags=re.UNICODE).strip(" ._") or "export"
    return f"{name[:60]}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
