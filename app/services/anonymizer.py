"""Обезличивание сообщений: телефоны, карты, паспорта, СНИЛС, почта и имена.

В GigaChat уходят маркеры вида [PER_12 | М. И.], полные имена лежат в name_map.
"""

import gc
import logging
import re
import time
import warnings
import zlib
from dataclasses import dataclass

warnings.filterwarnings("ignore", category=UserWarning)

logger = logging.getLogger(__name__)


@dataclass
class MaskStats:

    phones: int = 0
    cards: int = 0
    passports: int = 0
    snils: int = 0
    emails: int = 0
    names: int = 0

    @property
    def total(self) -> int:
        return (self.phones + self.cards + self.passports + self.snils
                + self.emails + self.names)

    def __add__(self, other: "MaskStats") -> "MaskStats":
        return MaskStats(
            phones=self.phones + other.phones,
            cards=self.cards + other.cards,
            passports=self.passports + other.passports,
            snils=self.snils + other.snils,
            emails=self.emails + other.emails,
            names=self.names + other.names,
        )


# Карты раньше паспортов: иначе 16-значный номер карты режется на «паспортные» куски.
CARD_RE = re.compile(r"\b\d{4}(?:[ -]\d{4}){3}\b|\b\d{16}\b")
PASSPORT_RE = re.compile(r"\b\d{4}[ -]?\d{6}\b")
SNILS_RE = re.compile(r"\b\d{3}[ -]\d{3}[ -]\d{3}[ -]\d{2}\b")
PHONE_RE = re.compile(r"(?:\+7|(?<!\d)8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}")

CARD_MASK = "[КАРТА СКРЫТА]"
PASSPORT_MASK = "[ПАСПОРТ СКРЫТ]"
SNILS_MASK = "[СНИЛС СКРЫТ]"
PHONE_MASK = "[ТЕЛЕФОН СКРЫТ]"
EMAIL_MASK = "[EMAIL СКРЫТ]"

# Natasha одна на процесс: тяжёлые модели грузим лениво.
_natasha = None
_NATASHA_BROKEN = object()
_NATASHA_IDLE_SECONDS = 15 * 60  # после 15 минут простоя модели выгружаем
_natasha_last_used = 0.0


