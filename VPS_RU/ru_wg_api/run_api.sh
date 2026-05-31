#!/bin/bash

# Включаем глобальный форвардинг
sysctl -w net.ipv4.ip_forward=1

echo "🚀 Starting RU Master Node..."

# Создаём оба набора заранее, чтобы правила iptables в api.py не падали на отсутствии set.
#   ru_nets      — гео-IP РФ (идёт напрямую)
#   blocked_nets — блокировки РКН из antifilter (принудительно в Германию)
ipset create ru_nets hash:net family inet hashsize 4096 maxelem 1000000 -exist
ipset create blocked_nets hash:net family inet hashsize 4096 maxelem 1000000 -exist

# Тёплый старт из кэша (volume переживает рестарт): маршрутизация работает сразу,
# не дожидаясь первого сетевого обновления.
CACHE_DIR="/etc/amnezia/amneziawg/cache"
if [ -s "$CACHE_DIR/ru_nets.cidr" ]; then
    grep -E '^[0-9.]+/[0-9]+$' "$CACHE_DIR/ru_nets.cidr" | sed 's/^/add ru_nets /' | ipset restore -!
fi
if [ -s "$CACHE_DIR/blocked_nets.cidr" ]; then
    grep -E '^[0-9.]+/[0-9]+$' "$CACHE_DIR/blocked_nets.cidr" | sed 's/^/add blocked_nets /' | ipset restore -!
fi

# Запускаем фонового демона обновления списков (раз в 12 часов)
(
  while true; do
    bash /app/update_ru_ips.sh
    sleep 43200
  done
) &

# Запуск API
exec /opt/venv/bin/python3 -u -m uvicorn api:app --host 0.0.0.0 --port 8000 --app-dir /app
