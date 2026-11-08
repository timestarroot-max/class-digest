"""Веб-панель учителя: страница и API на FastAPI, в одном процессе с ботом."""

import html
import json
import logging
import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from app.config import DEMO_CHAT_ID, DEMO_CHAT_TITLE, settings
from app.db.database import Database
from app.services import anonymizer, calendar_export, digest

logger = logging.getLogger(__name__)

# Шаблон страницы лежит рядом с этим файлом.
_TEMPLATE_PATH = Path(__file__).resolve().parent / "template.html"

# Приветствие родителям — дословно из анкеты.
WELCOME_TEXT = ('Здравствуйте! Я бот "Классный дайджест". Я помогаю учителю '
                'не пропускать важные сообщения: раз в день составляю для него '
                'короткую сводку. Личные данные (телефоны, номера карт, имена) '
                'я скрываю, а сообщения храню только до выдачи сводки и не '
                'дольше суток')

# Строка для запасной страницы, если шаблон вдруг не читается.
_FALLBACK_PAGE = (
    "<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    "<title>Классный дайджест</title></head><body>"
    "<h1>🌼 Классный дайджест</h1>"
    "<p>Веб-панель временно недоступна, бот продолжает работу.</p>"
    "</body></html>"
)

# Статус бота: главный агент вызывает set_bot_status после старта бота.
_bot_status: dict = {"running": False, "username": None}


def set_bot_status(running: bool, username: str | None = None) -> None:

    _bot_status["running"] = bool(running)
    _bot_status["username"] = username
    logger.info("Статус бота на панели: running=%s, username=%s", running, username)


class AnonymizeRequest(BaseModel):

    text: str = ""


class ScheduleRequest(BaseModel):

    time: str = ""


# Разрешённый формат времени автосводки: ЧЧ:ММ.
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _render_page() -> str:
    """Подстановка обычным replace, без f-строк: иначе фигурные скобки CSS и JS в шаблоне сломаются."""

    try:
        template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.exception("Шаблон страницы не прочитан: %s", exc)
        return _FALLBACK_PAGE

    running = bool(_bot_status["running"])
    username = _bot_status.get("username")
    status_on = "🟢 Бот работает" + (f" — {username}" if username else "")
    status_off = "🟡 Веб-панель без бота"
    current = status_on if running else status_off

    page = template
    page = page.replace("__PILL_CLASS__", "pill" if running else "pill wait")
    page = page.replace("__STATUS_LINE__", html.escape(current))
    # Для JS подставляем готовые строковые литералы через json.dumps.
    page = page.replace("__STATUS_ON__", json.dumps(status_on, ensure_ascii=False))
    page = page.replace("__STATUS_OFF__", json.dumps(status_off, ensure_ascii=False))
    page = page.replace("__WELCOME_TEXT__", html.escape(WELCOME_TEXT, quote=False))
    page = page.replace("__DEMO_TITLE__", html.escape(DEMO_CHAT_TITLE, quote=False))
    return page


