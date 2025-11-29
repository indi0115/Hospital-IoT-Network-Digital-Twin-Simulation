import json
import os
import random
import subprocess
import sys
import uuid
import zmq
import zmq.asyncio
import asyncio
import time
from aiohttp import web
from datetime import datetime
from collections import deque, defaultdict
from typing import Dict, List, Optional

# Initialize globals
ROOT = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(ROOT, "logs")
os.makedirs(LOGS, exist_ok=True)

METRIC_FILE = os.path.join(LOGS, "metrics.json")
DEV_SIM = os.path.join(ROOT, "device_stimulator.py")

# NEW: Hospital Topology Zones
HOSPITAL_ZONES = {
    "ICU": ["icu-heart1", "icu-o2-1", "icu-temp1", "icu-bp1"],
    "OR": ["or-surgery1", "or-monitor1", "or-camera1"],
    "Ward": ["ward-bed1", "ward-bed2", "ward-nurse1"]
}

# Congestion control variables
PACKET_DROPS = deque(maxlen=1000)
CONGESTION_DROP_THRESHOLD = 20
CONGESTION_WINDOW = 30
THROTTLED_PRIORITIES = {4, 5}

# NEW: Global state for manual actions/logs
manual_actions_log = deque(maxlen=100)
MANUAL_RETRANSMIT_SIM_AMOUNT = 50 # How many packets to 'recover' on manual retransmit

# NEW: Manual Congestion Override Flag
MANUAL_CONGESTION_OVERRIDE = False # <--- NEW GLOBAL FLAG

# NEW: Latency/Jitter tracking per device
device_latencies = defaultdict(list)  # device -> list of recent latencies
device_jitter_history = {}  # device -> recent jitter values
MAX_LATENCY_SAMPLES = 10

# Global state variables for latency and packet counts per protocol
protocol_latency_sum = {"HTTP": 0.0, "TCP": 0.0, "ZMQ": 0.0}
protocol_counts = {"HTTP": 0, "TCP": 0, "ZMQ": 0}
protocol_latency_sum_congested = {"HTTP": 0.0, "TCP": 0.0, "ZMQ": 0.0}
protocol_counts_congested = {"HTTP": 0, "TCP": 0, "ZMQ": 0}

# NEW: Protocol-specific alerts tracking
protocol_alerts = {"HTTP": 0, "TCP": 0, "ZMQ": 0}

# ✅ ARQ RETRANSMISSION TRACKING
delivered_sequences = defaultdict(set)  # device_id -> set of delivered seq numbers
retransmitted_total = 0
retransmitted_by_device = defaultdict(int)

total_lost = 0
student_devices = []
simulation_duration = 60
sim_process = None
IS_RUNNING = False
IS_CONGESTED = False
dashboard_ws_clients = set()
registered_devices = {}
current_throttled_devices = set()
last_sent_time = {}
devices_slowed_flag = {}

# NEW: Device failure/compromise states
failed_devices = set()
dos_attacking_devices = set()
device_zone_mapping = {}  # device_id -> zone

BASE_LATENCY = 20.0
CONGESTION_EXTRA = 150.0
DROP_LOW = 0.05
DROP_HIGH = 0.40

def safe_now_iso():
    return datetime.now().isoformat()

def record_packet_drop(device_id):
    now = time.time()
    PACKET_DROPS.append((device_id, now))

def is_congested_check():
    now = time.time()
    recent_drops = [stamp for _, stamp in PACKET_DROPS if now - stamp < CONGESTION_WINDOW]
    return len(recent_drops) >= CONGESTION_DROP_THRESHOLD

# FIXED: Now respects MANUAL_CONGESTION_OVERRIDE
def update_throttling():
    global current_throttled_devices, IS_CONGESTED
    
    if MANUAL_CONGESTION_OVERRIDE:
        # If manually overridden, enforce the current IS_CONGESTED state
        if IS_CONGESTED:
            throttled = {dev_id for dev_id, prio in registered_devices.items() if prio in THROTTLED_PRIORITIES}
            current_throttled_devices = throttled
        else:
            current_throttled_devices.clear()
        return

    # Dynamic Congestion Check (Original logic)
    if is_congested_check():
        throttled = {dev_id for dev_id, prio in registered_devices.items() if prio in THROTTLED_PRIORITIES}
        current_throttled_devices = throttled
        IS_CONGESTED = True
    else:
        current_throttled_devices.clear()
        IS_CONGESTED = False

