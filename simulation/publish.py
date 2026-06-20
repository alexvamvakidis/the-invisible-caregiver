#!/usr/bin/env python3
"""
Reads per-sensor JSON files and publishes each reading to its ThingsBoard
device via MQTT.

ThingsBoard MQTT auth: username = device access token, password = empty.
Topic for telemetry: v1/devices/me/telemetry

Each sensor has its own token stored in sensor_tokens.csv.
Run fetch_tokens.py first to populate those tokens.

Usage:
    python publish.py normal
    python publish.py decline --interval 10
    python publish.py hazard  --dry-run
"""

import csv
import json
import sys
import time
import argparse
import paho.mqtt.client as mqtt
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import TB_HOST, TB_PORT_MQTT, TOKENS_PATH, SENSOR_DATA_DIR

TB_TOPIC = "v1/devices/me/telemetry"


def load_tokens():
    with open(TOKENS_PATH, newline="") as f:
        return {row["sensor_id"]: row["token"] for row in csv.DictReader(f)}


def load_sensor_data(scenario):
    """
    Load all per-sensor JSON files for the given scenario and merge them
    into a chronological list of readings.
    Returns (meta_dict, readings_list) or (None, None) on error.
    """
    data_dir = Path(SENSOR_DATA_DIR) / scenario
    if not data_dir.exists():
        print(f"Error: {data_dir} not found. Run 'python simulate.py {scenario}' first.")
        return None, None

    sensor_files = sorted(data_dir.glob("*.json"))
    if not sensor_files:
        print(f"Error: no sensor files found in {data_dir}.")
        return None, None

    meta = {}
    all_readings = []

    for json_file in sensor_files:
        data = json.loads(json_file.read_text())
        if not meta:
            meta = {
                "scenario": data.get("scenario", scenario),
                "date":     data.get("date", ""),
            }
        sensor_id = data["sensor_id"]
        for r in data.get("readings", []):
            all_readings.append({
                "sensor_id": sensor_id,
                "timestamp": r["timestamp"],
                "value":     r["value"],
            })

    return meta, all_readings


def connect_clients(tokens, host, port):
    """One persistent MQTT client per sensor, each authenticated with its token."""
    clients = {}
    for sensor_id, token in tokens.items():
        if not token:
            continue
        client = mqtt.Client(client_id=sensor_id, protocol=mqtt.MQTTv311)
        client.username_pw_set(username=token, password="")
        try:
            client.connect(host, port, keepalive=60)
            client.loop_start()
            clients[sensor_id] = client
        except Exception as e:
            print(f"  Could not connect {sensor_id}: {e}")
    return clients


def disconnect_clients(clients):
    for client in clients.values():
        client.loop_stop()
        client.disconnect()


def publish_reading(client, sensor_id, value, timestamp_dt):
    payload = {
        "ts":     int(timestamp_dt.timestamp() * 1000),
        "values": {sensor_id: value},
    }
    result = client.publish(TB_TOPIC, json.dumps(payload), qos=1)
    return "ok" if result.rc == mqtt.MQTT_ERR_SUCCESS else f"rc_{result.rc}"


def publish(scenario, host, port, interval, dry_run):
    tokens = load_tokens()

    missing = [sid for sid, tok in tokens.items() if not tok]
    if missing and not dry_run:
        print(f"Warning: {len(missing)} sensor(s) have no token — run fetch_tokens.py first.")
        print(f"  Missing: {', '.join(missing)}\n")

    meta, all_readings = load_sensor_data(scenario)
    if meta is None:
        return

    # Group readings by timestamp (chronological batches)
    groups = defaultdict(list)
    for r in all_readings:
        groups[r["timestamp"]].append(r)
    batches = [(ts, groups[ts]) for ts in sorted(groups)]

    total_readings = sum(len(b) for _, b in batches)

    print(f"\n{'─'*55}")
    print(f"  Scenario : {meta['scenario']}")
    print(f"  Dir      : {Path(SENSOR_DATA_DIR) / scenario}")
    print(f"  Date     : {meta['date']}")
    print(f"  Batches  : {len(batches)}  ({total_readings:,} total readings)")
    if dry_run:
        print(f"  Mode     : DRY RUN — not sending to ThingsBoard")
    else:
        print(f"  Broker   : mqtt://{host}:{port}")
        print(f"  Topic    : {TB_TOPIC}")
        print(f"  Interval : {interval}s between batches")
    print(f"{'─'*55}\n")

    clients = {}
    if not dry_run:
        print("Connecting MQTT clients …")
        clients = connect_clients(tokens, host, port)
        print(f"Connected {len(clients)}/{len(tokens)} sensor clients\n")

    today = datetime.today().date()
    for i, (ts_str, batch) in enumerate(batches):
        dt = datetime.fromisoformat(ts_str.replace("Z", ""))
        dt = dt.replace(year=today.year, month=today.month, day=today.day)
        print(f"[{i+1}/{len(batches)}]  {dt.strftime('%H:%M:%S')}  —  {len(batch)} sensor(s)")

        for r in batch:
            sensor_id = r["sensor_id"]
            print(f"  {sensor_id}: {r['value']}")

            if not dry_run:
                client = clients.get(sensor_id)
                if client:
                    result = publish_reading(client, sensor_id, r["value"], dt)
                    print(f"    → MQTT {result}")
                else:
                    print(f"    → SKIPPED (no token for {sensor_id})")

        print()
        if i < len(batches) - 1:
            time.sleep(interval)

    if not dry_run:
        disconnect_clients(clients)

    print(f"Done — {len(batches)} batches published for '{meta['scenario']}'.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Publish per-sensor scenario data to ThingsBoard via MQTT."
    )
    parser.add_argument("scenario", choices=["normal", "decline", "hazard"])
    parser.add_argument("--host", default=TB_HOST,
                        help=f"MQTT broker host (default: {TB_HOST})")
    parser.add_argument("--port", type=int, default=TB_PORT_MQTT,
                        help=f"MQTT broker port (default: {TB_PORT_MQTT})")
    parser.add_argument("--interval", type=float, default=0,
                        help="Seconds between batches (default: 0 — bulk upload)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print readings without connecting to ThingsBoard")
    args = parser.parse_args()

    publish(args.scenario, args.host, args.port, args.interval, args.dry_run)
