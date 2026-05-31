import os
import re
import subprocess
from pathlib import Path
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from datetime import datetime, timedelta

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Версия формата маршрутизации конфига. Должна совпадать с ROUTING_VERSION в
# ru_wg_api/api.py. Ключи с меньшей версией считаются устаревшими (полный туннель,
# без обхода дата-центро-враждебных РФ-сервисов) — им шлём ежедневное напоминание.
ROUTING_VERSION = 1

VERSION_FILE = "/app/VERSION_FILE"
BACKUP_FILE = "/volumes/backups/backup_latest.tar.gz"
GIT_REPO = os.getenv("GIT_REPO", "") 
GIT_USERNAME = os.getenv("GIT_USERNAME", "")
GIT_TOKEN = os.getenv("GIT_TOKEN", "")

# API серверов (Бот и WireGuard теперь в одной сети, поэтому 127.0.0.1)
WG_API_URL = os.getenv("WG_API_URL", "http://127.0.0.1:8000/api")
DE_AGENT_URL = os.getenv("DE_AGENT_URL", "http://10.13.13.254:8000/api")

CONFIGS_DIR = Path("/volumes/configs")
CONFIGS_DIR.mkdir(parents=True, exist_ok=True)

# --- ГЛОБАЛЬНОЕ СОСТОЯНИЕ ---
state_data = {
    "dashboard_running": False,
    "dashboard_task": None,
    "graph_running": False,
    "graph_task": None,
    "active_menus": {},
    "last_known_active_count": -1,
    "support_context": {},
    "bg_tasks": set()
}

# --- ВРЕМЯ (МОСКВА UTC+3) ---
def get_moscow_now():
    """Возвращает текущее Московское время"""
    return datetime.utcnow() + timedelta(hours=3)

def dt_to_moscow(dt):
    """Конвертирует UTC datetime из базы данных в Московское время"""
    if not dt: return dt
    return dt + timedelta(hours=3)

def ts_to_moscow(ts):
    """Конвертирует Unix Timestamp в Московское время"""
    return datetime.utcfromtimestamp(ts) + timedelta(hours=3)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def check_admin(user_id):
    return user_id == ADMIN_ID

def escape_md(text: str) -> str:
    if not text: return ""
    return str(text).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")

# Карта транслитерации кириллицы (для имён тоннелей AmneziaWG)
_TRANSLIT_MAP = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
    'і': 'i', 'ї': 'yi', 'є': 'e', 'ґ': 'g',
}

def sanitize_name(raw: str, fallback: str = "user") -> str:
    """Делает имя ключа безопасным для имени тоннеля AmneziaWG/WireGuard.

    Приложение AmneziaWG принимает только имена вида [A-Za-z0-9_=+.-] длиной
    до 15 символов и ругается «неверно задано поле имя» на кириллицу/эмодзи.
    Здесь: кириллица -> латиница (транслит), эмодзи/символы -> отбрасываются,
    пробелы -> '_'. Регистр латиницы сохраняется ('Иван' -> 'Ivan').
    """
    if not raw:
        return fallback
    out = []
    for ch in raw.strip():
        low = ch.lower()
        if low in _TRANSLIT_MAP:
            t = _TRANSLIT_MAP[low]
            out.append(t.upper() if ch.isupper() else t)
        elif ch.isascii() and ch.isalnum():
            out.append(ch)
        elif ch in ' _-.':
            out.append('_')
        # всё прочее (эмодзи, иероглифы, спецсимволы) — отбрасываем
    name = re.sub(r'_+', '_', ''.join(out)).strip('_-.')
    name = name[:15].strip('_-.')
    return name or fallback

async def extract_tg_id(message, context):
    if not message: return None

    if message.contact and message.contact.user_id:
        return message.contact.user_id

    if message.forward_date:
        if message.forward_from:
            return message.forward_from.id
        return "HIDDEN"

    if message.text:
        text = message.text.strip()
        if text.lstrip('-').isdigit():
            return int(text)
        if text.startswith("@"):
            try:
                chat = await context.bot.get_chat(text)
                return chat.id
            except BadRequest:
                return "INVALID"

    return None

def get_current_version():
    try:
        if os.path.exists(VERSION_FILE):
            with open(VERSION_FILE, "r") as f: return f.read().strip()
    except Exception: pass
    return "Unknown"

def get_update_info():
    local_hash = "unknown"
    local_version = get_current_version()
    remote_hash = "unknown"
    remote_version = "unknown"

    try:
        if os.path.exists("/volumes/VERSION"):
            with open("/volumes/VERSION", "r") as f:
                local_hash = f.read().strip()[:7]
    except Exception: pass

    try:
        if not GIT_REPO: return local_hash, local_version, remote_hash, remote_version
        auth_repo_url = GIT_REPO
        if GIT_TOKEN and "https://" in GIT_REPO and "@" not in GIT_REPO:
            prefix = "https://"
            suffix = GIT_REPO[len(prefix):]
            auth_repo_url = f"{prefix}{GIT_USERNAME}:{GIT_TOKEN}@{suffix}" if GIT_USERNAME else f"{prefix}{GIT_TOKEN}@{suffix}"
        
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        
        cmd_hash = f"git ls-remote '{auth_repo_url}' refs/heads/main refs/heads/master"
        output = subprocess.check_output(cmd_hash, shell=True, stderr=subprocess.STDOUT, env=env, timeout=15).decode().strip()
        if output:
            remote_hash = output.split()[0][:7]

        cmd_ver = (
            "rm -rf /tmp/repo_check && mkdir -p /tmp/repo_check && cd /tmp/repo_check && "
            "git init && "
            f"git remote add origin '{auth_repo_url}' && "
            "git config core.sparseCheckout true && "
            "echo 'VPS_RU/VERSION' >> .git/info/sparse-checkout && "
            "echo 'VERSION' >> .git/info/sparse-checkout && "
            "(git pull --depth=1 origin main >/dev/null 2>&1 || git pull --depth=1 origin master >/dev/null 2>&1) && "
            "(cat VPS_RU/VERSION 2>/dev/null || cat VERSION 2>/dev/null)"
        )
        output_ver = subprocess.check_output(cmd_ver, shell=True, stderr=subprocess.STDOUT, env=env, timeout=30).decode().strip()
        if output_ver:
            remote_version = output_ver.split('\n')[-1].strip()

    except Exception as e:
        print(f"Error checking update: {e}")

    return local_hash, local_version, remote_hash, remote_version

async def safe_delete(context, chat_id, message_id):
    try: await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception: pass

async def stop_bg_tasks():
    state_data["dashboard_running"] = False
    if state_data["dashboard_task"]:
        state_data["dashboard_task"].cancel()
        state_data["dashboard_task"] = None
    state_data["graph_running"] = False
    if state_data["graph_task"]:
        state_data["graph_task"].cancel()
        state_data["graph_task"] = None

def deregister_menu(chat_id):
    if chat_id in state_data["active_menus"]:
        del state_data["active_menus"][chat_id]

async def broadcast_message(app, text, db_ref):
    try:
        tg_ids = await db_ref.get_all_tg_ids()
        if ADMIN_ID not in tg_ids: tg_ids.append(ADMIN_ID)
        tg_ids = list(set(tg_ids))
        for uid in tg_ids:
            try:
                if uid == ADMIN_ID:
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛡 Панель управления", callback_data="back_to_main")]])
                else:
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Личный кабинет", callback_data="client_menu")]])
                
                await app.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown", reply_markup=kb)
            except Exception: pass
    except Exception as e: print(f"Broadcast error: {e}")