# NEW: Latency/Jitter calculation
def calculate_jitter(latencies: List[float]) -> float:
    if len(latencies) < 2:
        return 0.0
    return round(max(latencies) - min(latencies), 2)

def update_device_latency(device_id: str, latency: float):
    device_latencies[device_id].append(latency)
    if len(device_latencies[device_id]) > MAX_LATENCY_SAMPLES:
        device_latencies[device_id].pop(0)
    device_jitter_history[device_id] = calculate_jitter(device_latencies[device_id])

# NEW: Zone status calculation
def get_zone_status() -> Dict[str, Dict]:
    zone_status = {}
    for zone, devices in HOSPITAL_ZONES.items():
        zone_devices_active = sum(1 for d in devices if d not in failed_devices and d not in dos_attacking_devices)
        zone_congested = any(d in current_throttled_devices for d in devices)
        zone_status[zone] = {
            "total_devices": len(devices),
            "active_devices": zone_devices_active,
            "congested": zone_congested,
            "health": "HEALTHY" if zone_devices_active == len(devices) else "DEGRADED"
        }
    return zone_status

async def broadcast_to_dashboard(msg: dict):
    if not dashboard_ws_clients:
        return
    text = json.dumps(msg)
    dead = set()
    for ws in dashboard_ws_clients:
        try:
            await ws.send_str(text)
        except Exception:
            dead.add(ws)
    dashboard_ws_clients.difference_update(dead)

def compute_current_metrics():
    zone_status = get_zone_status()
    
    metrics = {
        "HTTP_avg_latency": round(protocol_latency_sum["HTTP"] / max(protocol_counts["HTTP"], 1), 2),
        "TCP_avg_latency": round(protocol_latency_sum["TCP"] / max(protocol_counts["TCP"], 1), 2),
        "ZMQ_avg_latency": round(protocol_latency_sum["ZMQ"] / max(protocol_counts["ZMQ"], 1), 2),
        "total_sent": sum(protocol_counts.values()) + sum(protocol_counts_congested.values()) + total_lost,
        "total_received": sum(protocol_counts.values()) + sum(protocol_counts_congested.values()),
        "total_lost": total_lost,
        "retransmitted_total": retransmitted_total,
        "retransmitted_by_device": dict(retransmitted_by_device),
        "congested": IS_CONGESTED,
        "throttled_devices": list(current_throttled_devices),
        "failed_devices": list(failed_devices),
        "dos_devices": list(dos_attacking_devices),
        "zone_status": zone_status,
        "protocol_alerts": protocol_alerts,
        "manual_logs": list(manual_actions_log)
    }
    return metrics

def save_metrics_file(metrics: dict):
    try:
        with open(METRIC_FILE, "w", encoding="utf-8") as f:
            json.dump(metrics, f, indent=2)
        print(f"[INFO] Metrics saved to {METRIC_FILE}")
    except Exception as e:
        print(f"[WARN] Could not save metrics file: {e}")

async def metrics_broadcaster():
    print("[METRICS] Broadcaster task started")
    while IS_RUNNING:
        await asyncio.sleep(2)
        metrics = compute_current_metrics()
        await broadcast_to_dashboard({"type": "METRICS_UPDATE", "metrics": metrics, "timestamp": safe_now_iso()})

async def congestion_monitor_loop():
    print("[CONGESTION] Monitor started")
    while IS_RUNNING:
        # update_throttling now respects MANUAL_CONGESTION_OVERRIDE
        update_throttling() 
        await broadcast_to_dashboard({
            "type": "CONGESTION_UPDATE",
            "congested": IS_CONGESTED,
            "throttled_devices": list(current_throttled_devices),
            "timestamp": safe_now_iso()
        })
        await asyncio.sleep(3)

async def ping_handler(request):
    return web.json_response({"status": "ok", "time": safe_now_iso()})

