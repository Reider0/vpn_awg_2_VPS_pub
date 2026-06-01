import asyncio
import time
from datetime import datetime
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from utils import (
    escape_md, WG_API_URL, state_data, check_admin, CONFIGS_DIR, dt_to_moscow,
    ts_to_moscow, safe_delete, GOSUSLUGI_APP_WARNING
)
from database import db
from wireguard_manager import create_peer, delete_peer

# Сколько секунд старый ключ продолжает работать ПОСЛЕ выдачи нового конфига —
# чтобы клиент успел импортировать новый и не остался без интернета во время замены.
REGEN_GRACE_SECONDS = 45


async def _issue_new_config(context, chat_id, user, deliver: bool = True):
    """Перевыпуск ключа, ШАГ 1 (config-first): создаёт новый пир с актуальным набором
    split-tunnel исключений, прописывает текущую routing_version и СНАЧАЛА выдаёт
    клиенту новый конфиг + QR. Старый пир пока остаётся живым (его снимет _retire_old_peer
    после grace-периода). Возвращает (old_uuid, name)."""
    old_uuid = user['uuid']
    name = user['name']
    tg_ids = user.get('tg_ids', [])
    exp_at = user.get('expires_at')

    bypass_cidrs = await db.get_all_bypass_cidrs()
    rv = await db.get_routing_version()

    new_uid, c_path, q_path = await create_peer(name, dns_type="classic", bypass_cidrs=bypass_cidrs)
    await db.execute(
        "INSERT INTO users (name, uuid, created_at, expires_at, routing_version) VALUES ($1, $2, NOW(), $3, $4)",
        name, new_uid, exp_at, rv
    )
    for tid in tg_ids:
        await db.link_user_telegram(new_uid, tid)
    await db.log_event("Client Regen", f"New config issued for {name} (routing v{rv}); old {old_uuid} pending retire.")

    if deliver:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ **Новый конфиг ключа «{escape_md(name)}» готов!**\n\n"
                f"📥 Импортируйте его в AmneziaWG *прямо сейчас*. Старый ключ продолжит "
                f"работать ещё ~{REGEN_GRACE_SECONDS} сек — чтобы вы не остались без "
                f"интернета во время замены."
            ),
            parse_mode=ParseMode.MARKDOWN
        )
        await context.bot.send_document(chat_id=chat_id, document=open(c_path, "rb"), caption=f"📄 {name}")
        await context.bot.send_photo(chat_id=chat_id, photo=open(q_path, "rb"))

    return old_uuid, name


async def _retire_old_peer(old_uuid, name):
    """Перевыпуск ключа, ШАГ 2: снимает СТАРЫЙ пир из ядра/конфига (файлы не трогаем —
    на диске уже новый конфиг) и удаляет старую запись из БД. Запускается после
    grace-периода."""
    try:
        await delete_peer(old_uuid, name, purge_files=False)
    except Exception as e:
        print(f"Retire old peer error ({name}/{old_uuid}): {e}")
    await db.execute("DELETE FROM users WHERE uuid=$1", old_uuid)
    await db.log_event("Client Regen", f"Old peer retired after grace: {name} ({old_uuid}).")


def _schedule_retire(retire_list):
    """Фоновая задача: ждёт grace-период и снимает старые пиры. Не блокирует хендлер."""
    async def _finalize():
        await asyncio.sleep(REGEN_GRACE_SECONDS)
        for old_uuid, name in retire_list:
            await _retire_old_peer(old_uuid, name)
    task = asyncio.create_task(_finalize())
    state_data.setdefault("bg_tasks", set()).add(task)
    task.add_done_callback(state_data["bg_tasks"].discard)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

async def get_live_peers_status():
    live_map = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{WG_API_URL}/peers", timeout=3) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    now = int(time.time())
                    for p in data:
                        hs = p.get('latest_handshake', 0)
                        if hs > 0 and (now - hs) < 180:
                            live_map[p.get('uuid')] = True
    except: pass
    return live_map

