"""Доступ только для ADMIN_ID. Все остальные получают отказ, обработчики не вызываются."""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update, User

logger = logging.getLogger(__name__)

DENIED_TEXT = "⛔ Доступ запрещён. Этот бот предназначен только для личного использования владельцем."


class AccessMiddleware(BaseMiddleware):
    def __init__(self, admin_ids: frozenset[int]):
        self.admin_ids = admin_ids
        self._last_denied: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is not None and user.id in self.admin_ids:
            return await handler(event, data)

        if isinstance(event, Update):
            uid = user.id if user else 0
            logger.warning("Отклонён доступ: user_id=%s username=%s", uid, getattr(user, "username", None))
            try:
                if event.callback_query is not None:
                    # на нажатие кнопки отвечаем всегда, иначе у пользователя «висит» индикатор
                    await event.callback_query.answer(DENIED_TEXT, show_alert=True)
                elif event.message is not None:
                    # на сообщения — не чаще раза в 10 с (защита от спама)
                    now = time.monotonic()
                    if now - self._last_denied.get(uid, 0) >= 10:
                        self._last_denied[uid] = now
                        await event.message.answer(DENIED_TEXT)
            except Exception:  # noqa: BLE001
                pass
        return None
