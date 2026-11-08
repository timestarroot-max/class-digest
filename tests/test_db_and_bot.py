"""Тесты базы, планировщика и клавиатур. Бот и модуль digest подменяются фейками, реальные сообщения в MAX не отправляются."""

import os
import sys
import types
from datetime import datetime

# pytest видит только каталог tests/, поэтому корень проекта добавляем сами.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from maxapi.enums.chat_type import ChatType
from maxapi.types.attachments.buttons import CallbackButton, LinkButton

from app.bot.keyboards import back_menu, main_menu, schedule_menu
from app.config import DEMO_CHAT_ID
from app.db.database import Database
from app.services.scheduler import (
    cleanup_job,
    create_scheduler,
    send_scheduled_digests,
)


# Вспомогательные функции тестов


async def _make_db(tmp_path) -> Database:
    database = Database(str(tmp_path / "test.db"))
    await database.init()
    return database


def _flatten(builder):
    return [button for row in builder.payload for button in row]


class _FixedClock:
    """Часы, которые всегда показывают 19:00."""

    @staticmethod
    def now(tz=None):
        return datetime(2025, 10, 15, 19, 0)


class _FakeBot:

    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)


def _install_fake_digest(monkeypatch, make_digest) -> list:
    """Подменяет модуль digest фейком и возвращает список chat_id вызовов."""
    calls: list[int] = []

    async def fake_make_digest(db_arg, chat_id):
        calls.append(chat_id)
        return make_digest(chat_id)

    fake_digest = types.ModuleType("app.services.digest")
    fake_digest.make_digest = fake_make_digest
    monkeypatch.setitem(sys.modules, "app.services.digest", fake_digest)
    # Планировщик берёт digest из пакета app.services, поэтому подменяем и там.
    import app.services as services_pkg
    monkeypatch.setattr(services_pkg, "digest", fake_digest, raising=False)
    return calls


# Инициализация базы


async def test_init_creates_tables_and_wal(tmp_path):
    """init создаёт таблицы и включает режим WAL."""
    db = await _make_db(tmp_path)
    async with aiosqlite.connect(db.path) as conn:
        cur = await conn.cursor()
        await cur.execute("PRAGMA journal_mode")
        assert (await cur.fetchone())[0] == "wal"
        await cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in await cur.fetchall()}
        await cur.close()
    assert {"chats", "messages_buffer", "digests_history", "stats"} <= tables
    # close безопасен и не мешает дальнейшей работе.
    await db.close()
    await db.bump_stat("ping")


# Буфер сообщений


async def test_buffer_add_get_count_clear(tmp_path):
    """add/get/count/clear буфера: порядок, поля, лимит."""
    db = await _make_db(tmp_path)
    assert await db.count_messages(10) == 0
    assert await db.get_messages(10) == []

    await db.add_message(10, "О. С.", "первое", masked=2)
    await db.add_message(10, "М. И.", "второе")
    await db.add_message(11, "А. Б.", "другой чат")

    assert await db.count_messages(10) == 2
    assert await db.count_messages(11) == 1

    messages = await db.get_messages(10)
    # По возрастанию id.
    assert [m["text"] for m in messages] == ["первое", "второе"]
    first = messages[0]
    assert set(first) == {
        "id", "chat_id", "sender", "text", "masked", "name_map",
        "created_at",
    }
    assert first["chat_id"] == 10
    assert first["sender"] == "О. С."
    assert first["masked"] == 2
    # created_at — строка SQLite вида «YYYY-MM-DD HH:MM:SS».
    assert len(first["created_at"]) == 19
    assert first["created_at"][4] == "-" and first["created_at"][10] == " "

    # Лимит выдачи.
    assert len(await db.get_messages(10, limit=1)) == 1


async def test_buffer_cleared_after_digest_issue(tmp_path):
    """Сценарий «очистка после выдачи»: новая сводка не видит старых сообщений."""
    db = await _make_db(tmp_path)
    for i in range(3):
        await db.add_message(7, f"Родитель {i}", f"сообщение {i}", masked=i)

    messages = await db.get_messages(7)
    assert len(messages) == 3

    removed = await db.clear_buffer(7)
    assert removed == 3
    assert await db.count_messages(7) == 0
    assert await db.get_messages(7) == []
    # Повторная очистка не удаляет ничего лишнего.
    assert await db.clear_buffer(7) == 0