async def check_connection_animation(context, chat_id, message_id, uuid=None):
    frames = ["🟡", "🟡", "🟡"]
    bar_states =["░░░░░", "█░░░░", "██░░░", "███░░", "████░", "█████"]
    
    for i in range(6):
        await asyncio.sleep(0.5)
        try:
            icon = frames[i % len(frames)]
            bar = bar_states[i]
            text = f"{icon} **Проверка соединения...**\n`[{bar}]`"
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode=ParseMode.MARKDOWN)
        except Exception: pass

    is_online = False
    last_hs_time = 0
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{WG_API_URL}/peers", timeout=3) as resp:
                peers = await resp.json()
                target_uuids = [uuid] if uuid else[u['uuid'] for u in await db.get_users_by_tg_id(chat_id)]
                now = int(time.time())
                for p in peers:
                    if p.get('uuid') in target_uuids:
                        hs = p.get('latest_handshake', 0)
                        if hs > last_hs_time: last_hs_time = hs
                        if hs > 0 and (now - hs) < 180: is_online = True
    except Exception: pass

    keyboard = [[InlineKeyboardButton("🔙 К списку проверок", callback_data="client_select_check")]]
    if is_online:
        date_str = ts_to_moscow(last_hs_time).strftime('%H:%M:%S')
        res_text = f"🟢 **Соединение успешно!**\n\nСервер видит ваше устройство.\nПоследняя активность: `{date_str} МСК`"
    else:
        res_text = f"🔴 **Нет соединения**\n\nСервер не видит трафика от вас.\n1. Убедитесь, что VPN включен.\n2. Попробуйте открыть любой сайт.\n3. Нажмите проверить еще раз."
        keyboard.insert(0,[InlineKeyboardButton("🔄 Проверить еще раз", callback_data=f"check_conn_{uuid}")])

    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=res_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

# --- ГЛАВНОЕ МЕНЮ КЛИЕНТА ---

async def send_client_menu(context: ContextTypes.DEFAULT_TYPE, user_id: int, first_name: str = None):
    if not first_name:
        try:
            chat = await context.bot.get_chat(user_id)
            first_name = chat.first_name or "Пользователь"
        except Exception:
            first_name = "Пользователь"

    keys = await db.get_users_by_tg_id(user_id)
    
    if not keys:
        if check_admin(user_id):
            await context.bot.send_message(
                chat_id=user_id, 
                text="❌ У вас нет привязанных ключей, но вы Админ.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚪 Вернуться в Админку", callback_data="back_to_main")]])
            )
        else:
            await context.bot.send_message(chat_id=user_id, text="❌ У вас нет привязанных ключей VPN.")
        return

    text = f"👋 Привет, **{escape_md(first_name)}**!\n\nУ вас привязано ключей: **{len(keys)}**.\n"
    
    keyboard = [[InlineKeyboardButton("🔑 Мои ключи", callback_data="client_my_keys")],[InlineKeyboardButton("⚡️ Проверить соединение", callback_data="client_select_check")],[InlineKeyboardButton("📊 Моя статистика", callback_data="client_stats")],[InlineKeyboardButton("🌐 Рос. сервисы (исключения)", callback_data="client_bypass_info")],[InlineKeyboardButton("🆘 Сообщить о проблеме", callback_data="support_start")]
    ]
    if check_admin(user_id):
        keyboard.append([InlineKeyboardButton("🚪 Выйти из режима клиента", callback_data="back_to_main")])
    
    await context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def client_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    first_name = update.effective_user.first_name
    keys = await db.get_users_by_tg_id(user_id)
    
    if not keys:
        if check_admin(user_id):
            text = "❌ У вас нет привязанных ключей, но вы Админ."
            kb = [[InlineKeyboardButton("🚪 Вернуться в Админку", callback_data="back_to_main")]]
            if update.callback_query:
                await update.callback_query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(kb))
            else:
                await context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(kb))
            return
        else:
            if update.callback_query:
                await update.callback_query.edit_message_text(text="❌ У вас нет привязанных ключей VPN.")
            else:
                await context.bot.send_message(chat_id=user_id, text="❌ У вас нет привязанных ключей VPN.")
            return

    text = f"👋 Привет, **{escape_md(first_name)}**!\n\nУ вас привязано ключей: **{len(keys)}**.\n"
    
    keyboard = [[InlineKeyboardButton("🔑 Мои ключи", callback_data="client_my_keys")],[InlineKeyboardButton("⚡️ Проверить соединение", callback_data="client_select_check")],[InlineKeyboardButton("📊 Моя статистика", callback_data="client_stats")],[InlineKeyboardButton("🌐 Рос. сервисы (исключения)", callback_data="client_bypass_info")],[InlineKeyboardButton("🆘 Сообщить о проблеме", callback_data="support_start")]
    ]
    if check_admin(user_id):
        keyboard.append([InlineKeyboardButton("🚪 Выйти из режима клиента", callback_data="back_to_main")])
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
        await context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

