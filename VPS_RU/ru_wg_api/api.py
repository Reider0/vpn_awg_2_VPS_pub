import os
import uuid
import subprocess
import urllib.request
import re
import time
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()

# --- КОНФИГУРАЦИЯ ---
ENV_SERVER_URL = os.getenv("SERVER_URL") or os.getenv("SERVER_IP")
SERVER_PORT = int(os.getenv("SERVERPORT", "51820"))
VPN_SUBNET = os.getenv("INTERNAL_SUBNET", "10.13.13.0")

CONF_DIR = "/etc/amnezia/amneziawg" 
CONF_FILE = f"{CONF_DIR}/wg0.conf"
PRIVATE_KEY_FILE = f"{CONF_DIR}/private.key"
PUBLIC_KEY_FILE = f"{CONF_DIR}/public.key"

OBFUSCATION_PARAMS = (
    "Jc = 4\n"
    "Jmin = 40\n"
    "Jmax = 70\n"
    "S1 = 0\n"
    "S2 = 0\n"
    "H1 = 1\n"
    "H2 = 2\n"
    "H3 = 3\n"
    "H4 = 4\n"
)

if not os.path.exists(CONF_DIR):
    os.makedirs(CONF_DIR, exist_ok=True)

class PeerCreate(BaseModel):
    name: str
    dns_type: str = "classic"

class BackupData(BaseModel):
    conf: str
    priv: str
    pub: str

class GhostTarget(BaseModel):
    public_key: str
    purge_config: bool = True

