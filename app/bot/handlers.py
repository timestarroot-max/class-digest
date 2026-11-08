"""Обработчики бота: команды, кнопки и тихий сбор сообщений чата."""

import asyncio
import logging
import re
from pathlib import Path

from maxapi import Bot, F, Router
from maxapi.context import MemoryContext, State, StatesGroup
from maxapi.enums.chat_type import ChatType
from maxapi.types import (
    BotAdded,
    BotRemoved,
    BotStarted,
    Command,
    CommandStart,
    InputMedia,
    MessageCallback,
    MessageCreated,
)
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from app.bot.keyboards import back_menu, main_menu, schedule_menu
from app.config import DEMO_CHAT_ID, DEMO_CHAT_TITLE, DATA_DIR, settings
from app.db.database import Database
from app.services import anonymizer, calendar_export, digest, llm_gigachat

router = Router()
logger = logging.getLogger(__name__)

# Текст приветствия для родителей — дословно из анкеты проекта.
WELCOME_TEXT = ('Здравствуйте! Я бот "Классный дайджест". Я помогаю учителю '
                'не пропускать важные сообщения: раз в день составляю для него '
                'короткую сводку. Личные данные (телефоны, номера карт, имена) '
                'я скрываю, а сообщения храню только до выдачи сводки и не '
                'дольше суток')

ERROR_TEXT = "⚠️ Что-то пошло не так, попробуйте ещё раз"

ALREADY_LINKED_TEXT = (
    "Этот чат уже подключён к классному руководителю — сводки приходят ему."
)

PRIVATE_GREETING = (
    "Здравствуйте! Я бот «Классный дайджест»: тихо слежу за чатом класса, "
    "прячу личные данные и присылаю вам короткую сводку.\n\n"
    "Выберите действие:"
)

HELP_TEXT = (
    "ℹ️ Как подключить «Классный дайджест»\n\n"
    "1️⃣ Откройте чат класса. В меню группы нажмите «Добавить участника».\n"
    "2️⃣ В списке найдите бота «96_Идея_фикс» или используйте поиск.\n"
    "3️⃣ Выберите бота и нажмите «Добавить в чат».\n"
    "4️⃣ В плашке «Показать историю чата новым участникам?» выберите «Показать».\n\n"
    "Дальше бот сам напишет в чат и подскажет, как выдать ему право "
    "«Читать сообщения» для ежедневных сводок.\n\n"
    "💡 Если при назначении оставить право «Удалять сообщения», бот будет "
    "сам убирать из чата свои служебные подсказки и ответы на команды — "
    "засоряться чат не будет."
)

ADMIN_HINT = (
    "🔑 Последний шаг — выдайте боту право читать сообщения:\n\n"
    "1️⃣ В меню группы нажмите «Администраторы».\n"
    "2️⃣ Нажмите «Добавить администратора» и выберите бота «96_Идея_фикс».\n"
    "3️⃣ Уберите лишние разрешения — «Читать сообщения» должно быть "
    "включено. Нажмите «Назначить администратором».\n"
    "MAX может держать часть прав включёнными принудительно — это нормально.\n\n"
    "Готово! Раз в день я буду присылать сводку этого чата в личные "
    "сообщения классному руководителю. Личные данные (телефоны, карты, "
    "имена) скрываются до сводки, сообщения хранятся не дольше суток.\n\n"
    "💡 Совет: если оставить боту право «Удалять сообщения», служебные "
    "сообщения (это напоминание, подсказки команд) будут исчезать из чата "
    "сами — родительская переписка останется чистой."
)

ADMIN_SUCCESS_DM = (
    "🎉 Готово! Меня назначили администратором чата «{title}» "
    "с правом «Читать сообщения» — подключение завершено.\n\n"
    "Что я умею:\n"
    "• 📊 Сводка за день — короткий дайджест по 4 темам одним нажатием;\n"
    "• ⏰ Расписание — автосводка каждый день в удобное время "
    "(по умолчанию 19:00);\n"
    "• 📅 Календарь (.ics) — важные даты класса одним файлом\n"
    "  в Яндекс/Google/Apple Календарь;\n"
    "• 🌐 Веб-панель — статистика, песочница обезличивания и демо;\n"
    "• 🧪 Демо (для жюри) — покажет работу бота за минуту.\n\n"
    "Сообщения я читаю только для сводки: телефоны, карты и имена "
    "прячу до отправки в ИИ, храню не дольше суток."
)

