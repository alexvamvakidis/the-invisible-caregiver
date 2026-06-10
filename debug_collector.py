#!/usr/bin/env python3
"""
Debug script — shows data at each stage of the ThingsBoard pipeline.

Usage:
    python debug_collector.py                        # all rooms
    python debug_collector.py --room kitchen         # one room only
    python debug_collector.py --window-ms 3600000    # last 1 hour
"""

import json
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import TB_URL, TB_USERNAME, TB_PASSWORD, HISTORY_WINDOW_MS, SENSOR_KEYS_BY_ROOM
from data.collector import get_token, get_device_id_map, get_telemetry, summarize


def pretty(obj):
    print(json.dumps(obj, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Debug ThingsBoard data pipeline")
    parser.add_argument("--room", default=None, help="Filter to a single room")
    parser.add_argument("--window-ms", type=int, default=HISTORY_WINDOW_MS,
                        help=f"Lookback window in ms (default: {HISTORY_WINDOW_MS})")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  TB_URL       : {TB_URL}")
    print(f"  Window (ms)  : {args.window_ms}  ({args.window_ms // 1000 // 60} minutes)")
    print(f"{'='*60}\n")

    # ── Step 1: Auth ──────────────────────────────────────────────
    print("── Step 1: Authenticating…")
    try:
        token = get_token()
        print(f"  ✓ Token obtained\n")
    except Exception as e:
        sys.exit(f"  ✗ Auth failed: {e}")

    # ── Step 2: Device ID map ─────────────────────────────────────
    print("── Step 2: Fetching device list…")
    try:
        device_id_map = get_device_id_map(token)
        print(f"  ✓ {len(device_id_map)} devices found")
        for name, uid in device_id_map.items():
            print(f"    {name:40s} → {uid}")
        print()
    except Exception as e:
        sys.exit(f"  ✗ Device fetch failed: {e}")

    # ── Step 3 + 4: Raw telemetry → summarize(), side by side per sensor ────
    print("── Step 3 & 4: Raw data vs summarized, per sensor…")
    rooms = {args.room: SENSOR_KEYS_BY_ROOM[args.room]} if args.room else SENSOR_KEYS_BY_ROOM
    raw = {}

    for room, keys in rooms.items():
        print(f"\n{'='*60}")
        print(f"  ROOM: {room.upper()}")
        print(f"{'='*60}")
        raw[room] = {}

        for sensor_id in keys:
            device_id = device_id_map.get(sensor_id)
            if device_id is None:
                print(f"\n  ✗ {sensor_id} — NOT FOUND in ThingsBoard")
                continue

            data   = get_telemetry(device_id, [sensor_id], token, args.window_ms)
            values = data.get(sensor_id, [])
            raw[room][sensor_id] = data

            print(f"\n  ┌─ {sensor_id}")

            # RAW
            print(f"  │  RAW  ({len(values)} data points):")
            if not values:
                print(f"  │    ⚠ EMPTY")
            else:
                # Print first 3 and last 3 to keep output readable
                sample = values[:3] + (["  ..."] if len(values) > 6 else []) + values[-3:] if len(values) > 6 else values
                for v in sample:
                    if isinstance(v, str):
                        print(f"  │    {v}")
                    else:
                        print(f"  │    ts={v['ts']}  value={v['value']!r}  (type: {type(v['value']).__name__})")

            # SUMMARIZED
            summary_single = summarize({room: {sensor_id: data}})
            info = summary_single.get(room, {}).get(sensor_id)

            print(f"  │  SUMMARIZED:")
            if not info:
                print(f"  │    ⚠ No summary produced")
            elif info["type"] == "continuous":
                print(f"  │    type=continuous  min={info['min']}  max={info['max']}  avg={info['avg']}")
            else:
                print(f"  │    type=boolean  current={info['current']!r}  (type: {type(info['current']).__name__})")
                for ev in info.get("events", []):
                    print(f"  │      → value={ev['value']!r}  ts={ev['ts']}  duration_sec={ev['duration_sec']}")
            print(f"  └{'─'*40}")


if __name__ == "__main__":
    main()
