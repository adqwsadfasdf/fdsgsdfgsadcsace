"""Модели базы данных (SQLite + SQLAlchemy 2.x, асинхронно через aiosqlite)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    select,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

# Тип записи публикации
KIND_CREATED = "created"  # создана ботом
KIND_IMPORTED = "imported"  # своя, добавлена готовой ссылкой
KIND_COMPETITOR = "competitor"  # чужая публикация, только отслеживание просмотров
OWN_KINDS = (KIND_CREATED, KIND_IMPORTED)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Хранит дату/время в UTC (naive в БД), возвращает timezone-aware datetime."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- служебные


class Setting(Base):
    """Настройки, изменяемые пользователем из бота (значения в JSON)."""

    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class AppState(Base):
    """Внутреннее состояние приложения (лидеры, фильтры поиска, время сбора и т.п.)."""

    __tablename__ = "app_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class TelegraphAccount(Base):
    __tablename__ = "telegraph_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    short_name: Mapped[str] = mapped_column(String(64), nullable=False)
    author_name: Mapped[str | None] = mapped_column(String(128))
    access_token: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------- справочники


class Author(Base):
    __tablename__ = "authors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    name_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class YouTubeVideo(Base):
    __tablename__ = "youtube_videos"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    url: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class Competitor(Base):
    """Конкурент (канал, проект, человек) — группа чужих публикаций для отслеживания."""

    __tablename__ = "competitors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    name_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------- партии


class Batch(Base):
    """Партия массового создания публикаций (для защиты от дублей и восстановления)."""

    __tablename__ = "batches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    content_html: Mapped[str] = mapped_column(Text, nullable=False)
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("telegraph_accounts.id", ondelete="SET NULL"))
    # draft | running | completed | partial | cancelled | interrupted
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class BatchItem(Base):
    __tablename__ = "batch_items"
    __table_args__ = (UniqueConstraint("batch_id", "position", name="uq_batch_item_pos"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    author_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # pending | creating | uncertain | created | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    publication_id: Mapped[int | None] = mapped_column(ForeignKey("publications.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False)


# --------------------------------------------------------------------------- публикации


class Publication(Base):
    __tablename__ = "publications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id", ondelete="SET NULL"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("telegraph_accounts.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    author_id: Mapped[int] = mapped_column(ForeignKey("authors.id"), nullable=False, index=True)
    author_name: Mapped[str] = mapped_column(String(128), nullable=False)
    content_html: Mapped[str] = mapped_column(Text, nullable=False)
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    path: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False, index=True)
    views: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    views_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    youtube_video_id: Mapped[int | None] = mapped_column(
        ForeignKey("youtube_videos.id", ondelete="SET NULL"), index=True
    )
    is_favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    label: Mapped[str | None] = mapped_column(String(64))
    # unknown | new | growing | stable | declining
    activity_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    status_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sync_error: Mapped[str | None] = mapped_column(String(256))
    # created | imported | competitor
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default=KIND_CREATED,
                                      server_default=KIND_CREATED, index=True)
    competitor_id: Mapped[int | None] = mapped_column(
        ForeignKey("competitors.id", ondelete="SET NULL"), index=True
    )
    # Когда бот начал следить за счётчиком (для добавленных готовых и чужих публикаций)
    tracked_since: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ViewSnapshot(Base):
    """Снимок общего счётчика просмотров (getViews без даты) в момент taken_at (unix-время)."""

    __tablename__ = "view_snapshots"
    __table_args__ = (Index("ix_snap_pub_time", "publication_id", "taken_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publication_id: Mapped[int] = mapped_column(ForeignKey("publications.id", ondelete="CASCADE"), nullable=False)
    taken_at: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    views: Mapped[int] = mapped_column(Integer, nullable=False)
    # Разница с предыдущим снимком. Отрицательное значение = счётчик уменьшился (корректировка Telegraph).
    delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class DailyView(Base):
    """Фактическое количество просмотров за календарные сутки (getViews с year/month/day)."""

    __tablename__ = "daily_views"
    publication_id: Mapped[int] = mapped_column(
        ForeignKey("publications.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD
    views: Mapped[int] = mapped_column(Integer, nullable=False)
    is_final: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fetched_at: Mapped[int] = mapped_column(Integer, nullable=False)


class SelectedPublication(Base):
    """Текущий набор выбранных публикаций для массовых операций (переживает перезапуск)."""

    __tablename__ = "selected_publications"
    publication_id: Mapped[int] = mapped_column(
        ForeignKey("publications.id", ondelete="CASCADE"), primary_key=True
    )
    selected_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class NotificationLog(Base):
    __tablename__ = "notification_log"
    __table_args__ = (Index("ix_notif_kind_entity", "kind", "entity", "sent_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    entity: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[int | None] = mapped_column(Integer)
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------- движок


def create_engine(path: Path) -> AsyncEngine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    return engine


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


_MIGRATION_COLUMNS = {
    # таблица: [(колонка, SQL-определение)]
    "publications": [
        ("kind", "VARCHAR(16) NOT NULL DEFAULT 'created'"),
        ("competitor_id", "INTEGER REFERENCES competitors(id) ON DELETE SET NULL"),
        ("tracked_since", "DATETIME"),
    ],
}
_MIGRATION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_publications_kind ON publications (kind)",
    "CREATE INDEX IF NOT EXISTS ix_publications_competitor_id ON publications (competitor_id)",
]


def _migrate(sync_conn) -> list[str]:  # noqa: ANN001
    """Добавляет недостающие колонки в базу, созданную старой версией бота (данные сохраняются)."""
    added = []
    for table, columns in _MIGRATION_COLUMNS.items():
        existing = {row[1] for row in sync_conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}
        for name, ddl in columns:
            if name not in existing:
                sync_conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                added.append(f"{table}.{name}")
    for ddl in _MIGRATION_INDEXES:
        sync_conn.exec_driver_sql(ddl)
    return added


async def init_db(engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as session:
        try:
            state = await session.get(AppState, "schema_version")
        except Exception:  # noqa: BLE001 — таблицы ещё нет (новая база)
            state = None
        if state is not None and int(state.value) > SCHEMA_VERSION:
            raise RuntimeError(
                f"База данных создана более новой версией бота (схема {state.value}). Обновите код проекта."
            )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)  # создаёт новые таблицы (в т.ч. competitors)
        added = await conn.run_sync(_migrate)
    if added:
        logger.info("Обновлена структура базы: добавлены колонки %s", ", ".join(added))
    async with sessionmaker() as session:
        state = await session.get(AppState, "schema_version")
        if state is None:
            session.add(AppState(key="schema_version", value=str(SCHEMA_VERSION)))
        else:
            state.value = str(SCHEMA_VERSION)
        await session.commit()
    logger.info("База данных готова")


async def get_or_create_author(session: AsyncSession, name: str) -> Author:
    from utils.validators import name_key

    key = name_key(name)
    author = (await session.execute(select(Author).where(Author.name_key == key))).scalar_one_or_none()
    if author is None:
        author = Author(name=name, name_key=key)
        session.add(author)
        await session.flush()
    else:
        author.last_used_at = utcnow()
    return author
