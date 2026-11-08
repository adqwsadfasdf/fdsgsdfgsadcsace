"""Управление аккаунтами Telegraph (createAccount / getAccountInfo)."""
from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database import TelegraphAccount
from services.telegraph import Account, TelegraphAPIError, TelegraphClient

logger = logging.getLogger(__name__)


def mask_token(token: str) -> str:
    return token[:4] + "…" + token[-4:] if len(token) > 10 else "…"


class AccountManager:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], client: TelegraphClient,
                 short_name: str, env_token: str | None):
        self._sessionmaker = sessionmaker
        self._client = client
        self._short_name = short_name
        self._env_token = env_token

    async def ensure_account(self) -> TelegraphAccount:
        """Активный аккаунт: токен из .env или автоматически созданный через createAccount."""
        async with self._sessionmaker() as session:
            if self._env_token:
                account = (
                    await session.execute(
                        select(TelegraphAccount).where(TelegraphAccount.access_token == self._env_token)
                    )
                ).scalar_one_or_none()
                if account is None:
                    info = await self._safe_info(self._env_token)
                    account = TelegraphAccount(
                        short_name=(info.short_name if info else self._short_name) or self._short_name,
                        author_name=info.author_name if info else None,
                        access_token=self._env_token,
                    )
                    session.add(account)
                    await session.flush()
                await self._activate(session, account.id)
                await session.commit()
                return account

            account = (
                await session.execute(select(TelegraphAccount).where(TelegraphAccount.is_active.is_(True)))
            ).scalar_one_or_none()
            if account is not None:
                return account
        return await self.create_new_account()

    async def _safe_info(self, token: str) -> Account | None:
        try:
            return await self._client.get_account_info(token, ["short_name", "author_name", "page_count"])
        except TelegraphAPIError as exc:
            logger.error("Токен TELEGRAPH_TOKEN не принят Telegraph: %s", exc.code)
            raise
        except Exception as exc:  # noqa: BLE001 — сеть может быть недоступна при старте
            logger.warning("Не удалось проверить токен Telegraph (%s), продолжаем", exc)
            return None

    @staticmethod
    async def _activate(session: AsyncSession, account_id: int) -> None:
        await session.execute(update(TelegraphAccount).values(is_active=False))
        await session.execute(
            update(TelegraphAccount).where(TelegraphAccount.id == account_id).values(is_active=True)
        )

    async def create_new_account(self) -> TelegraphAccount:
        created = await self._client.create_account(self._short_name)
        async with self._sessionmaker() as session:
            account = TelegraphAccount(
                short_name=created.short_name or self._short_name,
                author_name=created.author_name,
                access_token=created.access_token or "",
            )
            session.add(account)
            await session.flush()
            await self._activate(session, account.id)
            await session.commit()
            logger.info(
                "Создан аккаунт Telegraph %s (токен %s сохранён в БД)",
                account.short_name, mask_token(account.access_token),
            )
            return account

    async def add_existing(self, token: str) -> tuple[TelegraphAccount, Account, bool]:
        """Подключает существующий аккаунт Telegraph по access_token (для импорта и редактирования его страниц).

        Активный аккаунт (для новых публикаций) не меняется. Возвращает (аккаунт, данные API, новый ли).
        """
        token = (token or "").strip()
        if not token or len(token) > 128 or any(ch.isspace() for ch in token):
            raise TelegraphAPIError("ACCESS_TOKEN_INVALID", "ACCESS_TOKEN_INVALID")
        info = await self._client.get_account_info(token, ["short_name", "author_name", "page_count"])
        async with self._sessionmaker() as session:
            account = (
                await session.execute(select(TelegraphAccount).where(TelegraphAccount.access_token == token))
            ).scalar_one_or_none()
            if account is not None:
                return account, info, False
            account = TelegraphAccount(
                short_name=info.short_name or self._short_name, author_name=info.author_name,
                access_token=token, is_active=False,
            )
            session.add(account)
            await session.commit()
            logger.info("Подключён аккаунт Telegraph %s (токен %s)", account.short_name, mask_token(token))
            return account, info, True

    async def list_accounts(self) -> list[TelegraphAccount]:
        async with self._sessionmaker() as session:
            return list((await session.execute(select(TelegraphAccount).order_by(TelegraphAccount.id))).scalars())

    async def active(self) -> TelegraphAccount:
        async with self._sessionmaker() as session:
            account = (
                await session.execute(select(TelegraphAccount).where(TelegraphAccount.is_active.is_(True)))
            ).scalar_one_or_none()
        if account is None:
            account = await self.ensure_account()
        return account

    async def token_for(self, account_id: int | None) -> str:
        if account_id is not None:
            async with self._sessionmaker() as session:
                account = await session.get(TelegraphAccount, account_id)
            if account is not None:
                return account.access_token
        return (await self.active()).access_token

    async def info(self) -> tuple[TelegraphAccount, Account | None, str | None]:
        account = await self.active()
        try:
            info = await self._client.get_account_info(account.access_token)
            return account, info, None
        except Exception as exc:  # noqa: BLE001
            return account, None, str(exc)