# --- НОВОЕ МЕНЮ: УПРАВЛЕНИЕ КЛЮЧАМИ ---

async def client_my_keys_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, send_new: bool = False):
    user_id = update.effective_user.id
    keys = await db.get_users_by_tg_id(user_id)
    
    if not keys:
        if update.callback_query and not send_new:
            await update.callback_query.answer("У вас нет ключей.", show_alert=True)
        else:
            await context.bot.send_message(user_id, "У вас нет ключей.")
        return
    
    live_status = await get_live_peers_status()
    
    text = "🔑 **Мои ключи**\n\nВыберите ключ для управления:\n"
    keyboard =[]
    
    for k in keys:
        if not k.get('is_active', True): status_icon = "🔴"
        elif live_status.get(k['uuid']): status_icon = "🟢"
        else: status_icon = "🟡"
        
        btn_text = f"{status_icon} {k['name']}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"client_key_manage_{k['uuid']}")])
    
    if len(keys) > 1:
        keyboard.append([InlineKeyboardButton("🔄 Перевыпустить все ключи", callback_data="client_regen_all")])
        
    keyboard.append([InlineKeyboardButton("🔙 В главное меню", callback_data="client_menu")])
    
    if update.callback_query and not send_new:
        await update.callback_query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
        await context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def client_key_manage_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, uuid_val: str):
    query = update.callback_query
    user = await db.get_user_by_uuid(uuid_val)
    if not user:
        await query.answer("Ключ не найден.", show_alert=True)
        return
        
    live_status = await get_live_peers_status()
    
    if not user.get('is_active', True): status_text = "🔴 Отключен (Приостановлен)"
    elif live_status.get(user['uuid']): status_text = "🟢 Онлайн"
    else: status_text = "🟡 Офлайн"
        
    exp = f"до {dt_to_moscow(user['expires_at']).strftime('%d.%m.%Y')}" if user.get('expires_at') else "Навсегда"
    
    text = f"🔑 **Управление ключом**\n\n" \
           f"👤 **Имя:** `{escape_md(user['name'])}`\n" \
           f"📡 **Статус:** {status_text}\n" \
           f"⏳ **Срок действия:** {exp}\n\n" \
           f"Выберите действие:"
           
    keyboard = [[InlineKeyboardButton("📥 Скачать конфиг", callback_data=f"client_download_{uuid_val}")],[InlineKeyboardButton("🔄 Перевыпустить ключ", callback_data=f"client_regen_{uuid_val}")],[InlineKeyboardButton("🔙 Назад к списку", callback_data="client_my_keys")]
    ]
    
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def client_regen_all_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    keyboard = [[InlineKeyboardButton("✅ ДА, перевыпустить все", callback_data="do_client_regen_all")],[InlineKeyboardButton("🔙 Отмена", callback_data="client_my_keys")]
    ]
    await query.edit_message_text(
        "⚠️ **Массовый перевыпуск ключей**\n\nВсе ваши старые ключи будут удалены и заменены на новые. Вам придется обновить конфигурации на всех ваших устройствах.\n\nВы уверены?", 
        reply_markup=InlineKeyboardMarkup(keyboard), 
        parse_mode=ParseMode.MARKDOWN
    )

