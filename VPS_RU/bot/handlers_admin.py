import os
import asyncio
import time
import json
from datetime import timedelta, datetime
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument, InputMediaPhoto
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest

from utils import (
    ADMIN_ID, WG_API_URL, escape_md, state_data, stop_bg_tasks, deregister_menu, 
    safe_delete, get_current_version, get_update_info, broadcast_message, get_moscow_now, ts_to_moscow, dt_to_moscow,
    DE_AGENT_URL
)
from ui import main_menu
from database import db
from monitor import get_dashboard
from graphs import generate_vpn_graph
from backup_manager import create_backup, restore_backup

async def return_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id=None, chat_id=None):
    await stop_bg_tasks()
    bot = context.bot if hasattr(context, 'bot') else context.bot

    if not chat_id and update and update.effective_chat:
        chat_id = update.effective_chat.id
    if not message_id and update and update.callback_query:
        message_id = update.callback_query.message.message_id
    
    active_count = 0
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{WG_API_URL}/status", timeout=2) as resp:
                if resp.status == 200: active_count = (await resp.json()).get("active_peers", 0)
    except Exception: pass
    
    try:
        support_count = await db.fetch_val("SELECT COUNT(*) FROM support_tickets WHERE status='open'")
        support_count = support_count or 0
    except Exception:
        support_count = 0

    text = "🛡 **VPN Dashboard (Dual Node)**\nВыберите действие:"
    markup = main_menu(active_count=active_count, support_count=support_count)
    sent_msg = None

    if not message_id:
        sent_msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
    else:
        try:
            sent_msg = await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
        except BadRequest as e:
            if "not modified" in str(e):
                state_data["active_menus"][chat_id] = message_id
                return
            await safe_delete(context, chat_id, message_id)
            sent_msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)

    if sent_msg: state_data["active_menus"][chat_id] = sent_msg.message_id

async def update_persistent_backup(context: ContextTypes.DEFAULT_TYPE, force_new: bool = False):
    try:
        archive_path = await asyncio.to_thread(create_backup)
        if not os.path.exists(archive_path): 
            return False, "Файл архива не создан."

        saved_msg_id = await db.get_setting("backup_message_id")
        caption = f"💾 **Актуальный бэкап системы (RU Master)**\n📅 Дата (МСК): {get_moscow_now().strftime('%d.%m.%Y %H:%M:%S')}\nℹ️ Сообщение обновляется автоматически."

        if force_new:
            with open(archive_path, "rb") as f:
                msg = await context.bot.send_document(chat_id=ADMIN_ID, document=f, caption=caption, parse_mode=ParseMode.MARKDOWN)
                try: await context.bot.pin_chat_message(chat_id=ADMIN_ID, message_id=msg.message_id)
                except: pass
                await db.set_setting("backup_message_id", str(msg.message_id))
            return True, "Успешно"

        sent_new = False
        if saved_msg_id:
            try:
                with open(archive_path, "rb") as f:
                    await context.bot.edit_message_media(chat_id=ADMIN_ID, message_id=int(saved_msg_id), media=InputMediaDocument(media=f, caption=caption, parse_mode=ParseMode.MARKDOWN))
            except Exception: 
                sent_new = True
        else:
            sent_new = True

        if sent_new:
            if saved_msg_id: 
                await safe_delete(context, ADMIN_ID, int(saved_msg_id))
            with open(archive_path, "rb") as f:
                msg = await context.bot.send_document(chat_id=ADMIN_ID, document=f, caption=caption, parse_mode=ParseMode.MARKDOWN)
                try: await context.bot.pin_chat_message(chat_id=ADMIN_ID, message_id=msg.message_id)
                except: pass
                await db.set_setting("backup_message_id", str(msg.message_id))
                
        return True, "Успешно"
    except Exception as e: 
        print(f"Backup update error: {e}")
        return False, str(e)

async def dashboard_loop(context, chat_id, message_id):
    while state_data["dashboard_running"]:
        try:
            text = await get_dashboard()
            keyboard = [[InlineKeyboardButton("🔙 Главное меню (Стоп)", callback_data="back_to_main")]]
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            if "not found" in str(e):
                state_data["dashboard_running"] = False
                break
        await asyncio.sleep(5)

