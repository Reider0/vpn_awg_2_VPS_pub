import os
import time
import aiohttp
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from utils import escape_md, stop_bg_tasks, deregister_menu, ADMIN_ID, CONFIGS_DIR, WG_API_URL, dt_to_moscow
from database import db
from wireguard_manager import create_peer, delete_peer, pause_peer, resume_peer
from handlers_client import send_client_menu

async def users_list_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0):
    await stop_bg_tasks()
    deregister_menu(update.effective_chat.id)
    
    users = await db.get_all_users()
    
    live_peers = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{WG_API_URL}/peers", timeout=3) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    now = int(time.time())
                    for p in data:
                        hs = p.get('latest_handshake', 0)
                        if hs > 0 and (now - hs) < 180:
                            live_peers[p.get('uuid')] = True
    except: pass

    items_per_page = 5
    total_users = len(users)
    total_pages = max(1, (total_users + items_per_page - 1) // items_per_page)
    
    if page >= total_pages: page = total_pages - 1
    if page < 0: page = 0

    start = page * items_per_page
    end = start + items_per_page
    current_users = users[start:end]

    keyboard =[]
    for u in current_users:
        if not u.get('is_active', True):
            status_icon = "🔴"
        elif live_peers.get(u['uuid']):
            status_icon = "🟢"
        else:
            status_icon = "🟡"
            
        keyboard.append([InlineKeyboardButton(f"{status_icon} {u['name']}", callback_data=f"user_detail_{u['uuid']}")])

    if total_pages > 1:
        prev_page = (page - 1) % total_pages
        next_page = (page + 1) % total_pages
        nav_row =[
            InlineKeyboardButton("⬅️ Назад", callback_data=f"users_page_{prev_page}"),
            InlineKeyboardButton("Вперед ➡️", callback_data=f"users_page_{next_page}")
        ]
        keyboard.append(nav_row)
    
    keyboard.append([InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")])
    
    text = (
        "👥 **Управление пользователями**\n"
        "🟢 Онлайн | 🟡 Офлайн | 🔴 Отключен\n\n"
        "Выберите пользователя:\n\n"
        f"📄 Страница: `[{page + 1}/{total_pages}]`"
    )
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
        await context.bot.send_message(chat_id=ADMIN_ID, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def render_user_detail(context, chat_id, message_id, uuid):
    user = await db.get_user_by_uuid(uuid)
    if not user: return

    is_online = False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{WG_API_URL}/peers", timeout=3) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    now = int(time.time())
                    for p in data:
                        if p.get('uuid') == uuid:
                            hs = p.get('latest_handshake', 0)
                            if hs > 0 and (now - hs) < 180: is_online = True
    except: pass

    if not user.get('is_active', True): status_str = "🔴 Отключен"
    elif is_online: status_str = "🟢 Онлайн"
    else: status_str = "🟡 Офлайн"

    safe_name = escape_md(user['name'])
    tg_ids = user.get('tg_ids',[])
    tg_status = ", ".join([f"`{tid}`" for tid in tg_ids]) if tg_ids else "Не привязан"
    
    exp_str = dt_to_moscow(user['expires_at']).strftime('%d.%m.%Y %H:%M') if user.get('expires_at') else "Навсегда"
    created_str = dt_to_moscow(user['created_at']).strftime('%d.%m.%Y')
    
    user_ips = await db.get_user_ips(uuid)
    trusted_list = [r['ip'] for r in user_ips if r['status'] == 'trusted']
    pending_list = [r['ip'] for r in user_ips if r['status'] == 'pending']
    
    ips_text = ""
    if trusted_list or pending_list:
        ips_text += "\n🌐 **Сети IP (Anti-Sharing):**\n"
        for ip in trusted_list[:4]: ips_text += f"  ✅ `{ip}` (Доверенная)\n"
        for ip in pending_list[:4]: ips_text += f"  ⏳ `{ip}` (Проверка)\n"
        if len(trusted_list) + len(pending_list) > 8: ips_text += "  ...\n"
    else:
        ips_text += "\n🌐 **Сети:** Пока нет данных\n"

    text = (
        f"👤 **{safe_name}**\n"
        f"🆔 `{user['uuid']}`\n"
        f"📊 Статус: {status_str}\n"
        f"⏳ Годен до: {exp_str} (МСК)\n"
        f"📱 TG ID: {tg_status}\n"
        f"📅 Создан: {created_str}\n"
        f"{ips_text}"
    )

    keyboard =[]
    if user.get('is_active', True):
        keyboard.append([InlineKeyboardButton("⏸ Заморозить ключ", callback_data=f"act_pause_{uuid}")])
    else:
        keyboard.append([InlineKeyboardButton("▶️ Разморозить ключ", callback_data=f"act_resume_{uuid}")])

    keyboard.append([InlineKeyboardButton("🔗 Привязать TG ID", callback_data=f"link_tg_{uuid}")])
    if tg_ids:
        keyboard.append([InlineKeyboardButton("✂️ Отвязать TG ID", callback_data=f"unlink_tg_{uuid}")])
    
    if user_ips:
        keyboard.append([InlineKeyboardButton("🧹 Сбросить историю сетей", callback_data=f"clear_ips_{uuid}")])
    
    keyboard.extend([[InlineKeyboardButton("📨 Отправить конфиг", callback_data=f"act_resend_{uuid}")],[InlineKeyboardButton("❌ Удалить пользователя", callback_data=f"confirm_delete_{uuid}")],[InlineKeyboardButton("🔙 Назад к списку", callback_data="users_page_0")]
    ])
    
    if message_id:
        try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        except Exception: pass

async def user_detail_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, uuid):
    await stop_bg_tasks()
    chat_id = update.effective_chat.id
    deregister_menu(chat_id)
    await render_user_detail(context, chat_id, update.callback_query.message.message_id, uuid)

async def clear_user_ips(update: Update, context: ContextTypes.DEFAULT_TYPE, uuid):
    await db.execute("DELETE FROM user_ips WHERE uuid=$1", uuid)
    await update.callback_query.answer("История IP-адресов очищена!")
    await user_detail_menu(update, context, uuid)

async def confirm_delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, uuid):
    deregister_menu(update.effective_chat.id)
    user = await db.get_user_by_uuid(uuid)
    if not user: return
    text = f"⚠️ **Вы уверены, что хотите удалить {escape_md(user['name'])}?**\n\nКлюч перестанет работать, файлы будут удалены навсегда."
    keyboard = [[InlineKeyboardButton("✅ ДА, Удалить", callback_data=f"do_delete_{uuid}")],[InlineKeyboardButton("🔙 Нет, Отмена", callback_data=f"user_detail_{uuid}")]]
    await update.callback_query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def action_delete_user(update: Update, context: ContextTypes.DEFAULT_TYPE, uuid):
    user = await db.get_user_by_uuid(uuid)
    if not user: 
        await update.callback_query.answer("Пользователь уже удален")
        await users_list_menu(update, context, 0)
        return

    await update.callback_query.answer("Удаление...")
    try:
        await delete_peer(uuid, user['name'])
        await db.execute("DELETE FROM users WHERE uuid=$1", uuid)
        await db.log_event("Delete", f"Deleted key {user['name']}")
        await update.callback_query.answer("Успешно удален!", show_alert=True)
        await users_list_menu(update, context, 0)
    except Exception as e:
        await update.callback_query.message.reply_text(f"❌ Ошибка удаления: {e}")

async def action_resend_config(update: Update, context: ContextTypes.DEFAULT_TYPE, uuid):
    user = await db.get_user_by_uuid(uuid)
    if not user: return
    
    await update.callback_query.answer("Поиск файлов...")
    name = user['name']
    chat_id = update.effective_chat.id
    
    cf = CONFIGS_DIR / f"{name}.conf"
    qf = CONFIGS_DIR / f"{name}.png"
    cf_old = CONFIGS_DIR / f"{name}_Full.conf"
    qf_old = CONFIGS_DIR / f"{name}_Full.png"

    try:
        if cf.exists():
            await context.bot.send_document(chat_id=chat_id, document=open(cf, "rb"), caption=f"📄 Ваш конфиг: {name}")
            if qf.exists(): await context.bot.send_photo(chat_id=chat_id, photo=open(qf, "rb"))
        elif cf_old.exists():
            await context.bot.send_document(chat_id=chat_id, document=open(cf_old, "rb"), caption=f"📄 Ваш конфиг: {name} (Legacy)")
            if qf_old.exists(): await context.bot.send_photo(chat_id=chat_id, photo=open(qf_old, "rb"))
        else:
            await update.callback_query.answer("❌ Файлы конфигурации не найдены на диске!", show_alert=True)
    except Exception as e:
        await update.callback_query.answer(f"Ошибка отправки: {e}", show_alert=True)

async def generate_key_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_bg_tasks()
    deregister_menu(update.effective_chat.id)
    keyboard = [[InlineKeyboardButton("🔙 Отмена", callback_data="back_to_main")]]
    await update.callback_query.edit_message_text("✏️ Введите имя пользователя:", reply_markup=InlineKeyboardMarkup(keyboard))
    context.user_data["state"] = "awaiting_name"
    context.user_data["menu_msg_id"] = update.callback_query.message.message_id

async def finish_key_creation(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_id=None):
    name = context.user_data.get("name")
    exp_days = context.user_data.get("expiry_days", 0)
    dns_type = context.user_data.get("dns_type", "classic")
    chat_id = update.effective_chat.id
    menu_id = context.user_data.get("menu_msg_id")

    if menu_id:
        try: await context.bot.edit_message_text(chat_id=chat_id, message_id=menu_id, text="⏳ Генерирую конфигурацию...")
        except: pass

    try:
        # Новый ключ получает актуальный набор split-tunnel исключений и текущую
        # (динамическую) версию маршрутизации — чтобы он сразу считался «свежим».
        bypass_cidrs = await db.get_all_bypass_cidrs()
        rv = await db.get_routing_version()
        new_uid, c_path, q_path = await create_peer(name, dns_type=dns_type, bypass_cidrs=bypass_cidrs)
        expires_at = datetime.utcnow() + timedelta(days=exp_days) if exp_days > 0 else None

        await db.execute("INSERT INTO users (name, uuid, created_at, expires_at, routing_version) VALUES ($1, $2, NOW(), $3, $4) ON CONFLICT (uuid) DO NOTHING", name, new_uid, expires_at, rv)
        await db.log_event("Create Key", f"Created key {name}. Expiry: {exp_days} days. DNS: {dns_type}")
        
        if tg_id: await db.link_user_telegram(new_uid, tg_id)
        
        await context.bot.send_message(chat_id=chat_id, text=f"✅ **Ключ сгенерирован!**\n\nВы можете добавить его в приложение AmneziaWG.", parse_mode=ParseMode.MARKDOWN)
        await context.bot.send_document(chat_id=chat_id, document=open(c_path, "rb"), caption=f"📄 {name}")
        await context.bot.send_photo(chat_id=chat_id, photo=open(q_path, "rb"))
        
        if tg_id:
            try:
                await context.bot.send_message(chat_id=tg_id, text="🎉 **Привет!** Администратор создал для вас VPN-ключ и привязал его к этому Telegram-аккаунту.\n\nВот ваш файл конфигурации:", parse_mode=ParseMode.MARKDOWN)
                await context.bot.send_document(chat_id=tg_id, document=open(c_path, "rb"), caption=f"📄 Ваш VPN конфиг: {name}")
                await context.bot.send_photo(chat_id=tg_id, photo=open(q_path, "rb"))
                await send_client_menu(context, tg_id)
                await context.bot.send_message(chat_id=chat_id, text=f"✅ Конфиг и меню успешно отправлены клиенту `{tg_id}`.")
            except Exception as e:
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Клиент `{tg_id}` не получил конфиг (возможно, он не запустил бота командой /start):\n`{e}`", parse_mode=ParseMode.MARKDOWN)
        
        keyboard = [[InlineKeyboardButton("🔙 В главное меню", callback_data="back_to_main")]]
        await context.bot.send_message(chat_id=chat_id, text="Готово! Что делаем дальше?", reply_markup=InlineKeyboardMarkup(keyboard))
        
        context.user_data["state"] = None
    except Exception as e:
        keyboard = [[InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")]]
        if menu_id: await context.bot.edit_message_text(chat_id=chat_id, message_id=menu_id, text=f"❌ Ошибка: {e}", reply_markup=InlineKeyboardMarkup(keyboard))
        context.user_data["state"] = None