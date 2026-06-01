import os
import re
import time
import socket
import ipaddress
import psutil
import asyncio
import aiohttp
from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram import Update
from telegram.ext import ContextTypes
from database import db
from utils import (
    get_moscow_now, dt_to_moscow, broadcast_message, DE_AGENT_URL, WG_API_URL,
    ADMIN_ID, escape_md, GOSUSLUGI_APP_WARNING, analyze_resource, CONFIGS_DIR, ROUTING_VERSION
)

# --- SPLIT-TUNNEL: дата-центро-враждебные РФ-сервисы (мимо VPN, через домашний канал) ---
# Источник правды — БД (таблица bypass_exclusions, см. database.py). Здесь только
# логика проверки дрейфа и формирования уведомлений.

UPGRADE_INSTRUCTION = (
    "🔄 *Как обновить (новый ключ выдаётся автоматически):*\n"
    "1️⃣ Нажми «Перевыпустить» ниже — бот пришлёт новый `.conf` и QR.\n"
    "2️⃣ В приложении *AmneziaWG* удали старое подключение.\n"
    "3️⃣ Добавь новое одним из способов:\n"
    "   • *QR:* «＋» → «Сканировать QR-код» → наведи на новый QR;\n"
    "   • *Файл:* «＋» → «Импорт из файла» → выбери новый `.conf`.\n"
    "4️⃣ Включи VPN. Готово — сервисы из списка ниже заработают."
)

def _check_bypass_drift(entries):
    """Синхронно: резолвит домены исключений и проверяет, что их IP всё ещё внутри
    заявленных подсетей. entries: список (domain, [cidr, ...]). Возвращает список
    'ушедших' адресов (drift)."""
    drifted = []
    for domain, cidrs in entries:
        nets = []
        for c in cidrs:
            try:
                nets.append(ipaddress.ip_network(c))
            except ValueError:
                continue
        if not nets:
            continue
        try:
            infos = socket.getaddrinfo(domain, 443, socket.AF_INET)
            ips = sorted({i[4][0] for i in infos})
        except Exception:
            continue
        for ip in ips:
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if not any(addr in n for n in nets):
                drifted.append(f"{domain} → {ip}")
    return drifted

async def _bypass_drift_entries():
    """Готовит (domain, [cidr,...]) из БД для проверки дрейфа."""
    rows = await db.get_bypass_exclusions()
    return [(r['domain'], [c.strip() for c in (r['cidrs'] or '').split(',') if c.strip()]) for r in rows]


def _config_is_split_tunnel(name):
    """Определяет по выданному .conf, применён ли split-tunnel (обход), читая AllowedIPs.
    Полный туннель содержит '0.0.0.0/0', split-tunnel — раздробленные подсети без него.
    Возвращает True/False, либо None если файл не найден (судить не можем)."""
    for fn in (f"{name}.conf", f"{name}_Full.conf", f"{name}_Smart.conf"):
        p = CONFIGS_DIR / fn
        if p.exists():
            try:
                txt = p.read_text()
            except Exception:
                continue
            m = re.search(r"AllowedIPs\s*=\s*(.+)", txt)
            if m:
                return "0.0.0.0/0" not in m.group(1)
    return None


async def reconcile_routing_versions():
    """Одноразовая сверка при старте: чинит ключи, которые УЖЕ были перевыпущены до
    фикса бага (routing_version записался как 0), хотя их конфиг по факту содержит
    split-tunnel. Такие ключи иначе бесконечно получали бы напоминания о перевыпуске.
    Настоящие старые ключи (полный туннель) остаются на 0 и продолжают получать
    напоминания — это корректно."""
    try:
        rows = await db.fetch_all("SELECT uuid, name, COALESCE(routing_version,0) AS rv FROM users")
    except Exception as e:
        print(f"reconcile_routing_versions: {e}")
        return 0

    fixed = 0
    for r in rows:
        if r['rv'] >= ROUTING_VERSION:
            continue
        is_split = await asyncio.to_thread(_config_is_split_tunnel, r['name'])
        if is_split is True:
            await db.execute("UPDATE users SET routing_version=$1 WHERE uuid=$2", ROUTING_VERSION, r['uuid'])
            fixed += 1
    if fixed:
        await db.log_event("Routing", f"Reconciled routing_version for {fixed} already-reissued key(s).")
        print(f"✅ Реконсиляция: проставлена актуальная routing_version у {fixed} уже-перевыпущенных ключей.")
    return fixed

