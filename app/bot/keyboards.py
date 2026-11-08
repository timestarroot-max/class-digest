"""Клавиатуры бота: главное меню, меню расписания, кнопка назад."""

from maxapi.types.attachments.buttons import CallbackButton, LinkButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder


def _is_shareable_url(url: str | None) -> bool:
    """MAX не принимает localhost в кнопках-ссылках, такие адреса показываем только текстом."""
    if not url:
        return False
    low = url.lower()
    if not low.startswith(("http://", "https://")):
        return False
    return "localhost" not in low and "127.0.0.1" not in low


def main_menu(web_url: str | None = None) -> InlineKeyboardBuilder:
    """Подписи короткие: длинные MAX обрезает, когда в ряду две кнопки."""
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="📊 Сводка за день", payload="menu:digest"),
        CallbackButton(text="⏰ Расписание", payload="menu:schedule"),
    )
    builder.row(
        CallbackButton(text="📅 Календарь (.ics)", payload="menu:calendar"),
        CallbackButton(text="🧪 Демо (для жюри)", payload="menu:demo"),
    )
    if web_url and _is_shareable_url(web_url):
        builder.row(LinkButton(text="🌐 Веб-панель учителя", url=web_url))
    builder.row(CallbackButton(text="ℹ️ Инструкция", payload="menu:help"))
    return builder


def schedule_menu() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for pair in (("14:00", "15:00"), ("16:00", "17:00"),
                 ("18:00", "19:00"), ("20:00", "21:00")):
        builder.row(
            CallbackButton(text=pair[0], payload=f"sch:{pair[0]}"),
            CallbackButton(text=pair[1], payload=f"sch:{pair[1]}"),
        )
    builder.row(
        CallbackButton(text="✍️ Ввести своё время", payload="sch:custom")
    )
    builder.row(CallbackButton(text="⬅️ Назад", payload="menu:back"))
    return builder


def back_menu() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="⬅️ В меню", payload="menu:back"))
    return builder