def _get_natasha():
    """Natasha поднимается лениво (модели около гигабайта) и выгружается после простоя."""
    global _natasha, _natasha_last_used
    now = time.monotonic()
    if _natasha is _NATASHA_BROKEN:
        return _natasha
    if _natasha is not None and now - _natasha_last_used > _NATASHA_IDLE_SECONDS:
        logger.info("Natasha не использовалась %s мин — выгружаем модели",
                    _NATASHA_IDLE_SECONDS // 60)
        _natasha = None
        gc.collect()
    if _natasha is None:
        try:
            from natasha import (
                Doc,
                MorphVocab,
                NewsEmbedding,
                NewsMorphTagger,
                NewsNERTagger,
                Segmenter,
            )

            # Эмбеддинги общие для обоих теггеров.
            embeddings = NewsEmbedding()
            _natasha = {
                "doc": Doc,
                "segmenter": Segmenter(),
                "vocab": MorphVocab(),
                "morph": NewsMorphTagger(embeddings),
                "ner": NewsNERTagger(embeddings),
            }
        except Exception:
            logger.warning("Natasha недоступна, имена останутся в тексте")
            _natasha = _NATASHA_BROKEN
    _natasha_last_used = now
    return _natasha


def _to_initials(name: str) -> str:
    initials = []
    for word in name.split():
        for letter in word:
            if letter.isalpha():
                initials.append(letter.upper() + ".")
                break
    return " ".join(initials)


def _token_number(normal_name: str) -> int:
    """Номер от нормализованного имени: один человек в любом падеже даёт тот же маркер."""
    return zlib.crc32(normal_name.strip().lower().encode("utf-8")) % 9973 + 1


def _token(prefix: str, normal_name: str) -> str:
    number = _token_number(normal_name)
    initials = _to_initials(normal_name)
    if initials:
        return f"[{prefix}_{number} | {initials}]"
    return f"[{prefix}_{number}]"


# Маркеры вида [PER_12 | М. И.] и [USR_3]: GigaChat иногда пишет их без пробелов.
_TOKEN_RE = re.compile(r"\[(PER|USR)_(\d+)(?:\s*\|\s*([^\]]*))?\]")


def restore_names(text: str, mapping: dict) -> str:
    """Маркеры обратно в полные имена: сперва по номеру, потом по инициалам.

    Неопознанные обрезки маркеров вырезаем, чтобы учителю не попадал сырой [PER_...].
    """
    by_id = {}
    by_initials = {}
    for full, name in mapping.items():
        match = _TOKEN_RE.fullmatch(full.strip())
        if match:
            by_id.setdefault(f"{match.group(1)}_{match.group(2)}", name)
        ini = initials_of(name)
        if ini:
            by_initials.setdefault(ini.lower(), name)

    def _sub(match):
        key = f"{match.group(1)}_{match.group(2)}"
        if key in by_id:
            return by_id[key]
        initials = (match.group(3) or "").strip()
        return initials if initials else match.group(0)

    text = _TOKEN_RE.sub(_sub, text)

    # Сломанные маркеры ([PER_... | М. С.]) опознаём по инициалам, остальное вырезаем.
    broken = re.compile(r"\[\s*(?:PER|USR)[^\]]*\]", re.IGNORECASE)

    def _sub_broken(match):
        token = match.group(0)
        if "|" in token:
            tail = token.split("|", 1)[1].rstrip("]").strip()
            hit = by_initials.get(tail.lower())
            if hit:
                return hit
        return ""

    text = broken.sub(_sub_broken, text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def initials_of(full_name: str) -> str:
    """«Миша Смирнов» -> «М. С.», «Мария Александровна» -> «М. А.»."""
    words = [w for w in full_name.strip().split() if w]
    letters = [w[0].upper() for w in words[:2] if w[0].isalpha()]
    if not letters:
        return ""
    return ". ".join(letters) + "."


# Части речи, которых в именах не бывает: Natasha принимает за PER
# повелительные глаголы («Напомните», «Подскажите»).
_NOT_A_NAME_POS = {
    "VERB", "INFN", "PRTF", "PRTS", "GRND",
    "PREP", "CONJ", "NPRO", "PRCL", "INTJ",
}


def _is_not_a_name(span_text: str, vocab) -> bool:
    for word in span_text.split():
        try:
            parses = vocab.parse(word)
        except Exception:
            return False
        if parses and str(parses[0].pos) in _NOT_A_NAME_POS:
            return True
    return False


# Одиночные имена без фамилии NER пропускает («Маша уйдёт с урока») — добираем словарём.
_NAME_WORD_RE = re.compile(r"\b([А-ЯЁ][а-яё]{2,})\b")
_NAME_GRAMMEMES = {"Name", "Patr", "Surn"}


def _mask_single_names(masked: str, local_map: dict, stats: MaskStats, vocab) -> str:
    """Добор одиночных имён по словарю: обычные слова («Собрание», «Спасибо») не трогаем."""

    def _replace(match):
        word = match.group(1)
        try:
            parses = vocab.parse(word)
        except Exception:
            return word
        for parse in parses[:3]:
            grammemes = getattr(getattr(parse, "tag", None), "grammemes", None)
            if not grammemes:
                continue
            if _NAME_GRAMMEMES & {str(g) for g in grammemes}:
                normal = " ".join(
                    part.capitalize() for part in str(parse.normal_form).split()
                )
                token = _token("PER", normal)
                local_map[token] = normal
                stats.names += 1
                return token
        return word

    return _NAME_WORD_RE.sub(_replace, masked)


def mask_text(text: str, name_map: dict | None = None) -> tuple[str, MaskStats]:
    """Обезличенный текст и статистика; name_map дополняется парами «маркер → имя»."""
    stats = MaskStats()
    masked, stats.cards = CARD_RE.subn(CARD_MASK, text)
    masked, stats.passports = PASSPORT_RE.subn(PASSPORT_MASK, masked)
    masked, stats.snils = SNILS_RE.subn(SNILS_MASK, masked)
    masked, stats.phones = PHONE_RE.subn(PHONE_MASK, masked)
    masked, stats.emails = EMAIL_RE.subn(EMAIL_MASK, masked)

    pipeline = _get_natasha()
    if pipeline is _NATASHA_BROKEN:
        return masked, stats

    local_map = name_map if name_map is not None else {}
    try:
        doc = pipeline["doc"](masked)
        doc.segment(pipeline["segmenter"])
        doc.tag_ner(pipeline["ner"])
        # Без тегов MorphVocab не приведёт «Илью Иванова» к «Илья Иванов».
        doc.tag_morph(pipeline["morph"])
        spans = [s for s in doc.spans if s.type == "PER"]
        for span in sorted(spans, key=lambda item: item.start, reverse=True):
            if _is_not_a_name(span.text, pipeline["vocab"]):
                # «Напомните...» — глагол, а не имя: спан не трогаем.
                continue
            try:
                span.normalize(pipeline["vocab"])
                normal = span.normal or span.text
            except Exception:
                normal = span.text
            token = _token("PER", normal)
            masked = masked[:span.start] + token + masked[span.stop:]
            local_map[token] = normal
            stats.names += 1
    except Exception:
        logger.warning("Natasha не смогла разметить текст, имена оставлены как есть")

    # Имена без фамилии, которые NER пропустил.
    try:
        masked = _mask_single_names(masked, local_map, stats, pipeline["vocab"])
    except Exception:
        logger.warning("Словарный проход по одиночным именам не удался")

    return masked, stats


def mask_person_name(name: str, name_map: dict | None = None) -> str:
    clean = " ".join(str(name).split())
    if not clean:
        return clean
    if name_map is None:
        return _to_initials(clean)
    token = _token("USR", clean)
    name_map[token] = clean
    return token
