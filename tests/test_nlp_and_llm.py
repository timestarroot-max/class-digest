"""Проверки обезличивания, запасной сводки и экспорта в календарь."""

import sys
from pathlib import Path

# Проект не устанавливается в venv, поэтому добавляем корень к sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dataclasses

from app.config import settings as project_settings
from app.demo.sample_messages import DEMO_MESSAGES
from app.services import anonymizer, calendar_export, llm_gigachat

CATEGORY_HEADERS = [
    "❓ Вопросы к учителю",
    "🏥 Отсутствующие и справки",
    "💰 Договорённости и сборы",
    "⚡ Требует действия учителя",
]


def test_mask_phone_variants():
    masked, stats = anonymizer.mask_text("Позвоните +7 900 123-45-67")
    assert "[ТЕЛЕФОН СКРЫТ]" in masked
    assert "919" not in masked
    assert stats.phones == 1

    masked, stats = anonymizer.mask_text("тел 8 (910) 123 45 67")
    assert "[ТЕЛЕФОН СКРЫТ]" in masked
    assert "910" not in masked
    assert stats.phones == 1

    masked, stats = anonymizer.mask_text("наберите 89001234567")
    assert "[ТЕЛЕФОН СКРЫТ]" in masked
    assert "1234" not in masked
    assert stats.phones == 1


def test_mask_bank_cards():
    masked, stats = anonymizer.mask_text("Карта 4276550012345678, вторая 4276 5500 1234 5678")
    assert masked.count("[КАРТА СКРЫТА]") == 2
    assert stats.cards == 2
    assert "4276" not in masked
    assert "1234" not in masked


def test_mask_passport():
    masked, stats = anonymizer.mask_text("паспорт 4510 123456")
    assert "[ПАСПОРТ СКРЫТ]" in masked
    assert "4510" not in masked
    assert "123456" not in masked
    assert stats.passports == 1

    masked, stats = anonymizer.mask_text("серия 4510123456")
    assert "[ПАСПОРТ СКРЫТ]" in masked
    assert stats.passports == 1


def test_mask_snils():
    masked, stats = anonymizer.mask_text("СНИЛС 123-456-789 45")
    assert "[СНИЛС СКРЫТ]" in masked
    assert "456" not in masked
    assert stats.snils == 1


def test_mask_email():
    masked, stats = anonymizer.mask_text("пишите на t.frolova1975@yandex.ru")
    assert "[EMAIL СКРЫТ]" in masked
    assert "yandex" not in masked
    assert "frolova" not in masked
    assert stats.emails == 1


def test_natasha_replaces_names_with_initials():
    masked, stats = anonymizer.mask_text("Маша Иванова заболела")
    # В нейросеть уходит маркер с инициалами, само имя — нет.
    assert "[PER_" in masked and "М. И." in masked
    assert "Маша" not in masked and "Иванова" not in masked
    assert stats.names >= 1


def test_natasha_replaces_patronymic():
    masked, _stats = anonymizer.mask_text("Анна Сергеевна")
    assert masked.strip().startswith("[PER_") and "А. С." in masked


def test_name_map_and_restore():
    """Маркер обратим: в карту идёт именительный падеж, обратно — полное имя."""
    name_map = {}
    masked, _stats = anonymizer.mask_text(
        "Завтра отведу Илью Иванова к врачу", name_map=name_map
    )
    assert len(name_map) == 1
    token, full_name = next(iter(name_map.items()))
    assert full_name == "Илья Иванов", "имя должно быть в именительном падеже"
    assert token in masked and "Илья" not in masked

    # Тот же человек в другом падеже — тот же номер маркера.
    name_map2 = {}
    masked2, _stats = anonymizer.mask_text(
        "Справку по Илье Иванову принесём завтра", name_map=name_map2
    )
    assert next(iter(name_map2)) == token

    # Обратная подстановка: маркер -> полное имя в сводке учителю.
    restored = anonymizer.restore_names(
        f"• {token} — завтра к врачу", name_map
    )
    assert "Илья Иванов" in restored and "[PER_" not in restored

    # Модель могла написать маркер без инициалов — имя всё равно вернётся.
    bare = token.split(" | ")[0] + "]"
    restored2 = anonymizer.restore_names(f"• {bare} — к врачу", name_map)
    assert "Илья Иванов" in restored2


