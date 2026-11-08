"""Фоновые задачи: раз в минуту смотрим, кому пора автосводку, и раз в час чистим буфер от сообщений старше суток."""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.db.database import Database

logger = logging.getLogger(__name__)

# Учителя выбирают время по Москве, а сервер может стоять в другом поясе.
_MOSCOW = ZoneInfo("Europe/Moscow")


def create_scheduler(db: Database, bot=None) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        send_scheduled_digests,
        trigger="interval",
        seconds=60,
        args=[db, bot],
        id="send_scheduled_digests",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        cleanup_job,
        trigger="interval",
        minutes=60,
        args=[db],
        id="cleanup_job",
        max_instances=1,
        coalesce=True,
    )
    return scheduler


async def send_scheduled_digests(db: Database, bot=None) -> None:
    """Сверяем время с московскими часами и рассылаем сводки. bot=None — проверка без отправки: только лог."""
    try:
        now_time = datetime.now(_MOSCOW).strftime("%H:%M")
        schedules = await db.get_all_schedules()

        for item in schedules:
            try:
                user_id = item.get("user_id")
                planned_time = item.get("time")
                if not user_id or not planned_time:
                    continue
                if planned_time != now_time:
                    continue

                chat_ids = await db.get_teacher_chats(user_id)
                if not chat_ids:
                    logger.debug(
                        "Пора отправлять сводку пользователю %s, "
                        "но привязанных чатов нет", user_id,
                    )
                    continue

                if bot is None:
                    # make_digest сразу чистит буфер, поэтому без бота его не зовём.
                    logger.info(
                        "[тест] Пользователю %s пора отправить сводки по "
                        "%s чатам (бот не задан — только лог)",
                        user_id, len(chat_ids),
                    )
                    continue

                # Ленивый импорт: digest тяжёлый, при старте модуля он не нужен.
                from app.services import digest

                for chat_id in chat_ids:
                    result = await digest.make_digest(db, chat_id)
                    if not result.text:
                        # В буфере не было сообщений — пропускаем тихо.
                        continue
                    await bot.send_message(
                        user_id=user_id, text=result.text
                    )
                    logger.info(
                        "Автосводка чата %s отправлена пользователю %s",
                        chat_id, user_id,
                    )
            except Exception:
                logger.exception(
                    "Не удалось отправить автосводку пользователю %s",
                    item.get("user_id"),
                )
    except Exception:
        logger.exception("Ошибка в задаче send_scheduled_digests")


async def cleanup_job(db: Database) -> None:
    try:
        removed = await db.cleanup_old_messages(hours=24)
        old_digests = await db.cleanup_old_digests(hours=24)
        if old_digests:
            logger.info("Цифровая гигиена: удалено %s старых сводок", old_digests)
        if removed:
            logger.info(
                "Цифровая гигиена: удалено %s сообщений старше суток",
                removed,
            )
    except Exception:
        logger.exception("Ошибка в задаче cleanup_job")