notified_cache = set()
last_ip_cache = {}

ghost_cache = {}
paused_cache = {}
flapping_cache = {}        
resource_alert_cache = {}  

# ------------------------ DASHBOARD ------------------------
async def get_dashboard():
    cpu_ru = psutil.cpu_percent()
    ram_ru = psutil.virtual_memory().percent
    disk_ru = psutil.disk_usage("/").percent
    peers_text = "0"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{WG_API_URL}/status", timeout=3) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    total = data.get("peers_count", 0)
                    active = data.get("active_peers", 0)
                    peers_text = f"{active} [ {total} ]"
                else:
                    peers_text = "⚠️ Ошибка API"
    except Exception:
        peers_text = "⚠️ Сервер недоступен"

    de_status_text = "⚠️ Офлайн / Недоступен"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{DE_AGENT_URL}/system_stats", timeout=3) as resp:
                if resp.status == 200:
                    de_data = await resp.json()
                    de_status_text = f"CPU: {de_data.get('cpu', 0)}% | RAM: {de_data.get('ram', 0)}% | Disk: {de_data.get('disk', 0)}%"
    except Exception:
        pass

    try:
        bypass_count = len(await db.get_all_bypass_cidrs())
        outdated_count = len(await db.get_outdated_keys(await db.get_routing_version()))
    except Exception:
        bypass_count = 0
        outdated_count = 0

    return (
        f"📊 **Дашборд Системы**\n\n"
        f"🇷🇺 **RU Сервер (Master)**\n"
        f"CPU:   {cpu_ru}%\n"
        f"RAM:   {ram_ru}%\n"
        f"Диск:  {disk_ru}%\n\n"
        f"🇩🇪 **DE Сервер (Agent)**\n"
        f"{de_status_text}\n\n"
        f"🔌 **Активных VPN сессий:** {peers_text}\n"
        f"🚫 **Not-allow addr (мимо VPN):** {bypass_count} диап.\n"
        f"♻️ **Ключей на старом формате:** {outdated_count}"
    )

# ------------------------ СИСТЕМНЫЕ АЛЕРТЫ ------------------------
async def resource_monitor_loop(app):
    while True:
        await asyncio.sleep(300) 
        now = time.time()
        
        def should_alert(key):
            if key not in resource_alert_cache or (now - resource_alert_cache[key]) > 3600:
                resource_alert_cache[key] = now
                return True
            return False

        alerts = []

        cpu_ru = psutil.cpu_percent(interval=1)
        ram_ru = psutil.virtual_memory().percent
        disk_ru = psutil.disk_usage("/").percent
        
        if cpu_ru > 90 and should_alert("RU_CPU"): alerts.append(f"🇷🇺 **RU CPU:** {cpu_ru}%")
        if ram_ru > 95 and should_alert("RU_RAM"): alerts.append(f"🇷🇺 **RU RAM:** {ram_ru}%")
        if disk_ru > 90 and should_alert("RU_DISK"): alerts.append(f"🇷🇺 **RU Диск:** {disk_ru}%")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{DE_AGENT_URL}/system_stats", timeout=5) as resp:
                    if resp.status == 200:
                        de_data = await resp.json()
                        cpu_de = de_data.get('cpu', 0)
                        ram_de = de_data.get('ram', 0)
                        disk_de = de_data.get('disk', 0)
                        
                        if cpu_de > 90 and should_alert("DE_CPU"): alerts.append(f"🇩🇪 **DE CPU:** {cpu_de}%")
                        if ram_de > 95 and should_alert("DE_RAM"): alerts.append(f"🇩🇪 **DE RAM:** {ram_de}%")
                        if disk_de > 90 and should_alert("DE_DISK"): alerts.append(f"🇩🇪 **DE Диск:** {disk_de}%")
        except Exception:
            if should_alert("DE_DOWN"): alerts.append("🇩🇪 **DE Агент недоступен!** (Упал туннель или сервис)")

        if alerts and ADMIN_ID:
            msg = "⚠️ **Критическая нагрузка на систему!**\n\n" + "\n".join(alerts)
            try: await app.bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode="Markdown")
            except: pass

