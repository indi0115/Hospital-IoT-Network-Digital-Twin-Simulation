import asyncio
import aiohttp
import zmq
import zmq.asyncio
import json
import random
import time
import requests

BACKEND_CONFIG = "http://localhost:8080/config"
BACKEND_DELIVERED = "http://localhost:8080/delivered"  # ✅ NEW ARQ endpoint
TEST_RETRY_DELAY = 2
THRESHOLD = 5

# Define minimal send intervals (seconds) by priority for slow down during high congestion
MIN_SEND_INTERVALS = {1: 0.7, 2: 1.0, 3: 1.5, 4: 2.5, 5: 4.0}

DEVICE_METADATA = {
    "Temperature Sensor": {"min": 36.1, "max": 37.5, "units": ["°C", "°F"]},
    "Heart Rate Monitor": {"min": 50, "max": 120, "units": ["bpm"]},
    "Oxygen Saturation": {"min": 90, "max": 100, "units": ["%"]},
    "Blood Pressure Systolic": {"min": 90, "max": 140, "units": ["mmHg"]},
    "Blood Pressure Diastolic": {"min": 60, "max": 90, "units": ["mmHg"]},
    "Surgery Monitor": {"min": 95, "max": 100, "units": ["%"]},
    "Bed Sensor": {"min": 0, "max": 100, "units": ["%"]},
}

# ✅ ARQ GLOBAL STATE - Per device sequence tracking
device_seq = {}            # device_id -> next seq number to use
device_sent_buffer = {}    # device_id -> { seq: packet_dict }

last_sent_time = {}
packet_drop_counters = {}

# Threshold to consider congestion started
CONGESTION_DROP_THRESHOLD = 10
# Time window (seconds) to evaluate congestion
CONGESTION_WINDOW = 30

async def fetch_config():
    for _ in range(5):
        try:
            r = requests.get(BACKEND_CONFIG, timeout=3)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            print(f"[SIM] Config fetch error: {e}")
            await asyncio.sleep(TEST_RETRY_DELAY)
    return None

def realistic_value(device_type):
    meta = DEVICE_METADATA.get(device_type)
    if not meta:
        return round(random.uniform(1, 100), 2)
    min_val = meta["min"]
    max_val = meta["max"]
    mean = (min_val + max_val) / 2
    stddev = (max_val - min_val) / 6
    val = random.gauss(mean, stddev)
    return round(min(max(val, min_val), max_val), 2)

def is_congested(device_id):
    drops = packet_drop_counters.get(device_id, [])
    now = time.time()
    drops = [t for t in drops if now - t < CONGESTION_WINDOW]
    packet_drop_counters[device_id] = drops
    return len(drops) >= CONGESTION_DROP_THRESHOLD

def record_packet_drop(device_id):
    now = time.time()
    if device_id not in packet_drop_counters:
        packet_drop_counters[device_id] = []
    packet_drop_counters[device_id].append(now)

# ✅ NEW: ARQ Retransmission function
async def retransmit_missing(device_id):
    print(f"[ARQ][{device_id}] Starting retransmission phase...")
    
    # 1) Ask backend which seq were delivered
    try:
        resp = requests.post(BACKEND_DELIVERED, json={"device_id": device_id}, timeout=5)
        if resp.status_code != 200:
            print(f"[SIM][{device_id}] could not fetch delivered seq, skipping retransmit")
            return
        delivered = set(resp.json().get("delivered", []))
    except Exception as e:
        print(f"[SIM][{device_id}] delivered fetch error: {e}")
        return

    # 2) Determine missing from sent buffer
    sent_map = device_sent_buffer.get(device_id, {})
    missing_seqs = [s for s in sent_map.keys() if s not in delivered]
    
    if not missing_seqs:
        print(f"[SIM][{device_id}] No missing packets to retransmit")
        return

    print(f"[SIM][{device_id}] Retransmitting {len(missing_seqs)} lost packets")

    # 3) Resend them over same protocol they were originally sent with
    async with aiohttp.ClientSession() as session:
        for seq in missing_seqs:
            pkt = dict(sent_map[seq])  # Copy original packet
            pkt["retransmit"] = True   # Mark as retransmission
            proto = pkt.get("protocol", "HTTP").upper()
            
            ok = False
            if proto == "HTTP":
                ok = await send_http(session, "http://localhost:8080/http_in", pkt)
            elif proto == "TCP":
                ok = await send_tcp_retransmit(pkt)
            elif proto == "ZMQ":
                ok = await send_zmq_retransmit(pkt)
            
            if ok:
                print(f"[ARQ][{device_id}] ✅ Retransmitted seq {seq} ({proto})")
            else:
                print(f"[ARQ][{device_id}] ❌ FAILED retransmit seq {seq} ({proto})")

