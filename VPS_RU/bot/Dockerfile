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
# Устойчивость к блокировке pypi.org/Fastly (частая беда РФ-хостингов: ICMP идёт,
# а TCP/443 к Fastly режется -> ReadTimeout/ConnectTimeout). Сначала быстрый заход
# на официальный PyPI; при недоступности автоматически откат на зеркало (aliyun,
# мимо Fastly, доступно из РФ). Зеркало можно переопределить:
#   docker compose build --build-arg PIP_MIRROR=<url-до-simple/>
ARG PIP_MIRROR=https://mirrors.aliyun.com/pypi/simple/
RUN pip install --no-cache-dir --timeout 20 --retries 1 -r requirements.txt \
 || pip install --no-cache-dir --timeout 120 --retries 10 --index-url "$PIP_MIRROR" -r requirements.txt

COPY . .

CMD ["python", "bot.py"]