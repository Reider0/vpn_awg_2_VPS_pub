#!/bin/bash

# Включаем глобальный форвардинг
sysctl -w net.ipv4.ip_forward=1

echo "🚀 Starting RU Master Node..."

# Создаем пустой список для маршрутизатора, чтобы iptables не выдал ошибку при старте
ipset create ru_nets hash:net -exist

# Запускаем фонового демона обновления списков РУ IP (раз в 24 часа)
(
  while true; do
    bash /app/update_ru_ips.sh
    sleep 86400
  done
) &

# Запуск API
exec /opt/venv/bin/python3 -u -m uvicorn api:app --host 0.0.0.0 --port 8000 --app-dir /app