"""Точка входа: python bot.py"""
from __future__ import annotations

import asyncio
import logging
import sys

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, ErrorEvent

from app_context import App
from config import Config, ConfigError, load_config
from database import Setting, create_engine, create_sessionmaker, init_db
from handlers import setup_routers
from middlewares.access import AccessMiddleware
from scheduler.tasks import setup_scheduler
from services.accounts import AccountManager
from services.analytics import Analytics
from services.batch_creator import BatchCreator
from services.notifications import Notifier
from services.settings_store import SettingsStore
from services.statistics_collector import StatisticsCollector
from services.telegraph import TelegraphClient
from utils.fsm_storage import SQLiteStorage
from utils.logger import setup_logging

logger = logging.getLogger("bot")

ANALYTICS_KEYS = {
    "snapshot_interval_minutes", "status_min_age_hours", "status_min_views", "status_growth_ratio",
    "status_decline_ratio", "dead_views_24h", "trending_min_views_24h",
}


async def build_app(config: Config) -> App:
    engine = create_engine(config.database_path)
    db = create_sessionmaker(engine)
    await init_db(engine, db)

    settings = SettingsStore(db)
    await settings.load()
    async with db() as session:
        if await session.get(Setting, "snapshot_interval_minutes") is None:
            # значение из .env используется, пока интервал не изменён в настройках бота
            await settings.set("snapshot_interval_minutes", config.default_snapshot_interval)

    telegraph = TelegraphClient(
        api_url=config.telegraph_api_url,
        timeout=config.telegraph_timeout,
        min_interval=config.telegraph_min_interval,
        max_retries=config.telegraph_max_retries,
        concurrency=config.telegraph_concurrency,
    )
    await telegraph.start()
    accounts = AccountManager(db, telegraph, config.telegraph_short_name, config.telegraph_token)
    analytics = Analytics(db, settings)
    collector = StatisticsCollector(db, telegraph, analytics, settings)
    creator = BatchCreator(db, telegraph, accounts, analytics, settings, author_url=config.telegraph_author_url)

    app = App(
        config=config, engine=engine, db=db, settings=settings, telegraph=telegraph, accounts=accounts,
        analytics=analytics, collector=collector, creator=creator,
        http=aiohttp.ClientSession(headers={"User-Agent": "TelegraphStatsBot/1.0"}),
    )
    app.notifier = Notifier(app)

    async def on_change(key: str, _value) -> None:
        if key in ANALYTICS_KEYS:
            analytics.invalidate()

    settings.on_change(on_change)
    return app


async def on_error(event: ErrorEvent, app: App) -> bool:
    logger.exception("Необработанная ошибка при обработке обновления", exc_info=event.exception)
    update = event.update
    try:
        if update.callback_query is not None:
            await update.callback_query.answer("❗ Произошла ошибка. Подробности записаны в лог.", show_alert=True)
        elif update.message is not None:
            await update.message.answer("❗ Произошла ошибка. Подробности записаны в лог. Попробуйте ещё раз.")
    except Exception:  # noqa: BLE001
        pass
    return True


async def notify_recovered(app: App, bot: Bot) -> None:
    from handlers.create_posts import format_report, report_kb
    from utils.formatting import split_message

    try:
        reports = await app.creator.recover_on_startup()
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка восстановления прерванных партий")
        return
    for report in reports:
        text = "♻️ <b>Создание партии было прервано перезапуском бота.</b>\n\n" + format_report(report)
        chunks = split_message(text)
        for admin_id in app.config.admin_ids:
            try:
                for chunk in chunks[:-1]:
                    await bot.send_message(admin_id, chunk)
                await bot.send_message(admin_id, chunks[-1], reply_markup=report_kb(report))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Не удалось отправить отчёт о восстановлении: %s", exc)


async def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    setup_logging(config.log_level, config.log_dir)
    logger.info("Запуск бота. База: %s, часовой пояс: %s", config.database_path, config.tz_name)

    app = await build_app(config)
    bot = Bot(
        token=config.bot_token,
        # Предпросмотр ссылок отключён: Telegram не будет загружать страницы telegra.ph,
        # и проверка/показ статистики не создаёт искусственных просмотров.
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    app.bot = bot

    try:
        account = await app.accounts.ensure_account()
        logger.info("Аккаунт Telegraph: %s", account.short_name)
    except Exception as exc:  # noqa: BLE001
        logger.error("Аккаунт Telegraph не готов (%s). Создание публикаций будет недоступно до устранения.", exc)

    dp = Dispatcher(storage=SQLiteStorage(app.db))
    dp.update.outer_middleware(AccessMiddleware(config.admin_ids))
    dp.include_router(setup_routers())
    dp.errors.register(on_error)

    scheduler = setup_scheduler(app)
    app.scheduler = scheduler

    async def on_startup() -> None:
        await bot.set_my_commands([
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="menu", description="Главное меню"),
            BotCommand(command="cancel", description="Отменить текущее действие"),
        ])
        scheduler.start()
        app.spawn(notify_recovered(app, bot), name="recover-batches")
        logger.info("Бот запущен")

    dp.startup.register(on_startup)

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, app=app, allowed_updates=dp.resolve_used_update_types(),
                               handle_signals=True)
    finally:
        logger.info("Остановка…")
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await app.shutdown_tasks()
        await app.telegraph.close()
        if app.http is not None:
            await app.http.close()
        await bot.session.close()
        await app.engine.dispose()
        logger.info("Бот остановлен")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
