"""Операции над публикациями: выборки для списков, поиск, избранное, выбор, метки, правка в Telegraph."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update

from database import (
    KIND_COMPETITOR,
    Author,
    Batch,
    Competitor,
    Publication,
    SelectedPublication,
    YouTubeVideo,
    get_or_create_author,
)
from services.analytics import PubStats, sort_stats
from services.telegraph import describe_error
from utils.validators import name_key, parse_telegraph_path

if TYPE_CHECKING:
    from app_context import App

logger = logging.getLogger(__name__)

# Контексты списков
CTX_TITLES = {
    "all": "📚 Все публикации",
    "best": "🏆 Лучшие публикации",
    "stat": "📊 Публикации",
    "author": "👤 Публикации автора",
    "yt": "🎬 Публикации группы",
    "nolink": "🎬 Публикации без YouTube",
    "fav": "⭐ Избранное",
    "search": "🔍 Результаты поиска",
    "sel": "☑️ Выбранные публикации",
    "batch": "📦 Публикации партии",
    "imp": "📥 Добавленные готовые публикации",
    "comp": "🕵️ Все публикации конкурентов",
    "compc": "🕵️ Конкурент",
}
COMPETITOR_CTXS = {"comp", "compc"}


@dataclass
class SearchFilters:
    text: str | None = None  # автор ИЛИ заголовок
    author: str | None = None
    title: str | None = None
    date_from: str | None = None  # ISO, локальная дата
    date_to: str | None = None
    telegraph: str | None = None  # path
    youtube: str | None = None  # 11-символьный id видео
    label: str | None = None
    favorites: bool = False

    def is_empty(self) -> bool:
        return not any([self.text, self.author, self.title, self.date_from, self.telegraph,
                        self.youtube, self.label, self.favorites])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "SearchFilters":
        data = data or {}
        return cls(**{k: data.get(k) for k in cls.__dataclass_fields__ if k in data})


async def load_filters(app: "App") -> SearchFilters:
    return SearchFilters.from_dict(await app.settings.get_state("search_filters", {}))


async def save_filters(app: "App", filters: SearchFilters) -> None:
    await app.settings.set_state("search_filters", filters.to_dict())


def match_filters(st: PubStats, f: SearchFilters, tz: ZoneInfo, video_ids: dict[int, str]) -> bool:
    if f.text:
        q = name_key(f.text)
        if q not in name_key(st.author_name) and q not in name_key(st.title):
            return False
    if f.author and name_key(f.author) not in name_key(st.author_name):
        return False
    if f.title and name_key(f.title) not in name_key(st.title):
        return False
    if f.date_from or f.date_to:
        local_day = datetime.fromtimestamp(st.created_ts, tz=timezone.utc).astimezone(tz).date()
        if f.date_from and local_day < date.fromisoformat(f.date_from):
            return False
        if f.date_to and local_day > date.fromisoformat(f.date_to):
            return False
    if f.telegraph and st.path.casefold() != f.telegraph.casefold():
        return False
    if f.youtube:
        if st.youtube_video_id is None or video_ids.get(st.youtube_video_id) != f.youtube:
            return False
    if f.label and (not st.label or name_key(f.label) not in name_key(st.label)):
        return False
    if f.favorites and not st.is_favorite:
        return False
    return True


async def selected_ids(app: "App") -> set[int]:
    async with app.db() as session:
        return set((await session.execute(select(SelectedPublication.publication_id))).scalars().all())


async def query(app: "App", ctx: str, arg: int = 0, sort: str = "new") -> list[PubStats]:
    """Публикации для контекста списка, отсортированные."""
    if ctx in COMPETITOR_CTXS:
        stats = list((await app.analytics.get_competitors()).values())
        if ctx == "compc":
            stats = [s for s in stats if (s.competitor_id or 0) == arg]
        return sort_stats(stats, sort)
    stats = list((await app.analytics.get_all()).values())
    if ctx == "imp":
        stats = [s for s in stats if s.kind == "imported"]
    elif ctx == "author":
        stats = [s for s in stats if s.author_id == arg]
    elif ctx == "yt":
        stats = [s for s in stats if s.youtube_video_id == arg]
    elif ctx == "nolink":
        stats = [s for s in stats if s.youtube_video_id is None]
    elif ctx == "fav":
        stats = [s for s in stats if s.is_favorite]
    elif ctx == "batch":
        stats = [s for s in stats if s.batch_id == arg]
    elif ctx == "sel":
        chosen = await selected_ids(app)
        stats = [s for s in stats if s.id in chosen]
    elif ctx == "search":
        filters = await load_filters(app)
        videos = await video_map(app)
        stats = [s for s in stats if match_filters(s, filters, app.config.timezone, videos)]
    return sort_stats(stats, sort)


async def video_map(app: "App") -> dict[int, str]:
    async with app.db() as session:
        return {r.id: r.video_id for r in (await session.execute(select(YouTubeVideo.id, YouTubeVideo.video_id))).all()}


async def context_title(app: "App", ctx: str, arg: int) -> str:
    base = CTX_TITLES.get(ctx, "Публикации")
    async with app.db() as session:
        if ctx == "author":
            author = await session.get(Author, arg)
            return f"👤 {author.name}" if author else base
        if ctx == "yt":
            video = await session.get(YouTubeVideo, arg)
            if video:
                return f"🎬 {video.title or 'Видео ' + video.video_id}"
        if ctx == "batch":
            batch = await session.get(Batch, arg)
            if batch:
                return f"📦 Партия #{batch.id}: {batch.title}"
        if ctx == "compc":
            competitor = await session.get(Competitor, arg) if arg else None
            return f"🕵️ {competitor.name}" if competitor else "🕵️ Конкуренты без группы"
    return base


# ---------------------------------------------------------------------- изменения


async def set_favorite(app: "App", ids: list[int], value: bool) -> int:
    async with app.db() as session:
        res = await session.execute(update(Publication).where(Publication.id.in_(ids)).values(is_favorite=value))
        await session.commit()
    app.analytics.invalidate()
    return res.rowcount or 0


async def toggle_favorite(app: "App", pub_id: int) -> bool:
    async with app.db() as session:
        pub = await session.get(Publication, pub_id)
        if pub is None:
            raise LookupError("Публикация не найдена")
        pub.is_favorite = not pub.is_favorite
        value = pub.is_favorite
        await session.commit()
    app.analytics.invalidate()
    return value


async def set_label(app: "App", ids: list[int], label: str | None) -> int:
    async with app.db() as session:
        res = await session.execute(update(Publication).where(Publication.id.in_(ids)).values(label=label))
        await session.commit()
    app.analytics.invalidate()
    return res.rowcount or 0


async def delete_records(app: "App", ids: list[int]) -> int:
    """Удаляет записи ТОЛЬКО из внутренней базы. Страницы Telegraph не затрагиваются."""
    async with app.collector.lock:  # не пересекаться со сбором статистики
        async with app.db() as session:
            res = await session.execute(delete(Publication).where(Publication.id.in_(ids)))
            await session.commit()
    app.analytics.invalidate()
    logger.info("Удалены из базы записи публикаций: %s", ids)
    return res.rowcount or 0


async def toggle_selected(app: "App", pub_id: int) -> bool:
    async with app.db() as session:
        row = await session.get(SelectedPublication, pub_id)
        if row is None:
            if await session.get(Publication, pub_id) is None:
                raise LookupError("Публикация не найдена")
            session.add(SelectedPublication(publication_id=pub_id))
            selected = True
        else:
            await session.delete(row)
            selected = False
        await session.commit()
    return selected


async def add_selected(app: "App", ids: list[int]) -> int:
    current = await selected_ids(app)
    new = [i for i in ids if i not in current]
    if new:
        async with app.db() as session:
            existing = set((await session.execute(select(Publication.id).where(Publication.id.in_(new)))).scalars())
            for pid in new:
                if pid in existing:
                    session.add(SelectedPublication(publication_id=pid))
            await session.commit()
    return len(new)


async def clear_selected(app: "App") -> None:
    async with app.db() as session:
        await session.execute(delete(SelectedPublication))
        await session.commit()


async def author_pub_count(app: "App", names: list[str]) -> dict[str, int]:
    """Сколько публикаций уже было у каждого из авторов (по имени без учёта регистра)."""
    keys = {name_key(n): n for n in names}
    async with app.db() as session:
        rows = (
            await session.execute(
                select(Author.name_key, func.count(Publication.id))
                .join(Publication, (Publication.author_id == Author.id) & (Publication.kind != KIND_COMPETITOR),
                      isouter=True)
                .where(Author.name_key.in_(list(keys)))
                .group_by(Author.id)
            )
        ).all()
    return {keys[k]: int(c) for k, c in rows if k in keys}


async def find_by_telegraph(app: "App", text: str) -> Publication | None:
    path = parse_telegraph_path(text)
    if not path:
        return None
    async with app.db() as session:
        return (await session.execute(select(Publication).where(Publication.path == path))).scalar_one_or_none()


# ---------------------------------------------------------------------- редактирование Telegraph

EditProgress = Callable[[int, int], Awaitable[None]]
NOT_OWNED_TEXT = ("страница создана в другом аккаунте Telegraph — редактирование недоступно "
                  "(подключите токен этого аккаунта: «📥 Добавить готовые» → «🔑 Страницы другого аккаунта»)")


@dataclass
class EditResult:
    ok: list[int]
    failed: list[tuple[int, str]]


async def edit_in_telegraph(
    app: "App",
    ids: list[int],
    *,
    title: str | None = None,
    content: tuple[str, str] | None = None,  # (html, json)
    author: str | None = None,
    progress: EditProgress | None = None,
) -> EditResult:
    """Меняет страницы через editPage и обновляет базу. Просмотры при этом не сбрасываются."""
    result = EditResult(ok=[], failed=[])
    for n, pub_id in enumerate(ids, start=1):
        async with app.db() as session:
            pub = await session.get(Publication, pub_id)
            if pub is None:
                result.failed.append((pub_id, "не найдена"))
                continue
            if pub.kind == KIND_COMPETITOR:
                result.failed.append((pub_id, "чужая публикация (конкурент) — редактировать нельзя"))
                continue
            if pub.account_id is None:
                result.failed.append((pub_id, NOT_OWNED_TEXT))
                continue
            new_title = title or pub.title
            new_author = author or pub.author_name
            try:
                token = await app.accounts.token_for(pub.account_id)
                if content:
                    nodes = json.loads(content[1])
                else:
                    nodes = json.loads(pub.content_json or "[]")
                    if not nodes:
                        # добавленная готовая страница: текст в базе не хранится — берём текущий из Telegraph,
                        # чтобы смена заголовка или автора не стёрла содержимое
                        current = await app.telegraph.get_page(pub.path, return_content=True)
                        nodes = current.content or []
                        if not nodes:
                            raise RuntimeError("не удалось получить текущий текст страницы")
                        pub.content_json = json.dumps(nodes, ensure_ascii=False)
                page = await app.telegraph.edit_page(
                    token, pub.path, new_title, nodes, author_name=new_author,
                    author_url=app.config.telegraph_author_url,
                )
            except Exception as exc:  # noqa: BLE001
                result.failed.append((pub_id, describe_error(exc)))
                logger.warning("editPage %s не выполнен: %s", pub.path, exc)
            else:
                pub.title = page.title or new_title
                if content:
                    pub.content_html, pub.content_json = content
                if author and name_key(author) != name_key(pub.author_name):
                    new_author_row = await get_or_create_author(session, author)
                    pub.author_id = new_author_row.id
                pub.author_name = new_author
                await session.commit()
                result.ok.append(pub_id)
        if progress is not None:
            try:
                await progress(n, len(ids))
            except Exception:  # noqa: BLE001
                pass
    app.analytics.invalidate()
    return result
