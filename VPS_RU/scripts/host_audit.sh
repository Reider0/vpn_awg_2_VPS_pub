#!/bin/bash

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLAG_DIR="$APP_DIR/volumes/flags"
REPORT_FILE="$FLAG_DIR/audit_report.json"
STATUS_FILE="$FLAG_DIR/audit_status"

mkdir -p "$FLAG_DIR"
rm -f "$REPORT_FILE"

# Надежная очистка текста для JSON
clean() {
    local text="$1"
    text="${text//$'\n'/ }"
    text="${text//$'\r'/}"
    text="${text//$'\t'/ }"
    text="${text//\\/\\\\}"
    text="${text//\"/\\\"}"
    text=$(printf "%s" "$text" | tr -d '\000-\037')
    printf "%s" "$text"
}

CAT_NET=""
CAT_HOST=""
CAT_DOCKER=""
CAT_VPN=""
CAT_STORAGE=""
CAT_SEC=""
CAT_LOGS=""

add_check() {
    local cat_var=$1
    local name=$(clean "$2")
    local status=$(clean "$3")
    local msg=$(clean "$4")
    local json_str="{\"name\":\"$name\",\"status\":\"$status\",\"msg\":\"$msg\"},"
    printf -v "$cat_var" "%s" "${!cat_var}${json_str}"
}

echo "network" > "$STATUS_FILE"

ping -c 1 -W 2 8.8.8.8 >/dev/null 2>&1
[ $? -eq 0 ] && add_check CAT_NET "Ping Google DNS (8.8.8.8)" "ok" "Доступно" || add_check CAT_NET "Ping Google DNS (8.8.8.8)" "error" "Таймаут"

ping -c 1 -W 2 1.1.1.1 >/dev/null 2>&1
[ $? -eq 0 ] && add_check CAT_NET "Ping Cloudflare (1.1.1.1)" "ok" "Доступно" || add_check CAT_NET "Ping Cloudflare (1.1.1.1)" "warning" "Таймаут"

ping -c 1 -W 2 google.com >/dev/null 2>&1
[ $? -eq 0 ] && add_check CAT_NET "DNS Разрешение имен" "ok" "Работает" || add_check CAT_NET "DNS Разрешение имен" "error" "Сбой DNS"

# ИСПРАВЛЕНО: Telegram заблокирован в РФ - это норма, ставим warning вместо error
curl -s -m 3 https://api.telegram.org >/dev/null 2>&1
[ $? -eq 0 ] && add_check CAT_NET "Доступность Telegram API" "ok" "Связь есть" || add_check CAT_NET "Доступность Telegram API" "warning" "Заблокировано (Норма для РФ)"

curl -s -m 3 https://github.com >/dev/null 2>&1
[ $? -eq 0 ] && add_check CAT_NET "Доступность GitHub" "ok" "Связь есть" || add_check CAT_NET "Доступность GitHub" "warning" "Недоступен"

FWD=$(sysctl -n net.ipv4.ip_forward 2>/dev/null || echo "0")
[ "$FWD" = "1" ] && add_check CAT_NET "IPv4 Forwarding (Маршрутизация)" "ok" "Включено" || add_check CAT_NET "IPv4 Forwarding (Маршрутизация)" "error" "Выключено"

GW=$(ip route show default | awk '{print $3}' | head -n 1)
[ -n "$GW" ] && add_check CAT_NET "Шлюз по умолчанию" "ok" "$GW" || add_check CAT_NET "Шлюз по умолчанию" "error" "Не найден"

TCP_CONN=$(ss -s 2>/dev/null | grep TCP: | grep -oP 'estab \K\d+')
[ -z "$TCP_CONN" ] && TCP_CONN=0
[ "$TCP_CONN" -lt 1000 ] && add_check CAT_NET "Активные TCP сессии" "ok" "$TCP_CONN" || add_check CAT_NET "Активные TCP сессии" "warning" "Высокая нагрузка: $TCP_CONN"