async def client_regen_all_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Запуск массового перевыпуска...")
    chat_id = query.message.chat_id
    
    user_id = update.effective_user.id
    keys = await db.get_users_by_tg_id(user_id)
    
    if not keys:
        await query.edit_message_text("❌ У вас нет ключей.")
        return
        
    await query.edit_message_text(
        "⏳ Готовлю новые конфиги для всех ключей…\n"
        "Сначала выдам новые ключи, и только потом сниму старые — не выключайте VPN.",
        parse_mode=ParseMode.MARKDOWN
    )

    # config-first: сначала выдаём ВСЕ новые конфиги, копим старые пиры на снятие
    retire_list = []
    for user in keys:
        try:
            retire_list.append(await _issue_new_config(context, chat_id, user))
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ Ошибка перевыпуска ключа {escape_md(user['name'])}: {e}")

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ Новые конфиги выданы. Старые ключи будут отключены через "
            f"~{REGEN_GRACE_SECONDS} сек — успейте импортировать новые в AmneziaWG."
        ),
        parse_mode=ParseMode.MARKDOWN
    )
    await safe_delete(context, chat_id, query.message.message_id)

    # снятие старых пиров — в фоне, после grace-периода
    if retire_list:
        _schedule_retire(retire_list)

# --- НОВОЕ МЕНЮ ПРОВЕРКИ ---

async def client_select_check_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    keys = await db.get_users_by_tg_id(user_id)

    text = "⚡️ **Проверка соединения**\n\nВыберите конкретный ключ для детальной диагностики или проверьте все сразу:"
    keyboard =[]

    for k in keys:
        keyboard.append([InlineKeyboardButton(f"🔎 {k['name']}", callback_data=f"check_conn_{k['uuid']}")])

    keyboard.append([InlineKeyboardButton("🚀 Проверить все ключи", callback_data="client_check_all")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="client_menu")])

    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def client_check_all_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Запуск диагностики...")
    await query.edit_message_text("🔄 **Диагностика всех ключей...**\nПожалуйста, подождите.", parse_mode=ParseMode.MARKDOWN)
    
    user_id = update.effective_user.id
    keys = await db.get_users_by_tg_id(user_id)
    
    peers_data =[]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{WG_API_URL}/peers", timeout=5) as resp:
                if resp.status == 200: peers_data = await resp.json()
    except Exception:
        await query.edit_message_text("❌ Ошибка связи с сервером. Попробуйте позже.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="client_select_check")]]))
        return

    report = "📊 **Результаты диагностики:**\n\n"
    now = int(time.time())
    
    for k in keys:
        uuid = k['uuid']
        name = escape_md(k['name'])
        peer_info = next((p for p in peers_data if p.get('uuid') == uuid), None)
        
        status_icon = "⚪️"
        status_msg = "Неизвестно"
        
        if not k.get('is_active', True):
            status_icon = "🔴"
            status_msg = "Ключ отключен (Пауза)"
        elif not peer_info:
            status_icon = "⚠️"
            status_msg = "Ошибка (не найден на сервере)"
        else:
            hs = peer_info.get('latest_handshake', 0)
            if hs > 0 and (now - hs) < 180:
                status_icon = "🟢"
                last_seen = ts_to_moscow(hs).strftime('%H:%M:%S')
                status_msg = f"Онлайн (Активен: {last_seen})"
            else:
                status_icon = "🟡"
                if hs == 0: status_msg = "Офлайн (Никогда не входил)"
                else:
                    last_seen = ts_to_moscow(hs).strftime('%d.%m %H:%M')
                    status_msg = f"Офлайн (Был: {last_seen})"
        
        report += f"{status_icon} **{name}**: {status_msg}\n"

    report += "\n💡 *Если статус 🟡 (Офлайн), проверьте, включен ли VPN в приложении.*"
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="client_select_check")]]
    await query.edit_message_text(text=report, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

# --- ОБРАБОТЧИКИ (ОСТАЛЬНЫЕ) ---

async def client_download_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, uuid_val: str):
    query = update.callback_query
    await query.answer("Подготовка файла...")
    user = await db.get_user_by_uuid(uuid_val)
    if not user: return
    
    name = user['name']
    chat_id = query.message.chat_id
    
    cf = CONFIGS_DIR / f"{name}.conf"
    qf = CONFIGS_DIR / f"{name}.png"
    
    if not cf.exists(): cf = CONFIGS_DIR / f"{name}_Full.conf"
    if not qf.exists(): qf = CONFIGS_DIR / f"{name}_Full.png"

    try:
        if cf.exists():
            await context.bot.send_document(chat_id=chat_id, document=open(cf, "rb"), caption=f"📄 Ваш VPN конфиг: {name}")
            if qf.exists(): await context.bot.send_photo(chat_id=chat_id, photo=open(qf, "rb"))
        else:
            await query.message.reply_text("❌ Файл конфигурации не найден. Попробуйте нажать 'Перевыпустить' для перевыпуска.")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Ошибка отправки: {e}")

