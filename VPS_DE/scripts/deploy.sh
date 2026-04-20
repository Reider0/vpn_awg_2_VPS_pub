#!/bin/bash
# Универсальный скрипт деплоя (сам определяет, RU это или DE)

# 1. Определяем абсолютные пути (магия контекста)
# SCRIPT_DIR - папка, где лежит сам скрипт (например, /root/vpn/VPS_RU/scripts)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# NODE_DIR - папка текущей ноды (на уровень выше, т.е. VPS_RU или VPS_DE)
NODE_DIR="$(dirname "$SCRIPT_DIR")"

# PROJECT_ROOT - корень всего репозитория (на уровень выше NODE_DIR)
PROJECT_ROOT="$(dirname "$NODE_DIR")"

echo "[Deploy] Начинаем процесс обновления..."
echo "[Deploy] Рабочая папка ноды: $NODE_DIR"
echo "[Deploy] Корень проекта: $PROJECT_ROOT"

# 2. Останавливаем контейнеры ТОЛЬКО для этой ноды
echo "[Deploy] Шаг 1: Остановка текущих контейнеров..."
cd "$NODE_DIR" || { echo "Ошибка: не могу перейти в $NODE_DIR"; exit 1; }
docker compose down
sleep 3 # Ждем, пока Docker корректно отпустит сети и порты

# 3. Обновляем код всего проекта
echo "[Deploy] Шаг 2: Обновление кода из Git..."
cd "$PROJECT_ROOT" || { echo "Ошибка: не могу перейти в корень $PROJECT_ROOT"; exit 1; }

# Подтягиваем переменные из .env ноды (если там лежит GIT_TOKEN для приватных репо)
if [ -f "$NODE_DIR/.env" ]; then
    export $(grep -E -v '^#' "$NODE_DIR/.env" | xargs)
fi

# Сбрасываем локальные изменения (если вдруг файлы правились руками) и тянем свежие
git fetch --all
git reset --hard origin/main || git reset --hard origin/master
git pull origin main || git pull origin master

# 4. Выдача прав (только на свою папку!)
echo "[Deploy] Шаг 3: Выдача прав на скрипты в папке $NODE_DIR..."
cd "$NODE_DIR" || exit 1
# Ищем все .sh файлы только внутри папки текущей ноды и делаем их исполняемыми
find . -type f -name "*.sh" -exec chmod +x {} \;

# 5. Запуск обновленных контейнеров
echo "[Deploy] Шаг 4: Сборка и запуск новых контейнеров..."
# Запускаем билд, чтобы подтянулись изменения из requirements.txt или Dockerfile
docker compose up -d --build

# Чистим старые неиспользуемые образы Docker, чтобы не забивать диск
docker image prune -f

echo "[Deploy] ✅ Обновление успешно завершено для ноды $(basename "$NODE_DIR")!"