DEF_IFACE=$(ip route show default | awk '{print $5}' | head -n 1)
MTU=$(cat /sys/class/net/$DEF_IFACE/mtu 2>/dev/null)
[ -n "$MTU" ] && add_check CAT_NET "MTU внешнего интерфейса ($DEF_IFACE)" "ok" "$MTU" || add_check CAT_NET "MTU внешнего интерфейса" "warning" "Не определен"

sleep 1

echo "host" > "$STATUS_FILE"

CPU_IDLE=$(vmstat 1 2 | tail -1 | awk '{print $15}')
CPU_USE=$(( 100 - ${CPU_IDLE:-0} ))
[ "$CPU_USE" -lt 90 ] && add_check CAT_HOST "Загрузка CPU" "ok" "${CPU_USE}%" || add_check CAT_HOST "Загрузка CPU" "warning" "Высокая: ${CPU_USE}%"

IO_WAIT=$(vmstat 1 2 | tail -1 | awk '{print $16}')
[ "${IO_WAIT:-0}" -lt 15 ] && add_check CAT_HOST "Дисковый I/O Wait" "ok" "${IO_WAIT:-0}%" || add_check CAT_HOST "Дисковый I/O Wait" "warning" "${IO_WAIT:-0}% (Медленный диск)"

LOAD=$(awk '{print $1}' /proc/loadavg)
add_check CAT_HOST "Load Average (1m)" "ok" "$LOAD"

RAM_USE=$(free -m | awk 'NR==2{if($2>0) printf "%d", $3*100/$2; else print "0"}')
[ -z "$RAM_USE" ] && RAM_USE=0
[ "$RAM_USE" -lt 95 ] && add_check CAT_HOST "Оперативная память (RAM)" "ok" "${RAM_USE}% занято" || add_check CAT_HOST "Оперативная память (RAM)" "error" "Критично: ${RAM_USE}%"

SWAP=$(free -m | awk 'NR==3{if($2>0) printf "%d", $3*100/$2; else print "0"}')
[ -z "$SWAP" ] && SWAP=0
add_check CAT_HOST "Файл подкачки (Swap)" "ok" "${SWAP}% занято"

FD_TOTAL=$(cat /proc/sys/fs/file-nr 2>/dev/null | awk '{print $1}')
add_check CAT_HOST "Открытые файловые дескрипторы" "ok" "$FD_TOTAL"

ZOMBIES=$(ps aux | awk '$8 ~ /Z/ {count++} END {print count+0}')
[ "$ZOMBIES" -eq 0 ] && add_check CAT_HOST "Зомби-процессы" "ok" "0" || add_check CAT_HOST "Зомби-процессы" "warning" "Найдено: $ZOMBIES"

UPTIME=$(awk '{print int($1/86400)"d "int(($1%86400)/3600)"h"}' /proc/uptime)
add_check CAT_HOST "Аптайм сервера" "ok" "$UPTIME"

TIMEDATE=$(timedatectl show 2>/dev/null | grep NTPSynchronized | cut -d= -f2)
[ "$TIMEDATE" = "yes" ] && add_check CAT_HOST "Синхронизация времени (NTP)" "ok" "Включена" || add_check CAT_HOST "Синхронизация времени (NTP)" "warning" "Не синхронизировано"

KERNEL=$(uname -r)
add_check CAT_HOST "Версия Ядра Linux" "ok" "$KERNEL"

sleep 1

echo "docker" > "$STATUS_FILE"

systemctl is-active --quiet docker
[ $? -eq 0 ] && add_check CAT_DOCKER "Служба Docker Daemon" "ok" "Active" || add_check CAT_DOCKER "Служба Docker Daemon" "error" "Остановлен"

docker compose version >/dev/null 2>&1
[ $? -eq 0 ] && add_check CAT_DOCKER "Плагин Docker Compose" "ok" "Установлен" || add_check CAT_DOCKER "Плагин Docker Compose" "error" "Не найден"

