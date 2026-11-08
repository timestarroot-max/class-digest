"""Сборка сводки: обезличенный буфер → сводка → история → очистка буфера."""

import logging
import re
from dataclasses import dataclass, field

from app.config import DEMO_CHAT_TITLE, settings
from app.db.database import Database
from app.demo.sample_messages import DEMO_MESSAGES
from app.services import anonymizer, calendar_export, llm_gigachat

logger = logging.getLogger(__name__)

# Заголовки четырёх категорий сводки.
CATEGORY_MARKS = ("❓", "🏥", "💰", "⚡")


def prettify_digest(text: str) -> str:
    """Раскладывает сводку по строкам: MAX переносы рисует, а ИИ шлёт всё одной строкой."""

    flat = " ".join((text or "").split())
    if not flat:
        return ""

    # Пустые категории не должны превращаться в очередь «— пусто —».
    flat = re.sub(r"(?:—\s*пусто\s*—\s*)+", "— пусто — ", flat)
    for mark in CATEGORY_MARKS:
        flat = flat.replace(mark, f"\n{mark}")

    lines: list[str] = []
    for raw in flat.split("\n"):
        head, _, tail = raw.strip().partition("•")
        head = head.strip()
        if head:
            if head.startswith(CATEGORY_MARKS) and lines:
                lines.append("")
            lines.append(head)
        for item in tail.split("•"):
            item = item.strip()
            if item:
                lines.append(f"• {item}")
    return "\n".join(lines)


@dataclass
class DigestResult:

    chat_id: int
    chat_title: str
    text: str
    engine: str          # "gigachat" | "fallback" | "none"
    messages_count: int
    masked_total: int
    error: str | None = None
    calendar_events: list = field(default_factory=list)


async def make_digest(db: Database, chat_id: int) -> DigestResult:
    """Сводка по буферу чата: пустой буфер — сводки нет (engine = 'none')."""

    messages = await db.get_messages(chat_id)
    title = await db.get_chat_title(chat_id) or "Чат класса"

    if not messages:
        return DigestResult(
            chat_id=chat_id,
            chat_title=title,
            text="",
            engine="none",
            messages_count=0,
            masked_total=0,
        )

    masked_total = sum(int(m.get("masked", 0) or 0) for m in messages)

    # Карта «маркер → полное имя»: по ней сводка вернёт учителю настоящие имена.
    restore_map: dict = {}
    for m in messages:
        for token, full_name in (m.get("name_map") or {}).items():
            restore_map.setdefault(token, full_name)

    try:
        summary = await llm_gigachat.summarize(messages)
    except Exception as exc:  # noqa: BLE001 - бот не должен падать из-за ИИ
        logger.exception("Ошибка при summarization чата %s: %s", chat_id, exc)
        summary = await llm_gigachat.fallback_summarize(messages)

    # В GigaChat ушли только маркеры — возвращаем учителю полные имена.
    teacher_text = anonymizer.restore_names(summary.text, restore_map)
    teacher_text = prettify_digest(teacher_text)

    header = f"📋 Классный дайджест — «{title}»"
    footer = (
        "🧮 Сводка подготовлена: "
        + ("GigaChat" if summary.engine == "gigachat" else "резервный локальный модуль")
    )
    full_text = f"{header}\n\n{teacher_text.strip()}\n\n{footer}"

    # События берём из сообщений, а не из текста сводки: ИИ может пересказать
    # дату иначе. Буфер сейчас будет очищен, поэтому события сохраняем в базу.
    calendar_events = calendar_export.extract_events(
        [m.get("text", "") for m in messages]
    )
    await db.save_events(chat_id, calendar_events)

    await db.save_digest(chat_id, teacher_text, summary.engine)
    await db.clear_buffer(chat_id)
    await db.bump_stat("digests_sent", 1)
    await db.bump_stat("masked_total", masked_total)

    logger.info(
        "Дайджест чата %s готов (%s, сообщений: %s)",
        chat_id, summary.engine, len(messages),
    )

    return DigestResult(
        chat_id=chat_id,
        chat_title=title,
        text=full_text,
        engine=summary.engine,
        messages_count=len(messages),
        masked_total=masked_total,
        calendar_events=calendar_events,
    )


async def load_demo_messages(db: Database, chat_id: int) -> dict:
    """25 демо-сообщений проходят то же маскирование, что и сообщения живого чата."""

    await db.link_chat(chat_id, 0, DEMO_CHAT_TITLE, is_demo=True)
    await db.clear_buffer(chat_id)

    examples = []
    total_stats = anonymizer.MaskStats()
    shown = 0

    # Эта же карта уходит в буфер — по ней сводка вернёт имена учителю.
    demo_map: dict = {}

    for item in DEMO_MESSAGES:
        masked_sender = anonymizer.mask_person_name(
            item["sender"], name_map=demo_map
        )
        masked_text, stats = anonymizer.mask_text(item["text"], name_map=demo_map)
        total_stats = total_stats + stats

        await db.add_message(
            chat_id, masked_sender, masked_text,
            masked=int(stats.total), name_map=demo_map,
        )

        # Три примера показываем целиком: было -> стало.
        if shown < 3 and stats.total > 0:
            examples.append({
                "sender": item["sender"],
                "original": item["text"],
                "masked_sender": masked_sender,
                "masked": masked_text,
                "found": stats.total,
            })
            shown += 1

    await db.bump_stat("messages_processed", len(DEMO_MESSAGES))

    return {
        "chat_id": chat_id,
        "count": len(DEMO_MESSAGES),
        "masked_total": total_stats.total,
        "phones": total_stats.phones,
        "cards": total_stats.cards,
        "passports": total_stats.passports,
        "snils": total_stats.snils,
        "emails": total_stats.emails,
        "names": total_stats.names,
        "examples": examples,
    }


def demo_greeting() -> str:

    return (
        "🧪 Демо-режим загружен!\n"
        "В буфер добавлено 25 реалистичных сообщений родительского чата "
        "3-го класса — с телефонами, номерами карт и именами.\n\n"
        "Нажмите «📊 Сводка за день», чтобы увидеть готовый дайджест."
    )


async def get_demo_calendar_events(db: Database, chat_id: int) -> list:
    """Сначала сохранённые события последней сводки; если их нет — ищем в текстах."""

    saved = await db.get_events(chat_id)
    if saved:
        return saved

    texts = []
    last = await db.get_last_digest(chat_id)
    if last:
        texts.append(last.get("text", ""))
    for m in await db.get_messages(chat_id):
        texts.append(m.get("text", ""))
    return calendar_export.extract_events(texts)
