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
# Устойчивость к блокировке/недоступности индекса пакетов (даунтайм в проде недопустим).
# Перебираем источники по очереди, первый рабочий выигрывает: свой PIP_MIRROR (если задан)
# -> официальный PyPI (быстрый фейл при блоке Fastly) -> aliyun -> tuna (оба мимо Fastly,
# доступны из РФ). Если ВСЕ недоступны — сборка падает, но прод не трогается (см. deploy.sh).
# Свой первым: docker compose build --build-arg PIP_MIRROR=<url-до-simple/>
ARG PIP_MIRROR=
RUN ok=0; \
    for IDX in "$PIP_MIRROR" "DEFAULT" "https://mirrors.aliyun.com/pypi/simple/" "https://pypi.tuna.tsinghua.edu.cn/simple/"; do \
        [ -z "$IDX" ] && continue; \
        echo ">>> pip via ${IDX}"; \
        if [ "$IDX" = "DEFAULT" ]; then \
            pip install --no-cache-dir --timeout 20 --retries 1 -r requirements.txt && { ok=1; break; }; \
        else \
            pip install --no-cache-dir --timeout 60 --retries 3 --index-url "$IDX" -r requirements.txt && { ok=1; break; }; \
        fi; \
        echo ">>> источник недоступен, пробую следующий..."; \
    done; \
    [ "$ok" = "1" ] || { echo "ВСЕ источники pip недоступны" >&2; exit 1; }

COPY . .

CMD ["python", "bot.py"]