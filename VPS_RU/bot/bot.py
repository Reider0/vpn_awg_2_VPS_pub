import logging
import asyncio
import aiohttp
import html
import os
import json
import time
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ParseMode

from utils import (
    BOT_TOKEN, ADMIN_ID, WG_API_URL, DE_AGENT_URL, escape_md, state_data, stop_bg_tasks, deregister_menu,
    safe_delete, get_current_version, broadcast_message, extract_tg_id, check_admin, sanitize_name
)
from database import db
from ui import main_menu
from monitor import (
    alert_loop, cleanup_peers, stats_collector_loop, self_healing_loop,
    expiration_loop, inactivity_loop, weekly_report_loop, log_cleanup_loop,
    auto_reboot_loop, scheduled_update_loop, resource_monitor_loop,
    routing_upgrade_loop, run_bypass_check_handler, bypass_notify_now_handler
)
from wireguard_manager import pause_peer, resume_peer

from handlers_client import (
    client_menu, send_client_menu, check_connection_handler, client_stats_handler, 
    client_regen_confirm, client_regen_action, support_start_handler, support_run_audit_handler, 
    support_ask_msg_handler, client_download_handler, client_select_check_menu, client_check_all_handler,
    client_my_keys_handler, client_key_manage_handler, client_regen_all_confirm_handler, client_regen_all_action_handler
)
from handlers_admin import (
    return_to_main_menu, update_persistent_backup, start_dashboard, confirm_reboot, do_reboot_server, 
    send_vpn_graph, online_users_menu, check_update, do_update, backup_now, download_logs, restore_cmd, 
    restore_file_handler, export_excel, run_audit_handler, schedule_update_menu, toggle_auto_update,
    support_admin_menu, support_user_tickets, support_ticket_detail, support_reply_start, support_close_ticket,
    de_confirm_reboot, do_de_reboot_server, de_read_logs,
    de_update, de_backup, de_run_audit
)
from handlers_users import (
    users_list_menu, user_detail_menu, confirm_delete_menu, action_delete_user, action_resend_config, 
    generate_key_request, finish_key_creation, render_user_detail, clear_user_ips
)

# --- ЗАДАЧИ БОТА (СИНХРОНИЗАЦИЯ И МОНИТОРИНГ) ---
async def sync_wg_config():
    try:
        saved_json = await db.get_setting("wg_config_backup")
        if saved_json:
            saved_data = json.loads(saved_json)
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{WG_API_URL}/backup_config", timeout=5) as resp:
                    if resp.status == 200:
                        current_data = await resp.json()
                        cur_conf = current_data.get("wg0.conf", "")
                        saved_conf = saved_data.get("wg0.conf", "")
                        
                        if len(cur_conf) < 50 or "[Peer]" not in cur_conf:
                            if "[Peer]" in saved_conf:
                                print("🔄 Restoring wg0.conf and keys from Database Backup...")
                                await session.post(f"{WG_API_URL}/restore_config", json=saved_data)

        print("🔍 Проведение аудита ключей (БД vs VPN)...")
        db_users = await db.get_all_users()
        db_uuids = [u['uuid'] for u in db_users]
        
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{WG_API_URL}/peers", timeout=5) as resp:
                if resp.status == 200:
                    wg_peers = await resp.json()
                    ghosts = 0
                    for p in wg_peers:
                        wg_uuid = p.get('uuid')
                        wg_pubkey = p.get('public_key')
                        
                        should_kill = False
                        if wg_uuid and wg_uuid not in db_uuids:
                            should_kill = True
                        
                        if should_kill and wg_pubkey:
                            print(f"👻 Удаление 'призрака' при запуске: {wg_pubkey}")
                            await session.post(f"{WG_API_URL}/kill_ghost", json={"public_key": wg_pubkey, "purge_config": True})
                            ghosts += 1
                    
                    if ghosts > 0:
                        print(f"✅ Аудит завершен. Уничтожено: {ghosts}")
                    else:
                        print("✅ Аудит завершен. База и VPN синхронизированы.")

    except Exception as e:
        print(f"Error syncing wg config: {e}")