# ✅ NEW: TCP retransmit helper
async def send_tcp_retransmit(pkt):
    try:
        reader, writer = await asyncio.open_connection("localhost", 8081)
        writer.write((json.dumps(pkt) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return True
    except Exception as e:
        print(f"[SIM][TCP-REXMIT] error: {e}")
        return False

# ✅ NEW: ZMQ retransmit helper
async def send_zmq_retransmit(pkt):
    ctx = zmq.asyncio.Context()
    sock = ctx.socket(zmq.PUSH)
    try:
        sock.connect("tcp://localhost:5555")
        await sock.send_json(pkt)
        return True
    except Exception as e:
        print(f"[SIM][ZMQ-REXMIT] error: {e}")
        return False
    finally:
        sock.close()
        ctx.term()

async def send_http(session, url, msg):
    try:
        async with session.post(url, json=msg, timeout=5) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[SIM][HTTP] send failed: {e}")
        return False

# ✅ FIXED: ARQ-enabled HTTP task
async def device_task_http(device, duration):
    url = "http://localhost:8080/http_in"
    device_id = device["id"]
    
    # ✅ ARQ: Initialize sequence counter and buffer
    if device_id not in device_seq:
        device_seq[device_id] = 1
    if device_id not in device_sent_buffer:
        device_sent_buffer[device_id] = {}
    
    sent = 0
    drop_count = 0
    start = time.time()
    
    async with aiohttp.ClientSession() as session:
        while time.time() - start < duration:
            now = time.time()
            priority = device.get("priority", 5)

            if is_congested(device_id):
                interval = MIN_SEND_INTERVALS.get(priority, 2)
                last_time = last_sent_time.get(device_id, 0)
                if now - last_time < interval:
                    print(f"[ALERT] {device_id} slowing due to congestion")
                    await asyncio.sleep(0.5)
                    continue
                last_sent_time[device_id] = now
            else:
                await asyncio.sleep(random.uniform(0.7, 1.5))

            # ✅ ARQ: Generate sequence number
            seq = device_seq[device_id]
            device_seq[device_id] += 1
            
            val = realistic_value(device.get("type", ""))
            msg = {
                "device_id": device_id,
                "protocol": "HTTP",
                "priority": priority,
                "value": val,
                "timestamp": now,
                "zone": device.get("zone", "Ward"),
                "seq": seq,           # ✅ NEW: Sequence number
                "retransmit": False   # ✅ NEW: Not retransmit
            }
            
            # ✅ ARQ: Store in buffer regardless of success
            device_sent_buffer[device_id][seq] = msg

            success = await send_http(session, url, msg)
            if not success:
                drop_count += 1
                record_packet_drop(device_id)
                if drop_count >= THRESHOLD:
                    await device_task_tcp(device, duration - (time.time() - start))
                    break
            else:
                sent += 1
                drop_count = 0

    print(f"[SIM][HTTP] {device_id} sent {sent}")
    
    # ✅ ARQ: RETRANSMIT MISSING at END
    await retransmit_missing(device_id)

# ✅ FIXED: ARQ-enabled TCP task
async def device_task_tcp(device, duration):
    device_id = device["id"]
    
    # ✅ ARQ: Initialize sequence counter and buffer
    if device_id not in device_seq:
        device_seq[device_id] = 1
    if device_id not in device_sent_buffer:
        device_sent_buffer[device_id] = {}
    
    try:
        reader, writer = await asyncio.open_connection("localhost", 8081)
    except Exception as e:
        print(f"[SIM][TCP] Connection error: {e}")
        return

    sent = 0
    drop_count = 0
    start = time.time()
    priority = device.get("priority", 5)

    while time.time() - start < duration:
        now = time.time()

        if is_congested(device_id):
            interval = MIN_SEND_INTERVALS.get(priority, 2)
            last_time = last_sent_time.get(device_id, 0)
            if now - last_time < interval:
                print(f"[ALERT] {device_id} slowing due to congestion")
                await asyncio.sleep(0.5)
                continue
            last_sent_time[device_id] = now

        # ✅ ARQ: Generate sequence number
        seq = device_seq[device_id]
        device_seq[device_id] += 1
        
        val = realistic_value(device.get("type", ""))
        msg = {
            "device_id": device_id,
            "protocol": "TCP",
            "priority": priority,
            "value": val,
            "timestamp": now,
            "zone": device.get("zone", "Ward"),
            "seq": seq,           # ✅ NEW: Sequence number
            "retransmit": False   # ✅ NEW: Not retransmit
        }
        
        # ✅ ARQ: Store in buffer
        device_sent_buffer[device_id][seq] = msg

        try:
            writer.write((json.dumps(msg) + "\n").encode())
            await writer.drain()
            sent += 1
            drop_count = 0
        except Exception as e:
            print(f"[SIM][TCP] send error {e}")
            drop_count += 1
            record_packet_drop(device_id)
            if drop_count >= THRESHOLD:
                await device_task_http(device, duration - (time.time() - start))
                break

        await asyncio.sleep(random.uniform(0.7, 1.2))

    writer.close()
    await writer.wait_closed()
    print(f"[SIM][TCP] {device_id} sent {sent}")
    
    # ✅ ARQ: RETRANSMIT MISSING at END
    await retransmit_missing(device_id)

# ✅ FIXED: ARQ-enabled ZMQ task
async def device_task_zmq(device, duration):
    device_id = device["id"]
    
    # ✅ ARQ: Initialize sequence counter and buffer
    if device_id not in device_seq:
        device_seq[device_id] = 1
    if device_id not in device_sent_buffer:
        device_sent_buffer[device_id] = {}
    
    ctx = zmq.asyncio.Context()
    sock = ctx.socket(zmq.PUSH)
    sock.connect("tcp://localhost:5555")

    sent = 0
    drop_count = 0
    start = time.time()
    priority = device.get("priority", 5)

    while time.time() - start < duration:
        now = time.time()

        if is_congested(device_id):
            interval = MIN_SEND_INTERVALS.get(priority, 2)
            last_time = last_sent_time.get(device_id, 0)
            if now - last_time < interval:
                print(f"[ALERT] {device_id} slowing due to congestion")
                await asyncio.sleep(0.5)
                continue
            last_sent_time[device_id] = now

        # ✅ ARQ: Generate sequence number
        seq = device_seq[device_id]
        device_seq[device_id] += 1
        
        val = realistic_value(device.get("type", ""))
        msg = {
            "device_id": device_id,
            "protocol": "ZMQ",
            "priority": priority,
            "value": val,
            "timestamp": now,
            "zone": device.get("zone", "Ward"),
            "seq": seq,           # ✅ NEW: Sequence number
            "retransmit": False   # ✅ NEW: Not retransmit
        }
        
        # ✅ ARQ: Store in buffer
        device_sent_buffer[device_id][seq] = msg

        try:
            await sock.send_json(msg)
            sent += 1
            drop_count = 0
        except Exception:
            drop_count += 1
            record_packet_drop(device_id)
            if drop_count >= THRESHOLD:
                await device_task_http(device, duration - (time.time() - start))
                break

        await asyncio.sleep(random.uniform(0.9, 1.7))

    sock.close()
    ctx.term()
    print(f"[SIM][ZMQ] {device_id} sent {sent}")
    
    # ✅ ARQ: RETRANSMIT MISSING at END
    await retransmit_missing(device_id)

async def main():
    cfg = await fetch_config()
    if not cfg:
        print("[SIM] Could not fetch config. Exiting.")
        return

    devices = cfg.get("devices", [])
    duration = int(cfg.get("duration", 60))

    for i, d in enumerate(devices):
        d.setdefault("id", f"dev-{i+1}")
        if "type" not in d:
            d["type"] = "Generic"
        if "min" not in d or "max" not in d:
            meta = DEVICE_METADATA.get(d.get("type"))
            if meta:
                d["min"] = meta["min"]
                d["max"] = meta["max"]
                d["unit"] = meta["units"][0]

    tasks = []
    for device in devices:
        proto = device.get("protocol", "HTTP").upper()
        if proto == "HTTP":
            tasks.append(asyncio.create_task(device_task_http(device, duration)))
        elif proto == "TCP":
            tasks.append(asyncio.create_task(device_task_tcp(device, duration)))
        elif proto == "ZMQ":
            tasks.append(asyncio.create_task(device_task_zmq(device, duration)))
        else:
            tasks.append(asyncio.create_task(device_task_http(device, duration)))

    await asyncio.gather(*tasks)
    
    # ✅ FINAL ARQ: Print summary
    print("\n🎉 [SIM] All device tasks complete. ARQ retransmissions done!")
    print(f"Sent buffers: {len(device_sent_buffer)} devices tracked")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[SIM] Interrupted by user.")