check_cont() {
    local stat=$(docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null || echo "missing")
    [ "$stat" = "running" ] && add_check CAT_DOCKER "Контейнер $1" "ok" "Running" || add_check CAT_DOCKER "Контейнер $1" "error" "$stat"
}

check_cont "vpn_bot"
check_cont "vpn_wireguard"
check_cont "vpn_db"

NET_EXISTS=$(docker network ls | grep vpn)
[ -n "$NET_EXISTS" ] && add_check CAT_DOCKER "Изолированная сеть Docker" "ok" "Существует" || add_check CAT_DOCKER "Изолированная сеть Docker" "error" "Не найдена"

D_SPACE=$(docker system df --format '{{.Size}}' | head -n 1)
add_check CAT_DOCKER "Объем данных Docker" "ok" "$D_SPACE"

EXITED=$(docker ps -aq -f status=exited | wc -l)
[ "$EXITED" -eq 0 ] && add_check CAT_DOCKER "Остановленные контейнеры" "ok" "0" || add_check CAT_DOCKER "Остановленные контейнеры" "warning" "$EXITED шт. (Тратят место)"

API_PORT=$(ss -tuln 2>/dev/null | grep -q ":8000 "; echo $?)
[ $API_PORT -ne 0 ] && add_check CAT_DOCKER "Порты внутри моста (API)" "ok" "Закрыты снаружи" || add_check CAT_DOCKER "Порты внутри моста (API)" "warning" "Торчат наружу!"

sleep 1

echo "vpn" > "$STATUS_FILE"

WG_PORT=$(ss -uln 2>/dev/null | grep ":51820")
[ -n "$WG_PORT" ] && add_check CAT_VPN "Прослушивание UDP 51820" "ok" "Открыт" || add_check CAT_VPN "Прослушивание UDP 51820" "error" "Порт закрыт/Не слушается"

docker exec vpn_wireguard ip link show wg0 >/dev/null 2>&1
[ $? -eq 0 ] && add_check CAT_VPN "Сетевой интерфейс wg0" "ok" "Поднят" || add_check CAT_VPN "Сетевой интерфейс wg0" "error" "Не найден в контейнере"

WG_IP=$(docker exec vpn_wireguard ip -4 addr show wg0 2>/dev/null | grep -oP 'inet \K[\d.]+')
[ -n "$WG_IP" ] && add_check CAT_VPN "IP-адрес ядра VPN" "ok" "$WG_IP" || add_check CAT_VPN "IP-адрес ядра VPN" "error" "Не назначен"

WG_MTU=$(docker exec vpn_wireguard cat /sys/class/net/wg0/mtu 2>/dev/null)
[ "$WG_MTU" = "1280" ] && add_check CAT_VPN "MTU туннеля wg0" "ok" "1280 (Оптимально)" || add_check CAT_VPN "MTU туннеля wg0" "warning" "Текущий: ${WG_MTU:-unknown}"

CONF_FILE="/volumes/wireguard/wg0.conf"
[ -f "$APP_DIR$CONF_FILE" ] && add_check CAT_VPN "Конфиг wg0.conf" "ok" "Существует" || add_check CAT_VPN "Конфиг wg0.conf" "error" "Отсутствует"

[ -f "$APP_DIR/volumes/wireguard/public.key" ] && add_check CAT_VPN "Ключи шифрования (Server)" "ok" "Существуют" || add_check CAT_VPN "Ключи шифрования (Server)" "error" "Отсутствуют"

OBFUSCATION=$(grep -E "Jc|Jmin|Jmax" "$APP_DIR$CONF_FILE" 2>/dev/null)
[ -n "$OBFUSCATION" ] && add_check CAT_VPN "Обфускация AmneziaWG" "ok" "Активна (Анти-DPI)" || add_check CAT_VPN "Обфускация AmneziaWG" "warning" "Параметры не найдены"

