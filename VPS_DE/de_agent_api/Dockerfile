# Используем официальный образ AmneziaWG
FROM amneziavpn/amnezia-wg:latest

# Устанавливаем Python и системные зависимости (включая компиляторы для psutil)
RUN apk add --no-cache \
    python3 \
    py3-pip \
    iproute2 \
    iptables \
    curl \
    bash \
    procps \
    iputils \
    gcc \
    musl-dev \
    python3-dev \
    linux-headers

# Создаем виртуальное окружение и ставим пакеты
COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt

# Копируем скрипты
COPY run_api.sh /run_api.sh
RUN chmod +x /run_api.sh

COPY api.py /app/api.py
WORKDIR /app

# Переопределяем Entrypoint для запуска FastAPI
ENTRYPOINT ["/run_api.sh"]