async def configure_handler(request):
    global student_devices, simulation_duration
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON"}, status=400)

    simulation_duration = int(body.get("duration", simulation_duration))
    incoming = body.get("devices", [])
    normalized = []

    for idx, d in enumerate(incoming):
        dev_id = d.get("id") or d.get("device_id") or d.get("name") or f"dev-{idx+1}"
        dev_type = d.get("type") or d.get("name") or f"device-{idx+1}"
        protocol = d.get("protocol") or d.get("proto") or "HTTP"
        priority = int(d.get("priority", 5))
        unit = d.get("unit", "")
        zone = d.get("zone", "Ward")
        r = d.get("range")

        if isinstance(r, (list, tuple)) and len(r) >= 2:
            try:
                rmin, rmax = float(r[0]), float(r[1])
            except Exception:
                rmin, rmax = 1.0, 10.0
        else:
            try:
                rmin = float(d.get("min", 1))
                rmax = float(d.get("max", 10))
            except Exception:
                rmin, rmax = 1.0, 10.0

        normalized.append({
            "id": str(dev_id), 
            "type": str(dev_type), 
            "protocol": str(protocol), 
            "priority": priority, 
            "range": (rmin, rmax), 
            "unit": unit,
            "zone": zone
        })
        registered_devices[str(dev_id)] = priority
        device_zone_mapping[str(dev_id)] = zone

    student_devices = normalized
    print(f"[CONFIG] Loaded {len(student_devices)} devices. Duration: {simulation_duration}s")
    return web.json_response({"status": "ok", "devices": student_devices, "duration": simulation_duration})

async def get_config_handler(request):
    return web.json_response({"devices": student_devices, "duration": simulation_duration})

async def start_handler(request):
    global sim_process, IS_RUNNING
    if IS_RUNNING:
        return web.json_response({"status": "error", "message": "already running"}, status=400)

    if not os.path.exists(DEV_SIM):
        msg = f"Simulator file not found: {DEV_SIM}"
        print(f"[ERROR] {msg}")
        return web.json_response({"status": "error", "message": msg}, status=500)

    IS_RUNNING = True
    print(f"[SIM] Starting device simulator: {DEV_SIM}")
    try:
        sim_process = subprocess.Popen([sys.executable, DEV_SIM], cwd=ROOT)
    except Exception as e:
        IS_RUNNING = False
        msg = f"failed to spawn simulator: {e}"
        print(f"[ERROR] {msg}")
        return web.json_response({"status": "error", "message": msg}, status=500)

    asyncio.create_task(simulation_lifecycle())
    return web.json_response({"status": "ok"})

async def http_ingest_handler(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "invalid json"}, status=400)

    asyncio.create_task(process_incoming_packet(body))
    return web.json_response({"status": "ok"})

# ✅ ARQ Delivered endpoint
async def delivered_handler(request):
    try:
        body = await request.json()
        device_id = body.get("device_id")
        if not device_id:
            return web.json_response({"status": "error", "message": "missing device_id"}, status=400)
        
        delivered = sorted(list(delivered_sequences[device_id]))
        return web.json_response({"status": "ok", "delivered": delivered})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=500)

# FIXED: Manual Retransmission Handler now simulates packet recovery and sends telemetry
async def retransmit_lost_packets_handler(request): 
    global manual_actions_log, total_lost, PACKET_DROPS
    
    timestamp = datetime.now().strftime("%H:%M:%S")
    
    # Calculate how many packets to 'recover' (simulate successful retransmission)
    # The simulation amount is capped by the current total lost packets
    recovered_count = min(total_lost, MANUAL_RETRANSMIT_SIM_AMOUNT)
    total_lost -= recovered_count
    
    # Clear the packet drop history to instantly relieve dynamic congestion
    PACKET_DROPS.clear()
    
    log_message = f"{timestamp} | NETWORK_SERVER (SYS) | Manual retransmission initiated. Recovered {recovered_count} lost packets. Total lost now: {total_lost}"
    
    # 1. Broadcast the system alert for the log
    await broadcast_to_dashboard({
        "type": "SYSTEM_ALERT", 
        "message": f"Manual retransmission initiated. Recovered {recovered_count} lost packets.", 
        "timestamp": safe_now_iso()
    })

    # 2. Simulate successful telemetry delivery for recovered packets (NEW LOGIC)
    if registered_devices:
        protocols = ["HTTP", "TCP", "ZMQ"]
        
        for i in range(recovered_count):
            # Create a generic simulated recovery packet
            sim_device_id = random.choice(list(registered_devices.keys()))
            sim_protocol = random.choice(protocols)
            sim_latency = BASE_LATENCY + random.uniform(2, 8) # Low latency to show fast recovery
            sim_zone = device_zone_mapping.get(sim_device_id, "Ward")
            
            # Broadcast as a standard TELEMETRY packet, but with a specific flag
            await broadcast_to_dashboard({
                "type": "TELEMETRY",
                "device_id": sim_device_id,
                "protocol": sim_protocol,
                "priority": registered_devices.get(sim_device_id, 5),
                "value": "RECOVERY DATA", # Placeholder data
                "delay_ms": round(sim_latency, 2),
                "jitter_ms": 0.0,
                "zone": sim_zone,
                "is_retransmit_sim": True, # Flag for frontend styling
                "message": f"RETRANSMITTED PACKET (Simulated)",
                "timestamp": safe_now_iso()
            })

    print(f"[ACTION] {log_message}")
    manual_actions_log.appendleft(log_message)

    return web.json_response({"status": "ok", "message": log_message})