async def test_cleanup_old_messages(tmp_path):
    """cleanup_old_messages удаляет строки старше N часов, свежие остаются."""
    db = await _make_db(tmp_path)
    await db.add_message(7, "О. С.", "свежее сообщение")
    async with aiosqlite.connect(db.path) as conn:
        await conn.execute(
            """INSERT INTO messages_buffer
                   (chat_id, sender, text, masked, created_at)
               VALUES (7, 'М. И.', 'старое сообщение', 0,
                       datetime('now', '-25 hours'))"""
        )
        await conn.commit()

    removed = await db.cleanup_old_messages(24)
    assert removed == 1
    assert await db.count_messages(7) == 1
    assert (await db.get_messages(7))[0]["text"] == "свежее сообщение"


async def test_cleanup_job_removes_old_messages(tmp_path):
    """Фоновая cleanup_job чистит старые сообщения и не падает."""
    db = await _make_db(tmp_path)
    await db.add_message(1, "О. С.", "старое")
    async with aiosqlite.connect(db.path) as conn:
        await conn.execute(
            "UPDATE messages_buffer SET created_at = datetime('now', '-25 hours')"
        )
        await conn.commit()
    await db.add_message(2, "М. И.", "свежее")

    await cleanup_job(db)

    assert await db.count_messages(1) == 0
    assert await db.count_messages(2) == 1


# Привязки чатов


async def test_chat_bindings(tmp_path):
    """link/unlink, владельцы, названия, список чатов."""
    db = await _make_db(tmp_path)
    await db.link_chat(100, 42, "3А класс")
    await db.link_chat(200, 42, "3Б класс")
    await db.link_chat(300, 0, "демо-чат", is_demo=True)

    assert await db.get_chat_owner(100) == 42
    assert await db.get_chat_owner(999) is None
    assert await db.get_chat_title(100) == "3А класс"
    assert await db.get_chat_title(999) is None

    # Демо-чат привязан к user_id=0 и не попадает реальному учителю.
    assert await db.get_teacher_chats(42) == [100, 200]
    assert 300 not in await db.get_teacher_chats(42)
    assert await db.get_teacher_chats(0) == [300]

    all_chats = {c["chat_id"]: c for c in await db.all_chats()}
    assert all_chats[100]["user_id"] == 42
    assert all_chats[100]["title"] == "3А класс"
    assert not all_chats[100]["is_demo"]
    assert all_chats[300]["is_demo"]

    await db.unlink_chat(100)
    assert await db.get_chat_owner(100) is None
    assert await db.get_teacher_chats(42) == [200]


# Расписания: время одно на учителя, демо-чат исключён


async def test_schedule_time_is_shared_between_teacher_chats(tmp_path):
    """set_schedule_time меняет время у ВСЕХ чатов учителя, DISTINCT даёт 1 запись."""
    db = await _make_db(tmp_path)
    # Демо-чат (user_id=0, is_demo=1) не должен попасть в автосводку.
    await db.link_chat(999001, 0, "3А (демо для жюри)", is_demo=True)
    # Учитель с двумя чатами.
    await db.link_chat(100, 42, "3А класс")
    await db.link_chat(200, 42, "3Б класс")

    # Дефолтное время из DEFAULT '19:00'.
    assert await db.get_schedule_time(42) == "19:00"
    assert await db.get_schedule_time(555) is None

    await db.set_schedule_time(42, "18:30")
    assert await db.get_schedule_time(42) == "18:30"

    # Время обновилось у обоих чатов (проверяем напрямую в SQL).
    async with aiosqlite.connect(db.path) as conn:
        cur = await conn.execute(
            "SELECT chat_id, schedule_time FROM chats WHERE user_id = 42"
        )
        times = dict(await cur.fetchall())
    assert times == {100: "18:30", 200: "18:30"}

    # Два чата одного учителя — одна запись DISTINCT, демо-чата нет.
    schedules = await db.get_all_schedules()
    assert schedules == [{"user_id": 42, "time": "18:30"}]

    # Новый чат учителя наследует уже выбранное время.
    await db.link_chat(300, 42, "3В класс")
    schedules = await db.get_all_schedules()
    assert len(schedules) == 1
    async with aiosqlite.connect(db.path) as conn:
        cur = await conn.execute(
            "SELECT schedule_time FROM chats WHERE chat_id = 300"
        )
        assert (await cur.fetchone())[0] == "18:30"


