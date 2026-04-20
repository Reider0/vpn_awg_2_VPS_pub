import os
import subprocess
from pathlib import Path
import shutil
import aiohttp
from utils import get_moscow_now, WG_API_URL
from database import db

BACKUP_DIR = Path("/volumes/backups")
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_FILE = BACKUP_DIR / "backup_latest.tar.gz"
DB_URL = os.getenv("DATABASE_URL")

def create_backup():
    timestamp = get_moscow_now().strftime("%Y%m%d%H%M%S")
    temp_backup = BACKUP_DIR / f"backup_{timestamp}.tar.gz"
    db_dump = BACKUP_DIR / "db_dump.sql"

    try:
        if db_dump.exists(): db_dump.unlink()
        subprocess.run(f"pg_dump --clean --if-exists -O -x '{DB_URL}' > {db_dump}", shell=True, check=True)
        subprocess.run(f"tar -czf {temp_backup} -C /volumes wireguard configs backups/db_dump.sql", shell=True, check=True)

        if BACKUP_FILE.exists(): BACKUP_FILE.unlink()
        shutil.copy(temp_backup, BACKUP_FILE)
        
        db_dump.unlink()
        temp_backup.unlink()

        return str(BACKUP_FILE)
    except Exception as e:
        print(f"❌ Backup failed: {e}")
        if db_dump.exists(): db_dump.unlink()
        if temp_backup.exists(): temp_backup.unlink()
        raise e

async def restore_backup(path: str):
    print(f"♻️ Restoring from {path}...")
    subprocess.run(f"tar -xzf {path} -C /volumes/ --overwrite", shell=True, check=True)

    db_dump = BACKUP_DIR / "db_dump.sql"
    if db_dump.exists():
        print("♻️ Restoring Database...")
        if db.pool:
            await db.pool.close()
            db.pool = None
        try:
            subprocess.run(f"psql '{DB_URL}' < {db_dump}", shell=True, check=True)
        finally:
            await db.connect()
        db_dump.unlink()
    else:
        print("⚠️ Warning: db_dump.sql not found in backup archive.")

    print("♻️ Reloading WireGuard Interfaces...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{WG_API_URL}/reload", timeout=10) as resp:
                if resp.status == 200: print("✅ WireGuard reloaded successfully.")
                else: print(f"❌ WireGuard reload failed: {await resp.text()}")
    except Exception as e:
        print(f"❌ Connection to WG API failed during restore: {e}")