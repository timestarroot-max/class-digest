"""Точка входа: бот MAX, планировщик и веб-панель в одном процессе."""

import asyncio
import logging
import os
from pathlib import Path

import uvicorn
from maxapi import Bot, Dispatcher

from app.config import DATA_DIR, has_real_bot_token, settings
from app.db.database import Database
from app.web import dashboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("class_digest")


# Задача сервера: держим ссылку, чтобы её не собрал сборщик мусора.
_web_task: asyncio.Task | None = None


async def run_web_panel(db: Database, done_event: asyncio.Event) -> uvicorn.Server:
    """Поднимает панель учителя в фоне на порту из настроек."""

    global _web_task
    app = dashboard.create_app(db)
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=settings.web_port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # сигналы обрабатывает главный цикл
    _web_task = asyncio.get_running_loop().create_task(
        _serve_and_signal(server, done_event)
    )
    return server


async def _serve_and_signal(server: uvicorn.Server, done_event: asyncio.Event) -> None:

    try:
        await server.serve()
    finally:
        done_event.set()


async def start_bot(db: Database) -> None:
    """Ошибки бота не должны ронять панель: ловим и продолжаем без него."""

    from app.services.scheduler import create_scheduler

    bot = None
    dp = None
    scheduler = None
    try:
        bot = Bot(token=settings.max_bot_token)
        me = await bot.get_me()
        dashboard.set_bot_status(True, me.username)
        logger.info("Бот @%s подключён к MAX Bot API", me.username)

        # Снимаем старые webhook-подписки: они блокируют long polling.
        await bot.delete_webhook()

        from app.bot import admin_event, handlers

        dp = Dispatcher()
        handlers.init(bot, db)
        dp.include_routers(handlers.router)

        # Это событие maxapi 1.2.3 не разбирает — ловим сами.
        admin_event.install_admin_hook(handlers.on_admin_permissions_changed)

        scheduler = create_scheduler(db, bot)
        scheduler.start()
        logger.info("Планировщик сводок запущен")

        await dp.start_polling(bot)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - бот не должен валить панель
        logger.exception("Не удалось запустить бота MAX: %s", exc)
        dashboard.set_bot_status(False, None)
        logger.warning("Веб-панель продолжает работать без бота")
        # Держим процесс живым: панель и планировщик полезны сами по себе.
        await asyncio.Event().wait()
    finally:
        if scheduler is not None:
            try:
                scheduler.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
        if bot is not None:
            try:
                await bot.close_session()
            except Exception:  # noqa: BLE001
                pass


async def main() -> None:

    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    logger.info(
        "Старт «Классного дайджеста» (веб-панель: http://0.0.0.0:%s)",
        settings.web_port,
    )

    db = Database(settings.database_path)
    await db.init()

    web_done = asyncio.Event()
    server = await run_web_panel(db, web_done)
    logger.info("Веб-панель учителя поднята на порту %s", settings.web_port)

    try:
        if has_real_bot_token():
            await start_bot(db)
        else:
            dashboard.set_bot_status(False, None)
            logger.warning(
                "MAX_BOT_TOKEN не задан — работаем в режиме веб-панели "
                "без бота (это нормально для локальной проверки)"
            )
            from app.services.scheduler import create_scheduler

            scheduler = create_scheduler(db, None)
            scheduler.start()
            await web_done.wait()
    finally:
        logger.info("Останавливаемся: закрываем базу и веб-панель")
        server.should_exit = True
        try:
            await asyncio.wait_for(web_done.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем")