async def check_connection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, uuid_val=None):
    query = update.callback_query
    await query.answer()
    msg = await query.edit_message_text("🟡 **Инициализация проверки...**", parse_mode=ParseMode.MARKDOWN)
    target_uuid = uuid_val if uuid_val != "all" else None
    
    task = asyncio.create_task(check_connection_animation(context, query.message.chat_id, msg.message_id, target_uuid))
    state_data["bg_tasks"].add(task)
    task.add_done_callback(state_data["bg_tasks"].discard)

async def client_stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    keys = await db.get_users_by_tg_id(user_id)
    
    text = "📊 **Ваша статистика (суточная активность):**\n\n"
    stats = await db.get_stats_24h()
    
    live_data = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{WG_API_URL}/peers", timeout=5) as resp:
                if resp.status == 200:
                    peers = await resp.json()
                    for p in peers:
                        live_data[p.get('uuid')] = p.get('rx', 0) + p.get('tx', 0)
    except Exception: pass

    for k in keys:
        uuid_val = k['uuid']
        user_stats = [s for s in stats if s['user_uuid'] == uuid_val]
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
            else:
                total_bytes += live_val
                
        mb = round(total_bytes / (1024 * 1024), 2)
        text += f"🔑 {escape_md(k['name'])}: `{mb} MB`\n"
        
    keyboard = [[InlineKeyboardButton("🔙 В меню", callback_data="client_menu")]]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def client_regen_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, uuid_val: str):
    query = update.callback_query
    keyboard = [[InlineKeyboardButton("✅ ДА, перевыпустить", callback_data=f"do_client_regen_{uuid_val}")],[InlineKeyboardButton("🔙 Отмена", callback_data=f"client_key_manage_{uuid_val}")]
    ]
    await query.edit_message_text(
        "⚠️ **Смена ключа**\n\nСтарый ключ будет безвозвратно удален. Вам выдадут новый файл конфигурации, который нужно будет заново добавить в приложение AmneziaWG.\nВы уверены?", 
        reply_markup=InlineKeyboardMarkup(keyboard), 
        parse_mode=ParseMode.MARKDOWN
    )

