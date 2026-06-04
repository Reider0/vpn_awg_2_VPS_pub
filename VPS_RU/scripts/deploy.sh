#!/bin/bash
# Универсальный скрипт деплоя (сам определяет, RU это или DE).
#
# Принцип минимального даунтайма:
#   1) тянем код, 2) СОБИРАЕМ образы пока старые контейнеры ещё работают,
#   3) только если сборка удалась — быстро пересоздаём контейнеры (краткий рестарт).
# Если сборка падает (например, недоступен индекс пакетов) — прод НЕ трогаем,
# VPN продолжает работать на старой версии. Даунтайм только на шаге пересоздания.

# 1. Абсолютные пути (магия контекста)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE_DIR="$(dirname "$SCRIPT_DIR")"        # VPS_RU или VPS_DE
PROJECT_ROOT="$(dirname "$NODE_DIR")"      # корень репозитория

echo "[Deploy] Начинаем процесс обновления..."
echo "[Deploy] Рабочая папка ноды: $NODE_DIR"
echo "[Deploy] Корень проекта: $PROJECT_ROOT"

# 2. Обновляем код всего проекта (контейнеры пока РАБОТАЮТ — даунтайма нет)
echo "[Deploy] Шаг 1: Обновление кода из Git..."
cd "$PROJECT_ROOT" || { echo "Ошибка: не могу перейти в корень $PROJECT_ROOT"; exit 1; }

# Подтягиваем переменные из .env ноды (если там лежит GIT_TOKEN для приватных репо)
if [ -f "$NODE_DIR/.env" ]; then
    export $(grep -E -v '^#' "$NODE_DIR/.env" | xargs)
fi

# Сбрасываем локальные изменения (если файлы правились руками) и тянем свежие
git fetch --all
git reset --hard origin/main || git reset --hard origin/master
git pull origin main || git pull origin master

# Фиксируем актуальный коммит, чтобы бот не считал, что обновление всё ещё доступно.
# (Раньше volumes/VERSION писался только install.sh → локальный хеш «застывал».)
mkdir -p "$NODE_DIR/volumes"
NEW_HASH="$(git rev-parse HEAD 2>/dev/null | cut -c1-7)"
if [ -n "$NEW_HASH" ]; then
    echo "$NEW_HASH" > "$NODE_DIR/volumes/VERSION"
    echo "[Deploy] Текущий коммит зафиксирован: $NEW_HASH"
fi

# 3. Права на скрипты этой ноды
echo "[Deploy] Шаг 2: Выдача прав на скрипты в папке $NODE_DIR..."
cd "$NODE_DIR" || exit 1
find . -type f -name "*.sh" -exec chmod +x {} \;

# 4. СБОРКА новых образов, пока старые контейнеры ещё работают (даунтайм = 0)
echo "[Deploy] Шаг 3: Сборка новых образов (старые контейнеры продолжают работать)..."
if ! docker compose build; then
    echo "[Deploy] ❌ Сборка не удалась — работающие контейнеры НЕ трогаю."
    echo "[Deploy] Деплой отменён, VPN продолжает работать на старой версии."
    exit 1
fi

# 5. Быстрое пересоздание контейнеров на новых образах (минимальный даунтайм)
echo "[Deploy] Шаг 4: Применение новых образов (краткий перезапуск)..."
docker compose up -d

# 6. Чистим старые неиспользуемые образы, чтобы не забивать диск
docker image prune -f

echo "[Deploy] ✅ Обновление успешно завершено для ноды $(basename "$NODE_DIR")!"
