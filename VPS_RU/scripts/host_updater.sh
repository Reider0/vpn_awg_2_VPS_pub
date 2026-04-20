#!/bin/bash
# Демон хоста (слушает команды от Telegram-бота)

# Определяем пути динамически
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE_DIR="$(dirname "$SCRIPT_DIR")"
FLAGS_DIR="$NODE_DIR/volumes/flags"

# Флаги
UPDATE_FLAG="$FLAGS_DIR/do_update"
REBOOT_FLAG="$FLAGS_DIR/do_reboot"
AUDIT_FLAG="$FLAGS_DIR/do_audit"
RESTART_WG_FLAG="$FLAGS_DIR/do_restart_wg"
CLEANUP_FLAG="$FLAGS_DIR/do_cleanup"

echo "[Updater] Демон запущен для ноды: $(basename "$NODE_DIR")"
echo "[Updater] Ожидание флагов в директории: $FLAGS_DIR"

# Убедимся, что папка для флагов существует
mkdir -p "$FLAGS_DIR"

while true; do
    # 1. ОБНОВЛЕНИЕ СИСТЕМЫ
    if [ -f "$UPDATE_FLAG" ]; then
        echo "[Updater] Найдена метка обновления. Запуск deploy.sh..."
        rm -f "$UPDATE_FLAG"
        
        # Запускаем скрипт деплоя с точным путем
        bash "$SCRIPT_DIR/deploy.sh"
        
        echo "[Updater] Цикл обновления завершен."
    fi

    # 2. ПЕРЕЗАГРУЗКА СЕРВЕРА
    if [ -f "$REBOOT_FLAG" ]; then
        echo "[Updater] Найдена метка перезагрузки! Сервер уходит в ребут..."
        rm -f "$REBOOT_FLAG"
        /usr/sbin/reboot
    fi

    # 3. АУДИТ ХОСТА
    if [ -f "$AUDIT_FLAG" ]; then
        echo "[Updater] Найдена метка аудита. Запуск host_audit.sh..."
        rm -f "$AUDIT_FLAG"
        bash "$SCRIPT_DIR/host_audit.sh"
        echo "[Updater] Аудит завершен."
    fi

    # 4. ЖЕСТКИЙ РЕСТАРТ WIREGUARD (SELF-HEALING)
    if [ -f "$RESTART_WG_FLAG" ]; then
        echo "[Updater] Найдена метка рестарта WG. Перезапуск контейнеров..."
        rm -f "$RESTART_WG_FLAG"
        # Ищем контейнер по имени или через docker compose
        cd "$NODE_DIR" && docker compose restart ru_wireguard || docker compose restart de_vpn_agent
    fi
    
    # 5. ОЧИСТКА МУСОРА
    if [ -f "$CLEANUP_FLAG" ]; then
        echo "[Updater] Очистка логов и кэша Docker..."
        rm -f "$CLEANUP_FLAG"
        docker system prune -af --volumes
        journalctl --vacuum-time=3d
    fi

    # Пауза перед следующей проверкой
    sleep 5
done