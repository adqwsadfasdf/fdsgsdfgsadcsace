"""Хранилище состояний FSM в SQLite: незавершённый мастер создания переживает перезапуск бота."""
from __future__ import annotations

import json
from typing import Any, Mapping

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database import AppState


def _key(key: StorageKey) -> str:
    return f"fsm:{key.bot_id}:{key.chat_id}:{key.user_id}:{key.thread_id or 0}:{key.destiny}"


class SQLiteStorage(BaseStorage):
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]):
        self._sessionmaker = sessionmaker

    async def _load(self, key: StorageKey) -> dict[str, Any]:
        async with self._sessionmaker() as session:
            row = await session.get(AppState, _key(key))
        if row is None:
            return {"state": None, "data": {}}
        try:
            return json.loads(row.value)
        except json.JSONDecodeError:
            return {"state": None, "data": {}}

    async def _save(self, key: StorageKey, payload: dict[str, Any]) -> None:
        async with self._sessionmaker() as session:
            row = await session.get(AppState, _key(key))
            empty = payload.get("state") is None and not payload.get("data")
            if empty:
                if row is not None:
                    await session.delete(row)
            else:
                text = json.dumps(payload, ensure_ascii=False)
                if row is None:
                    session.add(AppState(key=_key(key), value=text))
                else:
                    row.value = text
            await session.commit()

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        payload = await self._load(key)
        payload["state"] = state.state if isinstance(state, State) else state
        await self._save(key, payload)

    async def get_state(self, key: StorageKey) -> str | None:
        return (await self._load(key)).get("state")

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        payload = await self._load(key)
        payload["data"] = dict(data)
        await self._save(key, payload)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return dict((await self._load(key)).get("data") or {})

    async def close(self) -> None:
        return None
