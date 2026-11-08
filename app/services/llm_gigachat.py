"""Сводка через GigaChat, а без ключа или сети — локальным модулем по ключевым словам.

На выходе всегда стоит фильтр медицинских подробностей.
"""

import logging
import re
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger(__name__)

try:
    from gigachat import GigaChat, Messages, MessagesRole
except Exception:
    GigaChat = None
    Messages = None
    MessagesRole = None


@dataclass
class SummarizeResult:

    text: str
    engine: str
    error: str | None = None


CATEGORY_QUESTIONS = "❓ Вопросы к учителю"
CATEGORY_ABSENCE = "🏥 Отсутствующие и справки"
CATEGORY_MONEY = "💰 Договорённости и сборы"
CATEGORY_ACTIONS = "⚡ Требует действия учителя"

EMPTY_LINE = "— пусто —"

SYSTEM_PROMPT = (
    "Ты — помощник классного руководителя начальной школы. Тебе присылают "
    "обезличенные сообщения из родительского чата: люди обозначены "
    "маркерами вида [PER_12 | М. И.] (ученики и упомянутые люди) и "
    "[USR_3 | О. С.] (авторы сообщений), телефоны, карты, паспорта, СНИЛС "
    "и почта скрыты.\n"
    "Разложи сообщения строго по четырём категориям, заголовки сохрани дословно:\n"
    "❓ Вопросы к учителю — прямые вопросы и просьбы к педагогу;\n"
    "🏥 Отсутствующие и справки — кто пропустит уроки, опоздает, уйдёт к врачу, нужна ли справка;\n"
    "💰 Договорённости и сборы — деньги, документы, поездки, родительский комитет;\n"
    "⚡ Требует действия учителя — конкретные шаги классного руководителя.\n"
    "Жёсткие правила:\n"
    "1. Категорически запрещено указывать диагнозы, симптомы и названия лекарств. "
    "Пиши нейтрально: «ученик отсутствует по болезни», «посещение врача».\n"
    "2. Информационный шум (открытки, пожелания доброго утра, поздравления, «спасибо», "
    "«+1», споры не по делу) полностью игнорируй.\n"
    "3. Маркеры [PER_...] и [USR_...] копируй дословно, без изменений, без предлогов "
    "и без склонения. Маркер ученика ставь в начало пункта сразу после тире "
    "(«• [PER_12 | М. И.] — завтра к врачу»), маркер автора — в круглых скобках "
    "в конце пункта («... ([USR_3 | О. С.])»).\n"
    "4. Формат: краткие маркированные пункты «• ...». "
    "Если в категории ничего важного нет — напиши «— пусто —»."
)

# Основа слова + \w* ловит падежи и окончания: «ротавирусом», «температуру».
_MEDICAL_STEMS = (
    "ротавирус", "ротовирус", "ветряная оспа", "ветрянка", "оспа", "корь", "краснуха",
    "ангина", "тонзиллит", "грипп", "орви", "орз", "простуд", "температур", "жар",
    "кашель", "насморк", "ринит", "конъюнктивит", "отит", "стоматит", "бронхит",
    "пневмони", "аллерг", "нурофен", "парацетамол", "ибупрофен", "антибиотик",
    "свечи", "суспензи", "диаре", "рвот",
)

_MEDICAL_RE = re.compile(
    r"\b(?:" + "|".join(sorted(_MEDICAL_STEMS, key=len, reverse=True)) + r")\w*\b",
    re.IGNORECASE,
)

# «по болезни 39», «по болезни 39,5 градусов» -> «по болезни».
_ILLNESS_NUMBERS_RE = re.compile(
    r"по болезни[\s,]*(?:\d+(?:[.,]\d+)?|°|градус\w*)[\s,]*",
    re.IGNORECASE,
)
# Подряд идущие «по болезни, по болезни» -> одно «по болезни».
_ILLNESS_REPEAT_RE = re.compile(r"по болезни(?:[\s,]+по болезни)+", re.IGNORECASE)
# «по болезни» через короткую вставку («у нас (((», «уже») — оставляем первое.
_ILLNESS_GAP_REPEAT_RE = re.compile(r"по болезни([^.,!?]{1,30}?)\s+по болезни\b", re.IGNORECASE)
# «заболел, по болезни, высокая по болезни» — одна болезнь: схлопываем вставки.
_ILLNESS_CHAIN_RE = re.compile(r"по болезни[^.!?]{0,30}?по болезни", re.IGNORECASE)
# Слова вокруг нейтрального оборота («высокая по болезни», «по болезни градусов»).
_ILLNESS_ADJ_BEFORE_RE = re.compile(
    r"\b(?:высокая|сильная|слабая|низкая|большая|небольшая)\s+(?=по болезни)",
    re.IGNORECASE,
)
_ILLNESS_ADJ_AFTER_RE = re.compile(
    r"по болезни[\s,]+(?:высокая|сильная|слабая|низкая|большая|небольшая|градус\w*)",
    re.IGNORECASE,
)

