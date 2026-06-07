#!/usr/bin/env python3
"""
Reads a scenario JSON file and publishes
each sensor reading to its own ThingsBoard device via MQTT.

ThingsBoard MQTT auth: username = device access token, password = empty.
Topic for telemetry: v1/devices/me/telemetry

Each sensor has its own token stored in all_sensors.csv.
Run fetch_tokens.py first to populate those tokens.

Requirements:
    pip install paho-mqtt

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
from config.settings import TB_HOST, TB_PORT_MQTT, TOKENS_PATH, SCENARIO_FILES, SIM_INTERVAL_SEC


# Settings

TB_TOPIC = "v1/devices/me/telemetry"


# Load sensor tokens from CSV 

def load_tokens():
    with open(TOKENS_PATH, newline="") as f:
        return {row["sensor_id"]: row["token"] for row in csv.DictReader(f)}


# MQTT connection pool 

def connect_clients(tokens, host, port):
    """
    Create one persistent MQTT client per sensor and connect to ThingsBoard.
    In ThingsBoard MQTT auth: username = device access token, password = empty.
    Returns a dict of sensor_id → mqtt.Client.
    """
    clients = {}

    for sensor_id, token in tokens.items():
        if not token:
            continue  

        client = mqtt.Client(client_id=sensor_id, protocol=mqtt.MQTTv311)
        client.username_pw_set(username=token, password="")

        try:
            client.connect(host, port, keepalive=60)
            client.loop_start()  # start background thread for network traffic
            clients[sensor_id] = client
        except Exception as e:
            print(f"  Could not connect {sensor_id}: {e}")

    return clients


def disconnect_clients(clients):
    for client in clients.values():
        client.loop_stop()
        client.disconnect()


# Publish one reading via MQTT 

def publish_reading(client, reading, timestamp_dt, topic):
    payload = {
        "ts":     int(timestamp_dt.timestamp() * 1000),  # milliseconds
        "values": {reading["sensor_id"]: reading["value"]}
    }

    result = client.publish(topic, json.dumps(payload), qos=1)
    return "ok" if result.rc == mqtt.MQTT_ERR_SUCCESS else f"rc_{result.rc}"


# Main publish logic 

def publish(scenario, host, port, interval, dry_run):
    """
    Load the scenario JSON and replay all readings to ThingsBoard via MQTT.
    Each sensor uses its own MQTT client (connected with its own token).
    Waits 'interval' seconds between each timestamp batch.
    """
    # Load per-sensor tokens from CSV
    tokens = load_tokens()

    # Warn about missing tokens before starting
    missing = [sid for sid, tok in tokens.items() if not tok]
    if missing and not dry_run:
        print(f"Warning: {len(missing)} sensor(s) have no token — run fetch_tokens.py first.")
        print(f"  Missing: {', '.join(missing)}\n")

    # Load scenario file
    path = Path(SCENARIO_FILES[scenario])
    if not path.exists():
        print(f"Error: {path} not found. Run 'python simulate.py {scenario}' first.")
        return

    data = json.loads(path.read_text())

    # Group readings by timestamp
    groups = defaultdict(list)
    for reading in data["readings"]:
        groups[reading["timestamp"]].append(reading)
    batches = [(ts, groups[ts]) for ts in sorted(groups)]

    total_readings = sum(len(b) for _, b in batches)

    # Print summary header
    print(f"\n{'─'*55}")
    print(f"  Scenario : {data['scenario']}")
    print(f"  File     : {path}")
    print(f"  Date     : {data['date']}")
    print(f"  Batches  : {len(batches)}  ({total_readings:,} total readings)")
    if dry_run:
        print(f"  Mode     : DRY RUN — not sending to ThingsBoard")
    else:
        print(f"  Broker   : mqtt://{host}:{port}")
        print(f"  Topic    : {TB_TOPIC}")
        print(f"  Tokens   : per-sensor (from {TOKENS_PATH})")
        print(f"  Interval : {interval}s between batches")
    print(f"{'─'*55}\n")

    # Connect all MQTT clients up front (one per sensor)
    clients = {}
    if not dry_run:
        print("Connecting MQTT clients …")
        clients = connect_clients(tokens, host, port)
        print(f"Connected {len(clients)}/{len(tokens)} sensor clients\n")

    # Replay each batch
    for i, (ts_str, batch) in enumerate(batches):
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))

        print(f"[{i+1}/{len(batches)}]  {dt.strftime('%H:%M:%S')}  —  {len(batch)} sensor(s)")

        for reading in batch:
            sensor_id = reading["sensor_id"]
            print(f"  {json.dumps(reading)}")

            if not dry_run:
                client = clients.get(sensor_id)
                if client:
                    result = publish_reading(client, reading, dt, TB_TOPIC)
                    print(f"    → MQTT {result}")
                else:
                    print(f"    → SKIPPED (no token for {sensor_id})")

        print()

        # Wait before the next batch (not after the last one)
        if i < len(batches) - 1:
            time.sleep(interval)

    # Disconnect all clients cleanly
    if not dry_run:
        disconnect_clients(clients)

    print(f"Done — {len(batches)} batches published for '{data['scenario']}'.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Publish scenario data to ThingsBoard via MQTT (one token per sensor)."
    )
    parser.add_argument(
        "scenario",
        choices=["normal", "decline", "hazard"],
        help="Scenario to publish: normal | decline | hazard"
    )
    parser.add_argument(
        "--host",
        default=TB_HOST,
        help=f"MQTT broker host (default: {TB_HOST})"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=TB_PORT_MQTT,
        help=f"MQTT broker port (default: {TB_PORT_MQTT})"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=SIM_INTERVAL_SEC,
        help=f"Seconds between batches (default: {SIM_INTERVAL_SEC})"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print readings without connecting to ThingsBoard"
    )
    args = parser.parse_args()

    publish(args.scenario, args.host, args.port, args.interval, args.dry_run)
