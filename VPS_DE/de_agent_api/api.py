import os
import subprocess
import psutil
import tarfile
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI()

CONF_DIR = "/etc/amnezia/amneziawg"
CONF_FILE = f"{CONF_DIR}/wg0.conf"
FLAGS_DIR = "/volumes/flags"

os.makedirs(CONF_DIR, exist_ok=True)
os.makedirs(FLAGS_DIR, exist_ok=True)

class ConfigData(BaseModel):
    config_text: str

def run_cmd(cmd):
    try:
        subprocess.run(cmd, shell=True, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.decode().strip() if e.stderr else str(e)
        raise RuntimeError(err_msg)

# ----------------- УПРАВЛЕНИЕ WIREGUARD -----------------

@app.post("/api/wg/config")
def update_wg_config(data: ConfigData):
    try:
        # ВАЖНО: Удаляем строку DNS, чтобы wg-quick не крашился в Docker (где нет resolvconf)
        cleaned_config = "\n".join([line for line in data.config_text.splitlines() if not line.strip().startswith("DNS")])
        
        with open(CONF_FILE, "w") as f:
            f.write(cleaned_config)
        return {"status": "success", "message": "Config saved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/wg/reload")
def reload_wg():
    try:
        # Мягко гасим старый интерфейс
        subprocess.run("wg-quick down wg0", shell=True, stderr=subprocess.DEVNULL)
        subprocess.run("ip link delete wg0", shell=True, stderr=subprocess.DEVNULL)
        
        if not os.path.exists(CONF_FILE):
            raise Exception("wg0.conf not found. Upload it first.")
        
        # Поднимаем туннель
        run_cmd("wg-quick up wg0")
        
        # Включаем NAT (Маскарадинг) для выпуска трафика из туннеля в интернет Германии
        run_cmd("iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE")
        
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/wg/status")
def wg_status():
    try:
        output = subprocess.check_output("wg show wg0", shell=True).decode().strip()
        return {"status": "online", "details": output}
    except Exception:
        return {"status": "offline", "details": "Interface wg0 is down"}

# ----------------- СИСТЕМНЫЙ МОНИТОРИНГ И УПРАВЛЕНИЕ -----------------

@app.get("/api/system_stats")
def get_system_stats():
    try:
        cpu = psutil.cpu_percent(interval=0.5)
        ram = psutil.virtual_memory().percent
        try: disk = psutil.disk_usage("/hostfs").percent
        except: disk = psutil.disk_usage("/").percent

        return {"cpu": cpu, "ram": ram, "disk": disk}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/logs")
def get_logs(lines: int = 50):
    try:
        # ИСПРАВЛЕНО: Читаем системные логи хоста через chroot
        log_output = subprocess.check_output(f"chroot /hostfs journalctl -n {lines} --no-pager", shell=True).decode()
        return {"status": "success", "logs": log_output}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/host/reboot")
def trigger_reboot():
    try:
        with open(f"{FLAGS_DIR}/do_reboot", "w") as f: f.write("reboot_requested")
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/host/update")
def trigger_update():
    """Сигнал демону хоста на запуск deploy.sh для Германии"""
    try:
        with open(f"{FLAGS_DIR}/do_update", "w") as f: f.write("update_requested")
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/host/audit")
def trigger_audit():
    try:
        with open(f"{FLAGS_DIR}/do_audit", "w") as f: f.write("audit_requested")
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/host/audit_result")
def get_audit_result():
    report_file = f"{FLAGS_DIR}/audit_report.json"
    status_file = f"{FLAGS_DIR}/audit_status"
    
    if os.path.exists(report_file):
        with open(report_file, "r") as f: return {"status": "done", "report": f.read()}
    elif os.path.exists(status_file):
        with open(status_file, "r") as f: return {"status": "running", "step": f.read().strip()}
    else:
        return {"status": "not_started"}

@app.get("/api/backup")
def download_backup():
    """Архивирует конфигурацию DE агента и отдает файл"""
    try:
        archive_path = "/tmp/de_backup.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            if os.path.exists(CONF_DIR):
                tar.add(CONF_DIR, arcname=os.path.basename(CONF_DIR))
        return FileResponse(path=archive_path, filename="de_agent_backup.tar.gz", media_type="application/gzip")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))