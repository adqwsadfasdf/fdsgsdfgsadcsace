"""Отслеживание готовых публикаций: свои (созданные не ботом) и чужие (конкуренты).

Как и везде в боте, используется ТОЛЬКО официальный API (api.telegra.ph):
* getPage (без содержимого) — один раз при добавлении: заголовок, автор, текущий счётчик;
* getViews — счётчик просмотров (в т.ч. по годам, чтобы уточнить год создания страницы);
* getPageList — список страниц своего аккаунта (проверка, что страница ваша и её можно редактировать).
Сами страницы telegra.ph не открываются, поэтому добавление и отслеживание не создают просмотров.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable

from sqlalchemy import delete, func, select

from database import (
    KIND_COMPETITOR,
    KIND_IMPORTED,
    Competitor,
    Publication,
    TelegraphAccount,
    ViewSnapshot,
    get_or_create_author,
)
from services.telegraph import Page, TelegraphAPIError, describe_error
from utils.validators import ValidationError, clean_line, name_key, parse_telegraph_path

if TYPE_CHECKING:
    from app_context import App

logger = logging.getLogger(__name__)

MAX_LINKS = 500
COMPETITOR_NAME_MAX = 64
NO_AUTHOR = "Без автора"
_PATH_DATE_RE = re.compile(r"-(\d{2})-(\d{2})(?:-\d+)?$")
_SPLIT_RE = re.compile(r"[\s,;]+")
OWNER_SCAN_PAGES = 40  # максимум запросов getPageList (по 200 страниц) на один аккаунт

Progress = Callable[[int, int], Awaitable[None]]


# ---------------------------------------------------------------------- разбор ввода


@dataclass
class LinksParseResult:
    paths: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    duplicates: int = 0


def parse_links(text: str, limit: int = MAX_LINKS) -> LinksParseResult:
    """Ссылки Telegraph из текста: по одной в строке (допускаются пробелы, запятые, нумерация)."""
    result = LinksParseResult()
    seen: set[str] = set()
    for token in _SPLIT_RE.split(text or ""):
        token = token.strip().strip("()[]<>\"'«»")
        if not token or re.fullmatch(r"\d{1,3}[.)]?|[-•*·–—]+", token):
            continue
        path = parse_telegraph_path(token)
        if not path:
            result.invalid.append(token[:60])
            continue
        if path in seen:
            result.duplicates += 1
            continue
        seen.add(path)
        result.paths.append(path)
    if len(result.paths) > limit:
        raise ValidationError(f"Слишком много ссылок: {len(result.paths)}. За один раз — не больше {limit}.")
    return result


def validate_competitor_name(text: str) -> str:
    name = clean_line(text or "")
    if not name:
        raise ValidationError("Название не может быть пустым.")
    if len(name) > COMPETITOR_NAME_MAX:
        raise ValidationError(f"Название слишком длинное (максимум {COMPETITOR_NAME_MAX} символов).")
    return name


# ---------------------------------------------------------------------- дата создания


def date_from_path(path: str, today: date | None = None) -> date | None:
    """Telegraph дописывает к адресу дату создания «-ММ-ДД» (без года). Берём последнюю такую дату ≤ сегодня."""
    match = _PATH_DATE_RE.search(path)
    if not match:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    today = today or datetime.now(timezone.utc).date()
    for year in range(today.year, today.year - 9, -1):
        try:
            candidate = date(year, month, day)
        except ValueError:  # 29.02 в невисокосный год или мусор в адресе
            continue
        if candidate <= today:
            return candidate
    return None


async def estimate_created(app: "App", path: str, total_views: int) -> tuple[datetime, bool]:
    """(дата создания, точная ли). Год уточняется через getViews по годам: до года создания просмотров нет."""
    now = datetime.now(timezone.utc)
    guess = date_from_path(path, now.date())
    if guess is None:
        return now, False
    year = guess.year
    if total_views > 0:
        try:
            for y in range(guess.year - 1, guess.year - 6, -1):
                if await app.telegraph.get_views(path, year=y) > 0:
                    year = y  # в этом году просмотры уже были — страница старше
                else:
                    break
        except Exception as exc:  # noqa: BLE001 — дата не критична
            logger.info("Не удалось уточнить год создания %s: %s", path, describe_error(exc))
    try:
        created = datetime(year, guess.month, guess.day, tzinfo=timezone.utc)
    except ValueError:
        created = datetime(guess.year, guess.month, guess.day, tzinfo=timezone.utc)
    return min(created, now), True


# ---------------------------------------------------------------------- владелец страницы


async def owned_paths(app: "App") -> dict[str, int]:
    """path → id аккаунта для всех страниц подключённых аккаунтов Telegraph."""
    result: dict[str, int] = {}
    async with app.db() as session:
        accounts = (await session.execute(select(TelegraphAccount.id, TelegraphAccount.access_token))).all()
    for account_id, token in accounts:
        offset = 0
        for _ in range(OWNER_SCAN_PAGES):
            try:
                page_list = await app.telegraph.get_page_list(token, offset=offset, limit=200)
            except Exception as exc:  # noqa: BLE001
                logger.warning("getPageList аккаунта #%s не получен: %s", account_id, describe_error(exc))
                break
            for page in page_list.pages:
                result.setdefault(page.path, account_id)
            offset += len(page_list.pages)
            if not page_list.pages or offset >= page_list.total_count:
                break
    return result


# ---------------------------------------------------------------------- добавление


@dataclass
class TrackResult:
    added: list[tuple[int, str, int]] = field(default_factory=list)  # (id, заголовок, просмотров)
    moved: list[tuple[int, str]] = field(default_factory=list)  # перенесены из конкурентов в свои
    existing_own: list[str] = field(default_factory=list)
    existing_comp: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    not_owned: int = 0  # свои, но в аккаунте, токена которого у бота нет (не редактируются)

    @property
    def new_ids(self) -> list[int]:
        return [i for i, _, _ in self.added] + [i for i, _ in self.moved]


async def _record(app: "App", page: Page, kind: str, *, account_id: int | None,
                  competitor_id: int | None) -> int:
    created_at, _ = await estimate_created(app, page.path, int(page.views or 0))
    now = datetime.now(timezone.utc)
    author_name = clean_line(page.author_name or "")[:128] or NO_AUTHOR
    async with app.db() as session:
        existing = (await session.execute(select(Publication.id).where(Publication.path == page.path))).scalar()
        if existing is not None:
            return int(existing)
        author = await get_or_create_author(session, author_name)
        pub = Publication(
            kind=kind, account_id=account_id, competitor_id=competitor_id,
            title=(clean_line(page.title) or page.path)[:256], author_id=author.id, author_name=author_name,
            content_html="", content_json="[]", url=page.url, path=page.path,
            created_at=created_at, views=int(page.views or 0), views_updated_at=now, tracked_since=now,
        )
        session.add(pub)
        await session.flush()
        # Точка отсчёта: прирост считается только с момента добавления (раньше бот не следил)
        session.add(ViewSnapshot(publication_id=pub.id, taken_at=int(now.timestamp()),
                                 views=int(page.views or 0), delta=0))
        await session.commit()
        return pub.id


async def track_paths(app: "App", paths: list[str], *, competitor: bool, competitor_id: int | None = None,
                      progress: Progress | None = None) -> TrackResult:
    """Добавляет страницы по адресам: свои (competitor=False) или конкурента (competitor=True)."""
    result = TrackResult()
    async with app.db() as session:
        rows = (await session.execute(
            select(Publication.id, Publication.path, Publication.kind).where(Publication.path.in_(paths))
        )).all()
    known = {r.path: (r.id, r.kind) for r in rows}
    owners: dict[str, int] | None = None
    for n, path in enumerate(paths, start=1):
        try:
            if path in known:
                pub_id, kind = known[path]
                if kind != KIND_COMPETITOR:
                    result.existing_own.append(path)
                elif competitor:
                    result.existing_comp.append(path)
                else:
                    # была в конкурентах, а на самом деле своя — переносим, история просмотров сохраняется
                    if owners is None:
                        owners = await owned_paths(app)
                    async with app.db() as session:
                        pub = await session.get(Publication, pub_id)
                        pub.kind, pub.competitor_id = KIND_IMPORTED, None
                        pub.account_id = owners.get(path)
                        title = pub.title
                        await session.commit()
                    result.not_owned += path not in owners
                    result.moved.append((pub_id, title))
                continue
            try:
                page = await app.telegraph.get_page(path, return_content=False)
            except TelegraphAPIError as exc:
                result.failed.append((path, describe_error(exc)))
                continue
            if page.path != path and page.path in known:
                result.existing_own.append(path)
                continue
            account_id = None
            if not competitor:
                if owners is None:
                    owners = await owned_paths(app)
                account_id = owners.get(page.path)
                result.not_owned += account_id is None
            pub_id = await _record(app, page, KIND_COMPETITOR if competitor else KIND_IMPORTED,
                                   account_id=account_id, competitor_id=competitor_id if competitor else None)
            known[page.path] = (pub_id, KIND_COMPETITOR if competitor else KIND_IMPORTED)
            result.added.append((pub_id, page.title, int(page.views or 0)))
        except Exception as exc:  # noqa: BLE001 — одна ссылка не должна ломать весь список
            logger.exception("Ошибка добавления %s", path)
            result.failed.append((path, describe_error(exc)))
        finally:
            if progress is not None:
                try:
                    await progress(n, len(paths))
                except Exception:  # noqa: BLE001
                    pass
    app.analytics.invalidate()
    _after_add(app, result)
    return result


async def import_account(app: "App", account_id: int, progress: Progress | None = None) -> TrackResult:
    """Добавляет все страницы аккаунта Telegraph, которых ещё нет в базе (getPageList)."""
    result = TrackResult()
    async with app.db() as session:
        account = await session.get(TelegraphAccount, account_id)
        if account is None:
            raise LookupError("Аккаунт не найден")
        token = account.access_token
        known = {r.path: r.kind for r in (await session.execute(select(Publication.path, Publication.kind))).all()}
    pages: list[Page] = []
    offset = 0
    for _ in range(OWNER_SCAN_PAGES):
        page_list = await app.telegraph.get_page_list(token, offset=offset, limit=200)
        pages += page_list.pages
        offset += len(page_list.pages)
        if not page_list.pages or offset >= page_list.total_count:
            break
    for n, page in enumerate(pages, start=1):
        try:
            kind = known.get(page.path)
            if kind is not None and kind != KIND_COMPETITOR:
                result.existing_own.append(page.path)
            elif kind == KIND_COMPETITOR:
                async with app.db() as session:
                    pub = (await session.execute(select(Publication).where(Publication.path == page.path))).scalar_one()
                    pub.kind, pub.competitor_id, pub.account_id = KIND_IMPORTED, None, account_id
                    await session.commit()
                    result.moved.append((pub.id, pub.title))
            else:
                pub_id = await _record(app, page, KIND_IMPORTED, account_id=account_id, competitor_id=None)
                result.added.append((pub_id, page.title, int(page.views or 0)))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка добавления %s", page.path)
            result.failed.append((page.path, describe_error(exc)))
        finally:
            if progress is not None:
                try:
                    await progress(n, len(pages))
                except Exception:  # noqa: BLE001
                    pass
    app.analytics.invalidate()
    _after_add(app, result)
    return result


def _after_add(app: "App", result: TrackResult) -> None:
    """В фоне подгружаем фактические просмотры по дням за последние 30 дней (getViews с датой)."""
    ids = [i for i, _, _ in result.added]
    if ids:
        app.spawn(app.collector.sync_daily(ids), name="daily-after-import")


# ---------------------------------------------------------------------- конкуренты


@dataclass
class CompetitorStats:
    id: int  # 0 — публикации без группы
    name: str
    count: int = 0
    total: int = 0
    g24: int = 0
    g7: int = 0
    partial24: bool = False
    partial7: bool = False
    best_id: int | None = None
    best_title: str = ""
    best_views: int = 0

    @property
    def avg(self) -> float:
        return self.total / self.count if self.count else 0.0


async def get_or_create_competitor(app: "App", name: str) -> Competitor:
    name = validate_competitor_name(name)
    async with app.db() as session:
        row = (await session.execute(select(Competitor).where(Competitor.name_key == name_key(name)))).scalar()
        if row is None:
            row = Competitor(name=name, name_key=name_key(name))
            session.add(row)
            await session.commit()
        return row


async def rename_competitor(app: "App", competitor_id: int, name: str) -> None:
    name = validate_competitor_name(name)
    async with app.db() as session:
        other = (await session.execute(select(Competitor).where(Competitor.name_key == name_key(name)))).scalar()
        if other is not None and other.id != competitor_id:
            raise ValidationError(f"Конкурент «{other.name}» уже есть.")
        row = await session.get(Competitor, competitor_id)
        if row is None:
            raise LookupError("Конкурент не найден")
        row.name, row.name_key = name, name_key(name)
        await session.commit()
    app.analytics.invalidate()


async def delete_competitor(app: "App", competitor_id: int) -> int:
    """Удаляет конкурента и все его публикации ИЗ БАЗЫ бота (страницы Telegraph не затрагиваются)."""
    async with app.collector.lock:
        async with app.db() as session:
            if competitor_id:
                cond = Publication.competitor_id == competitor_id
            else:
                cond = Publication.competitor_id.is_(None)
            res = await session.execute(delete(Publication).where(Publication.kind == KIND_COMPETITOR).where(cond))
            if competitor_id:
                await session.execute(delete(Competitor).where(Competitor.id == competitor_id))
            await session.commit()
    app.analytics.invalidate()
    return res.rowcount or 0


async def move_to_competitor(app: "App", pub_id: int, competitor_id: int | None) -> None:
    async with app.db() as session:
        pub = await session.get(Publication, pub_id)
        if pub is None or pub.kind != KIND_COMPETITOR:
            raise LookupError("Публикация конкурента не найдена")
        pub.competitor_id = competitor_id or None
        await session.commit()
    app.analytics.invalidate()


async def competitor_name(app: "App", competitor_id: int | None) -> str:
    if not competitor_id:
        return "без группы"
    async with app.db() as session:
        row = await session.get(Competitor, competitor_id)
    return row.name if row else "без группы"


async def competitor_stats(app: "App") -> list[CompetitorStats]:
    """Сводка по каждому конкуренту (включая пустых) + «без группы», если такие публикации есть."""
    async with app.db() as session:
        rows = (await session.execute(select(Competitor.id, Competitor.name).order_by(func.lower(Competitor.name)))).all()
    by_id: dict[int, CompetitorStats] = {r.id: CompetitorStats(id=r.id, name=r.name) for r in rows}
    for st in (await app.analytics.get_competitors()).values():
        cid = st.competitor_id or 0
        if cid not in by_id:
            by_id[cid] = CompetitorStats(id=cid, name="Без группы")
        c = by_id[cid]
        c.count += 1
        c.total += st.total
        c.g24 += st.g24.value
        c.g7 += st.g7.value
        c.partial24 |= st.g24.quality not in {"exact", "future"}
        c.partial7 |= st.g7.quality not in {"exact", "future"}
        if c.best_id is None or st.total > c.best_views:
            c.best_id, c.best_title, c.best_views = st.id, st.title, st.total
    return list(by_id.values())
