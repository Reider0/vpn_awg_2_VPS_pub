FROM python:3.11-slim

WORKDIR /app

# Устанавливаем системные утилиты прямо в образ
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        git \
        iputils-ping \
        postgresql-client && \
    rm -rf /var/lib/apt/lists/*

# Настройка Git
RUN git config --global --add safe.directory '*' && \
    git config --global credential.helper ""

COPY requirements.txt .
# Устойчивость к медленному/флапающему pypi.org (см. ru_wg_api/Dockerfile).
# Зеркало при необходимости:
#   docker compose build --build-arg PIP_INDEX_URL=https://mirror.yandex.ru/mirrors/pypi/simple/
ARG PIP_INDEX_URL=https://pypi.org/simple/
RUN pip install --no-cache-dir --timeout 120 --retries 10 \
        --index-url "$PIP_INDEX_URL" \
        -r requirements.txt

COPY . .

CMD ["python", "bot.py"]