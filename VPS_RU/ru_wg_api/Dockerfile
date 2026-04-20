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

RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir fastapi uvicorn pydantic

COPY update_ru_ips.sh /app/update_ru_ips.sh
COPY run_api.sh /run_api.sh
RUN chmod +x /run_api.sh && chmod +x /app/update_ru_ips.sh

COPY api.py /app/api.py
WORKDIR /app

ENTRYPOINT ["/run_api.sh"]