def test_mask_person_name():
    assert anonymizer.mask_person_name("Ольга Смирнова") == "О. С."
    assert anonymizer.mask_person_name("Игорь Волков") == "И. В."

    # С картой имя отправителя становится маркером USR.
    name_map = {}
    token = anonymizer.mask_person_name("Ольга Смирнова", name_map=name_map)
    assert token.startswith("[USR_") and "О. С." in token
    assert name_map[token] == "Ольга Смирнова"


def test_mask_stats_total_and_sum():
    first = anonymizer.MaskStats(phones=1, cards=2, names=3)
    second = anonymizer.MaskStats(emails=4, snils=1)

    total = first + second

    assert first.total == 6
    assert total.phones == 1
    assert total.cards == 2
    assert total.names == 3
    assert total.emails == 4
    assert total.snils == 1
    assert total.passports == 0
    assert total.total == 11


def test_sanitize_medical_removes_diagnoses():
    text = "Миша болеет ротавирусом, температура 39, дали нурофен"
    clean = llm_gigachat.sanitize_medical(text)
    low = clean.lower()

    assert "ротавирус" not in low
    assert "температур" not in low
    assert "нурофен" not in low
    assert "39" not in clean
    assert "по болезни" in low


async def test_summarize_without_credentials_uses_fallback(monkeypatch):
    empty_settings = dataclasses.replace(project_settings, gigachat_credentials="")
    monkeypatch.setattr(llm_gigachat, "settings", empty_settings)

    result = await llm_gigachat.summarize(DEMO_MESSAGES)

    assert result.engine == "fallback"
    for header in CATEGORY_HEADERS:
        assert header in result.text
    low = result.text.lower()
    assert "ротавирус" not in low
    assert "температур" not in low


async def test_summarize_gigachat_unavailable_uses_fallback(monkeypatch):
    monkeypatch.setattr(llm_gigachat, "GigaChat", None)

    result = await llm_gigachat.summarize(DEMO_MESSAGES)

    assert result.engine == "fallback"
    assert "ротавирус" not in result.text.lower()


async def test_fallback_summarize_on_demo_messages():
    # В буфер попадают уже обезличенные сообщения — готовим так же, как бот.
    name_map = {}
    prepared = []
    for item in DEMO_MESSAGES[:6]:
        sender = anonymizer.mask_person_name(item["sender"], name_map=name_map)
        text, _stats = anonymizer.mask_text(item["text"], name_map=name_map)
        prepared.append({"sender": sender, "text": text})

    result = await llm_gigachat.fallback_summarize(prepared)

    for header in CATEGORY_HEADERS:
        assert header in result.text
    low = result.text.lower()
    assert "ротавирус" not in low
    assert "температур" not in low
    assert "•" in result.text
    assert result.engine == "fallback"

    # В сводке только маркеры, а restore_names возвращает полные имена.
    assert "[PER_" in result.text or "[USR_" in result.text
    restored = anonymizer.restore_names(result.text, name_map)
    assert "[PER_" not in restored and "[USR_" not in restored


def test_extract_events_finds_meeting():
    events = calendar_export.extract_events(["Родительское собрание 15 октября в 18:00"])

    assert len(events) == 1
    event = events[0]
    assert event["title"] == "Родительское собрание"
    assert event["date"] is not None
    assert event["date"].month == 10
    assert event["date"].day == 15
    assert event["time"] == "18:00"


