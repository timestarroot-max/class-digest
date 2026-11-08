"""Настройки из окружения и файла .env."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:

    # Токен бота из @MasterBot в мессенджере MAX.
    max_bot_token: str
    # Ключ доступа к API GigaChat (Authorization Key из личного кабинета).
    gigachat_credentials: str
    # Область доступа GigaChat, например GIGACHAT_API_PERS.
    gigachat_scope: str
    # Название модели GigaChat (актуальные: GigaChat-2-Max, GigaChat-2, GigaChat-2-Pro).
    gigachat_model: str
    # Путь к файлу SQLite.
    database_path: str
    # Порт встроенной веб-панели учителя.
    web_port: int
    # Публичный адрес веб-панели (для кнопки в меню бота).
    public_web_url: str
    # Время автосводки по умолчанию (ЧЧ:ММ).
    default_digest_time: str


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def load_settings() -> Settings:

    token = os.getenv("MAX_BOT_TOKEN", "").strip()
    return Settings(
        max_bot_token=token,
        gigachat_credentials=os.getenv("GIGACHAT_CREDENTIALS", "").strip(),
        gigachat_scope=os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip(),
        gigachat_model=os.getenv("GIGACHAT_MODEL", "GigaChat-2-Max").strip(),
        database_path=os.getenv("DATABASE_PATH", "data/class_digest.db"),
        web_port=_get_int("WEB_PORT", 8000),
        public_web_url=os.getenv("PUBLIC_WEB_URL", "http://localhost:8000").rstrip("/"),
        default_digest_time=os.getenv("DEFAULT_DIGEST_TIME", "19:00"),
    )


# Единый экземпляр настроек на весь процесс.
settings = load_settings()

def has_real_bot_token() -> bool:
    """Признак запуска без реального токена: панель живёт, бот ждёт токен."""

    placeholder = {"", "test", "demo", "token", "ваш_токен", "ваш токен"}
    return settings.max_bot_token.lower() not in placeholder


# Идентификатор виртуального чата для демо-режима (тест для жюри).
DEMO_CHAT_ID = 999001
DEMO_CHAT_TITLE = "3А класс (демо-чат для жюри)"

# Папка для рабочих файлов: база, выгрузки .ics.
DATA_DIR = os.getenv("DATA_DIR", "data")