async def start_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await stop_bg_tasks()
    deregister_menu(query.message.chat_id)
    
    state_data["dashboard_running"] = True
    state_data["dashboard_task"] = asyncio.create_task(dashboard_loop(context, query.message.chat_id, query.message.message_id))

# ==============================================================
# 🇷🇺 УПРАВЛЕНИЕ СЕРВЕРОМ RU (MASTER)
# ==============================================================

async def confirm_reboot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_bg_tasks()
    deregister_menu(update.effective_chat.id)
    keyboard = [[InlineKeyboardButton("🚨 ДА, Перезагрузить", callback_data="do_reboot_server")],[InlineKeyboardButton("🔙 Нет, Отмена", callback_data="back_to_main")]]
    await update.callback_query.edit_message_text("⚠️ **Внимание!**\nВы собираетесь перезагрузить **ФИЗИЧЕСКИЙ СЕРВЕР В РФ (RU Master)**.\nВы уверены?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def do_reboot_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await broadcast_message(context.application, "⚠️ **Внимание!**\n\nСервер уходит на перезагрузку. VPN будет недоступен 2-3 минуты.", db)
    await db.log_event("System", "Admin requested RU physical server reboot.")
    await update.callback_query.edit_message_text("🔄 **Команда на перезагрузку отправлена!**\n\nRU Сервер уходит в ребут.", parse_mode=ParseMode.MARKDOWN)
    
    os.makedirs("/volumes/flags", exist_ok=True)
    with open("/volumes/flags/was_rebooting", "w") as f: f.write("true")
    with open("/volumes/flags/do_reboot", "w") as f: f.write("reboot_requested")


# ==============================================================
# 🇩🇪 УПРАВЛЕНИЕ СЕРВЕРОМ DE (AGENT)
# ==============================================================

async def de_confirm_reboot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_bg_tasks()
    deregister_menu(update.effective_chat.id)
    keyboard = [[InlineKeyboardButton("🚨 ДА, Перезагрузить", callback_data="do_de_reboot_server")],[InlineKeyboardButton("🔙 Нет, Отмена", callback_data="back_to_main")]]
    await update.callback_query.edit_message_text("⚠️ **Внимание!**\nВы собираетесь удаленно перезагрузить **ФИЗИЧЕСКИЙ СЕРВЕР В ГЕРМАНИИ (DE Agent)**.\nВы уверены?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def do_de_reboot_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("🔄 Отправка команды ребута через туннель в Германию...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{DE_AGENT_URL}/host/reboot", timeout=5) as resp:
                if resp.status == 200:
                    await update.callback_query.edit_message_text("✅ **Команда принята.**\n\nСервер в Германии уходит в ребут (1-2 минуты).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]]), parse_mode=ParseMode.MARKDOWN)
                    await db.log_event("System", "Admin requested DE Agent physical reboot via tunnel.")
                else:
                    await update.callback_query.edit_message_text(f"❌ **Ошибка агента:** HTTP {resp.status}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]]), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.callback_query.edit_message_text(f"❌ **Ошибка связи с агентом:**\n`{e}`\nВозможно туннель упал.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]]), parse_mode=ParseMode.MARKDOWN)

async def de_read_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_bg_tasks()
    deregister_menu(update.effective_chat.id)
    await update.callback_query.edit_message_text("⏳ Подключение к агенту в Германии и чтение логов...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{DE_AGENT_URL}/logs?lines=150", timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logs = data.get("logs", "Логи пусты")
                    
                    log_path = "/tmp/de_agent_logs.txt"
                    with open(log_path, "w") as f:
                        f.write("=== LOGS FROM DE AGENT ===\n\n")
                        f.write(logs)
                        
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id, 
                        document=open(log_path, "rb"), 
                        caption="📑 Системные логи агента в Германии",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]])
                    )
                    await safe_delete(context, update.callback_query.message.chat_id, update.callback_query.message.message_id)
                else:
                    await update.callback_query.edit_message_text(f"❌ **Ошибка API агента:** HTTP {resp.status}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]]), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.callback_query.edit_message_text(f"❌ **Ошибка туннеля:**\nНе удалось связаться с агентом.\n`{e}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]]), parse_mode=ParseMode.MARKDOWN)

async def de_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_bg_tasks()
    deregister_menu(update.effective_chat.id)
    await update.callback_query.edit_message_text("⏳ Передаю команду на обновление агента в Германии (Git Pull)...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{DE_AGENT_URL}/host/update", timeout=5) as resp:
                if resp.status == 200:
                    await update.callback_query.edit_message_text("✅ **Процесс обновления запущен!**\n\nАгент в Германии скачивает новую версию из Git и перезапускает контейнеры. Это займет ~30-60 секунд.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]]), parse_mode=ParseMode.MARKDOWN)
                    await db.log_event("System", "Admin triggered DE Agent update via tunnel.")
                else:
                    await update.callback_query.edit_message_text(f"❌ **Ошибка API агента:** HTTP {resp.status}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]]), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.callback_query.edit_message_text(f"❌ **Ошибка связи:**\n`{e}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]]), parse_mode=ParseMode.MARKDOWN)