async def watch_online_count(app):
    while True:
        try:
            current_active = 0
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{WG_API_URL}/status", timeout=2) as resp:
                    if resp.status == 200: 
                        current_active = (await resp.json()).get("active_peers", 0)

            try:
                supp_count = await db.fetch_val("SELECT COUNT(*) FROM support_tickets WHERE status='open'")
                supp_count = supp_count or 0
            except Exception:
                supp_count = 0

            last_active = state_data.get("last_known_active_count", -1)
            last_supp = state_data.get("last_known_support_count", -1)

            if current_active != last_active or supp_count != last_supp:
                state_data["last_known_active_count"] = current_active
                state_data["last_known_support_count"] = supp_count
                
                for chat_id in list(state_data["active_menus"].keys()):
                    message_id = state_data["active_menus"][chat_id]
                    if check_admin(chat_id):
                        try:
                            await app.bot.edit_message_reply_markup(
                                chat_id=chat_id, 
                                message_id=message_id, 
                                reply_markup=main_menu(active_count=current_active, support_count=supp_count)
                            )
                        except Exception as e:
                            if "not found" in str(e) or "not modified" in str(e): 
                                deregister_menu(chat_id)
        except Exception: pass 
        await asyncio.sleep(5)

async def check_update_completion(app):
    flag_update = "/volumes/flags/was_updating"
    flag_reboot = "/volumes/flags/was_rebooting"
    
    text = None
    if os.path.exists(flag_update):
        os.remove(flag_update)
        text = f"✅ **Обновление завершено!**\n\nСервер снова онлайн.\nТекущая версия: `{get_current_version()}`\nВсе системы в норме."
    elif os.path.exists(flag_reboot):
        os.remove(flag_reboot)
        text = "✅ **Сервер перезагружен.**\n\nСервисы VPN восстановлены и готовы к работе."

    if text:
        await asyncio.sleep(15)
        try:
            await broadcast_message(app, text, db)
            await db.log_event("System", "Server update/reboot sequence completed successfully.")
            
            if ADMIN_ID:
                try:
                    active_count = 0
                    async with aiohttp.ClientSession() as session:
                        async with session.get(f"{WG_API_URL}/status", timeout=2) as resp:
                            if resp.status == 200: 
                                active_count = (await resp.json()).get("active_peers", 0)
                    
                    try:
                        supp_count = await db.fetch_val("SELECT COUNT(*) FROM support_tickets WHERE status='open'")
                        supp_count = supp_count or 0
                    except Exception:
                        supp_count = 0

                    msg = await app.bot.send_message(
                        chat_id=ADMIN_ID, 
                        text="🛡 **VPN Dashboard**\nВыберите действие:", 
                        reply_markup=main_menu(active_count=active_count, support_count=supp_count), 
                        parse_mode=ParseMode.MARKDOWN
                    )
                    state_data["active_menus"][ADMIN_ID] = msg.message_id
                except Exception: pass
        except Exception as e:
            print(f"Check update completion error: {e}")

async def auto_backup_loop(app):
    while True:
        await asyncio.sleep(43200)
        try:
            await update_persistent_backup(app)
        except Exception as e:
            print(f"Auto-backup error: {e}")