# ------------------------ MONITOR & ANTI-SHARING ------------------------
async def alert_loop(app):
    wg_is_down = False
    
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{WG_API_URL}/peers", timeout=5) as resp:
                    resp.raise_for_status()
                    peers_data = await resp.json()

            if wg_is_down:
                wg_is_down = False
                if ADMIN_ID:
                    kb_admin = InlineKeyboardMarkup([[InlineKeyboardButton("🛡 В админку", callback_data="back_to_main")]])
                    await app.bot.send_message(chat_id=ADMIN_ID, text="✅ VPN-сервер снова в сети.", reply_markup=kb_admin)
                    await db.log_event("System", "VPN Server is back online.")

            now = int(time.time())
            active_uuids = set()
            
            users_list = await db.get_all_users()
            users_dict = {u['uuid']: u for u in users_list}

            for peer in peers_data:
                uuid_val = peer.get("uuid")
                pubkey = peer.get("public_key")
                handshake = peer.get("latest_handshake", 0)
                endpoint = peer.get("endpoint", "")
                
                if not pubkey or pubkey == "(none)": continue
                
                is_ghost = False
                is_paused_violation = False
                
                if uuid_val not in users_dict: is_ghost = True
                elif not users_dict[uuid_val].get('is_active', True): is_paused_violation = True
                    
                if is_ghost or is_paused_violation:
                    try:
                        async with aiohttp.ClientSession() as kill_session:
                            await kill_session.post(f"{WG_API_URL}/kill_ghost", json={"public_key": pubkey, "purge_config": is_ghost}, timeout=5)
                    except Exception: pass
                    
                    if endpoint and endpoint != "(none)":
                        if is_ghost:
                            if pubkey not in ghost_cache or (now - ghost_cache[pubkey] > 3600):
                                ghost_cache[pubkey] = now
                                msg = f"🚨 **Несанкционированный доступ!**\n\nНеизвестный ключ (Призрак) попытался подключиться.\n📱 IP: `{endpoint}`\n🔑 PubKey: `{pubkey}`\n\n🛡 Сессия принудительно разорвана."
                                if ADMIN_ID: await app.bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode="Markdown")
                                await db.log_event("Security", f"Killed ghost connection from {endpoint}")
                        elif is_paused_violation:
                            if uuid_val not in paused_cache or (now - paused_cache[uuid_val] > 3600):
                                paused_cache[uuid_val] = now
                                u_name = escape_md(users_dict[uuid_val]['name'])
                                msg = f"🛡 **Блокировка доступа!**\n\nОтключенный пользователь **{u_name}** попытался подключиться.\n📱 IP: `{endpoint}`\n\n⛔️ Доступ отклонен."
                                if ADMIN_ID: await app.bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode="Markdown")
                                await db.log_event("Security", f"Blocked access for paused user {users_dict[uuid_val]['name']}")
                    continue

                hostname = endpoint.split(":")[0] if endpoint and endpoint != "(none)" else ""

                if handshake > 0 and (now - handshake) < 180 and hostname:
                    active_uuids.add(uuid_val)
                    user = users_dict.get(uuid_val)

                    if user:
                        prev_ip, prev_time = last_ip_cache.get(uuid_val, ("", 0))
                        
                        if hostname != prev_ip and prev_ip != "":
                            jumps = flapping_cache.get(uuid_val, [])
                            jumps.append(now)
                            jumps = [t for t in jumps if (now - t) < 300]
                            flapping_cache[uuid_val] = jumps
                            
                            if len(jumps) >= 3:
                                try:
                                    async with aiohttp.ClientSession() as session:
                                        await session.post(f"{WG_API_URL}/peers/{uuid_val}/pause")
                                except Exception: pass
                                
                                await db.execute("UPDATE users SET is_active=FALSE WHERE uuid=$1", uuid_val)
                                await db.log_event("Security", f"KEY COMPROMISED (Flapping): {user['name']}")
                                
                                if ADMIN_ID:
                                    safe_name = escape_md(user['name'])
                                    alert_msg = f"🚨 **КЛЮЧ СКОМПРОМЕТИРОВАН!**\n\n👤 Пользователь: **{safe_name}**\n🔄 Более 3 смен сети за 5 минут.\n⛔️ **Ключ заморожен.**"
                                    await app.bot.send_message(chat_id=ADMIN_ID, text=alert_msg, parse_mode="Markdown")

                                tg_ids = user.get('tg_ids', [])
                                kb_client = InlineKeyboardMarkup([[InlineKeyboardButton("🆘 Связаться с Админом", callback_data="support_start")]])
                                for tid in tg_ids:
                                    try: await app.bot.send_message(chat_id=tid, text="⚠️ **Ваш VPN-ключ заблокирован.**\n\nЗафиксировано использование на нескольких устройствах. Обратитесь к администратору.", parse_mode="Markdown", reply_markup=kb_client)
                                    except: pass
                                
                                flapping_cache[uuid_val] = []
                                last_ip_cache[uuid_val] = (hostname, now)
                                continue

                        if hostname != prev_ip or (now - prev_time) > 300:
                            is_new_ip = await db.track_user_ip(uuid_val, hostname)
                            if hostname != prev_ip and prev_ip != "" and (now - prev_time) < 300:
                                if is_new_ip and ADMIN_ID:
                                    safe_name = escape_md(user['name'])
                                    msg = f"⚠️ **Смена сети!**\n\n👤 {safe_name}\n🔄 Прыжок (менее 5 мин):\nС `{prev_ip}` на `{hostname}`"
                                    await app.bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode="Markdown")
                                    await db.log_event("Security", f"IP Jump: {prev_ip} -> {hostname} ({user['name']})")

                        last_ip_cache[uuid_val] = (hostname, now)

                    if uuid_val not in notified_cache:
                        device_set = await db.device_set(uuid_val)
                        if user:
                            safe_name = escape_md(user['name'])
                            if not device_set:
                                await db.execute("UPDATE users SET device=$1, first_connected_at=NOW() WHERE uuid=$2", uuid_val)
                                await db.log_event("Connection", f"First connection by {user['name']} from {hostname}")
                                if ADMIN_ID: await app.bot.send_message(chat_id=ADMIN_ID, text=f"🎉 **Новое подключение!**\n\n👤 {safe_name}\n📱 `{hostname}`\n🆔 `{uuid_val}`", parse_mode="Markdown")

                                tg_ids = user.get('tg_ids',[])
                                if tg_ids:
                                    msg_tg = f"🟢 **VPN Подключен!**\n\nКлюч: **{safe_name}**."
                                    kb_client = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Личный кабинет", callback_data="client_menu")]])
                                    for tid in tg_ids:
                                        try: await app.bot.send_message(chat_id=tid, text=msg_tg, parse_mode="Markdown", reply_markup=kb_client)
                                        except Exception: pass
                        notified_cache.add(uuid_val) 

            disconnected_uuids = notified_cache - active_uuids
            for uid in disconnected_uuids: notified_cache.remove(uid)

        except Exception as e:
            if not wg_is_down and isinstance(e, (aiohttp.ClientError, asyncio.TimeoutError)):
                wg_is_down = True
                await db.log_event("Error", "VPN API is unreachable")
                if ADMIN_ID: await app.bot.send_message(chat_id=ADMIN_ID, text="⚠️ VPN-сервер недоступен!")

        await asyncio.sleep(10)

