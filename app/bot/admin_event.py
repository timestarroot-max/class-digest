"""Ловим событие «бот назначен администратором»: maxapi 1.2.3 его не разбирает."""

import logging

logger = logging.getLogger(__name__)

# Так это событие называется в MAX Bot API (см. схему BotAdminPermissionsChangedUpdate).
ADMIN_UPDATE_TYPE = "bot_admin_permissions_changed"

_installed = False


def install_admin_hook(handler) -> None:
    """handler получает словарь события: {chat_id, user_id, is_admin, ...}."""
    global _installed
    if _installed:
        return

    import maxapi.dispatcher as dp_module

    original = dp_module.process_update_request

    async def wrapper(events, bot):
        try:
            batch = events.get("updates") or []
            rest = []
            for event in batch:
                if (isinstance(event, dict)
                        and event.get("update_type") == ADMIN_UPDATE_TYPE):
                    logger.info(
                        "Событие прав администратора: чат %s, is_admin=%s",
                        event.get("chat_id"), event.get("is_admin"),
                    )
                    try:
                        await handler(event)
                    except Exception:  # noqa: BLE001 - событие не должно валить polling
                        logger.exception("Ошибка в обработчике прав администратора")
                else:
                    rest.append(event)
            if len(rest) != len(batch):
                events = dict(events, updates=rest)
        except Exception:  # noqa: BLE001 - обёртка не должна ломать polling
            logger.exception("Ошибка в обёртке разбора апдейтов")
        return await original(events, bot)

    dp_module.process_update_request = wrapper
    _installed = True
    logger.info("Перехватчик bot_admin_permissions_changed установлен")