_GREETING_RE = re.compile(
    r"^(?:здравствуйте|добрый день|добрый вечер|доброе утро|дорогие родители|"
    r"девочки|мальчики|друзья)[!,. ]*",
    re.IGNORECASE,
)

QUESTION_HINTS = ("?", "подскаж", "уточните", "можно ли", "когда", "куда", "спрос")
ABSENCE_HINTS = (
    "заболе", "болеет", "болеем", "не прид", "не будет", "отсутств",
    "справк", "врач", "стоматолог", "температур", "педиатр",
)
MONEY_HINTS = (
    "сбор", "деньги", "руб", "карт", "оплат", "взнос", "экскурси", "поездк",
    "театр", "собрани", "документ", "сда", "заказ",
)
ACTION_HINTS = ("напомн", "принес", "подписа", "заявлени", "сообщ", "уточн")


# Что не должно уходить в модель: названия болезней и измерения (без коротких стемов,
# которые ловят обычные слова вроде «жарко»).
_MEDICAL_INPUT_RE = re.compile(
    r"\b(?:ротавирус\w*|ветряная оспа|ветрянка|корь|краснуха|ангина|тонзиллит|грипп\w*|"
    r"орви|орз|простуда|простудил\w*|кашель|насморк|бронхит|пневмони\w*|аллерги\w*|"
    r"антибиотик\w*|нурофен|парацетамол|ибупрофен|температур\w*|жар\b|рвота|диарея)\b",
    re.IGNORECASE,
)
_MEASURE_INPUT_RE = re.compile(r"\b\d{2}[.,]?\d?\s*(?:°|градус\w*)", re.IGNORECASE)
# После замены может остаться голая цифра: «температура 39» -> «по болезни 39».
_ILLNESS_NUMBER_TAIL_RE = re.compile(r"по болезни[\s,]*\d+[.,]?\d*", re.IGNORECASE)


def mask_medical_details(text: str) -> str:
    """Убирает диагнозы и измерения из текста, который уйдёт в модель."""
    clean = _MEASURE_INPUT_RE.sub("по болезни", text)
    clean = _MEDICAL_INPUT_RE.sub("по болезни", clean)
    clean = _ILLNESS_NUMBER_TAIL_RE.sub("по болезни", clean)
    return " ".join(_collapse_illness(clean).split())


# «по болезни и.» — висячие союзы после замены убираем.
_ILLNESS_DANGLING_RE = re.compile(
    r"по болезни[\s,]+(?:и|а|но|или|да)\b[\s,]*", re.IGNORECASE
)


def _collapse_illness(text: str) -> str:
    """«по болезни, высокая по болезни» -> «по болезни»."""
    clean = _ILLNESS_ADJ_BEFORE_RE.sub("", text)
    clean = _ILLNESS_ADJ_AFTER_RE.sub("по болезни", clean)
    clean = _ILLNESS_DANGLING_RE.sub("по болезни ", clean)
    clean = _ILLNESS_REPEAT_RE.sub("по болезни", clean)
    clean = _ILLNESS_GAP_REPEAT_RE.sub("по болезни\\1", clean)
    # Повторы могут идти цепочкой — схлопываем, пока есть что схлопывать.
    while True:
        merged = _ILLNESS_CHAIN_RE.sub("по болезни", clean)
        if merged == clean:
            break
        clean = merged
    return clean


def sanitize_medical(text: str) -> str:
    """Диагнозы, симптомы и лекарства заменяем на «по болезни»."""
    clean = _MEDICAL_RE.sub("по болезни", text)
    clean = _ILLNESS_NUMBERS_RE.sub("по болезни ", clean)
    return " ".join(_collapse_illness(clean).split())


# GigaChat меняет эмодзи заголовков и добавляет markdown-звёздочки — возвращаем свои.
_CANONICAL_HEADS = (
    (CATEGORY_QUESTIONS, ("вопрос",)),
    (CATEGORY_ABSENCE, ("отсутств", "справк")),
    (CATEGORY_MONEY, ("договор",)),
    (CATEGORY_ACTIONS, ("требует действия",)),
)


def _polish(text: str) -> str:
    """Убираем markdown-звёздочки GigaChat и возвращаем заголовкам наши эмодзи."""
    clean = text.replace("**", "")
    lines: list[str] = []
    for line in clean.splitlines():
        stripped = line.strip()
        replaced = False
        if stripped and "•" not in stripped and len(stripped) <= 45:
            low = stripped.lower()
            for canonical, keys in _CANONICAL_HEADS:
                if all(key in low for key in keys):
                    lines.append(canonical)
                    replaced = True
                    break
        if not replaced:
            lines.append(line)
    return "\n".join(lines)