# ------------------------ SELF-HEALING ------------------------
async def self_healing_loop(app):
    fail_count = 0
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{WG_API_URL}/health", timeout=5) as resp:
                    if resp.status == 200: fail_count = 0
                    else: fail_count += 1
        except Exception: fail_count += 1

        if fail_count >= 3:
            fail_count = 0
            await db.log_event("Self-Healing", "Interface hang detected. Triggering hard restart of wg0 container.")
            if ADMIN_ID:
                try: await app.bot.send_message(chat_id=ADMIN_ID, text="⚙️ **Self-Healing:** Зависание VPN. Жесткий перезапуск.")
                except Exception: pass
            
            os.makedirs("/volumes/flags", exist_ok=True)
            with open("/volumes/flags/do_restart_wg", "w") as f: f.write("true")
            
        await asyncio.sleep(180)

# ------------------------ EXPIRATION LOGIC ------------------------
async def expiration_loop(app):
    while True:
        try:
            users = await db.get_all_users()
            now = datetime.utcnow()
            for u in users:
                if u['is_active'] and u['expires_at'] and u['expires_at'] < now:
                    uuid_val, safe_name = u['uuid'], escape_md(u['name'])
                    try:
                        async with aiohttp.ClientSession() as session:
                            await session.post(f"{WG_API_URL}/peers/{uuid_val}/pause")
                    except Exception: pass
                    
                    await db.execute("UPDATE users SET is_active=FALSE WHERE uuid=$1", uuid_val)
                    await db.log_event("Expiration", f"Key {u['name']} expired and was paused.")
                    
                    if ADMIN_ID: await app.bot.send_message(chat_id=ADMIN_ID, text=f"⏳ **Ключ просрочен!**\n\nПользователь: **{safe_name}**", parse_mode="Markdown")
                    tg_ids = u.get('tg_ids',[])
                    kb_client = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Личный кабинет", callback_data="client_menu")]])
                    for tid in tg_ids:
                        try: await app.bot.send_message(chat_id=tid, text=f"⏳ Ваш VPN-ключ **{safe_name}** просрочен и был отключен.", parse_mode="Markdown", reply_markup=kb_client)
                        except Exception: pass
        except Exception as e: print(f"Expiration loop error: {e}")
        await asyncio.sleep(3600)