async def test_get_all_schedules_ignores_demo_chat(tmp_path):
    """Демо-чат с user_id=0 никогда не попадает в get_all_schedules."""
    db = await _make_db(tmp_path)
    await db.link_chat(DEMO_CHAT_ID, 0, "демо-чат", is_demo=True)
    await db.set_schedule_time(0, "19:00")  # время задано и у демо-чата

    assert await db.get_all_schedules() == []
    # Сама привязка при этом хранится — демо работает по кнопкам.
    assert await db.get_schedule_time(0) == "19:00"
    assert await db.get_teacher_chats(0) == [DEMO_CHAT_ID]


# История сводок и счётчики


async def test_digest_history(tmp_path):
    """save/get_last/get_history: свежая сводка первой."""
    db = await _make_db(tmp_path)
    await db.link_chat(55, 42, "3В класс")
    await db.save_digest(55, "Сводка №1", "fallback")
    await db.save_digest(55, "Сводка №2", "gigachat")

    assert await db.get_last_digest(56) is None

    last = await db.get_last_digest(55)
    assert last["chat_id"] == 55
    assert last["text"] == "Сводка №2"
    assert last["source"] == "gigachat"
    assert len(last["created_at"]) == 19

    history = await db.get_digest_history(55)
    assert [h["text"] for h in history] == ["Сводка №2", "Сводка №1"]
    assert len(await db.get_digest_history(55, limit=1)) == 1


async def test_stats(tmp_path):
    """bump_stat создаёт и увеличивает счётчики, get_stats даёт дефолты."""
    db = await _make_db(tmp_path)

    stats = await db.get_stats()
    assert stats["messages_processed"] == 0
    assert stats["digests_sent"] == 0
    assert stats["masked_total"] == 0

    await db.bump_stat("messages_processed")
    await db.bump_stat("messages_processed", 4)
    await db.bump_stat("masked_total", 7)

    stats = await db.get_stats()
    assert stats["messages_processed"] == 5
    assert stats["masked_total"] == 7
    assert stats["digests_sent"] == 0


# Клавиатуры (builder.payload — ряды с кнопками)


def test_main_menu_keyboard():
    """Главное меню: точные payload и текст демо-кнопки для жюри."""
    builder = main_menu()
    buttons = _flatten(builder)
    by_payload = {
        b.payload: b for b in buttons if isinstance(b, CallbackButton)
    }

    assert set(by_payload) == {
        "menu:digest", "menu:schedule", "menu:calendar", "menu:demo",
        "menu:help",
    }
    assert by_payload["menu:demo"].text == "🧪 Демо (для жюри)"
    assert by_payload["menu:digest"].text == "📊 Сводка за день"
    assert by_payload["menu:schedule"].text == "⏰ Расписание"
    assert by_payload["menu:calendar"].text == "📅 Календарь (.ics)"
    assert by_payload["menu:help"].text == "ℹ️ Инструкция"

    # Ряды: 2 + 2 + 1 — без адреса веб-кнопки нет.
    assert [len(row) for row in builder.payload] == [2, 2, 1]

    # Клавиатура собирается в настоящее вложение для MAX.
    markup = builder.as_markup()
    assert markup.payload.buttons


def test_main_menu_web_button():
    """Кнопка веб-панели — LinkButton с адресом; без адреса её нет."""
    builder = main_menu("https://school.example.ru/panel")
    links = [b for b in _flatten(builder) if isinstance(b, LinkButton)]
    assert len(links) == 1
    assert links[0].url == "https://school.example.ru/panel"
    assert links[0].text == "🌐 Веб-панель учителя"
    assert [len(row) for row in builder.payload] == [2, 2, 1, 1]

    assert not [
        b for b in _flatten(main_menu()) if isinstance(b, LinkButton)
    ]


