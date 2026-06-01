FROM amneziavpn/amnezia-wg:latest

RUN apk add --no-cache \
    python3 \
    py3-pip \
    iproute2 \
    iptables \
    curl \
    bash \
    procps \
    iputils \
    ipset

# Устойчивость к блокировке pypi.org/Fastly (частая беда РФ-хостингов: ICMP идёт,
# а TCP/443 к Fastly режется -> ReadTimeout/ConnectTimeout). Сначала быстрый заход
# на официальный PyPI; при недоступности автоматически откат на зеркало (aliyun,
# мимо Fastly, доступно из РФ). Зеркало можно переопределить:
#   docker compose build --build-arg PIP_MIRROR=<url-до-simple/>
ARG PIP_MIRROR=https://mirrors.aliyun.com/pypi/simple/
RUN python3 -m venv /opt/venv && \
    ( /opt/venv/bin/pip install --no-cache-dir --timeout 20 --retries 1 fastapi uvicorn pydantic \
   || /opt/venv/bin/pip install --no-cache-dir --timeout 120 --retries 10 --index-url "$PIP_MIRROR" fastapi uvicorn pydantic )

COPY update_ru_ips.sh /app/update_ru_ips.sh
COPY run_api.sh /run_api.sh
RUN chmod +x /run_api.sh && chmod +x /app/update_ru_ips.sh

COPY api.py /app/api.py
WORKDIR /app

ENTRYPOINT ["/run_api.sh"]