# ------------------------ INACTIVITY LOGIC ------------------------
async def inactivity_loop(app):
    while True:
        try:
            users = await db.get_all_users()
            now = datetime.utcnow()
            for u in users:
                if u.get('is_active', False):
                    last_active = u.get('last_active_at') or u.get('created_at')
                    if last_active and (now - last_active).days >= 30:
                        uuid_val, safe_name = u['uuid'], escape_md(u['name'])
                        try:
                            async with aiohttp.ClientSession() as session:
                                await session.post(f"{WG_API_URL}/peers/{uuid_val}/pause")
                        except Exception: pass
                        
                        await db.execute("UPDATE users SET is_active=FALSE WHERE uuid=$1", uuid_val)
                        await db.log_event("Inactivity", f"Key {u['name']} was paused due to 30 days of inactivity.")
                        if ADMIN_ID: await app.bot.send_message(chat_id=ADMIN_ID, text=f"💤 **Отключен за бездействие!**\n\nКлюч: **{safe_name}**", parse_mode="Markdown")
        except Exception as e: print(f"Inactivity loop error: {e}")
        await asyncio.sleep(86400) 

# ------------------------ WEEKLY REPORTS ------------------------
async def weekly_report_loop(app):
    while True:
        now_msk = get_moscow_now()
        if now_msk.weekday() == 6 and now_msk.hour == 20:
            try:
                users = await db.get_all_users()
                stats_24 = await db.get_stats_24h()
                live_data = {}
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(f"{WG_API_URL}/peers", timeout=5) as resp:
                            if resp.status == 200:
                                peers = await resp.json()
                                for p in peers: live_data[p.get('uuid')] = p.get('rx', 0) + p.get('tx', 0)
                except Exception: pass

                for u in users:
                    tg_ids = u.get('tg_ids',[])
                    if not tg_ids: continue
                    uuid_val = u['uuid']
                    user_stats =[s for s in stats_24 if s['user_uuid'] == uuid_val]
                    total_bytes = 0
                    prev_val = 0
                    
                    for s in user_stats:
                        val = s['bytes_in'] + s['bytes_out']
                        delta = val - prev_val
                        if delta < 0: delta = val
                        if prev_val == 0: delta = 0
                        total_bytes += delta
                        prev_val = val
                        
                    if uuid_val in live_data:
                        live_val = live_data[uuid_val]
                        if user_stats:
                            delta = live_val - prev_val
                            if delta < 0: delta = live_val
                            total_bytes += delta
                        else: total_bytes += live_val
                            
                    mb_used = round(total_bytes / (1024 * 1024), 2)
                    safe_name = escape_md(u['name'])
                    msg = f"📊 **Еженедельный отчет VPN**\n\nКлюч: **{safe_name}**\nИспользовано трафика: `{mb_used} MB`\nВаш VPN работает стабильно! 🚀"
                    kb_client = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Личный кабинет", callback_data="client_menu")]])
                    for tid in tg_ids:
                        try: await app.bot.send_message(chat_id=tid, text=msg, parse_mode="Markdown", reply_markup=kb_client)
                        except Exception: pass
                        
                await db.log_event("System", "Weekly reports dispatched.")
            except Exception as e: print(f"Weekly report error: {e}")
            await asyncio.sleep(86400)
        else: await asyncio.sleep(3600)

