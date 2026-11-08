"""Поиск событий в сообщениях и выгрузка расписания в формат .ics."""

import datetime
import logging
import re
import uuid
import zoneinfo

from ics import Calendar, Event

logger = logging.getLogger(__name__)

MAX_EVENTS = 10

_MONTH_BY_NAME = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

_MONTH_DAY_RE = re.compile(
    r"\b(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|"
    r"сентября|октября|ноября|декабря)\b(?:\s+(\d{4}))?",
    re.IGNORECASE,
)

# «15.10», «20.11.2026», «15-10»; месяц вне 1-12 отбрасывается при проверке.
_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\b")

_TIME_RE = re.compile(r"\b(\d{1,2})[:.](\d{2})\b")
_TIME_DASH_RE = re.compile(r"\bв\s+(\d{1,2})-(\d{2})\b")

_WEEKDAYS = {
    "понедельник": 0, "вторник": 1, "среду": 2, "четверг": 3,
    "пятницу": 4, "субботу": 5, "воскресенье": 6,
}

_EVENT_RULES = [
    ("собрани", "Родительское собрание"),
    ("экскурси", "Экскурсия"),
    ("театр", "Поход в театр"),
    ("музе", "Поход в музей"),
    ("олимпиад", "Олимпиада"),
    ("утренник", "Утренник"),
    ("линейк", "Линейка"),
]


def _safe_date(year: int, month: int, day: int) -> datetime.date | None:
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def _today() -> datetime.date:
    """Сегодня по Москве: сервер живёт в UTC, и вечером «завтра» уезжало не туда."""
    try:
        return datetime.datetime.now(zoneinfo.ZoneInfo("Europe/Moscow")).date()
    except Exception:  # noqa: BLE001 - нет tzdata: работаем по локальной дате
        return datetime.date.today()


def _next_weekday(weekday: int) -> datetime.date:
    today = _today()
    days_ahead = (weekday - today.weekday() - 1) % 7 + 1
    return today + datetime.timedelta(days=days_ahead)


def _find_dates(text_lower: str) -> tuple[datetime.date | None, list[tuple[int, int]]]:
    candidates: list[tuple[int, datetime.date]] = []
    spans: list[tuple[int, int]] = []
    today = _today()

    # Год без подсказки — текущий; прошедшую дату в следующий год не переносим.
    for match in _MONTH_DAY_RE.finditer(text_lower):
        day = int(match.group(1))
        month = _MONTH_BY_NAME[match.group(2).lower()]
        year = int(match.group(3)) if match.group(3) else today.year
        date = _safe_date(year, month, day)
        if date is not None:
            candidates.append((match.start(), date))
            spans.append((match.start(), match.end()))

    for match in _NUMERIC_DATE_RE.finditer(text_lower):
        day, month = int(match.group(1)), int(match.group(2))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        year_raw = match.group(3)
        if year_raw:
            year = int(year_raw)
            year = year + 2000 if year < 100 else year
        else:
            year = today.year
        date = _safe_date(year, month, day)
        if date is not None:
            candidates.append((match.start(), date))
            spans.append((match.start(), match.end()))

    # «Сегодня», «завтра», «послезавтра» в календарь не берём: в .ics это мусор,
    # а подстрока «завтра» внутри «послезавтра» раньше давала лишнее событие.
    for word, weekday in _WEEKDAYS.items():
        position = text_lower.find(word)
        if position >= 0:
            candidates.append((position, _next_weekday(weekday)))

    if not candidates:
        return None, spans
    return min(candidates, key=lambda item: item[0])[1], spans


def _find_time(text_lower: str, spans: list[tuple[int, int]]) -> str | None:
    for pattern in (_TIME_RE, _TIME_DASH_RE):
        for match in pattern.finditer(text_lower):
            if any(start <= match.start() < stop for start, stop in spans):
                continue
            hour, minute = int(match.group(1)), int(match.group(2))
            if hour > 23 or minute > 59:
                continue
            return f"{hour:02d}:{minute:02d}"
    return None


def _title_from(text_lower: str) -> str | None:
    """Одно название на событие: «поездка, автобус и экскурсии» — это не три события."""
    if "санкт-петербург" in text_lower:
        return "Поездка в Санкт-Петербург"
    if "собрани" in text_lower:
        return "Родительское собрание"
    if "сда" in text_lower and "документ" in text_lower:
        return "Сдача документов"
    if "театр" in text_lower:
        return "Поход в театр"
    if "музе" in text_lower:
        return "Поход в музей"
    for word, title in _EVENT_RULES:
        if word in text_lower:
            return title
    if "поездк" in text_lower:
        return "Поездка"
    if "поход" in text_lower:
        return "Поход"
    return None