# FIXED: Now respects the manual toggle persistently
async def toggle_congestion_handler(request):
    global IS_CONGESTED, MANUAL_CONGESTION_OVERRIDE
    try:
        body = await request.json()
    except Exception:
        body = {}

    toggle_value = bool(body.get("active", False))
    
    if toggle_value:
        # If user is turning congestion ON, set manual override and state
        MANUAL_CONGESTION_OVERRIDE = True
        IS_CONGESTED = True
        message = "Manual congestion override set to: ACTIVE (Network Congestion ON)"
    else:
        # If user is turning congestion OFF, disable override and resume dynamic check
        MANUAL_CONGESTION_OVERRIDE = False
        update_throttling() # Run the dynamic check immediately
        message = "Manual congestion override set to: DISABLED (Dynamic monitoring resumed)"

    # Apply throttling immediately to reflect the change
    update_throttling() 

    await broadcast_to_dashboard({
        "type": "SYSTEM_ALERT", 
        "message": message, 
        "congestion": IS_CONGESTED, 
        "timestamp": safe_now_iso()
    })
    print(f"[ALERT] Congestion manually toggled. Override: {MANUAL_CONGESTION_OVERRIDE}, State: {IS_CONGESTED}")
    return web.json_response({"status": "ok", "congestion": IS_CONGESTED})

# NEW: Device control endpoints
async def fail_device_handler(request):
    body = await request.json()
    device_id = body.get("device_id")
    if device_id:
        failed_devices.add(device_id)
        await broadcast_to_dashboard({
            "type": "DEVICE_ALERT",
            "device_id": device_id,
            "alert": f"Device {device_id} FAILED - No telemetry",
            "timestamp": safe_now_iso()
        })
    return web.json_response({"status": "ok"})

async def dos_attack_handler(request):
    body = await request.json()
    device_id = body.get("device_id")
    if device_id:
        dos_attacking_devices.add(device_id)
        await broadcast_to_dashboard({
            "type": "DEVICE_ALERT",
            "device_id": device_id,
            "alert": f"Device {device_id} DoS ATTACK - Flooding network",
            "timestamp": safe_now_iso()
        })
    return web.json_response({"status": "ok"})

async def recover_device_handler(request):
    body = await request.json()
    device_id = body.get("device_id")
    if device_id:
        failed_devices.discard(device_id)
        dos_attacking_devices.discard(device_id)
        await broadcast_to_dashboard({
            "type": "DEVICE_ALERT",
            "device_id": device_id,
            "alert": f"Device {device_id} RECOVERED",
            "timestamp": safe_now_iso()
        })
    return web.json_response({"status": "ok"})

async def dashboard_ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    dashboard_ws_clients.add(ws)

    try:
        await ws.send_str(json.dumps({"type": "CONNECTION_OK", "message": "Connected to backend"}))
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    _ = json.loads(msg.data)
                except Exception as e:
                    print(f"[WS] Invalid JSON from dashboard: {e}")
            elif msg.type == web.WSMsgType.ERROR:
                print(f"[WS] Error: {ws.exception()}")
                break
    except Exception as e:
        print(f"[WS] Connection error: {e}")
    finally:
        dashboard_ws_clients.discard(ws)
        print("[WS] Dashboard client disconnected.")
    return ws

