#!/bin/bash

# Включаем форвардинг трафика на уровне ядра контейнера
sysctl -w net.ipv4.ip_forward=1

echo "🔧 Configuring WireGuard paths..."
mkdir -p /etc/wireguard

# Создаем симлинк (wg-quick ищет конфиги в /etc/wireguard)
ln -sf /etc/amnezia/amneziawg/wg0.conf /etc/wireguard/wg0.conf

# Пробуем поднять туннель при старте, если конфиг уже существует
if [ -f "/etc/wireguard/wg0.conf" ]; then
    echo " Setting up wg0 interface..."
    
    # Страховка: зачищаем DNS-строку, если она туда как-то попала
    sed -i '/^DNS/d' /etc/wireguard/wg0.conf
    
    # Удаляем зависший интерфейс
    ip link delete wg0 2>/dev/null || true
    
    # Поднимаем туннель
    wg-quick up wg0 || echo "⚠️ Warning: Could not start wg0 automatically"
    
    # Настраиваем маскарад
    iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE || true
else
    echo "⚠️ Warning: wg0.conf not found. Waiting for manual config upload from RU Master."
fi

echo "🚀 Starting DE Agent (AmneziaWG Client + Monitor API)..."

# Запускаем API агента
exec /opt/venv/bin/python3 -u -m uvicorn api:app --host 0.0.0.0 --port 8000 --app-dir /app