MASQ=$(docker exec vpn_wireguard iptables -t nat -S 2>/dev/null | grep MASQUERADE)
[ -n "$MASQ" ] && add_check CAT_VPN "NAT Masquerade (Трафик)" "ok" "Настроено" || add_check CAT_VPN "NAT Masquerade (Трафик)" "error" "Отсутствует"

WG_DUMP=$(docker exec vpn_wireguard wg show wg0 dump 2>/dev/null | wc -l)
[ "$WG_DUMP" -ge 1 ] && add_check CAT_VPN "Ответ ядра WireGuard" "ok" "Успешно" || add_check CAT_VPN "Ответ ядра WireGuard" "error" "Ядро зависло/не отвечает"

TUN=$(ls /dev/net/tun 2>/dev/null)
[ -n "$TUN" ] && add_check CAT_VPN "Модуль TUN/TAP" "ok" "Доступен" || add_check CAT_VPN "Модуль TUN/TAP" "error" "Не найден"

sleep 1

echo "storage" > "$STATUS_FILE"

ROOT_DISK=$(df -h / | awk 'NR==2 {print $5}' | tr -d '%')
[ -z "$ROOT_DISK" ] && ROOT_DISK=0
[ "$ROOT_DISK" -lt 95 ] && add_check CAT_STORAGE "Свободное место на диске (/)" "ok" "${ROOT_DISK}% занято" || add_check CAT_STORAGE "Свободное место на диске (/)" "error" "Критично: ${ROOT_DISK}%"

INODES=$(df -i / | awk 'NR==2 {print $5}' | tr -d '%')
[ -z "$INODES" ] && INODES=0
[ "$INODES" -lt 95 ] && add_check CAT_STORAGE "Индексные дескрипторы (Inodes)" "ok" "${INODES}% занято" || add_check CAT_STORAGE "Индексные дескрипторы (Inodes)" "error" "Заканчиваются: ${INODES}%"

touch /tmp/audit_rw_test 2>/dev/null && rm /tmp/audit_rw_test 2>/dev/null
[ $? -eq 0 ] && add_check CAT_STORAGE "Права записи на диск" "ok" "Доступно (R/W)" || add_check CAT_STORAGE "Права записи на диск" "error" "Диск в режиме Read-Only!"

[ -d "$APP_DIR/volumes/database" ] && add_check CAT_STORAGE "Директория БД (/database)" "ok" "Смонтирована" || add_check CAT_STORAGE "Директория БД (/database)" "error" "Отсутствует"

# ИСПРАВЛЕНО: проверяем коннект с правильным пользователем БД vpn_admin
docker exec vpn_db pg_isready -U vpn_admin -d vpndb >/dev/null 2>&1
[ $? -eq 0 ] && add_check CAT_STORAGE "Соединение с PostgreSQL" "ok" "Принимает запросы" || add_check CAT_STORAGE "Соединение с PostgreSQL" "error" "Отказ в обслуживании"

[ -d "$APP_DIR/volumes/backups" ] && add_check CAT_STORAGE "Директория резервных копий" "ok" "Существует" || add_check CAT_STORAGE "Директория резервных копий" "warning" "Отсутствует"

BACKUP_FILE_PATH="$APP_DIR/volumes/backups/backup_latest.tar.gz"
if [ -f "$BACKUP_FILE_PATH" ]; then
    AGE=$(find "$BACKUP_FILE_PATH" -mtime -2)
    [ -n "$AGE" ] && add_check CAT_STORAGE "Актуальность Бэкапа" "ok" "Свежий (< 48ч)" || add_check CAT_STORAGE "Актуальность Бэкапа" "warning" "Устарел (> 48ч)"
else
    add_check CAT_STORAGE "Актуальность Бэкапа" "warning" "Бэкап не найден"
fi

[ -d "$APP_DIR/volumes/configs" ] && add_check CAT_STORAGE "Хранилище конфигов" "ok" "Доступно" || add_check CAT_STORAGE "Хранилище конфигов" "error" "Удалено"

