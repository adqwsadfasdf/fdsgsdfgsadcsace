"""YouTube-ссылки: разбор, нормализация и группировка публикаций.

YouTube-ссылка хранится ТОЛЬКО во внутренней базе бота и никогда не добавляется
в текст публикации Telegraph.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import aiohttp
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database import Publication, YouTubeVideo

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtube-nocookie.com", "www.youtube-nocookie.com",
}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
_PATH_PREFIXES = ("shorts", "embed", "live", "v", "e")


def parse_youtube(text: str | None, allow_bare_id: bool = True) -> str | None:
    """Возвращает 11-символьный ID видео или None."""
    text = (text or "").strip()
    if not text:
        return None
    if allow_bare_id and _ID_RE.fullmatch(text):
        return text
    candidate = text if "://" in text else f"https://{text}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    video_id: str | None = None
    if host in _SHORT_HOSTS:
        video_id = parts[0] if parts else None
    elif host in _HOSTS:
        if parts and parts[0] == "watch":
            video_id = (parse_qs(parsed.query).get("v") or [None])[0]
        elif len(parts) >= 2 and parts[0] in _PATH_PREFIXES:
            video_id = parts[1]
        else:
            video_id = (parse_qs(parsed.query).get("v") or [None])[0]
    if video_id and _ID_RE.fullmatch(video_id):
        return video_id
    return None


def canonical_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


async def fetch_title(http: aiohttp.ClientSession | None, video_id: str) -> str | None:
    """Название видео через публичный oEmbed YouTube (без API-ключа). Ошибки не критичны."""
    if http is None:
        return None
    try:
        async with http.get(
            "https://www.youtube.com/oembed",
            params={"url": canonical_url(video_id), "format": "json"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            title = (data or {}).get("title")
            return str(title)[:256] if title else None
    except Exception as exc:  # noqa: BLE001 — название необязательно
        logger.info("Не удалось получить название YouTube %s: %s", video_id, exc)
        return None


async def get_or_create_video(
    session: AsyncSession, video_id: str, http: aiohttp.ClientSession | None, fetch_titles: bool
) -> YouTubeVideo:
    video = (
        await session.execute(select(YouTubeVideo).where(YouTubeVideo.video_id == video_id))
    ).scalar_one_or_none()
    if video is None:
        title = await fetch_title(http, video_id) if fetch_titles else None
        video = YouTubeVideo(video_id=video_id, url=canonical_url(video_id), title=title)
        session.add(video)
        await session.flush()
    return video


async def bind(session: AsyncSession, pub_ids: list[int], video: YouTubeVideo) -> int:
    if not pub_ids:
        return 0
    result = await session.execute(
        update(Publication).where(Publication.id.in_(pub_ids)).values(youtube_video_id=video.id)
    )
    return result.rowcount or 0


async def unbind(session: AsyncSession, pub_ids: list[int]) -> int:
    if not pub_ids:
        return 0
    result = await session.execute(
        update(Publication).where(Publication.id.in_(pub_ids)).values(youtube_video_id=None)
    )
    return result.rowcount or 0


async def change_group_video(
    session: AsyncSession, group: YouTubeVideo, new_video_id: str,
    http: aiohttp.ClientSession | None, fetch_titles: bool,
) -> tuple[YouTubeVideo, bool]:
    """Меняет ссылку группы. Если такое видео уже есть — группы объединяются. Возвращает (группа, merged)."""
    if new_video_id == group.video_id:
        return group, False
    existing = (
        await session.execute(select(YouTubeVideo).where(YouTubeVideo.video_id == new_video_id))
    ).scalar_one_or_none()
    if existing is not None:
        await session.execute(
            update(Publication)
            .where(Publication.youtube_video_id == group.id)
            .values(youtube_video_id=existing.id)
        )
        await session.delete(group)
        return existing, True
    group.video_id = new_video_id
    group.url = canonical_url(new_video_id)
    group.title = await fetch_title(http, new_video_id) if fetch_titles else None
    return group, False


@dataclass(slots=True)
class GroupInfo:
    id: int
    video_id: str
    url: str
    title: str | None
    count: int

    @property
    def display(self) -> str:
        return self.title or f"Видео {self.video_id}"


async def list_groups(session: AsyncSession) -> list[GroupInfo]:
    rows = (
        await session.execute(
            select(YouTubeVideo, func.count(Publication.id))
            .join(Publication, Publication.youtube_video_id == YouTubeVideo.id, isouter=True)
            .group_by(YouTubeVideo.id)
            .order_by(YouTubeVideo.created_at.desc())
        )
    ).all()
    return [GroupInfo(v.id, v.video_id, v.url, v.title, int(c)) for v, c in rows]


async def cleanup_empty_groups(session: AsyncSession) -> int:
    """Удаляет группы без публикаций."""
    groups = await list_groups(session)
    removed = 0
    for g in groups:
        if g.count == 0:
            video = await session.get(YouTubeVideo, g.id)
            if video is not None:
                await session.delete(video)
                removed += 1
    return removed
