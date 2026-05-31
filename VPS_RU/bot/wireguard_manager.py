import os
import qrcode
import aiohttp
import json
from pathlib import Path
from database import db
from utils import WG_API_URL

CONFIGS_DIR = Path("/volumes/configs")
CONFIGS_DIR.mkdir(parents=True, exist_ok=True)

async def backup_wg_config():
    """Сохраняет резервную копию wg0.conf и ключей сервера в базу данных."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{WG_API_URL}/backup_config", timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    await db.set_setting("wg_config_backup", json.dumps(data))
    except Exception as e:
        print(f"Error backing up wg config: {e}")

async def create_peer(name: str, dns_type: str = "classic"):
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{WG_API_URL}/peers", json={"name": name, "dns_type": dns_type}) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise Exception(f"Ошибка VPN-сервера: {error_text}")
            data = await resp.json()

    uid = data["uid"]
    # Нормализуем: конфиг должен начинаться строго с [Interface] (без пустой строки),
    # иначе строгий парсер AmneziaWG считает файл некорректным.
    config_full = data["config"].strip() + "\n"

    conf_path = CONFIGS_DIR / f"{name}.conf"
    qr_path = CONFIGS_DIR / f"{name}.png"
    
    with open(conf_path, "w") as f: f.write(config_full)
    # ECC=L даёт максимум вместимости: split-tunnel конфиг крупнее (исключения в
    # AllowedIPs), и при ECC=M он может не влезть в сканируемый QR.
    qrcode.make(config_full, error_correction=qrcode.constants.ERROR_CORRECT_L).save(qr_path)
    
    await backup_wg_config()
    
    return uid, str(conf_path), str(qr_path)

async def pause_peer(uuid: str):
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{WG_API_URL}/peers/{uuid}/pause") as resp:
            if resp.status != 200: raise Exception("Ошибка API паузы")
    await backup_wg_config()

async def resume_peer(uuid: str):
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{WG_API_URL}/peers/{uuid}/resume") as resp:
            if resp.status != 200: raise Exception("Ошибка API возобновления")
    await backup_wg_config()

async def delete_peer(uuid: str, name: str):
    async with aiohttp.ClientSession() as session:
        async with session.delete(f"{WG_API_URL}/peers/{uuid}") as resp:
            if resp.status not in [200, 404]:
                error_text = await resp.text()
                raise Exception(f"Ошибка удаления API: {error_text}")

    try:
        if (CONFIGS_DIR / f"{name}.conf").exists(): (CONFIGS_DIR / f"{name}.conf").unlink()
        if (CONFIGS_DIR / f"{name}.png").exists(): (CONFIGS_DIR / f"{name}.png").unlink()
        if (CONFIGS_DIR / f"{name}_Full.conf").exists(): (CONFIGS_DIR / f"{name}_Full.conf").unlink()
        if (CONFIGS_DIR / f"{name}_Full.png").exists(): (CONFIGS_DIR / f"{name}_Full.png").unlink()
        if (CONFIGS_DIR / f"{name}_Smart.conf").exists(): (CONFIGS_DIR / f"{name}_Smart.conf").unlink()
        if (CONFIGS_DIR / f"{name}_Smart.png").exists(): (CONFIGS_DIR / f"{name}_Smart.png").unlink()
    except Exception as e:
        print(f"Ошибка удаления файлов: {e}")
        
    await backup_wg_config()