ENV_PERM=$(stat -c "%a" "$APP_DIR/.env" 2>/dev/null)
[ -z "$ENV_PERM" ] && ENV_PERM="none"
if [ "$ENV_PERM" = "600" ] || [ "$ENV_PERM" = "640" ]; then
    add_check CAT_STORAGE "Права доступа к .env" "ok" "Безопасные ($ENV_PERM)"
else
    add_check CAT_STORAGE "Права доступа к .env" "warning" "Открыты всем: $ENV_PERM (рекомендуется 600)"
fi

sleep 1

echo "security" > "$STATUS_FILE"

ROOT_SSH=$(grep "^PermitRootLogin yes" /etc/ssh/sshd_config 2>/dev/null)
[ -n "$ROOT_SSH" ] && add_check CAT_SEC "SSH Root Login" "warning" "Разрешен (Рекомендуется отключить)" || add_check CAT_SEC "SSH Root Login" "ok" "Защищен"

SSH_PASS=$(grep -iE "^PasswordAuthentication\s+yes" /etc/ssh/sshd_config 2>/dev/null)
[ -z "$SSH_PASS" ] && add_check CAT_SEC "Вход по паролю (SSH)" "ok" "Отключен (по ключам)" || add_check CAT_SEC "Вход по паролю (SSH)" "warning" "Разрешен (Уязвимо к брутфорсу)"

SSH_PORT=$(grep -iE "^Port\s+" /etc/ssh/sshd_config | awk '{print $2}' 2>/dev/null)
[ "${SSH_PORT:-22}" = "22" ] && add_check CAT_SEC "Порт SSH" "warning" "Стандартный 22 (Риск)" || add_check CAT_SEC "Порт SSH" "ok" "Нестандартный (${SSH_PORT:-22})"

UFW_STAT=$(ufw status 2>/dev/null | grep -i "active")
IPT_STAT=$(iptables -L -n 2>/dev/null | grep "Chain INPUT" | wc -l)
if [ -n "$UFW_STAT" ]; then
    add_check CAT_SEC "Межсетевой экран (Firewall)" "ok" "UFW Активен"
elif [ "$IPT_STAT" -gt 0 ]; then
    add_check CAT_SEC "Межсетевой экран (Firewall)" "ok" "Iptables настроен"
else
    add_check CAT_SEC "Межсетевой экран (Firewall)" "warning" "Не обнаружен"
fi

F2B=$(systemctl is-active fail2ban 2>/dev/null)
[ "$F2B" = "active" ] && add_check CAT_SEC "Служба Fail2Ban" "ok" "Защищает от брутфорса" || add_check CAT_SEC "Служба Fail2Ban" "warning" "Не установлена"

EMPTY_PW=$(awk -F: '($2 == "") {print $1}' /etc/shadow 2>/dev/null)
[ -z "$EMPTY_PW" ] && add_check CAT_SEC "Пустые пароли пользователей" "ok" "Не обнаружены" || add_check CAT_SEC "Пустые пароли пользователей" "error" "ОПАСНОСТЬ: Есть аккаунты без пароля"

visudo -c >/dev/null 2>&1
[ $? -eq 0 ] && add_check CAT_SEC "Синтаксис Sudoers" "ok" "Корректен" || add_check CAT_SEC "Синтаксис Sudoers" "error" "Сломан файл sudoers!"

TODAY_STR=$(date '+%b %e' | sed 's/  / /')
FAILED_TOTAL=$(grep "Failed password" /var/log/auth.log 2>/dev/null | wc -l)
FAILED_TODAY=$(grep "Failed password" /var/log/auth.log 2>/dev/null | grep "^$TODAY_STR" | wc -l)

if [ "$FAILED_TOTAL" -gt 50 ]; then
    add_check CAT_SEC "Брутфорс атаки (SSH)" "warning" "За сегодня: $FAILED_TODAY | Всего: $FAILED_TOTAL"
