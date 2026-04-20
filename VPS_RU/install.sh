#!/bin/bash

if [ "$EUID" -ne 0 ]; then
  echo "❌ Запустите с правами root: sudo bash install.sh"
  exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "🚀 Установка VPN Dashboard в: $APP_DIR"

echo "🛡️ Базовая настройка безопасности и обновление системы..."
apt-get update && apt-get upgrade -y
apt-get install -y fail2ban ufw curl git unattended-upgrades

systemctl enable fail2ban
systemctl start fail2ban

echo "🛡️ Настройка защиты ядра от DDoS и флуда (sysctl)..."
cat <<EOF > /etc/sysctl.d/99-vpn-security.conf
net.ipv4.tcp_syncookies = 1
net.ipv4.tcp_max_syn_backlog = 2048
net.ipv4.tcp_synack_retries = 2
net.ipv4.icmp_echo_ignore_broadcasts = 1
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1
net.ipv4.tcp_rfc1337 = 1
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
EOF
sysctl --system >/dev/null 2>&1

echo ""
echo "🚨 ЗАЩИТА ОТ БРУТФОРСА (Смена порта SSH)"
read -p "Введите новый порт для SSH (рекомендуется от 10000 до 65000) или нажмите Enter, чтобы оставить 22: " SSH_PORT
SSH_PORT=${SSH_PORT:-22}

if [ "$SSH_PORT" != "22" ]; then
    echo "🔧 Смена порта SSH на $SSH_PORT..."
    ufw allow $SSH_PORT/tcp
    sed -i "s/^#*Port .*/Port $SSH_PORT/" /etc/ssh/sshd_config
    systemctl restart ssh || systemctl restart sshd
    echo "✅ Порт SSH успешно изменен на $SSH_PORT!"
else
    echo "⚠️ Порт SSH оставлен стандартным (22). Включаем защиту..."
    ufw limit 22/tcp
fi

echo "📦 Проверка и настройка Docker..."
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com -o get-docker.sh
    sh get-docker.sh
    rm get-docker.sh
fi
apt-get update && apt-get install -y docker-compose-plugin git
systemctl enable docker
systemctl start docker

echo "🔧 Настройка прав..."
chmod +x "$APP_DIR/install.sh"
mkdir -p "$APP_DIR/scripts"
chmod +x "$APP_DIR/scripts/"*.sh
mkdir -p "$APP_DIR/volumes/flags"
mkdir -p "$APP_DIR/volumes/backups"
mkdir -p "$APP_DIR/volumes/configs"
mkdir -p "$APP_DIR/volumes/wireguard"
mkdir -p "$APP_DIR/volumes/database"

# ИСПРАВЛЕНИЕ: Умный поиск Git-репозитория
cd "$APP_DIR"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git rev-parse HEAD | cut -c1-7 > "$APP_DIR/volumes/VERSION"
else
    echo "unknown" > "$APP_DIR/volumes/VERSION"
fi

echo "⚙️ Настройка демона автообновлений..."
cat <<EOF > /etc/systemd/system/vpn-updater.service
[Unit]
Description=VPN Dashboard Auto-Updater Daemon
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
ExecStart=/bin/bash $APP_DIR/scripts/host_updater.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable vpn-updater
systemctl restart vpn-updater

if [ ! -f "$APP_DIR/.env" ]; then
    touch "$APP_DIR/.env"
fi
chmod 600 "$APP_DIR/.env"

echo "🚀 Запуск контейнеров..."
docker compose up -d --build

echo "⏳ Ожидание инициализации базы данных и API (15 секунд)..."
sleep 15

echo "🤖 Автоматическая генерация ключа DE_AGENT..."
docker exec vpn_bot python init_de_agent.py

if [ -f "$APP_DIR/volumes/DE_AGENT_CONFIG.txt" ]; then
    mv "$APP_DIR/volumes/DE_AGENT_CONFIG.txt" "$APP_DIR/DE_AGENT_CONFIG.txt"
    echo "================================================================"
    echo "🎉 КЛЮЧ ДЛЯ СЕРВЕРА В ГЕРМАНИИ УСПЕШНО СГЕНЕРИРОВАН!"
    echo "👉 $APP_DIR/DE_AGENT_CONFIG.txt"
    echo "Скопируй его в Германию по пути: /volumes/wireguard/wg0.conf"
    echo "================================================================"
fi

echo "✅ УСТАНОВКА И НАСТРОЙКА ЗАВЕРШЕНА!"