async def de_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_bg_tasks()
    deregister_menu(update.effective_chat.id)
    await update.callback_query.edit_message_text("⏳ Запрашиваю бэкап у агента в Германии...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{DE_AGENT_URL}/backup", timeout=15) as resp:
                if resp.status == 200:
                    backup_path = "/tmp/de_agent_backup_received.tar.gz"
                    with open(backup_path, 'wb') as f:
                        f.write(await resp.read())
                    
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id, 
                        document=open(backup_path, "rb"), 
                        caption="💾 **Бэкап конфигурации сервера DE (Агент)**",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]]),
                        parse_mode=ParseMode.MARKDOWN
                    )
                    await safe_delete(context, update.callback_query.message.chat_id, update.callback_query.message.message_id)
                else:
                    await update.callback_query.edit_message_text(f"❌ **Ошибка агента:** HTTP {resp.status}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]]), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.callback_query.edit_message_text(f"❌ **Ошибка загрузки:**\n`{e}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]]), parse_mode=ParseMode.MARKDOWN)

async def de_run_audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_bg_tasks()
    deregister_menu(update.effective_chat.id)
    chat_id = update.effective_chat.id
    msg_id = update.callback_query.message.message_id

    await update.callback_query.edit_message_text("⏳ Запускаю аудит на сервере в Германии...")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{DE_AGENT_URL}/host/audit", timeout=5) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}")
    except Exception as e:
        await update.callback_query.edit_message_text(f"❌ **Ошибка запуска аудита:**\n`{e}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]]), parse_mode=ParseMode.MARKDOWN)
        return

    for _ in range(30):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{DE_AGENT_URL}/host/audit_result", timeout=3) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("status") == "done":
                            report = json.loads(data["report"])
                            await send_de_audit_report(context, chat_id, msg_id, report)
                            return
                        elif data.get("status") == "running":
                            step = data.get("step", "...")
                            try:
                                await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=f"🇩🇪 **Аудит Германии...**\nТекущий этап: `{step}`", parse_mode=ParseMode.MARKDOWN)
                            except: pass
        except: pass
        await asyncio.sleep(1.5)
        
    await update.callback_query.edit_message_text("❌ Таймаут ожидания результатов аудита от Германии.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]]))

async def send_de_audit_report(context, chat_id, msg_id, report):
    full_report_path = "/tmp/de_audit_detailed.txt"
    with open(full_report_path, "w", encoding="utf-8") as rf:
        rf.write(f"=== АУДИТ DE AGENT ({get_moscow_now().strftime('%d.%m.%Y %H:%M:%S')}) ===\n\n")
        for cat_key, tests in report.items():
            rf.write(f"--- {cat_key.upper()} ---\n")
            for t in tests:
                icon = "[ OK ]" if t["status"] == "ok" else ("[WARN]" if t["status"] == "warning" else "[FAIL]")
                rf.write(f"{icon} {t['name']}: {t['msg']}\n")
            rf.write("\n")

    summary_text = "🇩🇪 **Итоги Аудита (DE Agent):**\n\n"
    for cat_key, tests in report.items():
        cat_fails = sum(1 for t in tests if t["status"] == "error")
        cat_warns = sum(1 for t in tests if t["status"] == "warning")
        cat_icon = "✅"
        if cat_fails > 0: cat_icon = "❌"
        elif cat_warns > 0: cat_icon = "⚠️"
        summary_text += f"{cat_icon} **{cat_key.capitalize()}**\n"

    summary_text += "\n📄 *Подробности в файле.*"
    
    await safe_delete(context, chat_id, msg_id)
    kb = [[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]]
    with open(full_report_path, "rb") as rf:
        await context.bot.send_document(
            chat_id=chat_id, 
            document=rf, 
            caption=summary_text, 
            reply_markup=InlineKeyboardMarkup(kb), 
            parse_mode=ParseMode.MARKDOWN
        )

# ==============================================================
# ОСТАЛЬНЫЕ ФУНКЦИИ (ГРАФИКИ, ПОЛЬЗОВАТЕЛИ, ОБНОВЛЕНИЯ RU)
# ==============================================================

async def graph_loop(context, chat_id, message_id):
    while state_data["graph_running"]:
        await asyncio.sleep(10)
        if not state_data["graph_running"]: break
        try:
            path = await generate_vpn_graph()
            keyboard = [[InlineKeyboardButton("🔙 Назад (Остановить)", callback_data="back_to_main")]]
            with open(path, "rb") as f:
                media = InputMediaPhoto(media=f, caption=f"📡 **Live-мониторинг трафика**\n⏳ Обновлено: `{get_moscow_now().strftime('%H:%M:%S')} МСК`", parse_mode=ParseMode.MARKDOWN)
                await context.bot.edit_message_media(chat_id=chat_id, message_id=message_id, media=media, reply_markup=InlineKeyboardMarkup(keyboard))
        except BadRequest as e:
            if "not modified" not in str(e): pass
        except Exception: pass

async def send_vpn_graph(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_bg_tasks()
    deregister_menu(update.effective_chat.id)
    query = update.callback_query
    await query.answer()
    await safe_delete(context, query.message.chat_id, query.message.message_id)

    try:
        path = await generate_vpn_graph()
        keyboard = [[InlineKeyboardButton("🔙 Назад (Остановить)", callback_data="back_to_main")]]
        msg = await context.bot.send_photo(chat_id=query.message.chat_id, photo=open(path, "rb"), caption=f"📡 **Live-мониторинг трафика**\n⏳ Обновлено: `{get_moscow_now().strftime('%H:%M:%S')} МСК`", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
        
        state_data["graph_running"] = True
        state_data["graph_task"] = asyncio.create_task(graph_loop(context, query.message.chat_id, msg.message_id))
    except Exception as e:
        await return_to_main_menu(update, context)
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"❌ Ошибка графика: {e}")

async def online_users_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_bg_tasks()
    deregister_menu(update.effective_chat.id)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{WG_API_URL}/peers", timeout=3) as resp:
                peers_data = await resp.json()

        db_users = await db.get_all_users()
        uuid_to_name = {u['uuid']: u['name'] for u in db_users}
        active_list =[]
        now = int(time.time())
        for peer in peers_data:
            last_hs = peer.get('latest_handshake', 0)
            diff = now - last_hs
            if last_hs > 0 and diff < 180:
                uuid_val = peer.get('uuid')
                name = uuid_to_name.get(uuid_val, "Unknown")
                date_str = ts_to_moscow(last_hs).strftime('%d.%m.%y %H:%M:%S')
                diff_str = str(timedelta(seconds=diff)).split('.')[0]
                active_list.append(f"👤 **{escape_md(name)}**\n🕒 МСК: {date_str} `[{diff_str} назад]`")

        text = "🟢 **Пользователи онлайн:**\n\n" + "\n\n".join(active_list) if active_list else "💤 **Сейчас никого нет онлайн.**"
    except Exception as e: text = f"⚠️ Ошибка получения данных: {e}"
    
    try:
        await update.callback_query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]]), parse_mode=ParseMode.MARKDOWN)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            await safe_delete(context, update.effective_chat.id, update.callback_query.message.message_id)
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]]), parse_mode=ParseMode.MARKDOWN)

