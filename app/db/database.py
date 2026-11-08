"""Хранилище бота на SQLite (aiosqlite): чаты, буфер обезличенных сообщений, история сводок, счётчики, календарь.

Соединение открывается на каждый вызов, поэтому база спокойно работает из бота, планировщика и панели сразу."""

import datetime
import logging
import os
import sqlite3

import secrets
import json

import aiosqlite

logger = logging.getLogger(__name__)

# Счётчики, которые веб-панель показывает даже на пустой базе.
_DEFAULT_STATS = ("messages_processed", "digests_sent", "masked_total")

_CREATE_CHATS = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    user_id INTEGER,
    title TEXT,
    schedule_time TEXT DEFAULT '19:00',
    is_demo INTEGER DEFAULT 0,
    created_at TEXT
)
"""

_CREATE_MESSAGES_BUFFER = """
CREATE TABLE IF NOT EXISTS messages_buffer (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    sender TEXT,
    text TEXT,
    masked INTEGER DEFAULT 0,
    name_map TEXT,
    created_at TEXT DEFAULT (datetime('now'))
)
"""

_CREATE_DIGESTS_HISTORY = """
CREATE TABLE IF NOT EXISTS digests_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    text TEXT,
    source TEXT,
    created_at TEXT DEFAULT (datetime('now'))
)
"""

_CREATE_STATS = """
CREATE TABLE IF NOT EXISTS stats (
    key TEXT PRIMARY KEY,
    value INTEGER DEFAULT 0
)
"""

_CREATE_CALENDAR_EVENTS = """
CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    title TEXT,
    date TEXT,
    time TEXT,
    created_at TEXT DEFAULT (datetime('now'))
)
"""

_CREATE_WEB_KEYS = """
CREATE TABLE IF NOT EXISTS web_keys (
    user_id INTEGER PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
)
"""

_CREATE_TEACHER_SETTINGS = """
CREATE TABLE IF NOT EXISTS teacher_settings (
    user_id INTEGER PRIMARY KEY,
    schedule_time TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
)
"""

class Database:

    def __init__(self, path: str) -> None:
        self.path = path

    async def init(self) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        async with aiosqlite.connect(self.path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            cur = await conn.cursor()
            await cur.execute(_CREATE_CHATS)
            await cur.execute(_CREATE_MESSAGES_BUFFER)
            await cur.execute(_CREATE_DIGESTS_HISTORY)
            await cur.execute(_CREATE_STATS)
            await cur.execute(_CREATE_CALENDAR_EVENTS)
            await cur.execute(_CREATE_WEB_KEYS)
            await cur.execute(_CREATE_TEACHER_SETTINGS)
            # Старая база могла не знать про name_map — добавляем колонку на месте.
            try:
                await cur.execute(
                    "ALTER TABLE messages_buffer ADD COLUMN name_map TEXT"
                )
            except sqlite3.OperationalError:
                pass  # колонка уже есть
            for ddl in (
                "ALTER TABLE chats ADD COLUMN admin_hint_mid TEXT",
                "ALTER TABLE chats ADD COLUMN bot_is_admin INTEGER DEFAULT 0",
                "ALTER TABLE chats ADD COLUMN bot_can_delete INTEGER DEFAULT 0",
            ):
                try:
                    await cur.execute(ddl)
                except sqlite3.OperationalError:
                    pass  # колонка уже есть
            await conn.commit()
            await cur.close()
            logger.info("База готова: %s", self.path)

    async def close(self) -> None:
        """Пустой метод: соединения закрываются сами, но main его вызывает."""
        return None

    # Привязки чатов

    async def link_chat(self, chat_id: int, user_id: int, title: str = "",
                        is_demo: bool = False) -> None:
        """Новый чат наследует уже выбранное учителем время сводки."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            # Личная настройка важнее: её могли задать ещё до привязки чата.
            await cur.execute(
                "SELECT schedule_time FROM teacher_settings WHERE user_id = ?",
                (user_id,),
            )
            row = await cur.fetchone()
            if row is None:
                await cur.execute(
                    """SELECT schedule_time FROM chats
                       WHERE user_id = ? AND chat_id != ?
                       ORDER BY created_at LIMIT 1""",
                    (user_id, chat_id),
                )
                row = await cur.fetchone()

            if row is not None:
                await cur.execute(
                    """INSERT INTO chats
                           (chat_id, user_id, title, schedule_time,
                            is_demo, created_at)
                       VALUES (?, ?, ?, ?, ?, datetime('now'))
                       ON CONFLICT(chat_id) DO UPDATE SET
                           user_id = excluded.user_id,
                           title = excluded.title,
                           is_demo = excluded.is_demo""",
                    (chat_id, user_id, title, row[0], int(is_demo)),
                )
            else:
                # Первый чат: время берётся из DEFAULT '19:00'.
                await cur.execute(
                    """INSERT INTO chats
                           (chat_id, user_id, title, is_demo, created_at)
                       VALUES (?, ?, ?, ?, datetime('now'))
                       ON CONFLICT(chat_id) DO UPDATE SET
                           user_id = excluded.user_id,
                           title = excluded.title,
                           is_demo = excluded.is_demo""",
                    (chat_id, user_id, title, int(is_demo)),
                )
            await conn.commit()
            await cur.close()

    # Служебные поля чата: гайд по назначению и статус админа

    async def set_admin_hint_mid(self, chat_id: int, mid: str | None) -> None:
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                "UPDATE chats SET admin_hint_mid = ? WHERE chat_id = ?",
                (mid, chat_id),
            )
            await conn.commit()
            await cur.close()

    async def get_admin_hint_mid(self, chat_id: int) -> str | None:
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                "SELECT admin_hint_mid FROM chats WHERE chat_id = ?",
                (chat_id,),
            )
            row = await cur.fetchone()
            await cur.close()
        return row[0] if row else None

    async def set_bot_admin_state(self, chat_id: int, is_admin: bool) -> None:
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                "UPDATE chats SET bot_is_admin = ? WHERE chat_id = ?",
                (int(is_admin), chat_id),
            )
            await conn.commit()
            await cur.close()

    async def get_bot_admin_state(self, chat_id: int) -> bool:
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                "SELECT bot_is_admin FROM chats WHERE chat_id = ?",
                (chat_id,),
            )
            row = await cur.fetchone()
            await cur.close()
        return bool(row and row[0])

    async def set_bot_delete_right(self, chat_id: int, can_delete: bool) -> None:
        """Право бота удалять сообщения в этом чате."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                "UPDATE chats SET bot_can_delete = ? WHERE chat_id = ?",
                (int(can_delete), chat_id),
            )
            await conn.commit()
            await cur.close()

    async def get_bot_delete_right(self, chat_id: int) -> bool:
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                "SELECT bot_can_delete FROM chats WHERE chat_id = ?",
                (chat_id,),
            )
            row = await cur.fetchone()
            await cur.close()
        return bool(row and row[0])

    async def unlink_chat(self, chat_id: int) -> None:
        """Бота убрали из чата — забираем с собой и все его данные."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
            await cur.execute(
                "DELETE FROM messages_buffer WHERE chat_id = ?", (chat_id,)
            )
            await cur.execute(
                "DELETE FROM digests_history WHERE chat_id = ?", (chat_id,)
            )
            await cur.execute(
                "DELETE FROM calendar_events WHERE chat_id = ?", (chat_id,)
            )
            await conn.commit()
            await cur.close()

    async def get_teacher_chats(self, user_id: int) -> list[int]:
        """Демо-чат висит на user_id=0, реальному учителю он не попадает."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                """SELECT chat_id FROM chats
                   WHERE user_id = ? ORDER BY chat_id""",
                (user_id,),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [row[0] for row in rows]

    async def get_chat_owner(self, chat_id: int) -> int | None:
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                "SELECT user_id FROM chats WHERE chat_id = ?", (chat_id,)
            )
            row = await cur.fetchone()
            await cur.close()
        return row[0] if row else None

    async def get_chat_title(self, chat_id: int) -> str | None:
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                "SELECT title FROM chats WHERE chat_id = ?", (chat_id,)
            )
            row = await cur.fetchone()
            await cur.close()
        return row[0] if row else None

    async def all_chats(self) -> list[dict]:
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                """SELECT chat_id, user_id, title, is_demo
                   FROM chats ORDER BY chat_id"""
            )
            rows = await cur.fetchall()
            await cur.close()
        return [
            {
                "chat_id": row[0],
                "user_id": row[1],
                "title": row[2],
                "is_demo": row[3],
            }
            for row in rows
        ]

    # Расписание: одно время на учителя

    async def set_schedule_time(self, user_id: int, time: str) -> None:
        """Время пишем и в настройки учителя, и во все его чаты."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                """INSERT INTO teacher_settings (user_id, schedule_time)
                   VALUES (?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       schedule_time = excluded.schedule_time,
                       updated_at = datetime('now')""",
                (user_id, time),
            )
            await cur.execute(
                "UPDATE chats SET schedule_time = ? WHERE user_id = ?",
                (time, user_id),
            )
            await conn.commit()
            await cur.close()

    async def get_schedule_time(self, user_id: int) -> str | None:
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                "SELECT schedule_time FROM teacher_settings WHERE user_id = ?",
                (user_id,),
            )
            row = await cur.fetchone()
            if row is None:
                # Настройку ещё не меняли — смотрим время первого чата учителя.
                await cur.execute(
                    """SELECT schedule_time FROM chats WHERE user_id = ?
                       ORDER BY created_at LIMIT 1""",
                    (user_id,),
                )
                row = await cur.fetchone()
            await cur.close()
        return row[0] if row else None

    async def get_all_schedules(self) -> list[dict]:
        """Только реальные учителя (демо висит на user_id=0). DISTINCT — одна рассылка, даже если чатов несколько."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                """SELECT DISTINCT user_id, schedule_time FROM chats
                   WHERE user_id > 0 AND is_demo = 0"""
            )
            rows = await cur.fetchall()
            await cur.close()
        return [{"user_id": row[0], "time": row[1]} for row in rows]

    # Буфер сообщений (только обезличенный текст)

    async def add_message(self, chat_id: int, sender: str, text: str,
                          masked: int = 0, name_map: dict | None = None) -> None:
        """name_map («маркер → полное имя») едет вместе с сообщением и стирается вместе с буфером."""
        raw_map = json.dumps(name_map, ensure_ascii=False) if name_map else None
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                """INSERT INTO messages_buffer
                       (chat_id, sender, text, masked, name_map)
                   VALUES (?, ?, ?, ?, ?)""",
                (chat_id, sender, text, int(masked), raw_map),
            )
            await conn.commit()
            await cur.close()

    async def get_messages(self, chat_id: int, limit: int = 500) -> list[dict]:
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                """SELECT id, chat_id, sender, text, masked, name_map, created_at
                   FROM messages_buffer WHERE chat_id = ?
                   ORDER BY id ASC LIMIT ?""",
                (chat_id, int(limit)),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [
            {
                "id": row[0],
                "chat_id": row[1],
                "sender": row[2],
                "text": row[3],
                "masked": row[4],
                "name_map": json.loads(row[5]) if row[5] else {},
                "created_at": row[6],
            }
            for row in rows
        ]

    async def count_messages(self, chat_id: int) -> int:
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                "SELECT COUNT(*) FROM messages_buffer WHERE chat_id = ?",
                (chat_id,),
            )
            row = await cur.fetchone()
            await cur.close()
        return int(row[0]) if row else 0

    async def clear_buffer(self, chat_id: int) -> int:
        """Чистит буфер после выдачи сводки, возвращает число удалённых строк."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                "DELETE FROM messages_buffer WHERE chat_id = ?", (chat_id,)
            )
            removed = cur.rowcount
            await conn.commit()
            await cur.close()
        return removed

    async def cleanup_old_messages(self, hours: int = 24) -> int:
        """created_at хранится в UTC, поэтому сравнение делает сам SQLite."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                """DELETE FROM messages_buffer
                   WHERE created_at <= datetime('now', ?)""",
                (f"-{int(hours)} hours",),
            )
            removed = cur.rowcount
            await conn.commit()
            await cur.close()
        if removed:
            logger.info(
                "Цифровая гигиена: удалено %s сообщений старше %s ч.",
                removed, hours,
            )
        return removed

    # История сводок: текст уже с именами для учителя, поэтому храним не дольше суток.

    async def save_digest(self, chat_id: int, text: str, source: str) -> None:
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                """INSERT INTO digests_history (chat_id, text, source)
                   VALUES (?, ?, ?)""",
                (chat_id, text, source),
            )
            await conn.commit()
            await cur.close()

    async def cleanup_old_digests(self, hours: int = 24) -> int:
        """В сводках настоящие имена детей — держим их тоже не дольше суток."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                "DELETE FROM digests_history "
                "WHERE created_at <= datetime('now', ?)",
                (f"-{int(hours)} hours",),
            )
            removed = cur.rowcount
            await conn.commit()
            await cur.close()
        return removed

    async def get_last_digest(self, chat_id: int) -> dict | None:
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                """SELECT chat_id, text, source, created_at
                   FROM digests_history WHERE chat_id = ?
                   ORDER BY id DESC LIMIT 1""",
                (chat_id,),
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            return None
        return {
            "chat_id": row[0],
            "text": row[1],
            "source": row[2],
            "created_at": row[3],
        }

    async def get_digest_history(self, chat_id: int, limit: int = 5) -> list[dict]:
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                """SELECT chat_id, text, source, created_at
                   FROM digests_history WHERE chat_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (chat_id, int(limit)),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [
            {
                "chat_id": row[0],
                "text": row[1],
                "source": row[2],
                "created_at": row[3],
            }
            for row in rows
        ]

    # Счётчики для веб-панели

    async def bump_stat(self, key: str, delta: int = 1) -> None:
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                """INSERT INTO stats (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value = value + excluded.value""",
                (key, int(delta)),
            )
            await conn.commit()
            await cur.close()

    async def get_stats(self) -> dict:
        stats: dict[str, int] = {key: 0 for key in _DEFAULT_STATS}
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute("SELECT key, value FROM stats")
            rows = await cur.fetchall()
            await cur.close()
        for key, value in rows:
            stats[key] = value
        return stats

    # События для календаря (.ics)

    async def save_events(self, chat_id: int, events: list) -> None:
        """Буфер после сводки чистят, а календарь нужен и потом. Старые события чата заменяем новыми."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                "DELETE FROM calendar_events WHERE chat_id = ?", (chat_id,)
            )
            for event in events:
                date_value = event.get("date")
                await cur.execute(
                    """INSERT INTO calendar_events
                           (chat_id, title, date, time)
                       VALUES (?, ?, ?, ?)""",
                    (
                        chat_id,
                        str(event.get("title") or "Событие"),
                        str(date_value) if date_value else None,
                        event.get("time"),
                    ),
                )
            await conn.commit()
            await cur.close()

    async def get_events(self, chat_id: int) -> list[dict]:
        """build_ics ждёт date объектом, поэтому строку из базы разбираем сами."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                """SELECT title, date, time FROM calendar_events
                   WHERE chat_id = ? ORDER BY id""",
                (chat_id,),
            )
            rows = await cur.fetchall()
            await cur.close()

        events: list[dict] = []
        for title, date_raw, time_raw in rows:
            date_value = None
            if date_raw:
                try:
                    date_value = datetime.date.fromisoformat(str(date_raw))
                except ValueError:
                    date_value = None
            events.append(
                {"title": title, "date": date_value, "time": time_raw}
            )
        return events

    async def get_or_create_web_key(self, user_id: int) -> str:
        """Ключ панели: один на учителя, создаётся при первом входе."""
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                "SELECT key FROM web_keys WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            if row:
                await cur.close()
                return str(row[0])

            key = secrets.token_urlsafe(18)
            await cur.execute(
                "INSERT OR IGNORE INTO web_keys (user_id, key) VALUES (?, ?)",
                (user_id, key),
            )
            # Ключ мог создать параллельный запрос — забираем его.
            await cur.execute(
                "SELECT key FROM web_keys WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            await conn.commit()
            await cur.close()
            return str(row[0]) if row else key

    async def get_user_by_web_key(self, key: str) -> int | None:
        if not key:
            return None
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.cursor()
            await cur.execute(
                "SELECT user_id FROM web_keys WHERE key = ?", (key,)
            )
            row = await cur.fetchone()
            await cur.close()
            return int(row[0]) if row else None
