"""Модульные тесты: разбор ввода, конвертация текста, YouTube, экспорт."""
from __future__ import annotations

import io
from datetime import date

import pytest
from openpyxl import load_workbook

from services.analytics import Growth, PubStats
from services.content import html_to_nodes, prepare_content
from services.export import COLUMNS, build_workbook
from services.youtube import canonical_url, parse_youtube
from utils.validators import (
    ValidationError,
    parse_authors,
    parse_date_range,
    parse_telegraph_path,
    validate_title,
)


def test_parse_authors_dedup_and_bullets():
    res = parse_authors("Иван\n\n- Дмитрий\n2. Александр\nиван\n  Максим  \n• Сергей", limit=100)
    assert res.authors == ["Иван", "Дмитрий", "Александр", "Максим", "Сергей"]
    assert res.duplicates == ["иван"]
    assert not res.errors


def test_parse_authors_limit_and_long():
    res = parse_authors("\n".join(f"A{i}" for i in range(5)) + "\n" + "x" * 200, limit=3)
    assert len(res.errors) == 2  # длинное имя + превышение лимита


def test_title_validation():
    assert validate_title("  Новости   технологий ") == "Новости технологий"
    with pytest.raises(ValidationError):
        validate_title("   ")
    with pytest.raises(ValidationError):
        validate_title("x" * 300)


def test_dates():
    assert parse_date_range("08.11.2026") == (date(2026, 11, 8), date(2026, 11, 8))
    assert parse_date_range("01.11.2026-08.11.2026") == (date(2026, 11, 1), date(2026, 11, 8))
    assert parse_date_range("08.11.2026 - 01.11.2026") == (date(2026, 11, 1), date(2026, 11, 8))
    assert parse_date_range("2026-11-08") == (date(2026, 11, 8), date(2026, 11, 8))
    with pytest.raises(ValidationError):
        parse_date_range("вчера")


def test_telegraph_path():
    assert parse_telegraph_path("https://telegra.ph/Novosti-11-08-2") == "Novosti-11-08-2"
    assert parse_telegraph_path("telegra.ph/Abc-01-02") == "Abc-01-02"
    assert parse_telegraph_path("https://example.com/x") is None


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ?t=10",
    "youtube.com/shorts/dQw4w9WgXcQ",
    "https://m.youtube.com/watch?feature=share&v=dQw4w9WgXcQ",
    "https://www.youtube.com/embed/dQw4w9WgXcQ",
    "https://www.youtube.com/live/dQw4w9WgXcQ?si=abc",
    "dQw4w9WgXcQ",
])
def test_youtube_parse(url):
    assert parse_youtube(url) == "dQw4w9WgXcQ"
    assert canonical_url("dQw4w9WgXcQ") == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_youtube_rejects():
    assert parse_youtube("https://vimeo.com/123") is None
    assert parse_youtube("https://youtube.com/watch?v=short") is None
    assert parse_youtube("Иван", allow_bare_id=False) is None


def test_html_to_nodes_paragraphs_and_format():
    html = 'Первый <b>жирный</b> абзац\nвторая строка\n\nВторой <a href="https://x.y">ссылка</a>' \
           '<blockquote>цитата\nдве</blockquote><pre>код\n  отступ</pre><tg-spoiler>спойлер</tg-spoiler>'
    nodes = html_to_nodes(html)
    assert nodes[0] == {"tag": "p", "children": ["Первый ", {"tag": "b", "children": ["жирный"]}, " абзац",
                                                {"tag": "br"}, "вторая строка"]}
    assert nodes[1]["tag"] == "p" and nodes[1]["children"][1] == {
        "tag": "a", "attrs": {"href": "https://x.y"}, "children": ["ссылка"]}
    assert nodes[2] == {"tag": "blockquote", "children": ["цитата", {"tag": "br"}, "две"]}
    assert nodes[3] == {"tag": "pre", "children": ["код\n  отступ"]}
    assert nodes[4] == {"tag": "p", "children": ["спойлер"]}


def test_prepare_content_limits():
    with pytest.raises(ValidationError):
        prepare_content("   \n\n  ")
    with pytest.raises(ValidationError):
        prepare_content("x" * 70000)
    nodes, js = prepare_content("Привет")
    assert nodes == [{"tag": "p", "children": ["Привет"]}] and "Привет" in js


def test_export_workbook():
    from zoneinfo import ZoneInfo

    st = PubStats(id=1, author_id=1, author_name="Иван", title="Новости", url="https://telegra.ph/N-11-08",
                  path="N-11-08", created_ts=1_700_000_000, total=15420, updated_ts=1_700_000_100,
                  is_favorite=True, youtube_video_id=7, label="тест", batch_id=1, sync_error=None,
                  g24=Growth(340, 0, "exact"), g7=Growth(1820, 0, "approx"), g30=Growth(6450, 5, "partial",
                                                                                      0, 100, 0, 50))
    st.status = "growing"
    data = build_workbook([st], {7: ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "Видео")}, "Тест",
                          ZoneInfo("Europe/Moscow"), "Europe/Moscow")
    wb = load_workbook(io.BytesIO(data))
    ws = wb["Публикации"]
    assert [c.value for c in ws[1]] == [c[0] for c in COLUMNS]
    row = [c.value for c in ws[2]]
    assert row[0] == 1 and row[2] == "Иван" and row[9] == 15420 and row[11] == 340
    assert row[4].startswith("https://www.youtube.com") and row[17] == "📈 Растёт"
    assert "уменьшался на 5" in row[16]
    assert "Пояснения" in wb.sheetnames