def test_schedule_menu_keyboard():
    """Меню расписания: 8 времён 14:00–21:00, своё время и назад."""
    builder = schedule_menu()
    buttons = _flatten(builder)

    times = ("14:00", "15:00", "16:00", "17:00",
             "18:00", "19:00", "20:00", "21:00")
    for time in times:
        button = next(b for b in buttons if b.text == time)
        assert button.payload == f"sch:{time}"

    payloads = {b.payload for b in buttons}
    assert "sch:custom" in payloads
    assert "menu:back" in payloads
    custom = next(b for b in buttons if b.payload == "sch:custom")
    assert custom.text == "✍️ Ввести своё время"
    back = next(b for b in buttons if b.payload == "menu:back")
    assert back.text == "⬅️ Назад"

    # Ряды: четыре пары времён, своё время, назад.
    assert [len(row) for row in builder.payload] == [2, 2, 2, 2, 1, 1]


def test_back_menu_keyboard():
    """Клавиатура возврата — одна кнопка «⬅️ В меню»."""
    builder = back_menu()
    assert [len(row) for row in builder.payload] == [1]
    button = builder.payload[0][0]
    assert button.text == "⬅️ В меню"
    assert button.payload == "menu:back"


# Планировщик


async def test_create_scheduler_jobs(tmp_path):
    """create_scheduler регистрирует две задачи с нужными интервалами."""
    db = await _make_db(tmp_path)
    scheduler = create_scheduler(db)
    assert isinstance(scheduler, AsyncIOScheduler)

    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == {"send_scheduled_digests", "cleanup_job"}
    # Проверка расписаний — каждые 60 секунд.
    assert jobs["send_scheduled_digests"].trigger.interval.total_seconds() == 60
    # Цифровая гигиена — каждые 60 минут.
    assert jobs["cleanup_job"].trigger.interval.total_seconds() == 3600


async def test_scheduled_digests_do_not_fire_at_wrong_time(
    monkeypatch, tmp_path
):
    """Время не совпало — ничего не собираем и не отправляем."""
    db = await _make_db(tmp_path)
    await db.link_chat(100, 42, "3А класс")
    await db.add_message(100, "О. С.", "текст")
    await db.set_schedule_time(42, "20:00")  # часы показывают 19:00

    calls = _install_fake_digest(
        monkeypatch, lambda chat_id: types.SimpleNamespace(text="сводка")
    )
    monkeypatch.setattr("app.services.scheduler.datetime", _FixedClock)

    bot = _FakeBot()
    await send_scheduled_digests(db, bot)
    assert calls == []
    assert bot.sent == []
    assert await db.count_messages(100) == 1


async def test_scheduled_digests_without_bot_only_logs(
    monkeypatch, tmp_path
):
    """bot=None: make_digest чистит буфер, поэтому без бота его вызывать нельзя."""
    db = await _make_db(tmp_path)
    await db.link_chat(100, 42, "3А класс")
    await db.add_message(100, "О. С.", "текст")
    await db.set_schedule_time(42, "19:00")  # совпадает с _FixedClock

    calls = _install_fake_digest(
        monkeypatch, lambda chat_id: types.SimpleNamespace(text="сводка")
    )
    monkeypatch.setattr("app.services.scheduler.datetime", _FixedClock)

    await send_scheduled_digests(db)  # бот не передан
    assert calls == []
    assert await db.count_messages(100) == 1


async def test_scheduled_digests_send_at_matching_time(
    monkeypatch, tmp_path
):
    """Наступило время — сводка уходит в ЛС учителя отдельным сообщением."""
    db = await _make_db(tmp_path)
    await db.link_chat(100, 42, "3А класс")
    await db.add_message(100, "О. С.", "Собрание 15 октября в 18:00")
    await db.set_schedule_time(42, "19:00")

    calls = _install_fake_digest(
        monkeypatch,
        lambda chat_id: types.SimpleNamespace(text=f"Сводка чата {chat_id}"),
    )
    monkeypatch.setattr("app.services.scheduler.datetime", _FixedClock)

    bot = _FakeBot()
    await send_scheduled_digests(db, bot)

    assert calls == [100]
    assert len(bot.sent) == 1
    assert bot.sent[0]["user_id"] == 42
    assert bot.sent[0]["text"] == "Сводка чата 100"