# Событие ищем внутри одного предложения, а не по всему сообщению.
_SENTENCE_RE = re.compile(r"(?<=[.!?...])\s+|\n+")


def _events_from_text(text: str) -> list[dict]:
    """Разбор по предложениям: название и дата из соседних предложений склеиваются."""
    events: list[dict] = []
    pending_title: str | None = None
    for segment in _SENTENCE_RE.split(str(text)):
        low = segment.lower().strip()
        if not low:
            continue
        title = _title_from(low)
        date, spans = _find_dates(low)
        if title and date is not None:
            events.append({"title": title, "date": date,
                           "time": _find_time(low, spans)})
            pending_title = None
        elif title:
            pending_title = title
        elif date is not None and pending_title and _can_glue(low, spans):
            events.append({"title": pending_title, "date": date,
                           "time": _find_time(low, spans)})
            pending_title = None
    return events


# Дату из соседнего предложения принимаем, если там есть время или глагол («выезжаем»).
_GLUE_VERBS = ("выезж", "выезд", "состо", "будет", "начн", "встреч",
               "отправл", "собираемся")
# Срок оплаты — не событие: «переводите до 10 октября» в календарь не берём.
_GLUE_BLOCK = ("перевед", "перевод", "оплат", "стоимост", "сдаём", "сдаем")


def _can_glue(segment_lower: str, spans: list[tuple[int, int]]) -> bool:
    if any(word in segment_lower for word in _GLUE_BLOCK):
        return False
    if _find_time(segment_lower, spans) is not None:
        return True
    return any(word in segment_lower for word in _GLUE_VERBS)


def extract_events(texts: list[str]) -> list[dict]:
    """События из текстов: {title, date, time}; дубли по (title, date) режем, больше 10 не отдаём."""
    events: list[dict] = []
    seen: set[tuple[str, datetime.date | None]] = set()
    for text in texts:
        for event in _events_from_text(str(text)):
            key = (event["title"], event["date"])
            if key in seen:
                continue
            seen.add(key)
            events.append(event)
            if len(events) >= MAX_EVENTS:
                logger.info("Событий больше %s, остальные не включаем", MAX_EVENTS)
                return events
    return events


def _parse_time(value: str | None) -> tuple[int, int]:
    if value:
        match = re.match(r"^(\d{1,2})[:.](\d{2})$", value.strip())
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
            if hour <= 23 and minute <= 59:
                return hour, minute
    return 9, 0


def _moscow_tz():
    try:
        return zoneinfo.ZoneInfo("Europe/Moscow")
    except Exception:
        return None


def build_ics(events: list[dict], calendar_name: str = "Классный дайджест") -> str:
    calendar = Calendar()
    tz = _moscow_tz()
    today = _today()
    # DTSTAMP по RFC 5545 — в UTC, поэтому берём не локальное время сервера.
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for number, event in enumerate(events, start=1):
        date = event.get("date")
        if date is not None and date < today:
            # Прошедшие события в календаре учителя только мешают.
            continue
        date = date or today
        hour, minute = _parse_time(event.get("time"))
        item = Event(
            name=str(event.get("title") or "Событие"),
            begin=datetime.datetime(date.year, date.month, date.day, hour, minute, tzinfo=tz),
        )
        item.uid = f"class-digest-{date.strftime('%Y%m%d')}-{number}-{uuid.uuid4().hex[:8]}"
        item.duration = datetime.timedelta(hours=1)
        calendar.events.add(item)

    text = calendar.serialize()
    text = text.replace("\r\r\n", "\r\n")

    # Библиотека не пишет название календаря и DTSTAMP — добавляем сами,
    # без них Google Calendar жалуется на файл.
    if calendar_name:
        text = text.replace(
            "BEGIN:VCALENDAR\r\n",
            f"BEGIN:VCALENDAR\r\nX-WR-CALNAME:{calendar_name}\r\n",
            1,
        )
    text = text.replace("BEGIN:VEVENT\r\n", f"BEGIN:VEVENT\r\nDTSTAMP:{stamp}\r\n")
    return text


def save_ics(events: list[dict], path: str, calendar_name: str = "Классный дайджест") -> str:
    content = build_ics(events, calendar_name)
    with open(path, "w", encoding="utf-8") as file:
        file.write(content)
    return str(path)
