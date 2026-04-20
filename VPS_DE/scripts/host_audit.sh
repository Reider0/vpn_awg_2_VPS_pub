#!/bin/bash

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLAG_DIR="$APP_DIR/volumes/flags"
REPORT_FILE="$FLAG_DIR/audit_report.json"
STATUS_FILE="$FLAG_DIR/audit_status"

mkdir -p "$FLAG_DIR"
rm -f "$REPORT_FILE"

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
CAT_VPN=""

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
[ $? -eq 0 ] && add_check CAT_NET "Ping Google (8.8.8.8)" "ok" "Доступно" || add_check CAT_NET "Ping Google" "error" "Таймаут"

FWD=$(sysctl -n net.ipv4.ip_forward 2>/dev/null || echo "0")
[ "$FWD" = "1" ] && add_check CAT_NET "IPv4 Forwarding" "ok" "Включено" || add_check CAT_NET "IPv4 Forwarding" "error" "Выключено"
sleep 1

echo "host" > "$STATUS_FILE"
CPU_IDLE=$(vmstat 1 2 | tail -1 | awk '{print $15}')
CPU_USE=$(( 100 - ${CPU_IDLE:-0} ))
[ "$CPU_USE" -lt 90 ] && add_check CAT_HOST "Загрузка CPU" "ok" "${CPU_USE}%" || add_check CAT_HOST "Загрузка CPU" "warning" "${CPU_USE}%"

RAM_USE=$(free -m | awk 'NR==2{if($2>0) printf "%d", $3*100/$2; else print "0"}')
[ "${RAM_USE:-0}" -lt 95 ] && add_check CAT_HOST "RAM" "ok" "${RAM_USE:-0}%" || add_check CAT_HOST "RAM" "error" "${RAM_USE:-0}%"
sleep 1

echo "vpn" > "$STATUS_FILE"
docker exec de_vpn_agent ip link show wg0 >/dev/null 2>&1
[ $? -eq 0 ] && add_check CAT_VPN "Интерфейс wg0" "ok" "Поднят" || add_check CAT_VPN "Интерфейс wg0" "error" "Упал"

MASQ=$(docker exec de_vpn_agent iptables -t nat -S 2>/dev/null | grep MASQUERADE)
[ -n "$MASQ" ] && add_check CAT_VPN "NAT Masquerade" "ok" "Настроено" || add_check CAT_VPN "NAT Masquerade" "error" "Отсутствует"

# Формируем JSON
CAT_NET="[${CAT_NET%,}]"
CAT_HOST="[${CAT_HOST%,}]"
CAT_VPN="[${CAT_VPN%,}]"

cat <<EOF > "$REPORT_FILE"
{
  "network": $CAT_NET,
  "host": $CAT_HOST,
  "vpn": $CAT_VPN
}
EOF

echo "done" > "$STATUS_FILE"