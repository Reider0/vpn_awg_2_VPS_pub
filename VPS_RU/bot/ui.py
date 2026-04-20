from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def main_menu(active_count=0, support_count=0):
    keyboard = [
        [
            InlineKeyboardButton("📊 Дашборд (RU+DE)", callback_data="start_dashboard"),
            InlineKeyboardButton("📈 График трафика", callback_data="vpn_graph")
        ],
        [
            InlineKeyboardButton("🔑 Создать ключ", callback_data="gen_key"),
            InlineKeyboardButton(f"🟢 Онлайн [{active_count}]", callback_data="show_online")
        ],
        [
            InlineKeyboardButton("👥 Пользователи", callback_data="users_page_0"),
            InlineKeyboardButton(f"🆘 Поддержка [{support_count}]", callback_data="support_admin_menu")
        ],
        [
            InlineKeyboardButton("🇷🇺 Управление RU", callback_data="menu_ru_server"),
            InlineKeyboardButton("🇩🇪 Управление DE", callback_data="menu_de_server")
        ],
        [
            InlineKeyboardButton("💾 Бэкапы и База", callback_data="menu_backups"),
            InlineKeyboardButton("👤 Режим Клиента", callback_data="client_menu")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def menu_ru_server():
    keyboard = [
        [InlineKeyboardButton("🛠 Глобальный Аудит RU", callback_data="run_audit"),
         InlineKeyboardButton("🔄 Обновить систему", callback_data="check_update")],
        [InlineKeyboardButton("🚨 Перезагрузить Сервер", callback_data="confirm_reboot")],
        [InlineKeyboardButton("⚠️ Режим Тех. Работ", callback_data="maintenance_warn")],
        [InlineKeyboardButton("🔙 Назад в главное меню", callback_data="back_to_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def menu_de_server():
    keyboard = [
        [InlineKeyboardButton("🛠 Аудит Агента DE", callback_data="de_run_audit"),
         InlineKeyboardButton("🔄 Обновить Агента", callback_data="de_update")],
        [InlineKeyboardButton("📑 Системные Логи", callback_data="de_read_logs"),
         InlineKeyboardButton("🚨 Перезагрузить Сервер", callback_data="de_confirm_reboot")],
        [InlineKeyboardButton("🔙 Назад в главное меню", callback_data="back_to_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def menu_backups():
    keyboard = [
        [InlineKeyboardButton("💾 Бэкап RU (Мастер)", callback_data="backup"),
         InlineKeyboardButton("♻️ Восстановить RU", callback_data="restore")],
        [InlineKeyboardButton("💾 Бэкап DE (Агент)", callback_data="de_backup")],
        [InlineKeyboardButton("📝 Логи сети (Excel)", callback_data="download_logs"),
         InlineKeyboardButton("📊 Выгрузка БД (Excel)", callback_data="export_excel")],
        [InlineKeyboardButton("🔙 Назад в главное меню", callback_data="back_to_main")]
    ]
    return InlineKeyboardMarkup(keyboard)