async def check_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_bg_tasks()
    deregister_menu(update.effective_chat.id)
    query = update.callback_query
    
    if query.data == "toggle_auto_update":
        local_hash, local_version, remote_hash, remote_version = context.user_data.get("update_info", ("unknown", "unknown", "unknown", "unknown"))
    else:
        await query.edit_message_text("⏳ Проверка обновлений RU Master (сверка хэшей)...")
        local_hash, local_version, remote_hash, remote_version = await asyncio.to_thread(get_update_info)
        context.user_data["update_info"] = (local_hash, local_version, remote_hash, remote_version)
        
    is_update_available = False
    if remote_hash != "unknown" and local_hash != "unknown" and remote_hash != local_hash:
        is_update_available = True
        
    safe_local_ver = escape_md(str(local_version))
    safe_remote_ver = escape_md(str(remote_version))
        
    text = f"📦 **Обновления RU Master**\n\n"
    text += f"Текущая версия: `{safe_local_ver}` (Хэш: `{local_hash}`)\n"
    text += f"Доступна версия: `{safe_remote_ver}` (Хэш: `{remote_hash}`)\n\n"
    
    if is_update_available:
        text += "⚠️ **Доступно новое обновление!**"
        btn = InlineKeyboardButton("✅ Обновить сейчас", callback_data="do_update")
    else:
        text += "✅ У вас установлена последняя версия."
        btn = InlineKeyboardButton("🔄 Переустановить", callback_data="do_update")
        
    auto_upd = await db.get_setting("auto_update_enabled")
    auto_upd_text = "Автообновления ✅" if auto_upd == "true" else "Автообновления ❌"
        
    keyboard = [
        [btn],
        [InlineKeyboardButton(auto_upd_text, callback_data="toggle_auto_update")],
        [InlineKeyboardButton("📅 Запланировать", callback_data="schedule_update")],
        [InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]
    ]
        
    try:
        await query.edit_message_text(
            text=text, 
            reply_markup=InlineKeyboardMarkup(keyboard), 
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            print(f"Ошибка отрисовки меню обновлений: {e}")

async def toggle_auto_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
        current = await db.get_setting("auto_update_enabled")
        new_state = "false" if current == "true" else "true"
        await db.set_setting("auto_update_enabled", new_state)
        await check_update(update, context)
    except Exception as e:
        print(f"Ошибка переключения автообновления: {e}")
        await query.answer(f"Ошибка: {e}", show_alert=True)

async def schedule_update_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_bg_tasks()
    query = update.callback_query
    keyboard = [[InlineKeyboardButton("🔙 Отмена", callback_data="back_to_main")]]
    await query.edit_message_text(
        "📅 **Планирование автоматического обновления**\n\n"
        "Введите дату и время проведения обновления в формате: `ДД.ММ.ГГГГ ЧЧ:ММ` (По Московскому Времени)\n"
        "Например: `15.05.2024 03:30`\n\n"
        "После сохранения система автоматически оповестит пользователей.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN
    )
    context.user_data["state"] = "awaiting_schedule_time"
    context.user_data["menu_msg_id"] = query.message.message_id

async def do_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.message.edit_reply_markup(reply_markup=None)
    await broadcast_message(context.application, "⚠️ **Технические работы**\n\nСервер уходит на обновление. Связь может прерваться на 1-2 минуты.", db)
    await db.log_event("System", "Admin triggered system update via Git.")
    
    status_msg = await update.callback_query.message.reply_text("⚙️ Шаг 1/3: Бэкап...")
    try:
        res = await update_persistent_backup(context)
        if isinstance(res, tuple) and not res[0]:
            await status_msg.edit_text(f"⚠️ Ошибка бэкапа: {res[1]}\nПродолжаю обновление...")
        else:
            await status_msg.edit_text("⚙️ Шаг 2/3: Бэкап OK.\n⚙️ Шаг 3/3: Сигнал обновления...")
    except Exception as e:
        await status_msg.edit_text(f"⚠️ Ошибка бэкапа: {e}\nПродолжаю обновление...")

    await status_msg.edit_text("🚀 **Обновление запущено.**\nКонтейнеры перезапускаются. Бот вернется через минуту.")

    os.makedirs("/volumes/flags", exist_ok=True)
    with open("/volumes/flags/was_updating", "w") as f: f.write("true")
    with open("/volumes/flags/do_update", "w") as f: f.write("update_requested")

# --- МЕНЮ ТЕХНИЧЕСКОЙ ПОДДЕРЖКИ ---
async def support_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_bg_tasks()
    query = update.callback_query
    deregister_menu(update.effective_chat.id)
    
    sql = """
        SELECT u.uuid, u.name, COUNT(t.id) as cnt 
        FROM users u 
        JOIN support_tickets t ON u.uuid = t.user_uuid 
        WHERE t.status='open' 
        GROUP BY u.uuid, u.name
    """
    rows = await db.fetch_all(sql)
    
    text = "🆘 **Активные обращения в Поддержку**\n\n"
    keyboard =[]
    
    if not rows:
        text += "✅ Нет открытых обращений. Все тикеты закрыты."
    else:
        text += "Выберите пользователя для просмотра тикетов:"
        for r in rows:
            keyboard.append([InlineKeyboardButton(f"👤 {r['name']} ({r['cnt']} шт)", callback_data=f"supp_usr_{r['uuid']}")])
            
    keyboard.append([InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def support_user_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE, uuid_val: str):
    query = update.callback_query
    user = await db.get_user_by_uuid(uuid_val)
    rows = await db.fetch_all("SELECT id, message, created_at FROM support_tickets WHERE user_uuid=$1 AND status='open' ORDER BY created_at ASC", uuid_val)
    
    name = user['name'] if user else "Неизвестно"
    text = f"👤 **Обращения от: {escape_md(name)}**\n\nВыберите конкретное обращение для ответа:"
    
    keyboard =[]
    for r in rows:
        short_text = r['message'][:20] + "..." if len(r['message']) > 20 else r['message']
        date_str = dt_to_moscow(r['created_at']).strftime('%d.%m %H:%M')
        keyboard.append([InlineKeyboardButton(f"[{date_str}] {short_text}", callback_data=f"supp_tkt_{r['id']}")])
        
    keyboard.append([InlineKeyboardButton("🔙 Назад к списку", callback_data="support_admin_menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def support_ticket_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, ticket_id: str):
    query = update.callback_query
    rows = await db.fetch_all("SELECT user_uuid, message, created_at FROM support_tickets WHERE id=$1", int(ticket_id))
    if not rows:
        await query.answer("Обращение уже закрыто или не найдено.")
        return await support_admin_menu(update, context)
        
    t = rows[0]
    user = await db.get_user_by_uuid(t['user_uuid'])
    name = user['name'] if user else "Неизвестно"
    date_str = dt_to_moscow(t['created_at']).strftime('%d.%m.%Y %H:%M')
    
    text = f"🎫 **Обращение #{ticket_id}**\n👤 Пользователь: `{escape_md(name)}`\n🕒 Время: `{date_str}`\n\n📝 **Текст:**\n_{escape_md(t['message'])}_"
    
    keyboard = [[InlineKeyboardButton("✍️ Ответить", callback_data=f"supp_rep_{ticket_id}")],[InlineKeyboardButton("✅ Закрыть (Без ответа)", callback_data=f"supp_clo_{ticket_id}")],[InlineKeyboardButton("🔙 Назад к пользователю", callback_data=f"supp_usr_{t['user_uuid']}")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def support_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE, ticket_id: str):
    query = update.callback_query
    context.user_data["state"] = "awaiting_support_reply"
    context.user_data["reply_ticket_id"] = ticket_id
    context.user_data["menu_msg_id"] = query.message.message_id
    
    keyboard = [[InlineKeyboardButton("🔙 Отмена", callback_data=f"supp_tkt_{ticket_id}")]]
    await query.edit_message_text("✍️ **Введите текст ответа:**\n\nЭтот текст будет отправлен пользователю, а само обращение будет автоматически помечено как закрытое.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def support_close_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE, ticket_id: str):
    query = update.callback_query
    await db.execute("UPDATE support_tickets SET status='closed' WHERE id=$1", int(ticket_id))
    await query.answer("Обращение закрыто!")
    await support_admin_menu(update, context)

# --- BACKUPS AND SYSTEM EXPORTS ---
async def backup_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    msg = await context.bot.send_message(chat_id=ADMIN_ID, text="⏳ **Создание бэкапа RU Master...**\nСохраняю базу данных и ключи, пожалуйста, подождите.", parse_mode=ParseMode.MARKDOWN)
    success, err = await update_persistent_backup(context, force_new=True)
    if success:
        await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg.message_id, text="✅ **Новый бэкап успешно создан и закреплен в шапке чата!**", parse_mode=ParseMode.MARKDOWN)
        await asyncio.sleep(4)
        await safe_delete(context, ADMIN_ID, msg.message_id)
    else:
        await context.bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg.message_id, text=f"❌ **Ошибка создания бэкапа:**\n`{err}`", parse_mode=ParseMode.MARKDOWN)

async def download_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("Генерация логов...", show_alert=True)
    try:
        path = await db.export_logs_to_excel("/volumes/backups/vpn_logs.xlsx")
        await context.bot.send_document(chat_id=ADMIN_ID, document=open(path, "rb"), caption="📑 Логи трафика (Excel)")
    except Exception as e: await context.bot.send_message(chat_id=ADMIN_ID, text=f"❌ Ошибка генерации: {e}")

async def restore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_bg_tasks()
    deregister_menu(update.effective_chat.id)
    keyboard = [[InlineKeyboardButton("🔙 Отмена", callback_data="back_to_main")]]
    await update.callback_query.edit_message_text("📤 **Отправьте боту файл резервной копии** `(.tar.gz)`", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    context.user_data["state"] = "awaiting_restore_file"
    context.user_data["menu_msg_id"] = update.callback_query.message.message_id

async def restore_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from utils import check_admin 
    if not check_admin(update.effective_user.id): return
    if context.user_data.get("state") != "awaiting_restore_file": return
    
    user_msg_id = update.message.message_id
    chat_id = update.message.chat_id
    menu_id = context.user_data.get("menu_msg_id")
    await safe_delete(context, chat_id, user_msg_id)

    try:
        if not update.message.document.file_name.endswith(".tar.gz"):
            if menu_id: await context.bot.edit_message_text(chat_id=chat_id, message_id=menu_id, text="❌ Пожалуйста, отправьте правильный файл архива с расширением `.tar.gz`", parse_mode=ParseMode.MARKDOWN)
            return

        file = await context.bot.get_file(update.message.document.file_id)
        await file.download_to_drive("/tmp/restore.tar.gz")
        
        if menu_id: await context.bot.edit_message_text(chat_id=chat_id, message_id=menu_id, text="⏳ Архив загружен. Начинаю восстановление...")
        
        await restore_backup("/tmp/restore.tar.gz")
        
        context.user_data["state"] = None
        await db.log_event("System", "System restored from backup archive.")
        await update_persistent_backup(context)
        await return_to_main_menu(update, context, message_id=menu_id, chat_id=chat_id)
    except Exception as e:
        if menu_id: 
            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]]
            await context.bot.edit_message_text(chat_id=chat_id, message_id=menu_id, text=f"❌ Ошибка восстановления:\n`{e}`", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def export_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("Генерация БД...", show_alert=True)
    try:
        path = await db.export_to_excel("/volumes/backups/users_db.xlsx")
        await context.bot.send_document(chat_id=ADMIN_ID, document=open(path, "rb"), caption="📊 База данных и Логи")
    except Exception as e: await context.bot.send_message(chat_id=ADMIN_ID, text=f"❌ Ошибка: {e}")

async def run_audit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_bg_tasks()
    deregister_menu(update.effective_chat.id)
    query = update.callback_query
    chat_id = query.message.chat_id
    msg_id = query.message.message_id

    await query.answer("Связь с демоном хоста (RU)...")

    flags_dir = "/volumes/flags"
    os.makedirs(flags_dir, exist_ok=True)
    
    status_file = os.path.join(flags_dir, "audit_status")
    report_file = os.path.join(flags_dir, "audit_report.json")
    if os.path.exists(status_file): os.remove(status_file)
    if os.path.exists(report_file): os.remove(report_file)

    with open(os.path.join(flags_dir, "do_audit"), "w") as f:
        f.write("run")

    stages = {
        "network": "🌐 Проверка сети и маршрутизации",
        "host": "💻 Анализ аппаратных ресурсов",
        "docker": "🐳 Проверка среды Docker",
        "vpn": "🛡 Тестирование ядра VPN",
        "storage": "🗄 Диагностика хранилища и БД",
        "security": "🔐 Аудит безопасности",
        "services": "⚙️ Анализ системных логов",
        "done": "✅ Сборка 50 параметров отчета"
    }

    dots_arr =[".  ", ".. ", "..."]
    current_stage = "init"
    
    for i in range(50):
        if os.path.exists(status_file):
            with open(status_file, "r") as f:
                current_stage = f.read().strip()
        
        if current_stage == "done" and os.path.exists(report_file):
            break

        dots = dots_arr[i % 3]
        text = "🛠 **Глобальный аудит Сервера (RU Master)**\n\nВыполняется глубокая проверка системы:\n\n"
        
        stage_keys = list(stages.keys())
        try:
            cur_idx = stage_keys.index(current_stage) if current_stage in stage_keys else -1
        except ValueError:
            cur_idx = -1
            
        for idx, (k, v) in enumerate(stages.items()):
            if k == "done": continue
            if idx < cur_idx:
                text += f"✅ {v}\n"
            elif idx == cur_idx:
                text += f"🔄 {v}{dots}\n"
            else:
                text += f"⏳ {v}\n"
        
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            pass
        
        await asyncio.sleep(0.6)

    if not os.path.exists(report_file):
        text = "❌ **Ошибка аудита:** Демон хоста не ответил.\nПроверьте: `sudo systemctl status vpn-updater`"
        kb = [[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]]
        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        return

    try:
        with open(report_file, "r") as f:
            report = json.load(f)
    except Exception as e:
        text = f"❌ **Ошибка парсинга отчета:**\n`{e}`"
        kb = [[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]]
        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        return

    full_report_path = "/tmp/audit_detailed_report.txt"
    with open(full_report_path, "w", encoding="utf-8") as rf:
        rf.write(f"=== ГЛОБАЛЬНЫЙ АУДИТ СЕРВЕРА RU ({get_moscow_now().strftime('%d.%m.%Y %H:%M:%S')}) ===\n\n")
        for cat_key, tests in report.items():
            cat_name = stages.get(cat_key, cat_key).upper()
            rf.write(f"--- {cat_name} ---\n")
            for t in tests:
                icon = "[ OK ]" if t["status"] == "ok" else ("[WARN]" if t["status"] == "warning" else "[FAIL]")
                rf.write(f"{icon} {t['name']}: {t['msg']}\n")
            rf.write("\n")

    total_tests = sum(len(tests) for tests in report.values())
    summary_text = f"📊 **Итоги Глобального Аудита (RU Master):**\n⏳ Всего проверок: {total_tests}\n\n"
    
    total_fails = 0
    total_warns = 0

    for cat_key, tests in report.items():
        cat_total = len(tests)
        cat_fails = sum(1 for t in tests if t["status"] == "error")
        cat_warns = sum(1 for t in tests if t["status"] == "warning")
        cat_ok = sum(1 for t in tests if t["status"] == "ok")
        
        total_fails += cat_fails
        total_warns += cat_warns
        
        cat_icon = "✅"
        if cat_fails > 0: cat_icon = "❌"
        elif cat_warns > 0: cat_icon = "⚠️"
        
        cat_name = stages.get(cat_key, cat_key)
        summary_text += f"{cat_icon} **{cat_name}** `[{cat_ok}/{cat_total} OK]`\n"

    summary_text += "\n"
    
    if total_fails == 0 and total_warns == 0:
        summary_text += "🚀 **Вердикт:** Сервер в идеальном состоянии!"
    else:
        summary_text += f"⚠️ **Вердикт:** Ошибок: {total_fails}, Предупреждений: {total_warns}\n"
        summary_text += "📄 *Подробности по каждому тесту читайте в файле ниже 👇*"

    if len(summary_text) > 1000:
        summary_text = summary_text[:950] + "...\n📄 *Полный отчет в файле.*"

    await safe_delete(context, chat_id, msg_id)
    
    kb = [[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]]
    with open(full_report_path, "rb") as rf:
        await context.bot.send_document(
            chat_id=chat_id, 
            document=rf, 
            caption=summary_text, 
            reply_markup=InlineKeyboardMarkup(kb), 
            parse_mode=ParseMode.MARKDOWN
        )

async def sub_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_bg_tasks()
    query = update.callback_query
    from ui import menu_ru_server, menu_de_server, menu_backups
    if query.data == "menu_ru_server":
        await query.edit_message_text("🇷🇺 **Панель управления RU (Мастер)**\n\nУправление российской нодой и ядром VPN.", reply_markup=menu_ru_server(), parse_mode=ParseMode.MARKDOWN)
    elif query.data == "menu_de_server":
        await query.edit_message_text("🇩🇪 **Панель управления DE (Агент)**\n\nУправление агентом для обхода блокировок.", reply_markup=menu_de_server(), parse_mode=ParseMode.MARKDOWN)
    elif query.data == "menu_backups":
        await query.edit_message_text("💾 **Управление резервными копиями и логами**", reply_markup=menu_backups(), parse_mode=ParseMode.MARKDOWN)