ADMIN_GROUP_DONE = (
    "✅ Права получены — я настроен и начинаю собирать сводку этого чата. "
    "Напоминание с инструкцией удалил, чтобы не мешать переписке."
)
# Если убрать напоминание не получилось — обещать это в тексте нельзя.
ADMIN_GROUP_DONE_KEEP = (
    "✅ Права получены — я настроен и начинаю собирать сводку этого чата."
)

ADMIN_LOST_DM = (
    "⚠️ Меня сняли с прав администратора в чате «{title}» — новые "
    "сообщения больше не попадают в сводку. Назначьте меня снова "
    "(«Администраторы» → «Добавить администратора», право "
    "«Читать сообщения»), и всё продолжит работать."
)

# В группе эти команды не работают, а подсказки не должны копиться.
GROUP_ONLY_TEXT = (
    "Эта команда работает только в личных сообщениях с ботом.\n"
    "Это сообщение исчезнет из чата через 30 секунд."
)
GROUP_ONLY_TEXT_NO_DELETE = (
    "Эта команда работает только в личных сообщениях с ботом.\n"
    "Если выдать боту право «Удалять сообщения», такие служебные "
    "сообщения будут исчезать из чата сами."
)
GROUP_ONLY_TTL = 30    # секунды: /demo и /digest в группе
GROUP_START_TTL = 120  # секунды: /start в группе (он же /connect)

# Время в формате ЧЧ:ММ: часы 00-23, минуты 00-59.
TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")

# Глобальные ссылки на бота и базу, заполняются в init().
_bot: Bot | None = None
_db: Database | None = None


def init(bot: Bot, db: Database) -> None:
    global _bot, _db
    _bot = bot
    _db = db
    logger.info("Хендлеры бота инициализированы")


def _extract_mid(sended) -> str | None:
    """id отправленного сообщения лежит в message.body.mid."""
    try:
        message = getattr(sended, "message", None)
        if message is None:
            message = sended
        body = getattr(message, "body", None)
        mid = getattr(body, "mid", None) if body is not None else None
        mid = mid or getattr(message, "mid", None)
        return str(mid) if mid else None
    except Exception:  # noqa: BLE001
        return None


def _message_mid(message) -> str | None:
    """mid входящего сообщения — пригодится, чтобы удалить саму команду."""
    try:
        body = getattr(message, "body", None)
        mid = getattr(body, "mid", None) if body is not None else None
        return str(mid) if mid else None
    except Exception:  # noqa: BLE001
        return None


async def _delete_later(mid: str, delay: int,
                       chat_id: int | None = None) -> None:
    """Убирает сообщение через delay секунд; при отказе снимает флаг права."""
    await asyncio.sleep(delay)
    try:
        await _bot.delete_message(mid)
        logger.info("Сообщение %s удалено из чата (таймер %s с)", mid, delay)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось удалить сообщение %s из чата: %s", mid, exc)
        if chat_id is not None and ("denied" in str(exc).lower()
                                    or "permission" in str(exc).lower()):
            # Право отозвали — обещать автоочистку больше нельзя.
            await _db.set_bot_delete_right(chat_id, False)


# Задачи удаления держим в ссылке: иначе asyncio может собрать их мусором.
_pending_deletions: set[asyncio.Task] = set()


def _schedule_delete(mid: str | None, delay: int,
                     chat_id: int | None = None) -> None:
    if not mid or _bot is None:
        return
    task = asyncio.create_task(_delete_later(str(mid), delay, chat_id))
    _pending_deletions.add(task)
    task.add_done_callback(_pending_deletions.discard)


def _is_group_message(message) -> bool:
    try:
        return message.recipient.chat_type == ChatType.CHAT
    except Exception:  # noqa: BLE001
        return False