# --- ОСНОВНЫЕ РОУТЕРЫ СООБЩЕНИЙ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if check_admin(user_id):
        context.user_data["state"] = None
        await stop_bg_tasks()
        await safe_delete(context, update.effective_chat.id, update.message.message_id)
        await return_to_main_menu(update, context)
        
        task = asyncio.create_task(update_persistent_backup(context))
        state_data.setdefault("bg_tasks", set()).add(task)
        task.add_done_callback(lambda t: state_data["bg_tasks"].discard(t))
        return

    keys = await db.get_users_by_tg_id(user_id)
    if keys: 
        await client_menu(update, context)
    else: 
        await context.bot.send_message(chat_id=user_id, text="⛔️ **Нет доступа**\n\nУ вас нет привязанных ключей.\nПопросите администратора добавить ваш Telegram ID.", parse_mode=ParseMode.MARKDOWN)

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_admin(update.effective_user.id): return
    await safe_delete(context, update.effective_chat.id, update.message.message_id)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    chat_id = update.message.chat_id
    user_msg_id = update.message.message_id
    
    if state == "awaiting_support_message":
        target_uuid = state_data["support_context"].get(chat_id)
        msg_text = update.message.text or "Контент без текста"
        
        user_name = "Неизвестный"
        if target_uuid:
            user = await db.get_user_by_uuid(target_uuid)
            if user: user_name = user['name']
            
            await db.execute("INSERT INTO support_tickets (user_uuid, message) VALUES ($1, $2)", target_uuid, msg_text)

        admin_text = f"🚨 <b>НОВОЕ ОБРАЩЕНИЕ В ПОДДЕРЖКУ</b> 🚨\n\n"
        admin_text += f"👤 Пользователь: <a href='tg://user?id={update.effective_user.id}'>{html.escape(update.effective_user.first_name)}</a>\n"
        admin_text += f"🔑 Ключ: <code>{html.escape(user_name)}</code>\n"
        admin_text += f"\n📝 <b>Описание проблемы:</b>\n<i>{html.escape(msg_text)}</i>"

        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode=ParseMode.HTML)
            await db.log_event("Support", f"Ticket from {user_name}: {msg_text[:50]}...")
            
            await context.bot.send_message(chat_id=chat_id, text="✅ **Ваше сообщение успешно отправлено!**\n\nОно зарегистрировано в системе. Администратор ответит вам в ближайшее время.", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text="❌ Ошибка отправки сообщения. Попробуйте позже.")

        context.user_data["state"] = None
        if chat_id in state_data["support_context"]:
            del state_data["support_context"][chat_id]
        
        await client_menu(update, context)
        return

    if not check_admin(update.effective_user.id): return
    await safe_delete(context, chat_id, user_msg_id)
    
    # ---------------- ОБРАБОТЧИКИ АДМИНА ----------------
    if state == "awaiting_schedule_time":
        time_str = update.message.text.strip()
        menu_id = context.user_data.get("menu_msg_id")
        
        try:
            dt = datetime.strptime(time_str, "%d.%m.%Y %H:%M")
            dt_db_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            await db.set_setting("scheduled_update", dt_db_str)
            
            kb = [[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]]
            text_success = f"✅ Обновление успешно запланировано на {time_str} (МСК).\nПользователи оповещены."
            if menu_id:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=menu_id, text=text_success, reply_markup=InlineKeyboardMarkup(kb))
            else:
                await context.bot.send_message(chat_id=chat_id, text=text_success, reply_markup=InlineKeyboardMarkup(kb))
            
            text_broadcast = f"⚙️ **Внимание!**\n\nЗапланировано техническое обновление системы.\n📅 Время: **{time_str} (МСК)**.\nВ этот период VPN может быть временно недоступен (1-2 минуты)."
            await broadcast_message(context.application, text_broadcast, db)
            
            context.user_data["state"] = None
        except ValueError:
            kb = [[InlineKeyboardButton("🔙 Отмена", callback_data="back_to_main")]]
            err_text = "❌ **Неверный формат!**\n\nПожалуйста, используйте формат: `ДД.ММ.ГГГГ ЧЧ:ММ`\nНапример: `15.05.2024 14:30`"
            if menu_id:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=menu_id, text=err_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return

    if state == "awaiting_support_reply":
        ticket_id = context.user_data.get("reply_ticket_id")
        reply_text = update.message.text.strip()
        menu_id = context.user_data.get("menu_msg_id")
        
        ticket = await db.fetch_all("SELECT user_uuid FROM support_tickets WHERE id=$1", int(ticket_id))
        if ticket:
            uuid_val = ticket[0]['user_uuid']
            user = await db.get_user_by_uuid(uuid_val)
            if user and user.get('tg_ids'):
                for tid in user['tg_ids']:
                    try:
                        await context.bot.send_message(chat_id=tid, text=f"🛠 **Ответ от Техподдержки:**\n\n_{escape_md(reply_text)}_", parse_mode="Markdown")
                    except: pass
            
            await db.execute("UPDATE support_tickets SET status='closed' WHERE id=$1", int(ticket_id))
            
            kb = [[InlineKeyboardButton("🔙 К списку обращений", callback_data="support_admin_menu")]]
            if menu_id:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=menu_id, text="✅ Ответ отправлен, обращение закрыто.", reply_markup=InlineKeyboardMarkup(kb))
        
        context.user_data["state"] = None
        return

    if state == "awaiting_name":
        # Транслит рус->лат + обрезка эмодзи, чтобы имя приняло приложение AmneziaWG
        name = sanitize_name(update.message.text)
        context.user_data["name"] = name
        menu_id = context.user_data.get("menu_msg_id")
        
        keyboard = [[InlineKeyboardButton("1 День", callback_data="set_exp_1"), InlineKeyboardButton("1 Неделя", callback_data="set_exp_7")],[InlineKeyboardButton("1 Месяц", callback_data="set_exp_30"), InlineKeyboardButton("Навсегда", callback_data="set_exp_0")],[InlineKeyboardButton("🔙 Отмена", callback_data="back_to_main")]]
        if menu_id: await context.bot.edit_message_text(chat_id=chat_id, message_id=menu_id, text=f"Имя: **{escape_md(name)}**\n\nВыберите срок действия ключа:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        context.user_data["state"] = "awaiting_expiry"

    elif state in["awaiting_tg_link_new_key", "awaiting_tg_link_existing"]:
        result = await extract_tg_id(update.message, context)
        
        menu_id = context.user_data.get("menu_msg_id")
        keyboard = [[InlineKeyboardButton("⏩ Пропустить/Отмена", callback_data="skip_tg_link")]]
        
        if result == "HIDDEN":
            text = "🔒 **Профиль пользователя скрыт!**\n\nTelegram не позволяет получить ID при пересылке сообщений от пользователей с настройками приватности.\n\n✅ **Решение:** Попросите пользователя прислать свой **Контакт** (📎 -> Контакт) и перешлите его сюда."
            if menu_id: await context.bot.edit_message_text(chat_id=chat_id, message_id=menu_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
            return
        
        if result == "INVALID":
            text = "❌ **Пользователь не найден**\n\nБот не может найти этого пользователя по @username. Возможно, он еще не запускал этого бота.\nПопросите ID или Контакт."
            if menu_id: await context.bot.edit_message_text(chat_id=chat_id, message_id=menu_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
            return

        if not result or not isinstance(result, int):
            text = "❌ **Не удалось определить ID**\n\nОтправьте:\n1. **Контакт** 📎 (лучший способ)\n2. Пересланное сообщение (если профиль открыт)\n3. Числовой ID\n4. @username (если юзер запускал бота)"
            if menu_id: await context.bot.edit_message_text(chat_id=chat_id, message_id=menu_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
            return

        if state == "awaiting_tg_link_new_key":
            await finish_key_creation(update, context, result)
        else:
            uuid_val = context.user_data.get("target_uuid")
            await db.link_user_telegram(uuid_val, result)
            context.user_data["state"] = None
            await context.bot.send_message(chat_id=chat_id, text=f"✅ Успешно привязан ID `{result}`!", parse_mode=ParseMode.MARKDOWN)
            if menu_id: await render_user_detail(context, chat_id, menu_id, uuid_val)
            
            try:
                await context.bot.send_message(
                    chat_id=result,
                    text="🎉 **Привет!** Администратор привязал этот Telegram-аккаунт к вашему VPN-ключу.\n\nТеперь вы можете управлять своим подключением прямо здесь.",
                    parse_mode=ParseMode.MARKDOWN
                )
                await send_client_menu(context, result)
                await context.bot.send_message(chat_id=chat_id, text=f"✅ Приветственное сообщение успешно отправлено пользователю `{result}`.")
            except Exception as e:
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Пользователь `{result}` не получил меню (возможно, он еще не запустил бота командой /start):\n`{e}`", parse_mode=ParseMode.MARKDOWN)

async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query.data not in ["start_dashboard", "vpn_graph", "show_online"]: 
        await stop_bg_tasks()

    query = update.callback_query
    data = query.data
    
    # Подменю управления серверами
    if data in ["menu_ru_server", "menu_de_server", "menu_backups"]:
        from handlers_admin import sub_menu_router
        await sub_menu_router(update, context)
        return
    
    # Роутер функций
    if data == "run_audit": await run_audit_handler(update, context); return
    
    if data == "support_start": await support_start_handler(update, context); return
    if data.startswith("support_audit_"): await support_run_audit_handler(update, context, data.split("support_audit_")[1]); return
    if data.startswith("support_ask_"): await support_ask_msg_handler(update, context, data.split("support_ask_")[1]); return

    if data == "client_menu":
        context.user_data["state"] = None
        await client_menu(update, context)
        return
        
    if data == "client_my_keys": await client_my_keys_handler(update, context); return
    if data.startswith("client_key_manage_"): await client_key_manage_handler(update, context, data.split("client_key_manage_")[1]); return
    if data == "client_regen_all": await client_regen_all_confirm_handler(update, context); return
    if data == "do_client_regen_all": await client_regen_all_action_handler(update, context); return
    
    if data == "client_select_check": await client_select_check_menu(update, context); return
    if data == "client_check_all": await client_check_all_handler(update, context); return
    if data.startswith("check_conn_"): await check_connection_handler(update, context, data.replace("check_conn_", "")); return
    
    if data == "client_stats": await client_stats_handler(update, context); return
    if data.startswith("client_download_"): await client_download_handler(update, context, data.split("client_download_")[1]); return
    
    if data.startswith("client_regen_"): await client_regen_confirm(update, context, data.split("client_regen_")[1]); return
    if data.startswith("do_client_regen_"): await client_regen_action(update, context, data.split("do_client_regen_")[1]); return

    if not check_admin(update.effective_user.id): return await query.answer("Доступ запрещен")

    if data == "back_to_main": await return_to_main_menu(update, context); return
    if data == "toggle_auto_update": await toggle_auto_update(update, context); return
    if data == "schedule_update": await schedule_update_menu(update, context); return
    
    if data == "de_confirm_reboot": await de_confirm_reboot(update, context); return
    if data == "do_de_reboot_server": await do_de_reboot_server(update, context); return
    if data == "de_read_logs": await de_read_logs(update, context); return
    if data == "de_update": await de_update(update, context); return
    if data == "de_backup": await de_backup(update, context); return
    if data == "de_run_audit": await de_run_audit(update, context); return
    
    if data == "maintenance_warn":
        kb = [[InlineKeyboardButton("✅ Отправить предупреждение", callback_data="do_maintenance_warn")],[InlineKeyboardButton("🔙 Отмена", callback_data="back_to_main")]]
        await query.edit_message_text("⚠️ Вы уверены, что хотите разослать всем пользователям предупреждение о тех. обслуживании?", reply_markup=InlineKeyboardMarkup(kb))
        return
    if data == "do_maintenance_warn":
        await broadcast_message(context.application, "⚠️ **Внимание!**\n\nВ настоящий момент проводится техническое обслуживание сервера.\nВозможны временные перебои в работе сервиса в течение ближайших часов.\nСпасибо за понимание!", db)
        await db.log_event("System", "Admin sent Maintenance broadcast.")
        await query.edit_message_text("✅ Предупреждение успешно разослано.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")]]))
        return
        
    if data == "support_admin_menu": await support_admin_menu(update, context); return
    if data.startswith("supp_usr_"): await support_user_tickets(update, context, data.split("supp_usr_")[1]); return
    if data.startswith("supp_tkt_"): await support_ticket_detail(update, context, data.split("supp_tkt_")[1]); return
    if data.startswith("supp_rep_"): await support_reply_start(update, context, data.split("supp_rep_")[1]); return
    if data.startswith("supp_clo_"): await support_close_ticket(update, context, data.split("supp_clo_")[1]); return

    if data == "skip_tg_link":
        if context.user_data.get("state") == "awaiting_tg_link_new_key":
            await finish_key_creation(update, context, None)
        else:
            uuid_val = context.user_data.get("target_uuid")
            await user_detail_menu(update, context, uuid_val)
        return
    
    if data.startswith("set_exp_"):
        context.user_data["expiry_days"] = int(data.split("_")[2])
        keyboard = [[InlineKeyboardButton("🌍 Классический DNS (1.1.1.1)", callback_data="set_dns_classic")],[InlineKeyboardButton("🛡 AdBlock DNS (Без рекламы)", callback_data="set_dns_adblock")],[InlineKeyboardButton("🔙 Отмена", callback_data="back_to_main")]]
        await query.edit_message_text("Выберите DNS-сервер:", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    if data.startswith("set_dns_"):
        context.user_data["dns_type"] = data.split("_")[2]
        keyboard = [[InlineKeyboardButton("⏩ Пропустить", callback_data="skip_tg_link")]]
        await query.edit_message_text("🔗 **Привязка Telegram**\n\nОтправьте:\n1️⃣ **Контакт** 📎 (Рекомендуется)\n2️⃣ @username\n3️⃣ ID", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        context.user_data["state"] = "awaiting_tg_link_new_key"
        return

    if data.startswith("users_page_"): await users_list_menu(update, context, int(data.split("_")[2])); return
    if data.startswith("user_detail_"): await user_detail_menu(update, context, data.split("user_detail_")[1]); return
    if data.startswith("clear_ips_"): await clear_user_ips(update, context, data.split("clear_ips_")[1]); return
        
    if data.startswith("link_tg_"):
        uuid_val = data.split("link_tg_")[1]
        keyboard = [[InlineKeyboardButton("🔙 Отмена", callback_data=f"user_detail_{uuid_val}")]]
        await query.edit_message_text("🔗 **Привязка Telegram**\n\nОтправьте:\n1️⃣ **Контакт** 📎\n2️⃣ @username\n3️⃣ ID", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        context.user_data["state"] = "awaiting_tg_link_existing"
        context.user_data["target_uuid"] = uuid_val
        context.user_data["menu_msg_id"] = query.message.message_id
        return
    if data.startswith("unlink_tg_"):
        uuid_val = data.split("unlink_tg_")[1]
        user = await db.get_user_by_uuid(uuid_val)
        keyboard = [[InlineKeyboardButton(f"❌ {tid}", callback_data=f"do_unlink_{uuid_val}_{tid}")] for tid in user.get('tg_ids',[])]
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=f"user_detail_{uuid_val}")])
        await query.edit_message_text("Выберите ID для удаления:", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    if data.startswith("do_unlink_"):
        parts = data.split("_")
        await db.unlink_user_telegram(parts[2], int(parts[3]))
        await query.answer("Успешно отвязан!")
        await user_detail_menu(update, context, parts[2])
        return

    if data.startswith("act_pause_"):
        uuid_val = data.split("act_pause_")[1]
        await query.answer("Заморозка...")
        await pause_peer(uuid_val)
        await db.execute("UPDATE users SET is_active=FALSE WHERE uuid=$1", uuid_val)
        await db.log_event("Pause", f"Manually paused key {uuid_val}")
        await user_detail_menu(update, context, uuid_val)
        return
    if data.startswith("act_resume_"):
        uuid_val = data.split("act_resume_")[1]
        await query.answer("Восстановление...")
        await resume_peer(uuid_val)
        await db.execute("UPDATE users SET is_active=TRUE WHERE uuid=$1", uuid_val)
        await db.log_event("Resume", f"Manually resumed key {uuid_val}")
        await user_detail_menu(update, context, uuid_val)
        return
    if data.startswith("confirm_delete_"): await confirm_delete_menu(update, context, data.split("confirm_delete_")[1]); return
    if data.startswith("do_delete_"): await action_delete_user(update, context, data.split("do_delete_")[1]); return
    if data.startswith("act_resend_"): await action_resend_config(update, context, data.split("act_resend_")[1]); return
    if data == "close_graph": await return_to_main_menu(update, context); return

    actions = {
        "start_dashboard": start_dashboard, "stop_dashboard": return_to_main_menu,
        "confirm_reboot": confirm_reboot, "do_reboot_server": do_reboot_server,
        "gen_key": generate_key_request, "vpn_graph": send_vpn_graph,
        "show_online": online_users_menu, "backup": backup_now, 
        "download_logs": download_logs, "restore": restore_cmd, 
        "check_update": check_update, "do_update": do_update,
        "show_users": lambda u, c: users_list_menu(u, c, 0), "export_excel": export_excel,
        "run_bypass_check": run_bypass_check_handler, "bypass_notify_now": bypass_notify_now_handler
    }
    
    if data in actions: await actions[data](update, context)
    else: await query.answer("...")

async def post_init(application):
    state_data.setdefault("bg_tasks", set())
    
    await sync_wg_config()
    
    tasks =[
        asyncio.create_task(alert_loop(application)),
        asyncio.create_task(cleanup_peers()),
        asyncio.create_task(stats_collector_loop()),
        asyncio.create_task(watch_online_count(application)),
        asyncio.create_task(check_update_completion(application)),
        asyncio.create_task(self_healing_loop(application)),
        asyncio.create_task(expiration_loop(application)),
        asyncio.create_task(inactivity_loop(application)),
        asyncio.create_task(weekly_report_loop(application)),
        asyncio.create_task(log_cleanup_loop(application)),
        asyncio.create_task(auto_backup_loop(application)),
        asyncio.create_task(auto_reboot_loop(application)),
        asyncio.create_task(scheduled_update_loop(application)),
        asyncio.create_task(resource_monitor_loop(application)),
        asyncio.create_task(routing_upgrade_loop(application))
    ]
    state_data["bg_tasks"].update(tasks)

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(db.connect())
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, restore_file_handler))
    app.add_handler(MessageHandler(~filters.COMMAND & ~filters.Document.ALL, handle_message))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_handler(CallbackQueryHandler(button_router))
    
    print("Бот успешно запущен (Dual Node System Enabled)...")
    app.run_polling()