async def tcp_client_handler(reader, writer):
    addr = writer.get_extra_info("peername")
    print(f"[TCP] Device connected from {addr}")
    try:
        while True:
            data = await reader.readline()
            if not data:
                break
            try:
                packet = json.loads(data.decode())
                asyncio.create_task(process_incoming_packet(packet))
            except Exception as e:
                print(f"[TCP] Bad data: {e}")
    finally:
        print(f"[TCP] Device {addr} disconnected")
        writer.close()
        await writer.wait_closed()

async def zmq_listener():
    ctx = zmq.asyncio.Context()
    sock = ctx.socket(zmq.PULL)
    try:
        sock.bind("tcp://*:5555")
        print("[ZMQ] Listener started on tcp://*:5555")
        while IS_RUNNING:
            try:
                msg = await asyncio.wait_for(sock.recv_json(), timeout=1.0)
                asyncio.create_task(process_incoming_packet(msg))
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"[ZMQ] Error receiving: {e}")
                break
    finally:
        sock.close()
        ctx.term()
        print("[ZMQ] Listener stopped")

# ✅ ARQ-ENABLED process_incoming_packet
async def process_incoming_packet(packet: dict):
    global total_lost, retransmitted_total

    device_id = packet.get("device_id") or packet.get("id") or packet.get("device") or f"dev-{uuid.uuid4().hex[:6]}"
    
    # NEW: Check if device is failed before processing
    if device_id in failed_devices:
        return

    protocol = str(packet.get("protocol") or packet.get("proto") or "HTTP").upper()
    priority = int(packet.get("priority", 5))
    value = packet.get("value", None)
    seq = packet.get("seq")
    is_retransmit = packet.get("retransmit", False)

    if device_id not in registered_devices:
        registered_devices[device_id] = priority
        device_zone_mapping[device_id] = "Ward"

    update_throttling()

    # NEW: DoS flood simulation - high packet rate
    if device_id in dos_attacking_devices:
        record_packet_drop(device_id)
        total_lost += 1
        if random.random() < 0.9:  # 90% drop for DoS
            await broadcast_to_dashboard({
                "type": "PACKET_DROP",
                "device_id": device_id,
                "protocol": protocol,
                "priority": priority,
                "reason": "DoS_FLOOD",
                "timestamp": safe_now_iso()
            })
            return

    MIN_SEND_INTERVALS = {1: 0.7, 2: 1.0, 3: 1.5, 4: 2.5, 5: 4.0}
    now = time.time()
    last_time = last_sent_time.get(device_id, 0)

    latency_ms = BASE_LATENCY + random.uniform(5, 20)
    drop_prob = DROP_LOW

    if IS_CONGESTED:
        latency_ms += CONGESTION_EXTRA if priority >= 3 else 10

        if device_id in current_throttled_devices:
            interval = MIN_SEND_INTERVALS.get(priority, 2)
            if now - last_time < interval:
                if not devices_slowed_flag.get(device_id, False):
                    devices_slowed_flag[device_id] = True
                    await broadcast_to_dashboard({
                        "type": "DEVICE_ALERT",
                        "device_id": device_id,
                        "protocol": protocol,
                        "priority": priority,
                        "alert": "Device slowing down due to congestion",
                        "timestamp": safe_now_iso()
                    })
                return
            else:
                if devices_slowed_flag.get(device_id, False):
                    devices_slowed_flag[device_id] = False
                    await broadcast_to_dashboard({
                        "type": "DEVICE_ALERT",
                        "device_id": device_id,
                        "protocol": protocol,
                        "priority": priority,
                        "alert": "Device restored to normal",
                        "timestamp": safe_now_iso()
                    })
            drop_prob = 0.01
        elif priority >= 4:
            drop_prob = DROP_HIGH
        else:
            drop_prob = DROP_LOW

    last_sent_time[device_id] = now

    # NEW: Protocol-specific alerts
    if protocol in protocol_alerts and total_lost > 50:
        protocol_alerts[protocol] += 1
        if protocol_alerts[protocol] % 10 == 0:
            await broadcast_to_dashboard({
                "type": "PROTOCOL_ALERT",
                "protocol": protocol,
                "alert": f"{protocol} retransmission spike detected",
                "drops": total_lost,
                "timestamp": safe_now_iso()
            })

    if random.random() < drop_prob:
        total_lost += 1
        record_packet_drop(device_id)
        await broadcast_to_dashboard({
            "type": "PACKET_DROP",
            "device_id": device_id,
            "protocol": protocol,
            "priority": priority,
            "timestamp": safe_now_iso()
        })
        return

    # ✅ ARQ LOGIC: Track delivered sequences and count retransmits
    if is_retransmit:
        # Count as retransmission (already delivered before, but resent)
        global retransmitted_total
        retransmitted_total += 1
        retransmitted_by_device[device_id] += 1
        print(f"[ARQ] Retransmitted packet from {device_id}, seq {seq}")
    else:
        # First time delivery - track sequence
        if seq is not None:
            delivered_sequences[device_id].add(seq)
            print(f"[ARQ] Delivered seq {seq} from {device_id}")

    # NEW: Update latency tracking
    update_device_latency(device_id, latency_ms)

    if IS_CONGESTED:
        protocol_counts_congested.setdefault(protocol, 0)
        protocol_latency_sum_congested.setdefault(protocol, 0.0)
        protocol_counts_congested[protocol] += 1
        protocol_latency_sum_congested[protocol] += latency_ms
    else:
        protocol_counts.setdefault(protocol, 0)
        protocol_latency_sum.setdefault(protocol, 0.0)
        protocol_counts[protocol] += 1
        protocol_latency_sum[protocol] += latency_ms

    jitter = device_jitter_history.get(device_id, 0.0)
    zone = device_zone_mapping.get(device_id, "Unknown")

    await broadcast_to_dashboard({
        "type": "TELEMETRY",
        "device_id": device_id,
        "protocol": protocol,
        "priority": priority,
        "value": value,
        "delay_ms": round(latency_ms, 2),
        "jitter_ms": jitter,
        "zone": zone,
        "timestamp": safe_now_iso()
    })