async def cleanup_peers():
    while True:
        await asyncio.sleep(3600)
        notified_cache.clear()

async def stats_collector_loop():
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{WG_API_URL}/peers", timeout=10) as resp:
                    if resp.status == 200:
                        peers_data = await resp.json()
                        now = int(time.time())
                        for peer in peers_data:
                            uuid_val, rx, tx, hs = peer.get("uuid"), peer.get("rx", 0), peer.get("tx", 0), peer.get("latest_handshake", 0)
                            if uuid_val and len(uuid_val) < 40:
                                await db.save_stats(uuid_val, rx, tx)
                                if hs > 0 and (now - hs) < 180: await db.execute("UPDATE users SET last_active_at=NOW() WHERE uuid=$1", uuid_val)
        except Exception: pass
        await asyncio.sleep(300)

async def log_cleanup_loop(app):
    while True:
        try:
            await db.cleanup_old_logs(days=7)
            os.makedirs("/volumes/flags", exist_ok=True)
            with open("/volumes/flags/do_cleanup", "w") as f: f.write("true")
        except Exception as e: print(f"🧹 Cleanup error: {e}")
        await asyncio.sleep(86400)

# ------------------------ AUTO-REBOOT ------------------------
async def auto_reboot_loop(app):
    while True:
        now_msk = get_moscow_now()
        if now_msk.weekday() == 6 and now_msk.hour == 4:
            try:
                last_reboot = await db.get_setting("last_auto_reboot")
                today_str = now_msk.strftime("%Y-%m-%d")
                if last_reboot != today_str:
                    await db.set_setting("last_auto_reboot", today_str)
                    text = "🔄 **Плановое обслуживание!**\n\nСервер автоматически уходит на перезагрузку."
                    await broadcast_message(app, text, db)
                    os.makedirs("/volumes/flags", exist_ok=True)
                    with open("/volumes/flags/was_rebooting", "w") as f: f.write("true")
                    with open("/volumes/flags/do_reboot", "w") as f: f.write("reboot_requested")
            except Exception as e: print(f"Auto-reboot error: {e}")
        await asyncio.sleep(60)

# ------------------------ SCHEDULED UPDATE ------------------------
async def scheduled_update_loop(app):
    while True:
        try:
            target_str = await db.get_setting("scheduled_update")
            if target_str:
                target_dt = datetime.strptime(target_str, "%Y-%m-%d %H:%M:%S")
                now_msk = get_moscow_now()
                if now_msk >= target_dt:
                    await db.execute("DELETE FROM settings WHERE key='scheduled_update'")
                    text = "🚀 **Обновление системы началось!**\n\nСервис уйдет в оффлайн на 1-2 минуты."
                    await broadcast_message(app, text, db)
                    os.makedirs("/volumes/flags", exist_ok=True)
                    with open("/volumes/flags/was_updating", "w") as f: f.write("true")
                    with open("/volumes/flags/do_update", "w") as f: f.write("update_requested")
        except Exception as e: pass
        await asyncio.sleep(60)