def create_app(db: Database) -> FastAPI:

    app = FastAPI(title="Классный дайджест — веб-панель учителя")
    logger.info("Веб-панель настраивается, публичный адрес: %s", settings.public_web_url)

    async def _panel_user(request: Request) -> int | None:
        """user_id владельца ключа из адреса (?k=...); между запросами ключ нигде не хранится."""
        key = request.query_params.get("k", "")
        try:
            return await db.get_user_by_web_key(key)
        except Exception:  # noqa: BLE001 - незнакомый ключ не должен ронять панель
            logger.exception("Не удалось проверить ключ панели")
            return None

    async def _my_chat_ids(user_id: int | None) -> list[int]:
        chat_ids: list[int] = [DEMO_CHAT_ID]
        if user_id:
            chat_ids = await db.get_teacher_chats(user_id) + [DEMO_CHAT_ID]
        return chat_ids

    @app.get("/app", response_class=HTMLResponse)
    async def dashboard_page():

        try:
            return HTMLResponse(_render_page())
        except Exception as exc:  # noqa: BLE001 - панель не должна падать
            logger.exception("Не удалось собрать страницу: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard_redirect(request: Request):
        """Корень сайта переносит на /app и сохраняет ключ в адресе."""

        query = request.url.query
        target = "/app" + ("?" + query if query else "")
        return RedirectResponse(target, status_code=307)

    @app.post("/api/anonymize")
    async def api_anonymize(payload: AnonymizeRequest):

        try:
            masked, stats = anonymizer.mask_text(payload.text)
            return {
                "masked": masked,
                "stats": {
                    "phones": stats.phones,
                    "cards": stats.cards,
                    "passports": stats.passports,
                    "snils": stats.snils,
                    "emails": stats.emails,
                    "names": stats.names,
                    "total": stats.total,
                },
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка песочницы обезличивания: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.post("/api/demo")
    async def api_demo(request: Request):
        """Демо-чат общий и обезличенный, поэтому демо доступно и гостям без ключа."""

        try:
            return await digest.load_demo_messages(db, DEMO_CHAT_ID)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Не удалось загрузить демо-сообщения: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.post("/api/digest")
    async def api_digest(request: Request):
        """Гость без ключа получает сводку только демо-чата."""

        try:
            user_id = await _panel_user(request)

            digests = []
            for chat_id in await _my_chat_ids(user_id):
                buffered = await db.count_messages(chat_id)
                if buffered == 0:
                    continue
                result = await digest.make_digest(db, chat_id)
                digests.append(
                    {
                        "text": result.text,
                        "engine": result.engine,
                        "messages_count": result.messages_count,
                        "masked_total": result.masked_total,
                        "chat_title": result.chat_title,
                    }
                )

            if not digests:
                return {
                    "digests": [],
                    "text": "",
                    "engine": "",
                    "messages_count": 0,
                    "masked_total": 0,
                    "chat_title": "",
                }

            text = "\n\n".join(item["text"] for item in digests)
            return {
                "digests": digests,
                "text": text,
                "engine": digests[0]["engine"],
                "messages_count": sum(item["messages_count"] for item in digests),
                "masked_total": sum(item["masked_total"] for item in digests),
                "chat_title": ", ".join(
                    item["chat_title"] for item in digests
                ),
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("Не удалось собрать сводку: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.get("/api/digest/latest")
    async def api_digest_latest(request: Request):

        try:
            user_id = await _panel_user(request)
            if user_id is None:
                last = await db.get_last_digest(DEMO_CHAT_ID)
                if not last:
                    return {"text": None}
                return {
                    "text": last.get("text", ""),
                    "source": last.get("source", ""),
                    "created_at": last.get("created_at", ""),
                }

            best = None
            for chat_id in await _my_chat_ids(user_id):
                last = await db.get_last_digest(chat_id)
                if last and (best is None or str(last.get("created_at", "")) > str(best.get("created_at", ""))):
                    best = last
            if not best:
                return {"text": None}
            return {
                "text": best.get("text", ""),
                "source": best.get("source", ""),
                "created_at": best.get("created_at", ""),
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("Не удалось прочитать последнюю сводку: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.get("/api/stats")
    async def api_stats(request: Request):

        try:
            user_id = await _panel_user(request)
            buffered = await db.count_messages(DEMO_CHAT_ID)
            counters = await db.get_stats()
            processed = int(counters.get("messages_processed", 0) or 0)
            digests_sent = int(counters.get("digests_sent", 0) or 0)
            masked_total = int(counters.get("masked_total", 0) or 0)
            # ~20 секунд ручной обработки на сообщение -> минуты экономии.
            saved_minutes = round(processed * 20 / 60)

            me = None
            my_chats = []
            if user_id:
                for chat_id in await db.get_teacher_chats(user_id):
                    title = await db.get_chat_title(chat_id) or f"Чат {chat_id}"
                    my_chats.append({"id": chat_id, "title": title})
                me = {
                    "user_id": user_id,
                    "chats": my_chats,
                    "schedule": await db.get_schedule_time(user_id),
                }

            # Гостю число своих чатов не показываем — это выглядело бы как личное.
            chats_count = len(my_chats) if user_id else None

            return {
                "chats": chats_count,
                "messages_buffered": int(buffered or 0),
                "masked_total": masked_total,
                "messages_processed": processed,
                "digests_sent": digests_sent,
                "saved_minutes": saved_minutes,
                "bot_running": bool(_bot_status["running"]),
                "me": me,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("Не удалось собрать статистику: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.post("/api/schedule")
    async def api_schedule(payload: ScheduleRequest, request: Request):

        try:
            user_id = await _panel_user(request)
            if user_id is None:
                return JSONResponse(
                    {"error": "Расписание доступно по личной ссылке из меню бота"},
                    status_code=401,
                )
            if not _TIME_RE.match(payload.time.strip()):
                return JSONResponse(
                    {"error": "Время должно быть в формате ЧЧ:ММ"},
                    status_code=400,
                )
            await db.set_schedule_time(user_id, payload.time.strip())
            return {"ok": True, "time": payload.time.strip()}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Не удалось сохранить расписание: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.get("/export.ics")
    async def export_ics(request: Request):

        try:
            user_id = await _panel_user(request)
            events: list[dict] = []
            seen: set[tuple] = set()
            for chat_id in await _my_chat_ids(user_id):
                for event in await db.get_events(chat_id):
                    mark = (
                        str(event.get("title", "")),
                        str(event.get("date", "")),
                        str(event.get("time", "")),
                    )
                    if mark in seen:
                        continue
                    seen.add(mark)
                    events.append(event)
            if not events and user_id is None:
                events = await digest.get_demo_calendar_events(db, DEMO_CHAT_ID)
            ics_text = calendar_export.build_ics(events)
            return Response(
                content=ics_text,
                media_type="text/calendar",
                headers={
                    "Content-Disposition": 'attachment; filename="class_events.ics"'
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Не удалось собрать .ics: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.get("/api/health")
    async def api_health():

        try:
            return {"status": "ok"}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка health-check: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

    return app