def run_cmd(cmd):
    try:
        if isinstance(cmd, list):
            subprocess.run(cmd, check=True, capture_output=True)
        else:
            subprocess.run(cmd, shell=True, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.decode().strip() if e.stderr else str(e)
        raise RuntimeError(err_msg)

def get_public_ip():
    try:
        return urllib.request.urlopen('https://ifconfig.me/ip').read().decode('utf8').strip()
    except Exception:
        return "127.0.0.1"

if ENV_SERVER_URL and ENV_SERVER_URL != "0.0.0.0":
    FINAL_SERVER_IP = ENV_SERVER_URL
else:
    FINAL_SERVER_IP = get_public_ip()

def read_config_blocks():
    if not os.path.exists(CONF_FILE): return[]
    with open(CONF_FILE, 'r') as f: content = f.read()
    pattern = r"(?m)^(?=\[Interface\]|\[Peer\]|# PAUSED \[Peer\])"
    blocks = re.split(pattern, content)
    return [b for b in blocks if b.strip()]

def setup_network():
    print("🔧 Configuring AmneziaWG Interface (RU Master)...")
    
    if not os.path.exists(PRIVATE_KEY_FILE):
        print("🔑 Generating server keys...")
        priv = subprocess.check_output(["wg", "genkey"]).decode().strip()
        with open(PRIVATE_KEY_FILE, "w") as f: f.write(priv)
        proc = subprocess.Popen(["wg", "pubkey"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        pub, _ = proc.communicate(input=priv.encode())
        with open(PUBLIC_KEY_FILE, "w") as f: f.write(pub.decode().strip())

    with open(PRIVATE_KEY_FILE, "r") as f: priv_key = f.read().strip()

    interface_block = f"[Interface]\nPrivateKey = {priv_key}\nListenPort = {SERVER_PORT}\n{OBFUSCATION_PARAMS}\n"
    
    if os.path.exists(CONF_FILE):
        with open(CONF_FILE, "r") as f: current_conf = f.read()
        if "Jc =" not in current_conf or "[Interface]" not in current_conf:
            blocks = read_config_blocks()
            peer_blocks = [b for b in blocks if b.strip().startswith("[Peer]") or b.strip().startswith("# PAUSED")]
            with open(CONF_FILE, "w") as f: f.write(interface_block + "\n" + "\n".join(peer_blocks))
    else:
        with open(CONF_FILE, "w") as f: f.write(interface_block)

    subprocess.run(["ip", "link", "delete", "wg0"], stderr=subprocess.DEVNULL)
    subprocess.Popen(["wireguard-go", "wg0"])
    time.sleep(1)

    server_ip_cidr = f"{VPN_SUBNET.rsplit('.', 1)[0]}.1/24"
    run_cmd(["ip", "address", "add", server_ip_cidr, "dev", "wg0"])
    
    temp_conf = f"/tmp/wg0_init.conf"
    with open(temp_conf, "w") as f: f.write(interface_block)
    
    run_cmd(["wg", "setconf", "wg0", temp_conf])
    run_cmd(["ip", "link", "set", "mtu", "1280", "up", "dev", "wg0"])

    # --- ФИКС rp_filter ДЛЯ АСИММЕТРИЧНОЙ МАРШРУТИЗАЦИИ ---
    # Отключаем строгую проверку обратного пути, чтобы ядро не уничтожало
    # ответы из внешнего интернета, которые приходят через туннель wg0.
    subprocess.run("sysctl -w net.ipv4.conf.all.rp_filter=0", shell=True, stderr=subprocess.DEVNULL)
    subprocess.run("sysctl -w net.ipv4.conf.default.rp_filter=0", shell=True, stderr=subprocess.DEVNULL)
    subprocess.run("sysctl -w net.ipv4.conf.eth0.rp_filter=0", shell=True, stderr=subprocess.DEVNULL)
    subprocess.run("sysctl -w net.ipv4.conf.wg0.rp_filter=0", shell=True, stderr=subprocess.DEVNULL)

    # Очистка таблиц iptables и маршрутов
    run_cmd("iptables -t nat -F")
    run_cmd("iptables -t mangle -F")
    run_cmd("iptables -F")
    subprocess.run("ip rule del fwmark 200 table 200", shell=True, stderr=subprocess.DEVNULL)
    subprocess.run("ip route flush table 200", shell=True, stderr=subprocess.DEVNULL)

    # Базовая логика для хождения в интернет
    run_cmd("iptables -P FORWARD ACCEPT")
    # eth0 валиден внутри контейнера Docker
    run_cmd("iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE")
    
    # Маскарадинг для самого интерфейса wg0
    run_cmd("iptables -t nat -A POSTROUTING -o wg0 -j MASQUERADE")
    
    run_cmd("iptables -A FORWARD -i wg0 -j ACCEPT")
    run_cmd("iptables -A FORWARD -o wg0 -j ACCEPT")

    # --- УМНАЯ МАРШРУТИЗАЦИЯ И ИЗОЛЯЦИЯ ---
    
    # 1. Исключения: Локальные сети и Docker-сети не отправляем в Германию
    for subnet in ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]:
        run_cmd(f"iptables -t mangle -A PREROUTING -d {subnet} -j RETURN")
        run_cmd(f"iptables -t mangle -A OUTPUT -d {subnet} -j RETURN")

    # 2. ИСКЛЮЧЕНИЕ ПЕТЛИ: Не маркировать трафик, который пришел ИЗ Германии
    run_cmd("iptables -t mangle -A PREROUTING -i wg0 -s 10.13.13.254 -j RETURN")
    
    # 3. ИСКЛЮЧЕНИЕ ПЕТЛИ 2: Зашифрованный трафик самого WireGuard идет мимо туннеля
    run_cmd("iptables -t mangle -A OUTPUT -p udp --sport 51820 -j RETURN")
    run_cmd("iptables -t mangle -A OUTPUT -p udp --dport 51820 -j RETURN")

    # Гарантируем существование наборов (наполняются скриптом update_ru_ips.sh):
    #   ru_nets      — гео-IP РФ (идут напрямую)
    #   blocked_nets — блокировки РКН (antifilter, принудительно в Германию)
    subprocess.run("ipset create ru_nets hash:net family inet hashsize 4096 maxelem 1000000 -exist", shell=True, stderr=subprocess.DEVNULL)
    subprocess.run("ipset create blocked_nets hash:net family inet hashsize 4096 maxelem 1000000 -exist", shell=True, stderr=subprocess.DEVNULL)

    # 3.5 ПРИНУДИТЕЛЬНЫЙ ОБХОД: заблокированные РКН ресурсы всегда уходят в Германию,
    #     даже если они размещены на российских IP (повышает качество обхода блокировок).
    #     Делаем НЕфатально: если blocked_nets недоступен, узел всё равно поднимется
    #     на базовой гео-логике, а не уйдёт в полный отказ.
    subprocess.run("iptables -t mangle -A PREROUTING -i wg0 -m set --match-set blocked_nets dst -j MARK --set-mark 200", shell=True, stderr=subprocess.DEVNULL)
    subprocess.run("iptables -t mangle -A OUTPUT -m set --match-set blocked_nets dst -j MARK --set-mark 200", shell=True, stderr=subprocess.DEVNULL)

    # 4. Маркируем трафик клиентов (VPN) для отправки в Германию, если это не РУ-сегмент
    run_cmd("iptables -t mangle -A PREROUTING -i wg0 -m set ! --match-set ru_nets dst -j MARK --set-mark 200")

    # 5. Маркируем локальный трафик бота (который живет в одной сети с API)
    run_cmd("iptables -t mangle -A OUTPUT -m set ! --match-set ru_nets dst -j MARK --set-mark 200")
    
    # Создаем отдельную таблицу маршрутизации
    subprocess.run("ip rule add fwmark 200 table 200", shell=True, stderr=subprocess.DEVNULL)
    subprocess.run("ip route add default dev wg0 table 200", shell=True, stderr=subprocess.DEVNULL)

    # БЕЗОПАСНОСТЬ: Закрываем порт API (8000) от внешнего мира
    run_cmd("iptables -A INPUT -i eth0 -p tcp --dport 8000 -j DROP")

    restore_peers()

def restore_peers():
    if not os.path.exists(CONF_FILE): return
    try:
        blocks = read_config_blocks()
        with open("/tmp/wg0_restore.conf", "w") as f:
            for b in blocks:
                if b.strip().startswith("[Peer]"): f.write(b)
        run_cmd(["wg", "addconf", "wg0", "/tmp/wg0_restore.conf"])
    except Exception as e:
        print(f"Restore warning: {e}")

setup_network()

def get_server_pubkey():
    if os.path.exists(PUBLIC_KEY_FILE):
        with open(PUBLIC_KEY_FILE, "r") as f: return f.read().strip()
    return "UNKNOWN"

def get_next_ip():
    used_ips = set(["1", "254"]) # Резервируем .1 (RU) и .254 (DE)
    blocks = read_config_blocks()
    for b in blocks:
        ip_match = re.search(r"AllowedIPs\s*=\s*[\d\.]+\.(\d+)/32", b)
        if ip_match: used_ips.add(ip_match.group(1))

    for i in range(2, 253):
        if str(i) not in used_ips:
            base = VPN_SUBNET.rsplit('.', 1)[0]
            return f"{base}.{i}"
    raise Exception("IP Limit Reached")

# --- API ENDPOINTS ---

@app.get("/api/backup_config")
def get_backup_config():
    try:
        conf, priv, pub = "", "", ""
        if os.path.exists(CONF_FILE):
            with open(CONF_FILE, "r") as f: conf = f.read()
        if os.path.exists(PRIVATE_KEY_FILE):
            with open(PRIVATE_KEY_FILE, "r") as f: priv = f.read()
        if os.path.exists(PUBLIC_KEY_FILE):
            with open(PUBLIC_KEY_FILE, "r") as f: pub = f.read()
        return {"wg0.conf": conf, "private.key": priv, "public.key": pub}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/restore_config")
def restore_backup_config(data: BackupData):
    try:
        with open(CONF_FILE, "w") as f: f.write(data.conf)
        with open(PRIVATE_KEY_FILE, "w") as f: f.write(data.priv)
        with open(PUBLIC_KEY_FILE, "w") as f: f.write(data.pub)
        setup_network()
        return {"status": "restored"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/health")
def health_check():
    try:
        subprocess.run(["ip", "link", "show", "wg0"], check=True, capture_output=True)
        return {"status": "ok", "role": "RU_Master"}
    except Exception:
        raise HTTPException(status_code=500, detail="Interface wg0 is down")

@app.get("/api/status")
def status():
    try:
        output = subprocess.check_output(["wg", "show", "wg0", "dump"]).decode().strip().split('\n')
        total_peers, active_peers = 0, 0
        now = int(time.time())

        blocks = read_config_blocks()
        for b in blocks:
            if b.strip().startswith("[Peer]"): total_peers += 1

        if len(output) > 1:
            for line in output[1:]:
                parts = line.split('\t')
                if len(parts) >= 5:
                    try:
                        handshake = int(parts[4])
                        if handshake > 0 and (now - handshake) < 180: active_peers += 1
                    except ValueError: pass
        return {"peers_count": total_peers, "active_peers": active_peers, "status": "ok"}
    except Exception as e:
        return {"peers_count": 0, "active_peers": 0, "status": "error", "detail": str(e)}

@app.get("/api/peers")
def get_peers():
    try:
        output = subprocess.check_output(["wg", "show", "wg0", "dump"]).decode().strip().split('\n')
        pubkey_to_uuid = {}
        
        blocks = read_config_blocks()
        for b in blocks:
            uuid_match = re.search(r"# UUID = (\S+)", b)
            pub_match = re.search(r"PublicKey\s*=\s*(\S+)", b)
            if uuid_match and pub_match:
                pubkey_to_uuid[pub_match.group(1)] = uuid_match.group(1)

        peers =[]
        if len(output) > 1:
            for line in output[1:]:
                parts = line.split('\t')
                if len(parts) >= 7:
                    pubkey, endpoint = parts[0], parts[2]
                    handshake = int(parts[4]) if parts[4].isdigit() else 0
                    rx, tx = int(parts[5]) if parts[5].isdigit() else 0, int(parts[6]) if parts[6].isdigit() else 0
                    peers.append({
                        "uuid": pubkey_to_uuid.get(pubkey, pubkey),
                        "public_key": pubkey,
                        "endpoint": endpoint,
                        "latest_handshake": handshake,
                        "rx": rx, "tx": tx
                    })
        return peers
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/reload")
def reload_vpn():
    try:
        setup_network()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/peers")
def create_peer(req: PeerCreate):
    try:
        priv_key = subprocess.check_output(["wg", "genkey"]).decode().strip()
        proc = subprocess.Popen(["wg", "pubkey"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        pub_key, _ = proc.communicate(input=priv_key.encode())
        pub_key = pub_key.decode().strip()
        
        server_pub = get_server_pubkey()
        
        is_de_agent = (req.name == "DE_AGENT")
        client_ip = "10.13.13.254" if is_de_agent else get_next_ip()
        uid = str(uuid.uuid4())

        target_dns = "94.140.14.14, 94.140.15.15" if req.dns_type == "adblock" else "1.1.1.1, 1.0.0.1"

        client_allowed_ips = "10.13.13.0/24" if is_de_agent else "0.0.0.0/0, ::/0"

        server_allowed_ips = "0.0.0.0/0, 10.13.13.254/32" if is_de_agent else f"{client_ip}/32"

        config_content = f"""
[Interface]
PrivateKey = {priv_key}
Address = {client_ip}/32
DNS = {target_dns}
MTU = 1280
{OBFUSCATION_PARAMS}

[Peer]
PublicKey = {server_pub}
Endpoint = {FINAL_SERVER_IP}:{SERVER_PORT}
AllowedIPs = {client_allowed_ips}
PersistentKeepalive = 25"""

        peer_block = f"\n[Peer]\n# Name = {req.name}\n# UUID = {uid}\nPublicKey = {pub_key}\nAllowedIPs = {server_allowed_ips}\n"
        
        with open(CONF_FILE, "a") as f: f.write(peer_block)
        run_cmd(["wg", "set", "wg0", "peer", pub_key, "allowed-ips", server_allowed_ips])

        return {"uid": uid, "config": config_content, "client_ip": client_ip}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/kill_ghost")
def kill_ghost(target: GhostTarget):
    try:
        run_cmd(["wg", "set", "wg0", "peer", target.public_key, "remove"])
        if target.purge_config:
            blocks = read_config_blocks()
            new_blocks =[]
            for b in blocks:
                if (b.strip().startswith("[Peer]") or b.strip().startswith("# PAUSED")) and f"PublicKey = {target.public_key}" in b:
                    continue 
                new_blocks.append(b)
            with open(CONF_FILE, 'w') as f: f.write("".join(new_blocks))
        return {"status": "killed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/peers/{uid}/pause")
def pause_peer(uid: str):
    try:
        blocks = read_config_blocks()
        new_blocks =[]
        found = False
        for b in blocks:
            if f"# UUID = {uid}" in b:
                found = True
                if b.strip().startswith("# PAUSED"):
                    new_blocks.append(b)
                    continue
                pub_match = re.search(r"PublicKey\s*=\s*(\S+)", b)
                if pub_match:
                    try: run_cmd(["wg", "set", "wg0", "peer", pub_match.group(1).strip(), "remove"])
                    except: pass
                paused_b = "\n".join([f"# PAUSED {line}" if line.strip() else line for line in b.splitlines()]) + "\n"
                new_blocks.append(paused_b)
            else:
                new_blocks.append(b)

        if not found: raise HTTPException(status_code=404, detail="Peer not found")
        with open(CONF_FILE, "w") as f: f.write("".join(new_blocks))
        return {"status": "paused"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/peers/{uid}/resume")
def resume_peer(uid: str):
    try:
        blocks = read_config_blocks()
        new_blocks =[]
        found = False
        for b in blocks:
            if f"# UUID = {uid}" in b:
                found = True
                if not b.strip().startswith("# PAUSED"):
                    new_blocks.append(b)
                    continue
                active_b = "\n".join([line.replace("# PAUSED ", "", 1) for line in b.splitlines()]) + "\n"
                new_blocks.append(active_b)
                pub_match = re.search(r"PublicKey\s*=\s*(\S+)", active_b)
                ip_match = re.search(r"AllowedIPs\s*=\s*(\S+)", active_b)
                if pub_match and ip_match:
                    try: run_cmd(["wg", "set", "wg0", "peer", pub_match.group(1).strip(), "allowed-ips", ip_match.group(1).strip()])
                    except: pass
            else:
                new_blocks.append(b)

        if not found: raise HTTPException(status_code=404, detail="Paused peer not found")
        with open(CONF_FILE, "w") as f: f.write("".join(new_blocks))
        return {"status": "resumed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/peers/{uid}")
def delete_peer(uid: str):
    try:
        blocks = read_config_blocks()
        new_blocks =[]
        found = False
        for b in blocks:
            if f"# UUID = {uid}" in b:
                found = True
                pub_match = re.search(r"PublicKey\s*=\s*(\S+)", b)
                if pub_match:
                    try: run_cmd(["wg", "set", "wg0", "peer", pub_match.group(1).strip(), "remove"])
                    except: pass
                continue 
            new_blocks.append(b)

        if not found: raise HTTPException(status_code=404, detail="Peer not found")
        with open(CONF_FILE, "w") as f: f.write("".join(new_blocks))
        return {"status": "deleted"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))