async def simulation_lifecycle():
    global sim_process, IS_RUNNING, simulation_duration

    print(f"[SIM] Simulation started for {simulation_duration} seconds")

    tcp_server = await asyncio.start_server(tcp_client_handler, "0.0.0.0", 8081)
    zmq_task = asyncio.create_task(zmq_listener())
    metrics_task = asyncio.create_task(metrics_broadcaster())
    congestion_task = asyncio.create_task(congestion_monitor_loop())

    await broadcast_to_dashboard({"type": "SIMULATION_START", "duration": simulation_duration, "timestamp": safe_now_iso()})

    try:
        await asyncio.sleep(simulation_duration)
    finally:
        print("[SIM] Shutting down...")
        IS_RUNNING = False

        zmq_task.cancel()
        metrics_task.cancel()
        congestion_task.cancel()
        try:
            await zmq_task
        except asyncio.CancelledError:
            pass
        try:
            await metrics_task
        except asyncio.CancelledError:
            pass
        try:
            await congestion_task
        except asyncio.CancelledError:
            pass

        tcp_server.close()
        await tcp_server.wait_closed()

        if sim_process:
            try:
                sim_process.terminate()
                sim_process.wait(timeout=5)
            except Exception:
                try:
                    sim_process.kill()
                except Exception:
                    pass
            sim_process = None

        final_metrics = compute_current_metrics()
        save_metrics_file(final_metrics)
        await broadcast_to_dashboard({"type": "FINAL_METRICS", "metrics": final_metrics, "timestamp": safe_now_iso()})

        print("[SIM] Complete")

@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        return web.Response(status=200, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type"
        })
    resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

async def create_app():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/ping", ping_handler)
    app.router.add_post("/configure", configure_handler)
    app.router.add_get("/config", get_config_handler)
    app.router.add_post("/start", start_handler)
    app.router.add_post("/http_in", http_ingest_handler)
    app.router.add_post("/delivered", delivered_handler)
    app.router.add_post("/toggle_congestion", toggle_congestion_handler)
    app.router.add_post("/retransmit_lost", retransmit_lost_packets_handler)
    app.router.add_get("/ws", dashboard_ws_handler)
    
    # NEW: Device control routes
    app.router.add_post("/fail_device", fail_device_handler)
    app.router.add_post("/dos_attack", dos_attack_handler)
    app.router.add_post("/recover_device", recover_device_handler)
    
    return app

async def app_runner():
    app = await create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    print("[API] READY on http://0.0.0.0:8080 - ✅ FULL ARQ RETRANSMISSION ENABLED!")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass
    try:
        asyncio.run(app_runner())
    except KeyboardInterrupt:
        print("\n[API] Server stopped.")