async def client_regen_action(update: Update, context: ContextTypes.DEFAULT_TYPE, uuid_val: str):
    query = update.callback_query
    await query.answer("Перевыпуск ключа...")
    chat_id = query.message.chat_id
    
    user = await db.get_user_by_uuid(uuid_val)
    if not user:
        await query.edit_message_text("❌ Ключ не найден.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="client_menu")]]))
        return
        
    await query.edit_message_text(
        f"⏳ Готовлю новый конфиг…\n"
        f"Сначала выдам новый ключ, и только спустя ~{REGEN_GRACE_SECONDS} сек сниму "
        f"старый — чтобы вы не остались без интернета. Не выключайте VPN.",
        parse_mode=ParseMode.MARKDOWN
    )

    # config-first: выдаём новый конфиг, старый пир снимаем в фоне после grace
    try:
        retire = await _issue_new_config(context, chat_id, user)
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Ошибка перевыпуска: {e}")
        return

    await safe_delete(context, chat_id, query.message.message_id)
    _schedule_retire([retire])

async def support_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    keys = await db.get_users_by_tg_id(user_id)
    
    if not keys:
        await query.answer("У вас нет ключей.")
        return

    if len(keys) == 1:
        await support_run_audit_handler(update, context, keys[0]['uuid'])
        return
    
    text = "🛠 **Служба поддержки**\n\nВыберите ключ, с которым возникла проблема:"
    keyboard = []
    for k in keys:
        keyboard.append([InlineKeyboardButton(f"🔑 {k['name']}", callback_data=f"support_audit_{k['uuid']}")])
    keyboard.append([InlineKeyboardButton("🔙 Отмена", callback_data="client_menu")])
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def support_run_audit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, uuid_val: str):
    query = update.callback_query
    await query.answer("Аудит ключа...")
    
    msg = await query.edit_message_text("🔄 **Провожу полную диагностику ключа...**", parse_mode=ParseMode.MARKDOWN)
    
    user = await db.get_user_by_uuid(uuid_val)
    if not user:
        await query.edit_message_text("❌ Ключ не найден.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="client_menu")]]))
        return

    issues_found = False
    status_text = f"🛠 **Результат диагностики ключа {escape_md(user['name'])}:**\n\n"

    if not user.get('is_active', True):
        status_text += "❌ **Статус:** Ваш ключ приостановлен администратором.\n"
        issues_found = True
    elif user.get('expires_at') and user['expires_at'] < datetime.utcnow():
        status_text += "❌ **Статус:** Срок действия вашего ключа истек.\n"
        issues_found = True
    else:
        status_text += "✅ **Статус:** Ключ активен и действителен.\n"

    server_ok = True
    is_online = False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{WG_API_URL}/peers", timeout=3) as resp:
                if resp.status == 200:
                    peers = await resp.json()
                    now = int(time.time())
                    for p in peers:
                        if p.get('uuid') == uuid_val:
                            hs = p.get('latest_handshake', 0)
                            if hs > 0 and (now - hs) < 180:
                                is_online = True
                else:
                    server_ok = False
    except:
        server_ok = False

    if not server_ok:
        status_text += "⚠️ **Сеть:** VPN-сервер временно недоступен (перезагрузка или сбой). Ожидайте.\n"
        issues_found = True
    elif is_online:
        status_text += "✅ **Сеть:** Соединение стабильное, сервер видит ваш трафик.\n"
    else:
        status_text += "⚠️ **Сеть:** Нет свежих подключений к серверу.\n"

    status_text += "\n💡 **Авто-рекомендации:**\n"
    
    if issues_found and not server_ok:
        status_text += "🔹 Подождите пару минут и проверьте статус снова. Сервер восстанавливается."
    elif not user.get('is_active', True) or (user.get('expires_at') and user['expires_at'] < datetime.utcnow()):
        status_text += "🔹 Напишите администратору для продления или активации ключа."
    elif is_online:
        status_text += "🔹 VPN работает идеально. Если сайты не открываются, проверьте, включен ли VPN в приложении."
    else:
        status_text += (
            "1️⃣ Проверьте, включен ли VPN в приложении AmneziaWG.\n"
            "2️⃣ Возможно, ваш провайдер блокирует сеть. Переключитесь с Wi-Fi на мобильный интернет LTE (или наоборот).\n"
            "3️⃣ Зайдите в 'Мои ключи' и нажмите **🔄 Перевыпустить**, чтобы обновить конфигурацию."
        )

    keyboard = [[InlineKeyboardButton("✅ Проблема решена (В меню)", callback_data="client_menu")],[InlineKeyboardButton("❌ Не помогло, написать Админу", callback_data=f"support_ask_{uuid_val}")]]
    
    await msg.edit_text(status_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def support_ask_msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, uuid_val: str):
    query = update.callback_query
    chat_id = query.message.chat_id
    
    state_data["support_context"][chat_id] = uuid_val
    context.user_data["state"] = "awaiting_support_message"
    
    keyboard = [[InlineKeyboardButton("🔙 Отмена", callback_data="client_menu")]]
    text = "📝 **Опишите вашу проблему:**\n\nНапишите одним сообщением, что именно не работает (например, 'не грузится инстаграм на wifi').\n\nЭто сообщение вместе с логами диагностики будет отправлено администратору."

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

# --- SPLIT-TUNNEL: просмотр исключений и заявка на неработающий сайт (клиент) ---

async def client_bypass_info_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rows = await db.get_bypass_exclusions()

    lines = [
        "🌐 **Доступ к российским сервисам (split-tunnel)**\n",
        "Эти ресурсы блокируют IP дата-центров, поэтому идут *напрямую* мимо VPN — "
        "так они снова работают (доступ открывается после перевыпуска ключа):\n",
    ]
    if rows:
        for r in rows:
            note = f" — {escape_md(r['note'])}" if r['note'] else ""
            lines.append(f"• `{escape_md(r['domain'])}`{note}")
    else:
        lines.append("_Список пуст._")
    lines.append("\n" + GOSUSLUGI_APP_WARNING)

    enabled = await db.get_routing_notify(update.effective_user.id)
    notify_label = "🔔 Напоминания о политиках: ВКЛ" if enabled else "🔕 Напоминания о политиках: ВЫКЛ"
    kb = [
        [InlineKeyboardButton("📝 Сообщить о неработающем сайте", callback_data="client_report_site")],
        [InlineKeyboardButton(notify_label, callback_data="client_notify_toggle")],
        [InlineKeyboardButton("🔙 В меню", callback_data="client_menu")],
    ]
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def client_notify_toggle_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_state = await db.toggle_routing_notify(update.effective_user.id)
    await update.callback_query.answer(
        "🔔 Напоминания о смене политик включены" if new_state else "🔕 Напоминания отключены",
        show_alert=False
    )
    await client_bypass_info_handler(update, context)

async def client_notify_off_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Быстрое отключение прямо из текста уведомления (кнопка «Не напоминать»)."""
    await db.set_routing_notify(update.effective_user.id, False)
    await update.callback_query.answer(
        "🔕 Готово — больше не буду напоминать о смене политик. Включить обратно можно в "
        "«Рос. сервисы (исключения)».",
        show_alert=True
    )

async def client_report_site_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data["state"] = "awaiting_bypass_report"
    kb = [[InlineKeyboardButton("🔙 Отмена", callback_data="client_bypass_info")]]
    await query.edit_message_text(
        "📝 **Какой ресурс не открывается?**\n\n"
        "Пришлите ссылку или домен одним сообщением — например, `gosuslugi.ru` "
        "или `https://lk.gosuslugi.ru`.\n\n"
        "Я проанализирую адрес и передам администратору на добавление в исключения. "
        "После добавления вам придёт уведомление с предложением перевыпустить ключ.",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN
    )