async def test_scheduled_digests_skip_empty_chats(monkeypatch, tmp_path):
    """Чат без сообщений пропускается тихо — пустых сводок не будет."""
    db = await _make_db(tmp_path)
    await db.link_chat(100, 42, "3А класс")
    await db.link_chat(200, 42, "3Б класс")  # в нём нет сообщений
    await db.set_schedule_time(42, "19:00")

    def make_result(chat_id):
        return types.SimpleNamespace(
            text="Сводка" if chat_id == 100 else ""
        )

    _install_fake_digest(monkeypatch, make_result)
    monkeypatch.setattr("app.services.scheduler.datetime", _FixedClock)

    bot = _FakeBot()
    await send_scheduled_digests(db, bot)

    # Отправлена только сводка непустого чата.
    assert len(bot.sent) == 1
    assert bot.sent[0]["user_id"] == 42
    assert bot.sent[0]["text"] == "Сводка"


async def test_scheduled_digests_ignore_demo_chat(monkeypatch, tmp_path):
    """Демо-чат не попадает в автосводку: его буфер остаётся для жюри."""
    db = await _make_db(tmp_path)
    await db.link_chat(DEMO_CHAT_ID, 0, "демо-чат", is_demo=True)
    await db.add_message(DEMO_CHAT_ID, "О. С.", "текст демо")
    await db.set_schedule_time(0, "19:00")
    # Настоящий учитель с пустым чатом.
    await db.link_chat(100, 42, "3А класс")
    await db.set_schedule_time(42, "19:00")

    calls = _install_fake_digest(
        monkeypatch, lambda chat_id: types.SimpleNamespace(text="сводка")
    )
    monkeypatch.setattr("app.services.scheduler.datetime", _FixedClock)

    bot = _FakeBot()
    await send_scheduled_digests(db, bot)

    # Обрабатывается только настоящий чат учителя.
    assert calls == [100]
    assert DEMO_CHAT_ID not in calls
    # Буфер демо-чата не тронут — демонстрация жюри не сломана.
    assert await db.count_messages(DEMO_CHAT_ID) == 1


async def test_scheduled_digests_survive_db_errors():
    """Сломанная база не роняет планировщик: ошибки только в лог."""
    await send_scheduled_digests(None)  # db=None — ошибка внутри


async def test_web_keys_personal_and_forever(tmp_path):
    """Ключ панели один на учителя: повторный вызов возвращает тот же."""
    db = await _make_db(tmp_path)

    key_first = await db.get_or_create_web_key(101)
    key_again = await db.get_or_create_web_key(101)
    key_other = await db.get_or_create_web_key(202)

    assert key_first and key_other
    assert key_first == key_again, "ключ должен быть постоянным"
    assert key_first != key_other, "у разных учителей ключи разные"

    assert await db.get_user_by_web_key(key_first) == 101
    assert await db.get_user_by_web_key(key_other) == 202
    assert await db.get_user_by_web_key("чужой-ключ") is None
    assert await db.get_user_by_web_key("") is None


async def test_schedule_before_first_chat(tmp_path):
    """Время, выбранное в панели до первого чата, не теряется."""
    db = await _make_db(tmp_path)

    assert await db.get_schedule_time(555) is None

    await db.set_schedule_time(555, "08:15")
    assert await db.get_schedule_time(555) == "08:15"

    # Первый чат учителя наследует заранее выбранное время.
    await db.link_chat(300, 555, "3В класс")
    async with aiosqlite.connect(db.path) as conn:
        cur = await conn.execute(
            "SELECT schedule_time FROM chats WHERE chat_id = 300"
        )
        assert (await cur.fetchone())[0] == "08:15"

    # Изменение времени обновляет и существующий чат, и настройку.
    await db.set_schedule_time(555, "07:45")
    assert await db.get_schedule_time(555) == "07:45"
    async with aiosqlite.connect(db.path) as conn:
        cur = await conn.execute(
            "SELECT schedule_time FROM chats WHERE chat_id = 300"
        )
        assert (await cur.fetchone())[0] == "07:45"