else
    add_check CAT_SEC "Брутфорс атаки (SSH)" "ok" "За сегодня: $FAILED_TODAY | Всего: $FAILED_TOTAL"
fi

sleep 1

echo "services" > "$STATUS_FILE"

systemctl is-active --quiet vpn-updater
[ $? -eq 0 ] && add_check CAT_LOGS "Демон vpn-updater" "ok" "Active" || add_check CAT_LOGS "Демон vpn-updater" "error" "Остановлен"

CRON_STAT=$(systemctl is-active cron 2>/dev/null || systemctl is-active crond 2>/dev/null)
[ "$CRON_STAT" = "active" ] && add_check CAT_LOGS "Планировщик (Cron)" "ok" "Работает" || add_check CAT_LOGS "Планировщик (Cron)" "warning" "Остановлен"

JOURNAL_STAT=$(systemctl is-active systemd-journald 2>/dev/null)
[ "$JOURNAL_STAT" = "active" ] && add_check CAT_LOGS "Системный Журнал (Journald)" "ok" "Работает" || add_check CAT_LOGS "Системный Журнал (Journald)" "error" "Остановлен"

FAILED_UNITS=$(systemctl list-units --state=failed --no-legend 2>/dev/null | wc -l)
[ "$FAILED_UNITS" -eq 0 ] && add_check CAT_LOGS "Упавшие службы Linux" "ok" "0" || add_check CAT_LOGS "Упавшие службы Linux" "warning" "Найдено: $FAILED_UNITS"

OOM=$(dmesg 2>/dev/null | grep -i "killed process" | wc -l)
[ "$OOM" -eq 0 ] && add_check CAT_LOGS "OOM Killer (Нехватка памяти)" "ok" "Не зафиксировано" || add_check CAT_LOGS "OOM Killer (Нехватка памяти)" "warning" "Были утечки памяти"

check_logs() {
    local errs=$(docker logs --tail 150 "$1" 2>&1 | grep -iE "error|fatal|exception|traceback" | grep -viE "Task was destroyed|CancelledError" | tail -n 1)
    if [ -n "$errs" ]; then
        local short_errs="${errs:0:80}"
        [ "${#errs}" -gt 80 ] && short_errs="${short_errs}..."
        local cln=$(clean "$short_errs")
        add_check CAT_LOGS "Логи контейнера $1" "warning" "$cln"
    else
        add_check CAT_LOGS "Логи контейнера $1" "ok" "Чисто"
    fi
}

check_logs "vpn_bot"
check_logs "vpn_wireguard"
check_logs "vpn_db"

UPDATES=$(apt-get -s upgrade 2>/dev/null | grep -Po "^Inst \K[^ ]+" | wc -l)
[ "${UPDATES:-0}" -eq 0 ] && add_check CAT_LOGS "Системные обновления ОС" "ok" "Все установлено" || add_check CAT_LOGS "Системные обновления ОС" "warning" "Доступно $UPDATES пакетов"

# ---- ПОДГОТОВКА JSON И ИСПРАВЛЕНИЕ RACE CONDITION ----

CAT_NET="[${CAT_NET%,}]"
CAT_HOST="[${CAT_HOST%,}]"
CAT_DOCKER="[${CAT_DOCKER%,}]"
CAT_VPN="[${CAT_VPN%,}]"
CAT_STORAGE="[${CAT_STORAGE%,}]"
CAT_SEC="[${CAT_SEC%,}]"
CAT_LOGS="[${CAT_LOGS%,}]"

cat <<EOF > "$REPORT_FILE"
{
  "network": $CAT_NET,
  "host": $CAT_HOST,
  "docker": $CAT_DOCKER,
  "vpn": $CAT_VPN,
  "storage": $CAT_STORAGE,
  "security": $CAT_SEC,
  "services": $CAT_LOGS
}
EOF

# ТОЛЬКО ПОСЛЕ сохранения файла сообщаем боту, что данные готовы!
echo "done" > "$STATUS_FILE"