async def _group_only_text(chat_id: int) -> str:
    """Текст подсказки: обещаем удаление только при выданном праве."""
    if await _db.get_bot_delete_right(chat_id):
        return GROUP_ONLY_TEXT
    return GROUP_ONLY_TEXT_NO_DELETE


async def _link_allowed(chat_id: int, user_id: int) -> bool:
    """Перепривязывать чат можно только его владельцу — иначе сводки с именами уведут."""
    owner = await _db.get_chat_owner(chat_id)
    return owner is None or owner == user_id


async def _cleanup_allowed(chat_id: int) -> bool:
    """Автоудаление включаем только при выданном праве «Удалять сообщения»."""
    return await _db.get_bot_delete_right(chat_id)


async def _probe_delete_right(chat_id: int, mid: str | None) -> bool:
    """Право удаления проверяем делом: в событии назначения MAX его не присылает.
    Пробуем убрать сообщение — получилось, значит право выдано."""
    if not mid:
        return False
    try:
        await _bot.delete_message(mid)
    except Exception as exc:  # noqa: BLE001 - права может не быть
        if "denied" in str(exc).lower() or "permission" in str(exc).lower():
            logger.info("Право удаления недоступно в чате %s (%s)", chat_id, exc)
            await _db.set_bot_delete_right(chat_id, False)
        else:
            logger.warning("Не удалось удалить сообщение %s: %s", mid, exc)
        return False
    logger.info("Право удаления подтверждено в чате %s", chat_id)
    await _db.set_bot_delete_right(chat_id, True)
    return True


async def _drop_admin_hint(chat_id: int) -> bool:
    """🔑-подсказка нужна только до назначения: убираем, как только бот станет админом."""
    mid = await _db.get_admin_hint_mid(chat_id)
    if not mid or not await _db.get_bot_admin_state(chat_id):
        return False
    if not await _probe_delete_right(chat_id, mid):
        return False
    await _db.set_admin_hint_mid(chat_id, None)
    return True


def _ready() -> bool:
    return _bot is not None and _db is not None


class Form(StatesGroup):

    waiting_time = State()


# --- Вспомогательные функции ---


async def _fetch_chat_title(event) -> str:
    try:
        chat = await event.fetch_chat()
    except Exception:
        chat = None
    if chat is not None:
        return chat.title or ""
    return ""


async def _notify_user(user_id: int, text: str) -> None:
    try:
        await _bot.send_message(user_id=user_id, text=text)
    except Exception:
        logger.exception("Не удалось отправить сообщение пользователю %s",
                         user_id)


async def _notify_chat(chat_id: int, text: str) -> None:
    try:
        await _bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        logger.exception("Не удалось отправить сообщение в чат %s", chat_id)


async def _answer_safely(event, text: str) -> None:
    try:
        if isinstance(event, MessageCallback):
            await _send_to_callback_user(event, text)
        else:
            await event.message.answer(text)
    except Exception:
        logger.exception("Не удалось сообщить пользователю об ошибке")


async def _send_to_callback_user(event: MessageCallback, text: str,
                                 builder: InlineKeyboardBuilder | None = None
                                 ) -> None:
    attachments = [builder.as_markup()] if builder is not None else None
    if event.message is not None:
        await event.send(text=text, attachments=attachments)
    else:
        await _bot.send_message(
            user_id=event.callback.user.user_id,
            text=text,
            attachments=attachments,
        )


async def _send_main_menu(user_id: int,
                          text: str = PRIVATE_GREETING) -> None:
    await _bot.send_message(
        user_id=user_id,
        text=text,
        attachments=[main_menu(await _panel_url(user_id)).as_markup()],
    )


async def _panel_url(user_id: int) -> str | None:
    """Ссылка на панель с личным ключом учителя; ключ создаётся при первом запросе."""
    url = settings.public_web_url
    if not url:
        return None
    try:
        key = await _db.get_or_create_web_key(user_id)
        sep = "&" if "?" in url else "?"
        return f"{url.rstrip('/')}/app{sep}k={key}"
    except Exception:
        logger.exception("Не удалось собрать личную ссылку на панель")
        return url


