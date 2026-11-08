"""Интеграционные тесты сервисов с mock Telegraph API."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

from database import Batch, BatchItem, DailyView, Publication, ViewSnapshot
from services import publications as pubs
from services import youtube as yt
from services.analytics import DAY, sort_stats
from services.batch_creator import BatchStateError
from services.content import prepare_content

AUTHORS = ["Иван", "Дмитрий", "Александр", "Максим", "Сергей"]
TEXT = "Здесь находится общий текст публикации. Он должен быть одинаковым во всех создаваемых публикациях."


async def _create(app, authors=AUTHORS, title="Новости технологий"):
    _, content_json = prepare_content(TEXT)
    batch_id = await app.creator.create_draft(title, TEXT, content_json, authors)
    progress = []

    async def cb(done, total, item):
        progress.append((done, total, item.author, item.status))

    report = await app.creator.run(batch_id, cb)
    return batch_id, report, progress


async def test_create_batch(app, mock_telegraph):
    batch_id, report, progress = await _create(app)
    assert report.status == "completed"
    assert [i.author for i in report.created] == AUTHORS
    assert len({i.url for i in report.created}) == 5
    assert progress[-1][:2] == (5, 5)
    assert len(mock_telegraph.pages) == 5
    for page in mock_telegraph.pages.values():
        assert page["title"] == "Новости технологий"
        assert page["content"] == [{"tag": "p", "children": [TEXT]}]
        assert "youtube" not in str(page["content"]).lower()
    async with app.db() as s:
        rows = (await s.execute(select(Publication))).scalars().all()
        assert sorted(p.author_name for p in rows) == sorted(AUTHORS)
        assert all(p.path and p.url.startswith("https://telegra.ph/") for p in rows)
        snaps = (await s.execute(select(func.count(ViewSnapshot.id)))).scalar()
        assert snaps == 5  # нулевая точка отсчёта для каждой страницы
    # повторный запуск завершённой партии не создаёт дублей
    with pytest.raises(BatchStateError):
        await app.creator.run(batch_id)
    assert len(mock_telegraph.pages) == 5


async def test_ambiguous_timeout_no_duplicate(app, mock_telegraph):
    mock_telegraph.slow_create_once = True  # страница создаётся, но ответ не успевает прийти
    _, report, _ = await _create(app, ["Иван", "Дмитрий"])
    assert report.status == "completed"
    assert len(mock_telegraph.pages) == 2, "после тайм-аута не должно быть дубликата"
    assert "getPageList" in mock_telegraph.calls


async def test_flood_wait_retry(app, mock_telegraph):
    mock_telegraph.flood_once = True
    _, report, _ = await _create(app, ["Иван"])
    assert report.status == "completed" and len(mock_telegraph.pages) == 1


async def test_partial_failure_and_resume(app, mock_telegraph):
    mock_telegraph.fail_author = "Максим"
    batch_id, report, _ = await _create(app)
    assert report.status == "partial"
    assert [i.author for i in report.not_created] == ["Максим"]
    assert len(report.created) == 4
    mock_telegraph.fail_author = None
    report2 = await app.creator.run(batch_id)
    assert report2.status == "completed" and len(report2.created) == 5
    assert len(mock_telegraph.pages) == 5


async def test_recovery_after_crash(app, mock_telegraph):
    """Имитация падения: страница создана в Telegraph, но бот не успел записать результат."""
    _, content_json = prepare_content(TEXT)
    batch_id = await app.creator.create_draft("Тест восстановления", TEXT, content_json, ["Иван", "Дмитрий"])
    token = await app.accounts.token_for(None)
    await app.telegraph.create_page(token, "Тест восстановления", [{"tag": "p", "children": [TEXT]}], "Иван")
    async with app.db() as s:
        await s.execute(update(Batch).where(Batch.id == batch_id).values(status="running"))
        await s.execute(update(BatchItem).where(BatchItem.batch_id == batch_id, BatchItem.position == 1)
                        .values(status="creating"))
        await s.commit()
    reports = await app.creator.recover_on_startup()
    assert len(reports) == 1 and reports[0].status == "interrupted"
    assert [i.author for i in reports[0].created] == ["Иван"]
    final = await app.creator.run(batch_id)
    assert final.status == "completed"
    assert len(mock_telegraph.pages) == 2, "страница Ивана не должна создаться повторно"


async def _insert_snapshots(app, pub_id, points):
    """points: [(секунд назад, views)] — по возрастанию времени."""
    now = int(time.time())
    async with app.db() as s:
        pub = await s.get(Publication, pub_id)
        prev = 0
        await s.execute(ViewSnapshot.__table__.delete().where(ViewSnapshot.publication_id == pub_id))
        for ago, views in points:
            s.add(ViewSnapshot(publication_id=pub_id, taken_at=now - ago, views=views, delta=views - prev))
            prev = views
        pub.views = prev
        pub.views_updated_at = datetime.now(timezone.utc)
        await s.commit()
    app.analytics.invalidate()


async def _age(app, pub_id, days):
    async with app.db() as s:
        await s.execute(update(Publication).where(Publication.id == pub_id)
                        .values(created_at=datetime.now(timezone.utc) - timedelta(days=days)))
        await s.commit()


async def test_growth_and_statuses(app):
    _, report, _ = await _create(app, ["Растущий", "Спадающий", "Новый", "Сброс"])
    ids = {i.author: i.publication_id for i in report.created}
    H = 3600
    # растущий: 10/сутки неделю, затем +100 за последние сутки
    await _age(app, ids["Растущий"], 10)
    pts = [(10 * DAY, 0)] + [((9 - d) * DAY, 10 * (d + 1)) for d in range(9)]
    pts.append((60, pts[-1][1] + 100))
    await _insert_snapshots(app, ids["Растущий"], pts)
    # спадающий: 100/сутки, затем +5
    await _age(app, ids["Спадающий"], 10)
    pts = [(10 * DAY, 0)] + [((9 - d) * DAY, 100 * (d + 1)) for d in range(9)]
    pts.append((60, pts[-1][1] + 5))
    await _insert_snapshots(app, ids["Спадающий"], pts)
    # сброс счётчика: 0 -> 500 -> 450 (корректировка) -> 480
    await _age(app, ids["Сброс"], 3)
    await _insert_snapshots(app, ids["Сброс"], [(3 * DAY, 0), (20 * H, 500), (10 * H, 450), (60, 480)])

    stats = await app.analytics.get_all()
    g = stats[ids["Растущий"]]
    assert g.g24.value == 100 and g.g24.quality == "exact"
    assert g.p24.value == 10
    assert g.status == "growing" and g.trend_score > 0
    d = stats[ids["Спадающий"]]
    assert d.g24.value == 5 and d.status == "declining"
    n = stats[ids["Новый"]]
    assert n.status == "new", "новая публикация не должна считаться неуспешной"
    assert n.g24.quality == "creation"
    r = stats[ids["Сброс"]]
    assert r.g24.value == 500 + 30 and r.g24.drops == 50, "уменьшение счётчика не вычитается"
    assert r.g24.value >= 0
    # сортировки и фильтры
    assert sort_stats(list(stats.values()), "low")[0].id == ids["Спадающий"]
    trending = [s.id for s in sort_stats(list(stats.values()), "trend")]
    assert ids["Растущий"] in trending and ids["Спадающий"] not in trending
    assert ids["Новый"] not in trending  # без просмотров — не «набирает»
    # рейтинг авторов
    authors = {a.name: a for a in await app.analytics.authors()}
    assert authors["Растущий"].g24 == 100 and authors["Растущий"].count == 1
    ov = await app.analytics.overview()
    assert ov.pubs == 4 and ov.growing == 2 and ov.declining == 1 and ov.new == 1


async def test_partial_and_approx_quality(app):
    _, report, _ = await _create(app, ["А", "Б"])
    a, b = (i.publication_id for i in report.created)
    await _age(app, a, 40)
    # история только за 5 часов у старой публикации -> partial
    await _insert_snapshots(app, a, [(5 * 3600, 1000), (60, 1100)])
    # был перерыв: последний снимок до начала окна — 3 суток назад -> approx
    await _age(app, b, 40)
    await _insert_snapshots(app, b, [(3 * DAY, 100), (60, 400)])
    stats = await app.analytics.get_all()
    assert stats[a].g24.quality == "partial" and stats[a].g24.value == 100
    assert 0 < stats[a].g24.coverage < 0.3
    assert stats[b].g24.quality == "approx" and stats[b].g24.value == 300


async def test_collector_and_daily(app, mock_telegraph):
    _, report, _ = await _create(app, ["Иван", "Дмитрий"])
    for path in mock_telegraph.pages:
        mock_telegraph.views[path] = 42
        today = datetime.now(timezone.utc).date()
        mock_telegraph.daily_views[(path, today.year, today.month, today.day)] = 40
    result = await app.collector.collect_all()
    assert result.ok == 2 and result.failed == 0
    stats = await app.analytics.get_all()
    assert all(s.total == 42 and s.g24.value == 42 for s in stats.values())
    stored = await app.collector.sync_daily()
    assert stored == 2
    async with app.db() as s:
        rows = (await s.execute(select(DailyView))).scalars().all()
        assert {r.views for r in rows} == {40} and not any(r.is_final for r in rows)
    pid = report.created[0].publication_id
    summary = await app.analytics.daily_summary(pid, stats[pid].created_ts)
    assert summary.today == 40 and summary.sum7 == 40
    # уменьшение счётчика при ручном обновлении
    path = next(iter(mock_telegraph.pages))
    mock_telegraph.views[path] = 30
    async with app.db() as s:
        target = (await s.execute(select(Publication.id).where(Publication.path == path))).scalar()
    old, new = await app.collector.refresh_one(target)
    assert (old, new) == (42, 30)
    st = await app.analytics.get(target)
    assert st.total == 30 and st.g24.value == 42 and st.g24.drops == 12
    # прореживание не ломает расчёт
    await app.collector.compact()
    assert (await app.analytics.get(target)).total == 30
    # страница пропала в Telegraph -> ошибка сохраняется, сбор продолжается
    mock_telegraph.pages.pop(path)
    result = await app.collector.collect_all()
    assert result.failed == 1 and result.ok == 1
    assert (await app.analytics.get(target)).sync_error


async def test_youtube_groups_favorites_selection_delete(app, mock_telegraph):
    _, report, _ = await _create(app)
    ids = [i.publication_id for i in report.created]
    async with app.db() as s:
        v1 = await yt.get_or_create_video(s, "dQw4w9WgXcQ", None, False)
        await yt.bind(s, ids[:3], v1)
        v2 = await yt.get_or_create_video(s, "aaaaaaaaaaa", None, False)
        await yt.bind(s, ids[3:], v2)
        await s.commit()
        groups = {g.video_id: g.count for g in await yt.list_groups(s)}
    app.analytics.invalidate()
    assert groups == {"dQw4w9WgXcQ": 3, "aaaaaaaaaaa": 2}
    assert len(await pubs.query(app, "yt", v1.id)) == 3
    # ссылка не попала в Telegraph
    assert all("dQw4w9WgXcQ" not in str(p["content"]) for p in mock_telegraph.pages.values())
    # смена ссылки группы на существующую -> объединение
    async with app.db() as s:
        group = await s.get(type(v2), v2.id)
        merged_group, merged = await yt.change_group_video(s, group, "dQw4w9WgXcQ", None, False)
        await s.commit()
    app.analytics.invalidate()
    assert merged and len(await pubs.query(app, "yt", v1.id)) == 5
    # поиск: автор + YouTube
    f = pubs.SearchFilters(author="иван", youtube="dQw4w9WgXcQ")
    await pubs.save_filters(app, f)
    found = await pubs.query(app, "search")
    assert [s.author_name for s in found] == ["Иван"]
    await pubs.save_filters(app, pubs.SearchFilters(telegraph=found[0].path))
    assert len(await pubs.query(app, "search")) == 1
    today = datetime.now(app.config.timezone).date().isoformat()
    await pubs.save_filters(app, pubs.SearchFilters(date_from=today, date_to=today))
    assert len(await pubs.query(app, "search")) == 5
    # избранное
    assert await pubs.toggle_favorite(app, ids[0]) is True
    assert [s.id for s in await pubs.query(app, "fav")] == [ids[0]]
    # выбор и массовые операции
    await pubs.add_selected(app, ids[:2])
    assert await pubs.selected_ids(app) == set(ids[:2])
    await pubs.set_label(app, ids[:2], "реклама")
    await pubs.set_favorite(app, ids[:2], True)
    assert len(await pubs.query(app, "fav")) == 2
    # удаление только из базы
    deleted = await pubs.delete_records(app, ids[:2])
    assert deleted == 2 and len(mock_telegraph.pages) == 5
    assert await pubs.selected_ids(app) == set()
    assert len(await app.analytics.get_all()) == 3


async def test_edit_in_telegraph(app, mock_telegraph):
    _, report, _ = await _create(app, ["Иван", "Дмитрий"])
    ids = [i.publication_id for i in report.created]
    html = "Новый <b>текст</b>"
    _, js = prepare_content(html)
    res = await pubs.edit_in_telegraph(app, ids, title="Новый заголовок", content=(html, js))
    assert len(res.ok) == 2 and not res.failed
    assert all(p["title"] == "Новый заголовок" for p in mock_telegraph.pages.values())
    res = await pubs.edit_in_telegraph(app, ids[:1], author="Пётр")
    st = await app.analytics.get(ids[0])
    assert st.author_name == "Пётр"


async def test_settings_and_scheduler(app):
    from scheduler.tasks import JOB_COLLECT, setup_scheduler

    scheduler = setup_scheduler(app)
    scheduler.start(paused=True)
    try:
        await app.settings.set("snapshot_interval_minutes", 45)
        job = scheduler.get_job(JOB_COLLECT)
        assert job.trigger.interval.total_seconds() == 45 * 60
        with pytest.raises(ValueError):
            await app.settings.set("snapshot_interval_minutes", 1)
    finally:
        scheduler.shutdown(wait=False)


async def test_fsm_storage_survives_restart(app):
    from aiogram.fsm.storage.base import StorageKey

    from handlers.states import CreatePosts
    from utils.fsm_storage import SQLiteStorage

    key = StorageKey(bot_id=1, chat_id=111, user_id=111)
    storage = SQLiteStorage(app.db)
    await storage.set_state(key, CreatePosts.authors)
    await storage.set_data(key, {"title": "Новости", "parts": ["a"]})
    fresh = SQLiteStorage(app.db)  # «после перезапуска»
    assert await fresh.get_state(key) == CreatePosts.authors.state
    assert (await fresh.get_data(key))["title"] == "Новости"
    await fresh.set_state(key, None)
    await fresh.set_data(key, {})
    assert await fresh.get_state(key) is None


async def test_notifications(app, mock_telegraph):
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text, reply_markup=None):
            sent.append(text)

    app.bot = FakeBot()
    _, report, _ = await _create(app, ["Иван"])
    pid = report.created[0].publication_id
    await app.settings.set("notify_daily_enabled", True)
    await app.settings.set("notify_daily_threshold", 50)
    await _insert_snapshots(app, pid, [(3600, 0), (60, 120)])
    assert await app.notifier.check() == 1
    assert "120" in sent[0]
    assert await app.notifier.check() == 0, "повторное уведомление не отправляется"