# ------------------------ ROUTING UPGRADE (split-tunnel напоминания) ------------------------
async def routing_upgrade_loop(app):
    """Ежедневно в 8:00 МСК напоминает владельцам устаревших ключей перевыпустить конфиг
    (чтобы заработали Госуслуги/MAX/банки) — по КАЖДОМУ ключу отдельно, пока не обновят.
    Заодно раз в день проверяет дрейф bypass-IP и алертит админа."""
    while True:
        now_msk = get_moscow_now()
        if now_msk.hour == 8:
            today = now_msk.strftime("%Y-%m-%d")
            try:
                if await db.get_setting("last_routing_notice") != today:
                    await db.set_setting("last_routing_notice", today)
                    await _send_upgrade_notices(app)
                    await _run_drift_alert(app)
            except Exception as e:
                print(f"Routing upgrade loop error: {e}")
            await asyncio.sleep(3600)
        else:
            await asyncio.sleep(600)

async def _send_upgrade_notices(app):
    current_version = await db.get_routing_version()
    outdated = await db.get_outdated_keys(current_version)
    rows = await db.get_bypass_exclusions()
    domains = ", ".join(f"`{escape_md(r['domain'])}`" for r in rows) if rows else "—"
    sent = 0
    for k in outdated:
        name = escape_md(k['name'])
        text = (
            f"🔔 **Обновите конфиг ключа «{name}»**\n\n"
            "Перевыпустите конфиг, чтобы получить прямой доступ (мимо VPN) к сервисам, "
            "которые блокируют дата-центры:\n"
            f"{domains}\n\n"
            "Старый ключ продолжит работать как прежде, но без обхода этих сервисов.\n\n"
            + UPGRADE_INSTRUCTION + "\n\n" + GOSUSLUGI_APP_WARNING
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Перевыпустить этот ключ", callback_data=f"client_regen_{k['uuid']}")],
            [InlineKeyboardButton("🌐 Список исключений", callback_data="client_bypass_info")],
            [InlineKeyboardButton("🔕 Не напоминать", callback_data="client_notify_off")],
            [InlineKeyboardButton("🏠 Личный кабинет", callback_data="client_menu")],
        ])
        for tid in k.get('tg_ids', []):
            try:
                # Уважаем персональный opt-out: пользователь мог сам отключить напоминания
                if not await db.get_routing_notify(tid):
                    continue
                await app.bot.send_message(chat_id=tid, text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
                sent += 1
            except Exception:
                pass
    if sent:
        await db.log_event("Routing", f"Daily upgrade notice sent ({sent} msg).")
    return sent

async def _run_drift_alert(app):
    entries = await _bypass_drift_entries()
    drifted = await asyncio.to_thread(_check_bypass_drift, entries)
    if drifted and ADMIN_ID:
        msg = ("⚠️ **Дрейф адресов исключений!**\n\nIP сервисов вышли за пределы "
               "заявленных подсетей — обнови их в админке: 🇷🇺 Управление RU → "
               "🌐 Исключения (split-tunnel):\n\n" +
               "\n".join(f"• `{d}`" for d in drifted))
        try:
            await app.bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass
        await db.log_event("Routing", f"Bypass drift detected: {len(drifted)} entries.")
    return drifted

# --- Ручной запуск из админки ---
async def run_bypass_check_handler(update, context):
    query = update.callback_query
    await query.answer("Проверяю адреса исключений...")
    try:
        entries = await _bypass_drift_entries()
        drifted = await asyncio.to_thread(_check_bypass_drift, entries)
        cidrs_count = len(await db.get_all_bypass_cidrs())
        outdated = await db.get_outdated_keys(await db.get_routing_version())
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка проверки: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]]))
        return

    lines = [
        "🛡 **Проверка split-tunnel (исключения)**\n",
        f"🚫 Подсетей-исключений (мимо VPN): **{cidrs_count}**",
        f"♻️ Ключей на старом формате: **{len(outdated)}**\n",
    ]
    if drifted:
        lines.append("⚠️ **Дрейф IP — обнови подсети в админке:**")
        lines += [f"• `{d}`" for d in drifted]
    else:
        lines.append("✅ Все сервисы в пределах заявленных подсетей.")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Список исключений", callback_data="bypass_list")],
        [InlineKeyboardButton("📨 Разослать напоминания сейчас", callback_data="bypass_notify_now")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")],
    ])
    await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def bypass_notify_now_handler(update, context):
    query = update.callback_query
    await query.answer("Рассылаю напоминания...")
    sent = await _send_upgrade_notices(context.application)
    await query.edit_message_text(
        f"✅ Напоминания разосланы по устаревшим ключам (сообщений: {sent}).",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]])
    )