async def _schedule_menu_text(user_id: int) -> str:
    current = await _db.get_schedule_time(user_id)
    if not current:
        current = settings.default_digest_time
    return ("⏰ Расписание сводки\n\n"
            f"Сейчас сводка приходит в {current}.\n"
            "Выберите новое время:")


async def _send_digests_to_user(user_id: int) -> None:
    """Сводка по каждому чату учителя — отдельным сообщением, плюс демо-чат."""
    chat_ids = await _db.get_teacher_chats(user_id)
    sent = 0
    for chat_id in chat_ids:
        result = await digest.make_digest(_db, chat_id)
        if not result.text:
            continue
        await _bot.send_message(user_id=user_id, text=result.text)
        sent += 1

    # Демо-чат показываем тоже — сценарий «Тест для жюри» проходит
    # полностью через личные сообщения.
    if (DEMO_CHAT_ID not in chat_ids
            and await _db.count_messages(DEMO_CHAT_ID) > 0):
        result = await digest.make_digest(_db, DEMO_CHAT_ID)
        if result.text:
            await _bot.send_message(user_id=user_id, text=result.text)
            sent += 1

    if sent == 0:
        await _bot.send_message(
            user_id=user_id,
            text="В буфере пока нет сообщений. Добавьте бота в чат класса "
                 "или загрузите 🧪 демо-режим.",
        )


def _one_line(text: str, limit: int = 200) -> str:
    flat = " ".join(str(text).split())
    if len(flat) > limit:
        flat = flat[: limit - 1] + "…"
    return flat


def _demo_report(info: dict) -> str:
    lines = [
        "🧪 Демо-режим (Тест для жюри)",
        "",
        f"В демо-чат «{DEMO_CHAT_TITLE}» загружено {info.get('count', 0)} "
        "сообщений реалистичного родительского чата 3-го класса.",
        "",
        "Личные данные, которые мы спрятали до сводки:",
        f"• 📞 телефоны: {info.get('phones', 0)}",
        f"• 💳 банковские карты: {info.get('cards', 0)}",
        f"• 🪪 паспорта: {info.get('passports', 0)}",
        f"• 🔢 СНИЛС: {info.get('snils', 0)}",
        f"• ✉️ email: {info.get('emails', 0)}",
        f"• 👤 имена: {info.get('names', 0)}",
        f"Всего замаскировано фрагментов: {info.get('masked_total', 0)}",
    ]
    examples = info.get("examples") or []
    if examples:
        lines += ["", "Как это выглядит «было → стало»:"]
        for example in examples[:3]:
            lines += [
                "",
                f"До: {_one_line(example.get('original', ''))}",
                f"После: {_one_line(example.get('masked', ''))}",
            ]
    lines += ["", "Нажмите «📊 Сводка за день», чтобы получить готовый "
                  "дайджест без личных данных."]
    return "\n".join(lines)


def _events_word(count: int) -> str:
    """«1 событие», «2 события», «5 событий»."""
    tail = count % 100
    if 11 <= tail <= 14:
        return "событий"
    last = count % 10
    if last == 1:
        return "событие"
    if 2 <= last <= 4:
        return "события"
    return "событий"


