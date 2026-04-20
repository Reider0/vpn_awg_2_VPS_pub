import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from database import db
import aiohttp
from utils import get_moscow_now, dt_to_moscow, WG_API_URL

async def generate_vpn_graph():
    stats = await db.get_stats_24h()
    live_data = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{WG_API_URL}/peers", timeout=3) as resp:
                if resp.status == 200:
                    peers = await resp.json()
                    for p in peers: live_data[p.get('uuid')] = p.get('rx', 0) + p.get('tx', 0)
    except Exception as e:
        print(f"Ошибка получения Live-данных: {e}")
    
    user_plot_data = {}
    prev_totals = {}
    
    for row in stats:
        uname, uuid_val, t = row['name'], row['user_uuid'], dt_to_moscow(row['last_seen'])
        total_bytes = row['bytes_in'] + row['bytes_out']
        
        if uuid_val not in user_plot_data:
            user_plot_data[uuid_val] = {'name': uname, 'times': [], 'deltas':[], 'last_total': 0}
            prev_totals[uuid_val] = total_bytes
        else:
            delta = total_bytes - prev_totals[uuid_val]
            if delta < 0: delta = total_bytes 
            prev_totals[uuid_val] = total_bytes
            
            user_plot_data[uuid_val]['times'].append(t)
            user_plot_data[uuid_val]['deltas'].append(delta / (1024 * 1024))
            user_plot_data[uuid_val]['last_total'] = total_bytes

    now = get_moscow_now()
    for uuid_val, current_total in live_data.items():
        if uuid_val in user_plot_data and len(user_plot_data[uuid_val]['times']) > 0:
            last_total = user_plot_data[uuid_val]['last_total']
            delta = current_total - last_total
            if delta < 0: delta = current_total
            
            user_plot_data[uuid_val]['times'].append(now)
            user_plot_data[uuid_val]['deltas'].append(delta / (1024 * 1024))

    plt.figure(figsize=(10, 5))
    has_data = any(len(data['times']) > 0 for data in user_plot_data.values())
            
    if not has_data:
        plt.text(0.5, 0.5, "Недостаточно данных для графика.\nОжидаем синхронизации... (1-5 минут)", ha='center', va='center', fontsize=12, color='gray')
        plt.title(f"Live Traffic МСК (Updated: {now.strftime('%H:%M:%S')})")
        plt.xticks([])
        plt.yticks([])
    else:
        for uuid_val, data in user_plot_data.items():
            if data['times']:
                plt.plot(data['times'], data['deltas'], label=data['name'], marker='o', markersize=4)
        
        plt.title(f"Live Traffic Dynamics МСК - MB (Updated: {now.strftime('%H:%M:%S')})")
        plt.xlabel("Time (MSK)")
        plt.ylabel("Traffic Load (MB)")
        plt.legend(loc='upper left', bbox_to_anchor=(1, 1))
        plt.grid(True, linestyle='--', alpha=0.7)
        plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        plt.xticks(rotation=45)
    
    plt.tight_layout()
    path = "/volumes/backups/vpn_graph.png"
    plt.savefig(path)
    plt.close('all')
    return path