# ------------------------ УПРАВЛЕНИЕ ИСКЛЮЧЕНИЯМИ (АДМИН) ------------------------
async def bypass_list_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rows = await db.get_bypass_exclusions()
    rv = await db.get_routing_version()

    lines = [
        f"🌐 **Исключения split-tunnel** (версия маршрутизации: `{rv}`)\n",
        "Эти подсети идут мимо VPN (через домашний канал клиента). При любом изменении "
        "списка версия поднимается, и пользователям уходит напоминание о перевыпуске.\n",
    ]
    kb = []
    if rows:
        for r in rows:
            note = f" — {escape_md(r['note'])}" if r['note'] else ""
            lines.append(f"• `{escape_md(r['domain'])}`{note}\n  `{r['cidrs']}`")
            kb.append([InlineKeyboardButton(f"🗑 {r['domain']}", callback_data=f"bypass_del_{r['id']}")])
    else:
        lines.append("_Список пуст._")

    kb.append([InlineKeyboardButton("➕ Добавить вручную", callback_data="bypass_add_manual")])
    kb.append([InlineKeyboardButton("📨 Напомнить о перевыпуске", callback_data="bypass_notify_now")])
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")])
    await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))

async def bypass_del_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, exid: str):
    query = update.callback_query
    new_v = await db.remove_bypass_exclusion(exid)
    await db.log_event("Routing", f"Bypass exclusion removed (id={exid}); routing -> v{new_v}.")
    await query.answer(f"Удалено. Версия маршрутизации: {new_v}.")
    await bypass_list_handler(update, context)

async def bypass_add_manual_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["state"] = "awaiting_bypass_add"
    kb = [[InlineKeyboardButton("🔙 Отмена", callback_data="bypass_list")]]
    await query.edit_message_text(
        "➕ **Добавить исключение**\n\nПришлите домен или ссылку (например, `mos.ru` "
        "или `https://lk.gosuslugi.ru`). Я разрешу адрес в IP и добавлю его подсети (/24) "
        "в обход VPN.",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN
    )

async def bypass_add_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, req_id: str, approve: bool = True):
    query = update.callback_query
    req = await db.get_bypass_request(req_id)
    if not req:
        await query.answer("Заявка не найдена.", show_alert=True)
        return
    if req['status'] != 'pending':
        await query.answer(f"Заявка уже обработана ({req['status']}).", show_alert=True)
        return

    if approve:
        cidrs = [c.strip() for c in (req['cidrs'] or '').split(',') if c.strip()]
        new_v = await db.add_bypass_exclusion(req['domain'], cidrs, note="по заявке клиента", source="client")
        await db.set_bypass_request_status(req_id, "approved")
        await db.log_event("Routing", f"Bypass exclusion added by request: {req['domain']} -> v{new_v}.")
        await query.edit_message_text(
            f"✅ Добавлено в исключения: `{escape_md(req['domain'])}`\n"
            f"Версия маршрутизации: `{new_v}` — клиентам уйдёт напоминание о перевыпуске.",
            parse_mode=ParseMode.MARKDOWN
        )
        try:
            await context.application.bot.send_message(
                chat_id=req['tg_id'],
                text=(f"✅ Сайт `{escape_md(req['domain'])}` добавлен в исключения!\n"
                      "Перевыпустите ключ в «Мои ключи», чтобы доступ заработал."),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔑 Мои ключи", callback_data="client_my_keys")]])
            )
        except Exception:
            pass
    else:
        await db.set_bypass_request_status(req_id, "rejected")
        await query.edit_message_text(f"❌ Заявка на `{escape_md(req['domain'])}` отклонена.", parse_mode=ParseMode.MARKDOWN)