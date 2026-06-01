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

# Устойчивость к медленному/флапающему доступу к pypi.org (частая беда РФ-хостингов):
# дефолтный per-read таймаут pip = 15с, поднимаем до 120с и добавляем ретраи.
# При полностью заблокированном pypi можно собрать через зеркало:
#   docker compose build --build-arg PIP_INDEX_URL=https://mirror.yandex.ru/mirrors/pypi/simple/
ARG PIP_INDEX_URL=https://pypi.org/simple/
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --timeout 120 --retries 10 \
        --index-url "$PIP_INDEX_URL" \
        fastapi uvicorn pydantic

COPY update_ru_ips.sh /app/update_ru_ips.sh
COPY run_api.sh /run_api.sh
RUN chmod +x /run_api.sh && chmod +x /app/update_ru_ips.sh

COPY api.py /app/api.py
WORKDIR /app

ENTRYPOINT ["/run_api.sh"]