import asyncio
import os
import tarfile
from datetime import datetime
import aiohttp
from telegram.ext import ApplicationBuilder
from database import db
from wireguard_manager import create_peer
from utils import BOT_TOKEN

ARCHIVE_PATH = "/volumes/backups/old_bkp.tar.gz"
DUMP_EXTRACT_PATH = "/tmp/old_db_dump.sql"

async def main():
    print("=== 🚀 УМНАЯ МИГРАЦИЯ (С ЧТЕНИЕМ СТАРОГО ДАМПА) ===")

    if not os.path.exists(ARCHIVE_PATH):
        print(f"❌ ОШИБКА: Файл {ARCHIVE_PATH} не найден!")
        print("Положите старый архив в папку volumes/backups/ под именем old_bkp.tar.gz")
        return

    print("📦 Извлекаем дамп старой БД из архива...")
    try:
        with tarfile.open(ARCHIVE_PATH, "r:gz") as tar:
            # Ищем db_dump.sql внутри архива
            dump_member = None
            for member in tar.getmembers():
                if member.name.endswith("db_dump.sql"):
                    dump_member = member
                    break
            
            if not dump_member:
                print("❌ ОШИБКА: Файл db_dump.sql не найден внутри архива!")
                return
            
            f = tar.extractfile(dump_member)
            with open(DUMP_EXTRACT_PATH, "wb") as out:
                out.write(f.read())
    except Exception as e:
        print(f"❌ Ошибка распаковки: {e}")
        return

    print("🔍 Анализируем старую базу данных...")
    old_users = []
    old_tg_links = []

    try:
        with open(DUMP_EXTRACT_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Парсим таблицу users
        in_users = False
        user_cols = []
        for line in lines:
            if line.startswith("COPY public.users "):
                in_users = True
                col_str = line[line.find("(")+1 : line.find(")")]
                user_cols = [c.strip() for c in col_str.split(",")]
                continue
            if in_users:
                if line.startswith("\\."):
                    in_users = False
                    continue
                vals = line.strip("\n").split("\t")
                old_users.append(dict(zip(user_cols, vals)))

        # Парсим таблицу user_tg_links
        in_links = False
        link_cols = []
        for line in lines:
            if line.startswith("COPY public.user_tg_links "):
                in_links = True
                col_str = line[line.find("(")+1 : line.find(")")]
                link_cols = [c.strip() for c in col_str.split(",")]
                continue
            if in_links:
                if line.startswith("\\."):
                    in_links = False
                    continue
                vals = line.strip("\n").split("\t")
                old_tg_links.append(dict(zip(link_cols, vals)))
    except Exception as e:
        print(f"❌ Ошибка парсинга SQL: {e}")
        return

    # Группируем пользователей и привязки
    users_map = {}
    for u in old_users:
        if u['name'] == 'DE_AGENT':
            continue
        exp = None
        if u.get('expires_at') and u['expires_at'] != '\\N':
            try:
                # Отбрасываем микросекунды, если они есть
                exp = datetime.strptime(u['expires_at'].split('.')[0], "%Y-%m-%d %H:%M:%S")
            except: pass
        
        users_map[u['uuid']] = {
            'name': u['name'],
            'expires_at': exp,
            'tg_ids': []
        }

    for l in old_tg_links:
        uuid_val = l['uuid']
        if uuid_val in users_map:
            users_map[uuid_val]['tg_ids'].append(int(l['tg_id']))

    print(f"✅ Найдено реальных пользователей для миграции: {len(users_map)}")

    ans = input("\nНачать генерацию новых ключей и массовую рассылку? (y/N): ")
    if ans.lower() != 'y':
        print("Отмена.")
        return

    await db.connect()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    print("\n🚀 Начинаем перевыпуск и рассылку...")
    for old_uuid, data in users_map.items():
        name = data['name']
        exp_at = data['expires_at']
        tg_ids = data['tg_ids']

        # Проверяем, не перенесли ли мы его уже (защита от дублей при повторном запуске)
        existing = await db.fetch_val("SELECT uuid FROM users WHERE name=$1", name)
        if existing:
            print(f"⏭ Пропуск {name} (Уже существует в текущей рабочей базе).")
            continue

        print(f"🔄 Создаем ключ для {name}...")
        try:
            # Создаем в рабочей среде
            new_uid, c_path, q_path = await create_peer(name, dns_type="classic")
            await db.execute("INSERT INTO users (name, uuid, created_at, expires_at) VALUES ($1, $2, NOW(), $3)", name, new_uid, exp_at)
            
            for tid in tg_ids:
                await db.link_user_telegram(new_uid, tid)
                try:
                    msg = "🔄 **Системное обновление!**\n\nАдминистратор перенес сервер на новую архитектуру обхода блокировок. Пожалуйста, удалите старый ключ из приложения AmneziaWG и добавьте этот новый файл:"
                    await app.bot.send_message(chat_id=tid, text=msg, parse_mode="Markdown")
                    await app.bot.send_document(chat_id=tid, document=open(c_path, "rb"))
                    print(f"  ✉️ Отправлено в Telegram (ID: {tid})")
                except Exception as e: 
                    print(f"  ❌ Ошибка отправки в TG: {e}")
        except Exception as e:
            print(f"❌ Ошибка генерации для {name}: {e}")

    print("\n🎉 Миграция успешно завершена! Рабочий туннель и текущая база не пострадали.")

if __name__ == "__main__":
    asyncio.run(main())