async def test_buffer_stores_name_map(tmp_path):
    """Карта «маркер -> имя» хранится вместе с сообщением и живёт не дольше буфера."""
    db = await _make_db(tmp_path)

    mapping = {"[USR_5 | О. С.]": "Ольга Смирнова"}
    await db.add_message(
        10, "[USR_5 | О. С.]", "текст", masked=3, name_map=mapping
    )
    (msg,) = await db.get_messages(10)
    assert msg["name_map"] == mapping

    await db.clear_buffer(10)
    assert await db.get_messages(10) == []


class _FakeAdminBot:

    def __init__(self):
        self.sent = []
        self.deleted = []

    async def send_message(self, chat_id=None, user_id=None, text=None,
                           attachments=None, **kwargs):
        target = chat_id if chat_id is not None else user_id
        self.sent.append({"target": target, "text": text,
                          "has_menu": bool(attachments)})
        return None

    async def delete_message(self, mid):
        self.deleted.append(mid)
        return None


def _fake_sended(mid: str):

    class Body:
        pass

    class Message:
        pass

    class Sended:
        pass

    body = Body()
    body.mid = mid
    message = Message()
    message.body = body
    sended = Sended()
    sended.message = message
    return sended


def test_extract_mid_reads_nested_body():
    """mid лежит в message.body.mid — раньше читали не туда."""
    from app.bot.handlers import _extract_mid

    assert _extract_mid(_fake_sended("mid.12345")) == "mid.12345"
    assert _extract_mid(None) is None


async def test_admin_event_success_flow(tmp_path, monkeypatch):
    """Назначение админом: право удаления проверяем делом, а не по списку прав."""
    from app.bot import handlers

    db = await _make_db(tmp_path)
    await db.link_chat(500, 42, "3А класс")
    await db.set_admin_hint_mid(500, "mid.hint1")

    bot = _FakeAdminBot()
    monkeypatch.setattr(handlers, "_bot", bot)
    monkeypatch.setattr(handlers, "_db", db)

    # В событии MAX права удаления нет вовсе — но удаление проходит.
    await handlers.on_admin_permissions_changed({
        "chat_id": 500, "user_id": 42, "is_admin": True,
        "permissions": ["read_all_messages"],
    })
    assert await db.get_bot_admin_state(500) is True
    assert await db.get_bot_delete_right(500) is True, "удаление прошло — право есть"
    assert bot.deleted == ["mid.hint1"], "подсказка должна удалиться из чата"
    assert await db.get_admin_hint_mid(500) is None
    # Сообщение в чат + ЛС тому, кто назначил.
    assert [s["target"] for s in bot.sent] == [500, 42]
    assert "удалил" in bot.sent[0]["text"], bot.sent[0]["text"]
    assert "администратором" in bot.sent[1]["text"]
    assert "Сводка за день" in bot.sent[1]["text"]

    # Шаг 2: перекрутили другие права — бот уже админ, тишина.
    bot.sent.clear()
    await handlers.on_admin_permissions_changed({
        "chat_id": 500, "user_id": 42, "is_admin": True,
        "permissions": ["delete_messages"],
    })
    assert bot.sent == [], "повторное назначение не должно спамить"

    # Шаг 3: сняли права — владелец чата получает предупреждение.
    await handlers.on_admin_permissions_changed({
        "chat_id": 500, "user_id": 42, "is_admin": False,
    })
    assert await db.get_bot_admin_state(500) is False
    assert len(bot.sent) == 1
    assert bot.sent[0]["target"] == 42
    assert "сняли" in bot.sent[0]["text"]


class _FakeConnectMessage:

    def __init__(self, chat_id, user_id):
        self.answered = []
        self.recipient = types.SimpleNamespace(
            chat_id=chat_id, chat_type=ChatType.CHAT)
        self.sender = types.SimpleNamespace(user_id=user_id)

    async def answer(self, text=None, attachments=None):
        self.answered.append(text)
        return _fake_sended("mid.new")