async def _send_calendar_to_user(user_id: int) -> None:
    """События берём из базы, а не из текста сводки: модель может переврать даты."""
    events: list[dict] = []
    for chat_id in await _db.get_teacher_chats(user_id):
        events += await _db.get_events(chat_id)

    # Демо-чат участвует тоже — жюри проверяет сценарий целиком.
    events += await digest.get_demo_calendar_events(_db, DEMO_CHAT_ID)

    # Одинаковые события из разных чатов — оставляем по одному.
    unique: list[dict] = []
    seen: set = set()
    for event in events:
        key = (event.get("title"), str(event.get("date")), event.get("time"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(event)
    events = unique

    if not events:
        await _bot.send_message(
            user_id=user_id,
            text="📅 Пока не нашлось событий с датами. Соберите сводку — "
                 "и даты собраний и экскурсий попадут в календарь.",
        )
        return

    calendar_dir = Path(DATA_DIR)
    calendar_dir.mkdir(parents=True, exist_ok=True)
    ics_path = calendar_export.save_ics(
        events, str(calendar_dir / "class_events.ics")
    )
    await _bot.send_message(
        user_id=user_id,
        text=(f"📅 Календарь класса готов: {len(events)} "
              f"{_events_word(len(events))}. "
              "Файл .ics открывается в Яндекс.Календаре, Google Calendar "
              "и Apple Календаре."),
        attachments=[InputMedia(path=ics_path)],
    )


# --- События бота: добавили / начали / удалили ---


@router.bot_added()
async def on_bot_added(event: BotAdded) -> None:
    if not _ready():
        return
    try:
        title = await _fetch_chat_title(event)
        if await _link_allowed(event.chat_id, event.user.user_id):
            await _db.link_chat(event.chat_id, event.user.user_id, title)
        else:
            logger.info("Чат %s уже привязан — привязку не меняем", event.chat_id)
        await _bot.send_message(chat_id=event.chat_id, text=WELCOME_TEXT)
        # Запоминаем id гайда: после назначения админом бот его уберёт.
        hint = await _bot.send_message(
            chat_id=event.chat_id, text=ADMIN_HINT
        )
        mid = _extract_mid(hint)
        if mid:
            await _db.set_admin_hint_mid(event.chat_id, mid)
        logger.info("Чат %s («%s») привязан к пользователю %s",
                    event.chat_id, title, event.user.user_id)
    except Exception:
        logger.exception("Ошибка при добавлении бота в чат %s", event.chat_id)
        await _notify_chat(event.chat_id, ERROR_TEXT)


@router.bot_started()
async def on_bot_started(event: BotStarted) -> None:
    if not _ready():
        return
    # В группах запуск ведёт on_start_command, здесь только личка.
    if getattr(event, "chat_id", 0) and event.chat_id < 0:
        return
    try:
        await _send_main_menu(event.user.user_id)
    except Exception:
        logger.exception("Ошибка в bot_started (пользователь %s)",
                         event.user.user_id)
        await _notify_user(event.user.user_id, ERROR_TEXT)


@router.bot_removed()
async def on_bot_removed(event: BotRemoved) -> None:
    if not _ready():
        return
    try:
        await _db.unlink_chat(event.chat_id)
        logger.info("Чат %s отвязан (бота удалили из чата)", event.chat_id)
    except Exception:
        logger.exception("Ошибка при отвязке чата %s", event.chat_id)


async def on_admin_permissions_changed(event: dict) -> None:
    """Назначили админом — убираем гайд из чата и пишем учителю в ЛС; сняли права — предупреждаем владельца."""
    if not _ready():
        return

    chat_id = event.get("chat_id")
    if not chat_id:
        return

    try:
        was_admin = await _db.get_bot_admin_state(chat_id)
        is_admin = bool(event.get("is_admin"))
        title = await _db.get_chat_title(chat_id) or "чат класса"

        # MAX право удаления в событии не присылает, поэтому флаг только
        # поднимаем, а опускает его проверка делом.
        permissions = [str(item) for item in (event.get("permissions") or [])]
        if permissions:
            logger.info("Права бота в чате %s: %s", chat_id, permissions)
            if any("delete" in item.lower() for item in permissions):
                await _db.set_bot_delete_right(chat_id, True)

        if is_admin and not was_admin:
            await _db.set_bot_admin_state(chat_id, True)

            # Право удаления в событии не приходит — проверяем делом.
            hint_removed = await _drop_admin_hint(chat_id)

            # Короткое подтверждение в чат и подробности — в ЛС.
            try:
                await _bot.send_message(
                    chat_id=chat_id,
                    text=(ADMIN_GROUP_DONE if hint_removed
                          else ADMIN_GROUP_DONE_KEEP),
                )
            except Exception:  # noqa: BLE001
                logger.warning("Не удалось написать в чат %s", chat_id)

            user_id = event.get("user_id")
            if user_id:
                try:
                    await _bot.send_message(
                        user_id=user_id,
                        text=ADMIN_SUCCESS_DM.format(title=title),
                        attachments=[
                            main_menu(await _panel_url(user_id)).as_markup()
                        ],
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Не удалось отправить ЛС пользователю %s", user_id
                    )
            logger.info("Бот назначен администратором чата %s", chat_id)

        elif not is_admin and was_admin:
            await _db.set_bot_admin_state(chat_id, False)
            owner = await _db.get_chat_owner(chat_id)
            if owner:
                try:
                    await _bot.send_message(
                        user_id=owner,
                        text=ADMIN_LOST_DM.format(title=title),
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("Не удалось предупредить владельца чата")
            logger.info("Бот снят с прав администратора чата %s", chat_id)

        # Если состояние не изменилось (переключали другие права) —
        # ничего не делаем, чтобы не спамить уведомлениями.
    except Exception:
        logger.exception("Ошибка при смене прав администратора в чате %s",
                         chat_id)


async def _connect_in_group(event: MessageCreated, ttl: int | None = None) -> None:
    """Привязка чата класса: приветствие и гайд. ttl — через сколько
    секунд убрать за собой (None — оставить сообщения)."""
    message = event.message
    chat_id = message.recipient.chat_id
    sender = message.sender
    if chat_id is None or sender is None:
        return
    title = await _fetch_chat_title(event)
    if not await _link_allowed(chat_id, sender.user_id):
        await message.answer(ALREADY_LINKED_TEXT)
        logger.info(
            "Чат %s уже привязан к другому учителю — привязку не меняем", chat_id
        )
        return
    await _db.link_chat(chat_id, sender.user_id, title)

    mids: list[str | None] = [_message_mid(message)] if ttl is not None else []

    welcome = await message.answer(WELCOME_TEXT)
    mids.append(_extract_mid(welcome))

    # Приветствие из анкеты оставляем дословно, гайд шлём отдельно.
    if await _db.get_bot_admin_state(chat_id):
        note = await message.answer(
            "Права администратора уже выданы — продолжаю работу. "
            "Сводка придёт в личные сообщения по расписанию."
        )
        mids.append(_extract_mid(note))
    else:
        hint = await message.answer(ADMIN_HINT)
        mid = _extract_mid(hint)
        mids.append(mid)
        if mid:
            await _db.set_admin_hint_mid(chat_id, mid)

    if ttl is not None:
        allowed = await _cleanup_allowed(chat_id)
        if not allowed:
            # Право в событии не приходит — проверяем делом, убрав команду.
            allowed = await _probe_delete_right(chat_id, mids[0])
            if allowed:
                mids = mids[1:]
        if allowed:
            for mid in mids:
                _schedule_delete(mid, ttl, chat_id)

    await _drop_admin_hint(chat_id)
    logger.info("Чат %s («%s») привязан к %s", chat_id, title, sender.user_id)


# --- Команды ---


@router.message_created(CommandStart())
async def on_start_command(event: MessageCreated) -> None:
    if not _ready():
        return
    try:
        message = event.message
        sender = message.sender
        if sender is None:
            return
        # В группе /start = /connect, только с уборкой через 2 минуты.
        if _is_group_message(message):
            await _connect_in_group(event, ttl=GROUP_START_TTL)
            return
        await message.answer(
            text=PRIVATE_GREETING,
            attachments=[main_menu(await _panel_url(sender.user_id)).as_markup()],
        )
    except Exception:
        logger.exception("Ошибка в команде /start")
        await _answer_safely(event, ERROR_TEXT)


@router.message_created(Command("connect"))
async def on_connect_command(event: MessageCreated) -> None:
    if not _ready():
        return
    try:
        if not _is_group_message(event.message):
            await event.message.answer(
                "Команда работает в групповом чате класса. "
                "Добавьте меня туда и напишите /connect."
            )
            return
        await _connect_in_group(event)
    except Exception:
        logger.exception("Ошибка в команде /connect")
        await _answer_safely(event, ERROR_TEXT)


@router.message_created(Command("digest"))
async def on_digest_command(event: MessageCreated) -> None:
    if not _ready():
        return
    try:
        message = event.message
        sender = message.sender
        if sender is None:
            return
        # В группе сводка не собирается: она приходит только учителю в ЛС.
        if _is_group_message(message):
            chat_id = message.recipient.chat_id
            allowed = await _cleanup_allowed(chat_id)
            command_gone = False
            if not allowed:
                command_gone = await _probe_delete_right(
                    chat_id, _message_mid(message)
                )
                allowed = command_gone
            reply = await message.answer(await _group_only_text(chat_id))
            if allowed:
                _schedule_delete(_extract_mid(reply), GROUP_ONLY_TTL, chat_id)
                if not command_gone:
                    _schedule_delete(_message_mid(message), GROUP_ONLY_TTL,
                                     chat_id)
            await _drop_admin_hint(chat_id)
            logger.info("Команда /digest в группе — подсказка про личные сообщения")
            return
        await _send_digests_to_user(sender.user_id)
    except Exception:
        logger.exception("Ошибка в команде /digest")
        await _answer_safely(event, ERROR_TEXT)


@router.message_created(Command("demo"))
async def on_demo_command(event: MessageCreated) -> None:
    if not _ready():
        return
    try:
        message = event.message
        sender = message.sender
        if sender is None:
            return
        # В группе демо не показываем: пары «До/После» — зрелище для учителя.
        if _is_group_message(message):
            chat_id = message.recipient.chat_id
            allowed = await _cleanup_allowed(chat_id)
            command_gone = False
            if not allowed:
                command_gone = await _probe_delete_right(
                    chat_id, _message_mid(message)
                )
                allowed = command_gone
            reply = await message.answer(await _group_only_text(chat_id))
            if allowed:
                _schedule_delete(_extract_mid(reply), GROUP_ONLY_TTL, chat_id)
                if not command_gone:
                    _schedule_delete(_message_mid(message), GROUP_ONLY_TTL,
                                     chat_id)
            await _drop_admin_hint(chat_id)
            logger.info("Команда /demo в группе — подсказка про личные сообщения")
            return
        info = await digest.load_demo_messages(_db, DEMO_CHAT_ID)
        # Кнопки меню обязательны: жюри сразу жмёт «📊 Сводка за день».
        await message.answer(
            text=_demo_report(info),
            attachments=[main_menu(await _panel_url(sender.user_id)).as_markup()],
        )
    except Exception:
        logger.exception("Ошибка в команде /demo")
        await _answer_safely(event, ERROR_TEXT)


@router.message_created(Command("schedule"))
async def on_schedule_command(event: MessageCreated) -> None:
    if not _ready():
        return
    try:
        sender = event.message.sender
        if sender is None:
            return
        await event.message.answer(
            text=await _schedule_menu_text(sender.user_id),
            attachments=[schedule_menu().as_markup()],
        )
    except Exception:
        logger.exception("Ошибка в команде /schedule")
        await _answer_safely(event, ERROR_TEXT)


# --- Нажатия на инлайн-кнопки ---


@router.message_callback()
async def on_callback(event: MessageCallback,
                      context: MemoryContext) -> None:
    if not _ready():
        return
    try:
        payload = event.callback.payload or ""
        user_id = event.callback.user.user_id

        if payload == "menu:digest":
            await event.answer()
            await _send_digests_to_user(user_id)

        elif payload == "menu:schedule":
            await event.answer()
            await _send_to_callback_user(
                event,
                await _schedule_menu_text(user_id),
                schedule_menu(),
            )

        elif payload == "sch:custom":
            await event.answer()
            await context.set_state(Form.waiting_time)
            await _send_to_callback_user(
                event, "Напишите время в формате ЧЧ:ММ, например 19:30."
            )

        elif payload.startswith("sch:"):
            await event.answer()
            await _set_schedule_time(event, user_id, payload[len("sch:"):])

        elif payload == "menu:back":
            await event.answer()
            await _send_main_menu(user_id)

        elif payload == "menu:help":
            await event.answer()
            await _send_to_callback_user(event, HELP_TEXT)

        elif payload == "menu:demo":
            await event.answer()
            info = await digest.load_demo_messages(_db, DEMO_CHAT_ID)
            await _send_to_callback_user(
                event, _demo_report(info),
                main_menu(await _panel_url(user_id)),
            )

        elif payload == "menu:calendar":
            await event.answer()
            await _send_calendar_to_user(user_id)

        else:
            # Неизвестная кнопка — просто снимаем «часики».
            await event.answer()

    except Exception:
        logger.exception("Ошибка при обработке кнопки %r",
                         event.callback.payload)
        await _answer_safely(event, ERROR_TEXT)


async def _set_schedule_time(event: MessageCallback, user_id: int,
                             chosen_time: str) -> None:
    if not TIME_RE.fullmatch(chosen_time):
        await _send_to_callback_user(
            event, "Не поняли время. Выберите кнопку или введите ЧЧ:ММ."
        )
        return
    await _db.set_schedule_time(user_id, chosen_time)
    await _send_to_callback_user(
        event,
        f"✅ Расписание установлено: {chosen_time}\n"
        "Каждый день в это время буду присылать сводку сюда.",
        back_menu(),
    )


# --- FSM: ввод своего времени ---


@router.message_created(Form.waiting_time, F.message.body.text)
async def on_custom_time(event: MessageCreated,
                         context: MemoryContext) -> None:
    if not _ready():
        return
    try:
        sender = event.message.sender
        text = (event.message.body.text or "").strip()
        if sender is None:
            return

        if text.startswith("/"):
            # Передумали и ввели команду — выходим из режима ввода.
            await context.clear()
            await event.message.answer(
                text="Настройка времени отменена.",
                attachments=[main_menu(await _panel_url(sender.user_id)).as_markup()],
            )
            return

        if not TIME_RE.fullmatch(text):
            await event.message.answer(
                "Не похоже на время. Напишите его в формате ЧЧ:ММ, "
                "например 19:30."
            )
            return

        await _db.set_schedule_time(sender.user_id, text)
        await context.clear()
        await event.message.answer(
            text=(f"✅ Расписание установлено: {text}\n"
                  "Каждый день в это время буду присылать сводку сюда."),
            attachments=[main_menu(await _panel_url(sender.user_id)).as_markup()],
        )
    except Exception:
        logger.exception("Ошибка при вводе своего времени")
        await _answer_safely(event, ERROR_TEXT)


# --- Тихий сборщик сообщений группового чата ---


@router.message_created(F.message.body.text, None)
async def collect_chat_message(event: MessageCreated) -> None:
    """Собираем сообщения привязанных чатов в буфер и не отвечаем в чат."""
    if not _ready():
        return
    try:
        message = event.message
        sender = message.sender
        text = (message.body.text or "").strip()
        if not text or sender is None or sender.is_bot:
            return
        if text.startswith("/"):
            return
        if message.recipient.chat_type != ChatType.CHAT:
            return
        chat_id = message.recipient.chat_id
        if chat_id is None:
            return
        if await _db.get_chat_owner(chat_id) is None:
            # Чат не привязан к боту — работать не с кем.
            return

        # Карта маркеров едет в буфер вместе с текстом: по ней сводка
        # вернёт учителю настоящие имена, а после выдачи всё сотрётся.
        name_map: dict = {}
        masked_sender = anonymizer.mask_person_name(
            sender.full_name, name_map=name_map
        )
        masked_text, stats = anonymizer.mask_text(text, name_map=name_map)
        # В буфере и в модели один и тот же текст: диагнозы прячем сразу.
        masked_text = llm_gigachat.mask_medical_details(masked_text)
        await _db.add_message(
            chat_id, masked_sender, masked_text,
            masked=int(stats.total), name_map=name_map or None,
        )
        await _db.bump_stat("messages_processed", 1)
        # Ничего не отвечаем — бот в чате класса работает тихо.
    except Exception:
        logger.exception("Не удалось сохранить сообщение в буфер")
