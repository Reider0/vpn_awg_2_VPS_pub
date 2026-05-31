import asyncio
import os
import shutil
from database import db
from wireguard_manager import create_peer
from utils import ROUTING_VERSION

async def main():
    print("Подключение к базе данных...")
    await db.connect()
    
    existing = await db.fetch_all("SELECT uuid FROM users WHERE name='DE_AGENT'")
    if existing:
        print("✅ Ключ DE_AGENT уже существует в базе (Пропуск).")
        return

    print("⏳ Генерация ключа DE_AGENT через API...")
    try:
        new_uid, c_path, q_path = await create_peer("DE_AGENT", dns_type="classic")
        await db.execute("INSERT INTO users (name, uuid, created_at, routing_version) VALUES ($1, $2, NOW(), $3)", "DE_AGENT", new_uid, ROUTING_VERSION)
        dest = "/volumes/DE_AGENT_CONFIG.txt"
        shutil.copy(c_path, dest)
        print(f"✅ Ключ DE_AGENT успешно создан и сохранен в {dest}.")
    except Exception as e:
        print(f"❌ Ошибка генерации DE_AGENT: {e}")

if __name__ == "__main__":
    asyncio.run(main())