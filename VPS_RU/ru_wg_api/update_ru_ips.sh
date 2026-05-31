#!/bin/bash
# Обновление списков маршрутизации (запускается фоном раз в 12ч, см. run_api.sh).
#
# Два набора ipset:
#   ru_nets       — гео-IP России. Трафик в эти сети идёт НАПРЯМУЮ (минуя Германию):
#                   банки, госуслуги, локальные сервисы.
#   blocked_nets  — реестр заблокированных РКН ресурсов (antifilter.download).
#                   Этот трафик ПРИНУДИТЕЛЬНО уходит в Германию, даже если ресурс
#                   размещён на российском IP. Так обход блокировок становится
#                   качественнее, не ломая прямую маршрутизацию РФ.
#
# Надёжность: данные кэшируются на диск (volume), и при сбое сети список НЕ
# обнуляется — используется последняя удачная версия из кэша.

set -uo pipefail

CACHE_DIR="/etc/amnezia/amneziawg/cache"
mkdir -p "$CACHE_DIR"
RU_CACHE="$CACHE_DIR/ru_nets.cidr"
BL_CACHE="$CACHE_DIR/blocked_nets.cidr"

# Строгая проверка формата IPv4-подсети, чтобы HTML-ошибки/мусор не попали в ipset
CIDR_RE='^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}/[0-9]{1,2}$'

# Загружает набор из cidr-файла в ядро (атомарно: create+flush+add одним restore)
load_set () {
    local set_name="$1" file="$2"
    [ -s "$file" ] || return 1
    {
        echo "create $set_name hash:net family inet hashsize 4096 maxelem 1000000 -exist"
        echo "flush $set_name"
        grep -E "$CIDR_RE" "$file" | sed "s/^/add $set_name /"
    } | ipset restore -!
}

# Скачивает гео-RU: пробует источники по очереди, первый валидный (>=100 сетей) выигрывает
fetch_geo_ru () {
    local tmp; tmp="$(mktemp)"
    local url
    for url in \
        "https://www.ipdeny.com/ipblocks/data/aggregated/ru-aggregated.zone" \
        "https://raw.githubusercontent.com/dvershinin/ip-country-cidr/master/ru-ipv4.cidr"
    do
        if curl -sSfL --max-time 30 "$url" 2>/dev/null | grep -E "$CIDR_RE" > "$tmp"; then
            if [ "$(wc -l < "$tmp")" -ge 100 ]; then
                mv "$tmp" "$RU_CACHE"
                return 0
            fi
        fi
    done
    rm -f "$tmp"
    return 1
}

# Скачивает список блокировок РКН с antifilter.download.
# Берём только суммаризированные подсети (ipsum+subnet) — этого достаточно: в гибридной
# модели blocked_nets влияет лишь на заблокированные ресурсы на РОССИЙСКИХ IP (зарубеж и так
# тунеллируется). Большие ip.lst/ipresolve.lst дали бы 150к зарубежных адресов-балласта.
fetch_blocked () {
    local tmp; tmp="$(mktemp)"
    # Быстрый путь: allyouneed.lst = ipsum.lst + subnet.lst одним файлом (один запрос вместо двух)
    if curl -sSfL --max-time 30 "https://antifilter.download/list/allyouneed.lst" 2>/dev/null | grep -E "$CIDR_RE" > "$tmp"; then
        if [ "$(wc -l < "$tmp")" -ge 100 ]; then
            sort -u "$tmp" > "$BL_CACHE"
            rm -f "$tmp"
            return 0
        fi
    fi
    # Фолбэк: собрать из частей, если allyouneed недоступен
    : > "$tmp"
    local url
    for url in \
        "https://antifilter.download/list/ipsum.lst" \
        "https://antifilter.download/list/subnet.lst"
    do
        curl -sSfL --max-time 30 "$url" 2>/dev/null | grep -E "$CIDR_RE" >> "$tmp" || true
    done
    if [ "$(wc -l < "$tmp")" -ge 100 ]; then
        sort -u "$tmp" > "$BL_CACHE"
        rm -f "$tmp"
        return 0
    fi
    rm -f "$tmp"
    return 1
}

echo "🔄 Обновление списков маршрутизации (гео-RU + блокировки РКН)..."

if fetch_geo_ru; then
    echo "✅ гео-RU обновлён: $(wc -l < "$RU_CACHE") сетей"
else
    echo "⚠️ гео-RU: сеть недоступна, используем кэш"
fi

if fetch_blocked; then
    echo "✅ antifilter (блокировки) обновлён: $(wc -l < "$BL_CACHE") сетей"
else
    echo "⚠️ antifilter: сеть недоступна, используем кэш"
fi

if load_set ru_nets "$RU_CACHE"; then echo "📥 ru_nets загружен в ядро"; else echo "⚠️ ru_nets: нет данных (ни сети, ни кэша)"; fi
if load_set blocked_nets "$BL_CACHE"; then echo "📥 blocked_nets загружен в ядро"; else echo "⚠️ blocked_nets: нет данных (ни сети, ни кэша)"; fi

echo "✅ Готово."
