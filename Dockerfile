# Образ бота «Классный дайджест» — один контейнер: бот + веб-панель.
FROM python:3.11-slim

# Привязка образа к репозиторию: пакет виден на странице проекта в GHCR.
LABEL org.opencontainers.image.source="https://github.com/timestarroot-max/class-digest"

WORKDIR /bot

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Оба файла зависимостей требуют Правила: Requierments.txt — так написано у организаторов.
COPY requirements.txt Requierments.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Эмбеддинги Natasha качаем на этапе сборки — бот потом стартует и без интернета.
RUN python -c "from natasha import Segmenter, NewsEmbedding, NewsNERTagger, MorphVocab; NewsNERTagger(NewsEmbedding()); MorphVocab(); Segmenter()"

# Каталог data/ (SQLite и файлы .ics) создаётся на месте при первом запуске.
EXPOSE 8000

CMD ["python", "-m", "app.main"]