def test_extract_events_past_date_stays_current_year():
    # «15 октября» без года не уезжает в следующий год, даже если дата прошла.
    events = calendar_export.extract_events(
        ["Поездка в Санкт-Петербург 15 октября, собираем по 3500 руб."]
    )

    assert len(events) == 1
    assert events[0]["date"].year == calendar_export._today().year
    assert events[0]["date"].month == 10
    assert events[0]["date"].day == 15


def test_extract_events_one_event_per_message():
    # «Поездка + экскурсии» в одном тексте — это одно событие, а не два.
    events = calendar_export.extract_events(
        ["Собираем на поездку в Санкт-Петербург 15 октября! "
         "Стоимость 3500 руб (автобус и экскурсии)"]
    )

    assert len(events) == 1
    assert events[0]["title"] == "Поездка в Санкт-Петербург"


def test_extract_events_two_events_in_one_message():
    # Поездка и собрание в одном тексте: время 18:00 принадлежит собранию.
    text = ("Дорогие родители, собираем деньги на поездку в Санкт-Петербург "
            "15 октября! Стоимость 3500 руб с ребёнка (автобус и экскурсии). "
            "И напоминаю: родительское собрание 15 октября в 18:00, всё обсудим")
    events = calendar_export.extract_events([text])

    by_title = {e["title"]: e for e in events}
    assert set(by_title) == {"Поездка в Санкт-Петербург", "Родительское собрание"}
    assert by_title["Поездка в Санкт-Петербург"]["time"] is None
    assert by_title["Родительское собрание"]["time"] == "18:00"
    assert by_title["Родительское собрание"]["date"].day == 15


def test_extract_events_no_phantom_from_payment_deadline():
    # «до 10 октября» — срок оплаты, а не дата экскурсии: событий ровно два.
    text = ("Дорогие родители, собираем деньги на поездку в Санкт-Петербург "
            "15 октября! Стоимость 3500 руб с ребёнка (автобус и экскурсии). "
            "Переводите на карту 4276 5500 1234 5678 (Сбербанк) до 10 октября. "
            "Вопросы организатору — +7 900 123-45-67. "
            "И напоминаю: родительское собрание 15 октября в 18:00, всё обсудим")
    events = calendar_export.extract_events([text])

    titles = {e["title"] for e in events}
    assert titles == {"Поездка в Санкт-Петербург", "Родительское собрание"}


def test_extract_events_title_and_date_in_neighbour_sentences():
    events = calendar_export.extract_events(
        ["Собираем на поездку! Выезжаем 15 октября в 8:00 от школы"]
    )

    assert len(events) == 1
    assert events[0]["title"] == "Поездка"
    assert events[0]["time"] == "08:00"


def test_extract_events_explicit_year_kept():
    events = calendar_export.extract_events(
        ["Родительское собрание 15 октября 2027 в 18:00"]
    )

    assert len(events) == 1
    assert events[0]["date"].year == 2027


def test_build_ics_keeps_russian_text():
    events = calendar_export.extract_events(["Родительское собрание 20 декабря в 18:00"])

    ics_text = calendar_export.build_ics(events)

    assert "BEGIN:VCALENDAR" in ics_text
    assert "END:VCALENDAR" in ics_text
    assert "BEGIN:VEVENT" in ics_text
    assert "SUMMARY:Родительское собрание" in ics_text
    assert "UID" in ics_text
    assert "DTSTAMP" in ics_text


def test_save_ics_writes_file(tmp_path):
    events = calendar_export.extract_events(["Экскурсия в музей 20 ноября в 12:00"])
    target = tmp_path / "class_events.ics"

    result = calendar_export.save_ics(events, str(target))

    assert result == str(target)
    content = target.read_text(encoding="utf-8")
    assert "BEGIN:VCALENDAR" in content
    assert "SUMMARY" in content