def _has_any(text_lower: str, hints: tuple) -> bool:
    return any(hint in text_lower for hint in hints)


def _category_of(text_lower: str) -> str | None:
    if _has_any(text_lower, QUESTION_HINTS):
        return CATEGORY_QUESTIONS
    if _has_any(text_lower, ABSENCE_HINTS):
        return CATEGORY_ABSENCE
    if _has_any(text_lower, MONEY_HINTS):
        return CATEGORY_MONEY
    if _has_any(text_lower, ACTION_HINTS):
        return CATEGORY_ACTIONS
    return None


# Маркер ученика выносим в начало пункта, чтобы полное имя не ломало согласование.
_PER_TOKEN_RE = re.compile(r"\[PER_\d+(?:\s*\|\s*[^\]]+)?\]")


def _make_point(sender: str, body: str) -> str | None:
    """Пункт сводки: без приветствий, маркер ученика — в начале."""
    clean = sanitize_medical(body)
    clean = _GREETING_RE.sub("", clean)
    while True:
        trimmed = _GREETING_RE.sub("", clean)
        if trimmed == clean:
            break
        clean = trimmed

    leads: list[str] = []
    for token in _PER_TOKEN_RE.findall(clean):
        if token not in leads:
            leads.append(token)
    if leads:
        clean = _PER_TOKEN_RE.sub("", clean)

    clean = " ".join(clean.split())
    clean = clean.lstrip(" ,;:-—").strip()
    if not clean and not leads:
        return None
    if leads:
        prefix = " ".join(leads)
        clean = f"{prefix} — {clean}" if clean else prefix
    if len(clean) > 110:
        clean = clean[:110].rstrip(" ,;:-") + "..."
    if sender:
        return f"• {clean} ({sender})"
    return f"• {clean}"


async def fallback_summarize(messages: list[dict]) -> SummarizeResult:
    """Запасная сводка по ключевым словам, без обращения к GigaChat."""
    buckets: dict[str, list[str]] = {
        CATEGORY_QUESTIONS: [],
        CATEGORY_ABSENCE: [],
        CATEGORY_MONEY: [],
        CATEGORY_ACTIONS: [],
    }
    seen: set[str] = set()

    for message in messages:
        text = str(message.get("text", "")).strip()
        if not text:
            continue
        category = _category_of(text.lower())
        if category is None:
            continue

        sender = str(message.get("sender", "") or "").strip()
        point = _make_point(sender, text)
        if point is None:
            continue

        fingerprint = re.sub(r"\W+", "", point.lower())[:32]
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        buckets[category].append(point)

    lines: list[str] = []
    for category, points in buckets.items():
        lines.append(category)
        if points:
            lines.extend(points[:5])
        else:
            lines.append(EMPTY_LINE)
        lines.append("")

    summary = "\n".join(lines).strip()
    return SummarizeResult(text=sanitize_medical(summary), engine="fallback")


def _extract_answer(response) -> tuple[str, str | None]:
    choices = getattr(response, "choices", None)
    if not choices:
        return "", None
    choice = choices[0]
    message = getattr(choice, "message", None)
    text = (getattr(message, "content", "") or "").strip()
    finish_reason = getattr(choice, "finish_reason", None)
    return text, finish_reason


async def summarize(messages: list[dict]) -> SummarizeResult:
    if not messages or GigaChat is None or not settings.gigachat_credentials:
        return await fallback_summarize(messages)

    dialog = "\n".join(
        f"{str(m.get('sender', ''))}: {str(m.get('text', ''))}".strip()
        for m in messages
    )
    payload = {
        "messages": [
            Messages(role=MessagesRole.SYSTEM, content=SYSTEM_PROMPT),
            Messages(role=MessagesRole.USER, content=dialog),
        ],
    }

    try:
        async with GigaChat(
            credentials=settings.gigachat_credentials,
            verify_ssl_certs=False,
            scope=settings.gigachat_scope,
            model=settings.gigachat_model,
        ) as giga:
            response = await giga.achat(payload)

        text, finish_reason = _extract_answer(response)
        if finish_reason == "blacklist" or not text:
            raise ValueError(f"пустой или заблокированный ответ (finish_reason={finish_reason})")

        return SummarizeResult(
            text=sanitize_medical(_polish(text)), engine="gigachat"
        )
    except Exception as exc:
        logger.warning("GigaChat не смог собрать сводку (%s), включаем локальный модуль", exc)
        result = await fallback_summarize(messages)
        return SummarizeResult(text=result.text, engine="fallback", error=str(exc))
