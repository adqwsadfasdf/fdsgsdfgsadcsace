"""Подключение всех роутеров в правильном порядке."""
from __future__ import annotations

from aiogram import Router


def setup_routers() -> Router:
    from handlers import (
        authors,
        create_posts,
        export,
        mass,
        publications,
        search,
        settings,
        start,
        statistics,
        tracking,
        youtube_groups,
    )

    root = Router(name="root")
    # start — первым: кнопки главного меню и /cancel работают из любого состояния
    root.include_routers(
        start.router,
        create_posts.router,
        tracking.router,
        youtube_groups.router,
        publications.router,
        mass.router,
        statistics.router,
        authors.router,
        search.router,
        export.router,
        settings.router,
        start.fallback_router,  # последним
    )
    return root