def test_relative_days_do_not_create_events():
    """«Завтра»/«послезавтра» событий не создают: «завтра» внутри «послезавтра» давало ложную дату."""
    assert calendar_export.extract_events(["Миша уже лучше, послезавтра выйдет"]) == []
    assert calendar_export.extract_events(["Во сколько завтра родительское собрание?"]) == []


def test_single_name_is_masked_by_dictionary():
    """Одиночные имена без фамилии («Маша») добираются словарным проходом."""
    name_map = {}
    masked, stats = anonymizer.mask_text("Маша завтра уйдёт с последнего урока", name_map)

    assert "Маша" not in masked
    assert "[PER_" in masked
    assert stats.names >= 1
    # Обычные слова не трогаем.
    plain, plain_stats = anonymizer.mask_text("Собрание и Спасибо всем", {})
    assert plain == "Собрание и Спасибо всем"
    assert plain_stats.names == 0


def test_demo_messages_content():
    assert len(DEMO_MESSAGES) == 25
    for message in DEMO_MESSAGES:
        assert message.get("sender")
        assert message["text"].strip()

    joined = " ".join(m["text"] for m in DEMO_MESSAGES)
    assert "4276 5500 1234 5678" in joined
    assert "+7 900 123-45-67" in joined
    assert "123-456-789 45" in joined
    assert "4510 123456" in joined
    assert "@" in joined
    assert "простуда" in joined.lower()
    assert "температура 39" in joined
    assert any("?" in m["text"] for m in DEMO_MESSAGES)


def test_demo_messages_get_fully_masked():
    for message in DEMO_MESSAGES:
        masked, _stats = anonymizer.mask_text(message["text"])
        assert "4276" not in masked
        assert "123-45-67" not in masked
        assert "123-456-789" not in masked
        assert "123456" not in masked
        assert "@" not in masked


def test_polish_normalizes_gigachat_markdown():
    """GigaChat любит звёздочки и свои эмодзи — заголовки возвращаются к нашим."""
    from app.services.llm_gigachat import _polish

    raw = (
        "🛑 **Отсутствующие и справки**\n"
        "• **Миша** болеет, завтра не придёт\n"
        "\n"
        "❓ **Вопросы к учителю**\n"
        "• Что принести к пятнице?"
    )
    out = _polish(raw)

    assert "**" not in out, "markdown-звёздочки должны исчезнуть"
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[0] == "🏥 Отсутствующие и справки"
    assert lines[1] == "• Миша болеет, завтра не придёт"
    assert "❓ Вопросы к учителю" in lines


def test_prettify_digest_breaks_lines():
    """Сводка из одной строки должна разъехаться по категориям и пунктам."""
    from app.services.digest import prettify_digest

    raw = ("❓ Вопросы к учителю • Что принести к пятнице? (С. М.) "
           "🏥 Отсутствующие и справки • Миша болеет, справку принесут "
           "💰 Договорённости и сборы — пусто — — пусто — "
           "⚡ Требует действия учителя — пусто —")
    out = prettify_digest(raw)

    assert out.count("\n") >= 5, "категории и пункты должны быть на своих строках"
    assert out.startswith("❓"), "сводка начинается с первой категории"
    assert "\n\n🏥" in out and "\n\n💰" in out and "\n\n⚡" in out
    assert "\n• Что принести" in out
    assert "— пусто — — пусто —" not in out
    assert "\n\n💰" in out, "перед новой категорией должна быть пустая строка"


def test_medical_phrase_not_duplicated():
    """Повторы «по болезни» в одной фразе схлопываются в один нейтральный оборот."""
    from app.services.llm_gigachat import sanitize_medical

    twice = sanitize_medical(
        "заболел, по болезни, высокая по болезни; завтра не придёт"
    )
    assert twice.count("по болезни") == 1, twice
    assert "высокая по болезни" not in twice

    once = sanitize_medical(
        "Миша Смирнов заболел, температура 39, высокая температура, сидит дома"
    )
    assert once.count("по болезни") == 1, once
    assert "39" not in once and "температур" not in once.lower()


