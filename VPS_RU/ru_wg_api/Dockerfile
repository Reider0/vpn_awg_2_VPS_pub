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

# Устойчивость к блокировке/недоступности индекса пакетов (даунтайм в проде недопустим).
# Перебираем источники, первый рабочий выигрывает: свой PIP_MIRROR -> PyPI -> aliyun -> tuna.
# Свой первым: docker compose build --build-arg PIP_MIRROR=<url-до-simple/>
ARG PIP_MIRROR=
RUN python3 -m venv /opt/venv && \
    ok=0; \
    for IDX in "$PIP_MIRROR" "DEFAULT" "https://mirrors.aliyun.com/pypi/simple/" "https://pypi.tuna.tsinghua.edu.cn/simple/"; do \
        [ -z "$IDX" ] && continue; \
        echo ">>> pip via ${IDX}"; \
        if [ "$IDX" = "DEFAULT" ]; then \
            /opt/venv/bin/pip install --no-cache-dir --timeout 20 --retries 1 fastapi uvicorn pydantic && { ok=1; break; }; \
        else \
            /opt/venv/bin/pip install --no-cache-dir --timeout 60 --retries 3 --index-url "$IDX" fastapi uvicorn pydantic && { ok=1; break; }; \
        fi; \
        echo ">>> источник недоступен, пробую следующий..."; \
    done; \
    [ "$ok" = "1" ] || { echo "ВСЕ источники pip недоступны" >&2; exit 1; }

COPY update_ru_ips.sh /app/update_ru_ips.sh
COPY run_api.sh /run_api.sh
RUN chmod +x /run_api.sh && chmod +x /app/update_ru_ips.sh

COPY api.py /app/api.py
WORKDIR /app

ENTRYPOINT ["/run_api.sh"]