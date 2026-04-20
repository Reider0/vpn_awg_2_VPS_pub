#!/bin/bash

# Скрипт установки DE Agent (Сервер в Германии)
set -e

if [ "$EUID" -ne 0 ]; then
  echo "❌ Запустите с правами root: sudo bash install.sh"
  exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "=========================================="
echo "🚀 Начинаем установку DE Agent в: $APP_DIR"
echo "=========================================="

echo "🛡️ Настройка безопасности и обновление системы..."
apt-get update && apt-get upgrade -y
apt-get install -y fail2ban ufw curl wget git jq iptables iproute2 procps unattended-upgrades

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
echo "---------------------------------------------------------"

if [ ! -f /swapfile ]; then
    echo "--> Создание файла подкачки (Swap) на 2 ГБ..."
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    sysctl vm.swappiness=10
    echo 'vm.swappiness=10' >> /etc/sysctl.conf
fi

if ! command -v docker &> /dev/null; then
    echo "--> Установка Docker..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sh get-docker.sh
    rm get-docker.sh
fi
apt-get update && apt-get install -y docker-compose-plugin
systemctl enable docker
systemctl start docker

echo "--> Создание директорий..."
chmod +x "$APP_DIR/install.sh"
mkdir -p "$APP_DIR/scripts"
chmod +x "$APP_DIR/scripts/"*.sh
mkdir -p "$APP_DIR/volumes/flags"
mkdir -p "$APP_DIR/volumes/wireguard"
chmod -R 777 "$APP_DIR/volumes/flags"

# ИСПРАВЛЕНИЕ: Умный поиск Git-репозитория
cd "$APP_DIR"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git rev-parse HEAD | cut -c1-7 > "$APP_DIR/volumes/VERSION"
else
    echo "unknown" > "$APP_DIR/volumes/VERSION"
fi

if [ ! -f "$APP_DIR/.env" ]; then
    echo "📝 Создаю пустой .env..."
    touch "$APP_DIR/.env"
fi
chmod 600 "$APP_DIR/.env"

echo "--> Настройка системного демона DE Agent..."
SERVICE_PATH="/etc/systemd/system/de-agent-updater.service"

cat <<EOF > $SERVICE_PATH
[Unit]
Description=DE VPN Agent Host Daemon
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=/bin/bash $APP_DIR/scripts/host_updater.sh
Restart=always
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable de-agent-updater.service
systemctl restart de-agent-updater.service

echo "--> Сборка и запуск контейнера de_vpn_agent..."
docker compose up -d --build

echo "✅ УСТАНОВКА DE AGENT ЗАВЕРШЕНА!"