async def test_connect_skips_hint_when_already_admin(tmp_path, monkeypatch):
    """/connect при уже выданных правах: приветствие есть, гайда нет."""
    from app.bot import handlers

    db = await _make_db(tmp_path)
    await db.link_chat(777, 42, "3А класс")
    await db.set_bot_admin_state(777, True)

    msg = _FakeConnectMessage(777, 42)
    event = types.SimpleNamespace(message=msg)
    monkeypatch.setattr(handlers, "_db", db)
    monkeypatch.setattr(handlers, "_bot", _FakeAdminBot())

    async def _title(event):
        return "3А класс"

    monkeypatch.setattr(handlers, "_fetch_chat_title", _title)

    await handlers.on_connect_command(event)

    texts = msg.answered
    assert any("Классный дайджест" in t for t in texts), "приветствие должно быть"
    assert any("уже выданы" in t for t in texts), "короткое подтверждение"
    assert not any("Администраторы" in t for t in texts), "гайда быть не должно"
    assert await db.get_admin_hint_mid(777) is None


async def test_bot_delete_right_flag(tmp_path):
    """Право удаления храним отдельно: от него зависит автоочистка чата."""
    db = await _make_db(tmp_path)
    await db.link_chat(901, 42, "3А класс")

    assert await db.get_bot_delete_right(901) is False
    await db.set_bot_delete_right(901, True)
    assert await db.get_bot_delete_right(901) is True


async def test_unlink_chat_removes_chat_data(tmp_path):
    """Бота убрали из чата — данные чата тоже уходят."""
    db = await _make_db(tmp_path)
    await db.link_chat(770, 42, "3А класс")
    await db.add_message(770, "[USR_1 | М. И.]", "текст", masked=1)
    await db.save_digest(770, "сводка с именами", "gigachat")
    await db.save_events(770, [{"title": "Собрание", "date": None, "time": "18:00"}])

    await db.unlink_chat(770)

    assert await db.get_chat_owner(770) is None
    assert await db.count_messages(770) == 0
    assert await db.get_last_digest(770) is None
    assert await db.get_events(770) == []


async def test_old_digests_are_cleaned(tmp_path):
    """История сводок тоже не хранится бессрочно."""
    import aiosqlite

    db = await _make_db(tmp_path)
    await db.link_chat(771, 42, "3А класс")
    await db.save_digest(771, "свежая сводка", "gigachat")
    async with aiosqlite.connect(db.path) as conn:
        await conn.execute(
            "UPDATE digests_history SET created_at = datetime('now', '-30 hours')"
        )
        await conn.commit()

    removed = await db.cleanup_old_digests(hours=24)

    assert removed == 1
    assert await db.get_last_digest(771) is None


async def test_link_allowed_only_for_owner(tmp_path, monkeypatch):
    """Чужой участник не может перепривязать чат на себя."""
    from app.bot import handlers

    db = await _make_db(tmp_path)
    await db.link_chat(780, 42, "3А класс")
    monkeypatch.setattr(handlers, "_db", db)

    assert await handlers._link_allowed(780, 42) is True
    assert await handlers._link_allowed(780, 999) is False
    assert await handlers._link_allowed(999999, 5) is True


async def test_admin_event_keeps_hint_without_delete_right(tmp_path, monkeypatch):
    """Если MAX не даёт удалять — подсказка остаётся и обещания в тексте нет."""
    from app.bot import handlers

    class DeniedBot(_FakeAdminBot):

        async def delete_message(self, mid):
            raise RuntimeError(
                "access.denied: Insufficient permissions to delete message"
            )

    db = await _make_db(tmp_path)
    await db.link_chat(510, 42, "3В класс")
    await db.set_admin_hint_mid(510, "mid.hint")

    bot = DeniedBot()
    monkeypatch.setattr(handlers, "_bot", bot)
    monkeypatch.setattr(handlers, "_db", db)

    await handlers.on_admin_permissions_changed({
        "chat_id": 510, "user_id": 42, "is_admin": True,
        "permissions": ["read_all_messages"],
    })

    assert await db.get_admin_hint_mid(510) == "mid.hint", "подсказку не тронули"
    assert await db.get_bot_delete_right(510) is False
    assert "удалил" not in bot.sent[0]["text"], bot.sent[0]["text"]