def test_digest_has_no_tail_dashes():
    """GigaChat вешает дефис в конце заголовков и пунктов — убираем."""
    from app.services.digest import prettify_digest

    raw = ("❓ Вопросы к учителю -\n"
           "• Что принести к пятнице? (С. М.) -\n"
           "• Кто дежурный? -")
    out = prettify_digest(raw)
    assert " -" not in out, out
    assert out.startswith("❓ Вопросы к учителю")


def test_medical_details_not_sent_to_llm():
    """Диагнозы и измерения нейтрализуются до отправки в модель."""
    from app.services.llm_gigachat import mask_medical_details

    clean = mask_medical_details(
        "Миша заболел, температура 39, простуда, справку принесём"
    )
    assert "39" not in clean, clean
    assert "простуда" not in clean.lower()
    assert "температур" not in clean.lower()
    assert "справку принесём" in clean
    # обычные слова не портим
    assert mask_medical_details("Завтра собрание в 18:00") == "Завтра собрание в 18:00"
    assert "жарко" in mask_medical_details("На улице жарко")


async def test_demo_examples_hide_medical_details(tmp_path):
    """Витрина демо показывает то же, что уходит в модель: без диагнозов."""
    from app.db.database import Database
    from app.services import digest

    db = Database(str(tmp_path / "demo.db"))
    await db.init()
    info = await digest.load_demo_messages(db, 999001)
    await db.close()

    pairs = " ".join(e["masked"] for e in info["examples"]).lower()
    assert "по болезни" in pairs, pairs
    for word in ("простуда", "температур", "ротавирус"):
        assert word not in pairs, f"{word} остался в примере «После»"


def test_medical_input_has_no_repeats():
    """В витрине не должно быть «по болезни, по болезни»."""
    from app.services.llm_gigachat import mask_medical_details

    out = mask_medical_details("у нас простуда, температура 39 второй день")
    assert out.count("по болезни") == 1, out
    assert "39" not in out, out


def test_digest_gets_events_lost_by_llm():
    """Событие, которое ИИ потерял, дописываем в сводку сами."""
    from datetime import date

    from app.services.digest import _add_missing_events

    text = ("❓ Вопросы к учителю\n"
            "• Что принести к пятнице? (С. М.)\n\n"
            "💰 Договорённости и сборы — пусто")
    events = [{"title": "Родительское собрание",
               "date": date(2026, 11, 14), "time": "18:30"}]

    out = _add_missing_events(text, events)

    assert "собрание" in out.lower(), out
    assert "14.11" in out and "18:30" in out
    assert "— пусто" not in out


def test_digest_does_not_duplicate_events():
    """Если событие в сводке уже есть — второй раз не добавляем."""
    from datetime import date

    from app.services.digest import _add_missing_events

    text = "💰 Договорённости и сборы\n• Родительское собрание 14 ноября в 18:30"
    events = [{"title": "Родительское собрание",
               "date": date(2026, 11, 14), "time": "18:30"}]

    assert _add_missing_events(text, events) == text


async def test_make_digest_keeps_event_from_llm_loss(tmp_path, monkeypatch):
    """Даже если модель выбросила событие, сводка его покажет."""
    from app.db.database import Database
    from app.services import digest, llm_gigachat

    db = Database(str(tmp_path / "d.db"))
    await db.init()
    await db.link_chat(-100, 42, "3А класс")
    await db.add_message(-100, "[USR_1 | М. И.]",
                         "Родительское собрание 14 ноября в 18:30", masked=1)

    async def fake_summarize(messages):
        return llm_gigachat.SummarizeResult(
            text="❓ Вопросы к учителю\n• Что принести?", engine="gigachat"
        )

    monkeypatch.setattr(llm_gigachat, "summarize", fake_summarize)
    result = await digest.make_digest(db, -100)
    await db.close()

    assert "собрание" in result.text.lower(), result.text
    assert "14.11" in result.text


