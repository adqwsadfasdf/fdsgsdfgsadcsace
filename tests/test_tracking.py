"""Добавление готовых своих публикаций и отслеживание конкурентов."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import func, select, text

from database import (
    KIND_COMPETITOR,
    KIND_IMPORTED,
    Competitor,
    Publication,
    ViewSnapshot,
    create_engine,
    create_sessionmaker,
    init_db,
)
from services import publications as pubs
from services import tracking
from utils.validators import ValidationError


def today_suffix() -> str:
    return datetime.now(timezone.utc).strftime("%m-%d")


# ---------------------------------------------------------------------- разбор


def test_parse_links():
    res = tracking.parse_links(
        "1. https://telegra.ph/Pervaya-11-08\n"
        "https://telegra.ph/Pervaya-11-08\n"  # повтор
        "graph.org/Vtoraya-11-07-2, telegra.ph/Tretya-01-01\n"
        "просто текст\n"
        "- https://example.com/x\n"
    )
    assert res.paths == ["Pervaya-11-08", "Vtoraya-11-07-2", "Tretya-01-01"]
    assert res.duplicates == 1
    assert "просто" in res.invalid and "https://example.com/x" in res.invalid
    with pytest.raises(ValidationError):
        tracking.parse_links("\n".join(f"https://telegra.ph/P-{i}-01-01" for i in range(3)), limit=2)


def test_date_from_path():
    today = date(2026, 11, 8)
    assert tracking.date_from_path("Title-11-08", today) == date(2026, 11, 8)
    assert tracking.date_from_path("Title-11-08-3", today) == date(2026, 11, 8)
    assert tracking.date_from_path("Title-12-31", today) == date(2025, 12, 31)  # в будущем — значит прошлый год
    assert tracking.date_from_path("Title-02-29", today) == date(2024, 2, 29)
    assert tracking.date_from_path("NoDate", today) is None
    assert tracking.date_from_path("Bad-13-45", today) is None


# ---------------------------------------------------------------------- свои готовые


async def test_import_foreign_account_link(app, mock_telegraph):
    url = mock_telegraph.add_page(f"Staryj-post-{today_suffix()}", "Старый пост", "Пётр", views=120)
    res = await tracking.track_paths(app, tracking.parse_links(url).paths, competitor=False)
    assert len(res.added) == 1 and res.not_owned == 1
    pub_id = res.added[0][0]
    async with app.db() as s:
        pub = await s.get(Publication, pub_id)
        assert pub.kind == KIND_IMPORTED and pub.account_id is None and pub.views == 120
        assert pub.tracked_since is not None
        snaps = (await s.execute(select(ViewSnapshot).where(ViewSnapshot.publication_id == pub_id))).scalars().all()
        assert [(x.views, x.delta) for x in snaps] == [(120, 0)], "точка отсчёта — текущий счётчик, прирост 0"
    st = (await app.analytics.get_all())[pub_id]
    assert st.kind == "imported" and st.total == 120
    assert st.g24.value == 0 and st.g24.quality == "partial", "без «с момента создания» — до добавления не следили"
    assert any(a.name == "Пётр" and a.count == 1 for a in await app.analytics.authors())
    assert [s.id for s in await pubs.query(app, "imp")] == [pub_id]

    # прирост после добавления считается
    mock_telegraph.views[f"Staryj-post-{today_suffix()}"] = 150
    await app.collector.collect_all()
    assert (await app.analytics.get(pub_id)).g24.value == 30

    # повторное добавление — без дубля
    again = await tracking.track_paths(app, [f"Staryj-post-{today_suffix()}"], competitor=False)
    assert not again.added and again.existing_own

    # чужой аккаунт — редактирование запрещено, страница не тронута
    result = await pubs.edit_in_telegraph(app, [pub_id], title="Новый")
    assert not result.ok and "другом аккаунте" in result.failed[0][1]
    assert mock_telegraph.pages[f"Staryj-post-{today_suffix()}"]["title"] == "Старый пост"


async def test_import_own_account_and_edit_keeps_content(app, mock_telegraph):
    account = await app.accounts.active()
    mock_telegraph.add_page(f"Moj-post-{today_suffix()}", "Мой пост", "Анна", views=5, token=account.access_token)
    res = await tracking.import_account(app, account.id)
    assert len(res.added) == 1
    pub_id = res.added[0][0]
    async with app.db() as s:
        assert (await s.get(Publication, pub_id)).account_id == account.id
    # смена заголовка не стирает текст: текущее содержимое берётся из Telegraph (getPage)
    result = await pubs.edit_in_telegraph(app, [pub_id], title="Мой пост (новый)")
    assert result.ok == [pub_id], result.failed
    page = mock_telegraph.pages[f"Moj-post-{today_suffix()}"]
    assert page["title"] == "Мой пост (новый)" and page["content"] == [{"tag": "p", "children": ["Исходный текст"]}]
    # повторный импорт аккаунта — ничего нового
    again = await tracking.import_account(app, account.id)
    assert not again.added and len(again.existing_own) == 1


async def test_connect_other_account_token(app, mock_telegraph):
    mock_telegraph.accounts["othertoken123"] = {"short_name": "Other", "author_name": None}
    mock_telegraph.add_page(f"Chuzhoj-{today_suffix()}", "Пост", "Б", token="othertoken123")
    account, info, new = await app.accounts.add_existing("othertoken123")
    assert new and info.page_count == 1 and not account.is_active
    assert (await app.accounts.active()).id != account.id, "основной аккаунт не меняется"
    _, _, new_again = await app.accounts.add_existing("othertoken123")
    assert not new_again
    res = await tracking.track_paths(app, [f"Chuzhoj-{today_suffix()}"], competitor=False)
    assert res.not_owned == 0
    with pytest.raises(Exception):
        await app.accounts.add_existing("bad token")


async def test_year_detection(app, mock_telegraph):
    path = f"Davnij-{today_suffix()}"
    mock_telegraph.add_page(path, "Давний", "В", views=500)
    year = datetime.now(timezone.utc).year
    mock_telegraph.year_views[(path, year - 1)] = 300
    mock_telegraph.year_views[(path, year - 2)] = 100
    res = await tracking.track_paths(app, [path], competitor=False)
    st = await app.analytics.get(res.added[0][0])
    assert datetime.fromtimestamp(st.created_ts, tz=timezone.utc).year == year - 2


# ---------------------------------------------------------------------- конкуренты


async def test_competitors_are_separate(app, mock_telegraph):
    rival = await tracking.get_or_create_competitor(app, "Канал Конкурента")
    assert (await tracking.get_or_create_competitor(app, "канал  конкурента")).id == rival.id
    urls = [mock_telegraph.add_page(f"Rival-{i}-{today_suffix()}", f"Пост {i}", "Конкурент", views=100 * i)
            for i in (1, 2)]
    res = await tracking.track_paths(app, tracking.parse_links("\n".join(urls)).paths,
                                     competitor=True, competitor_id=rival.id)
    assert len(res.added) == 2
    ids = res.new_ids

    # не попадают в свою аналитику
    assert not (await app.analytics.get_all())
    assert set(await app.analytics.get_competitors()) == set(ids)
    assert (await app.analytics.overview()).pubs == 0
    assert (await app.analytics.overview()).authors_all == 0
    assert not [a for a in await app.analytics.authors() if a.count > 0]
    assert await pubs.query(app, "all") == []
    assert {s.id for s in await pubs.query(app, "compc", rival.id)} == set(ids)
    assert await pubs.author_pub_count(app, ["Конкурент"]) == {"Конкурент": 0}

    # собираются тем же фоновым сбором getViews
    mock_telegraph.views[f"Rival-2-{today_suffix()}"] = 260
    await app.collector.collect_all()
    stats = {c.id: c for c in await tracking.competitor_stats(app)}
    assert stats[rival.id].count == 2 and stats[rival.id].total == 360 and stats[rival.id].g24 == 60
    assert stats[rival.id].best_views == 260

    # refresh_many и запрет редактирования
    mock_telegraph.views[f"Rival-1-{today_suffix()}"] = 101
    result = await app.collector.refresh_many(ids)
    assert result.ok == 2
    edit = await pubs.edit_in_telegraph(app, ids[:1], title="x")
    assert not edit.ok

    # повтор ссылки — без дубля; та же ссылка как «своя» — переносится из конкурентов
    again = await tracking.track_paths(app, [f"Rival-1-{today_suffix()}"], competitor=True)
    assert again.existing_comp and not again.added
    moved = await tracking.track_paths(app, [f"Rival-1-{today_suffix()}"], competitor=False)
    assert len(moved.moved) == 1
    assert len(await app.analytics.get_all()) == 1 and len(await app.analytics.get_competitors()) == 1

    # переименование и удаление (только из базы)
    await tracking.rename_competitor(app, rival.id, "Новое имя")
    assert await tracking.competitor_name(app, rival.id) == "Новое имя"
    deleted = await tracking.delete_competitor(app, rival.id)
    assert deleted == 1
    async with app.db() as s:
        assert (await s.execute(select(func.count(Competitor.id)))).scalar() == 0
        kinds = (await s.execute(select(Publication.kind))).scalars().all()
        assert kinds == [KIND_IMPORTED]
    assert f"Rival-2-{today_suffix()}" in mock_telegraph.pages, "страница Telegraph не удаляется"


async def test_missing_page_reported(app, mock_telegraph):
    res = await tracking.track_paths(app, ["Net-takoj-01-01"], competitor=True)
    assert not res.added and res.failed and "не найдена" in res.failed[0][1]


async def test_only_api_methods_used(app, mock_telegraph):
    """Добавление и отслеживание обращаются только к методам API, страницы не открываются."""
    url = mock_telegraph.add_page(f"Proverka-{today_suffix()}", "Проверка", "Г", views=3)
    await tracking.track_paths(app, tracking.parse_links(url).paths, competitor=True)
    await app.collector.collect_all()
    await app.collector.sync_daily()
    assert set(mock_telegraph.calls) <= {"createAccount", "getAccountInfo", "getPage", "getPageList", "getViews"}


# ---------------------------------------------------------------------- миграция


async def test_migration_from_v1(tmp_path):
    path = tmp_path / "old.db"
    engine = create_engine(path)
    db = create_sessionmaker(engine)
    await init_db(engine, db)
    # имитируем базу старой версии: без новых колонок
    v1_cols = ("id, batch_id, account_id, title, author_id, author_name, content_html, content_json, url, path, "
               "created_at, views, views_updated_at, youtube_video_id, is_favorite, label, activity_status, "
               "status_changed_at, sync_error")
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA foreign_keys=OFF"))
        await conn.execute(text(f"CREATE TABLE p_old AS SELECT {v1_cols} FROM publications"))
        await conn.execute(text("DROP TABLE publications"))
        await conn.execute(text("ALTER TABLE p_old RENAME TO publications"))
        await conn.execute(text("INSERT INTO publications (id, title, path, views) VALUES (1, 'Старая', 'Old-01-01', 7)"))
        await conn.execute(text("UPDATE app_state SET value='1' WHERE key='schema_version'"))
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(publications)"))).all()}
        assert "kind" not in cols
    await engine.dispose()

    engine = create_engine(path)
    db = create_sessionmaker(engine)
    await init_db(engine, db)
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(publications)"))).all()}
        assert {"kind", "competitor_id", "tracked_since"} <= cols
        version = (await conn.execute(text("SELECT value FROM app_state WHERE key='schema_version'"))).scalar()
        assert version == "2"
        row = (await conn.execute(text("SELECT title, views, kind FROM publications"))).one()
        assert tuple(row) == ("Старая", 7, "created"), "данные старой базы сохранены"
    await engine.dispose()


def test_kind_constants():
    assert json.dumps([KIND_IMPORTED, KIND_COMPETITOR]) == '["imported", "competitor"]'