def test_ics_skips_past_events():
    """Прошедшие события в календарь учителя не попадают."""
    import datetime

    from app.services.calendar_export import build_ics

    today = datetime.date.today()
    ics = build_ics([
        {"title": "Прошлое собрание",
         "date": today - datetime.timedelta(days=40), "time": "18:00"},
        {"title": "Будущая экскурсия",
         "date": today + datetime.timedelta(days=5), "time": None},
    ])
    assert "Прошлое собрание" not in ics
    assert "Будущая экскурсия" in ics


def test_medical_dangling_conjunction_removed():
    """После замены не должно оставаться «по болезни и»."""
    from app.services.llm_gigachat import sanitize_medical

    out = sanitize_medical("заболел, температура 39, и остался дома")
    assert "по болезни и остался" not in out, out
    assert "по болезни" in out


def test_digest_not_empty_when_llm_lost_message():
    """Короткое сообщение не должно превращаться в «— пусто»."""
    from app.services.digest import _ensure_not_empty

    messages = [{"text": "завтра контрольная по математике, повторите таблицу умножения",
                 "sender": "[USR_1 | О. Ш.]"}]
    out = _ensure_not_empty(
        "❓ Вопросы к учителю — пусто\n⚡ Требует действия учителя — пусто",
        messages,
    )

    assert "контрольная" in out, out
    assert out.count("•") == 1
    assert "О. Ш." in out
    assert "— пусто" in out


def test_empty_marker_not_glued_to_bullet():
    """«— пусто» не должно приклеиваться к пункту."""
    from app.services.digest import _fix_empty_markers

    text = "💰 Договорённости и сборы\n• Родительское собрание — 14.11 в 18:30 — пусто"
    out = _fix_empty_markers(text)

    assert "— пусто" not in out
    assert "Родительское собрание — 14.11 в 18:30" in out


async def test_make_digest_keeps_short_message(tmp_path, monkeypatch):
    """Даже если модель вернула пустоту, сообщение попадёт в сводку."""
    from app.db.database import Database
    from app.services import digest, llm_gigachat

    db = Database(str(tmp_path / "d.db"))
    await db.init()
    await db.link_chat(-101, 42, "3А класс")
    await db.add_message(-101, "[USR_1 | О. Ш.]",
                         "завтра контрольная по математике, повторите таблицу умножения",
                         masked=1)

    async def fake_summarize(messages):
        return llm_gigachat.SummarizeResult(
            text="❓ Вопросы к учителю — пусто\n💰 Договорённости и сборы — пусто",
            engine="gigachat",
        )

    monkeypatch.setattr(llm_gigachat, "summarize", fake_summarize)
    result = await digest.make_digest(db, -101)
    await db.close()

    assert "контрольная" in result.text.lower(), result.text


def test_digest_single_line_model_answer_is_kept():
    """Модель пишет сводку одной строкой — буфер в этом случае добирать нельзя."""
    from app.services.digest import _ensure_not_empty

    messages = [{"text": "Здравствуйте! Миша Смирнов заболел, по болезни, сидим дома 😔",
                 "sender": "[USR_1 | С. О.]"}]
    model = ("❓ Вопросы к учителю • Куда сдать форму? (А. Б.) "
             "🏥 Отсутствующие и справки • Миша болеет (С. О.)")

    assert _ensure_not_empty(model, messages) == model


def test_digest_stays_empty_for_noise_only():
    """Из одних открыток и «+1» сводку не собираем."""
    from app.services.digest import _ensure_not_empty

    messages = [
        {"text": "С добрым утром, дорогие родители!", "sender": "[USR_1 | О. С.]"},
        {"text": "+1", "sender": "[USR_2 | А. Б.]"},
        {"text": "Спасибо!", "sender": "[USR_3 | М. П.]"},
    ]
    out = _ensure_not_empty("❓ Вопросы к учителю — пусто", messages)

    assert "•" not in out, out
