#!/usr/bin/env python3
"""
Generates 24 hours of sensor data for a chosen scenario and saves it
to a JSON file.

Scenario event definitions live in separate JSON files:
    events_normal.json   — Normal Day
    events_decline.json  — Subtle Decline
    events_hazard.json   — Acute Hazard

Usage:
    python simulate.py normal
    python simulate.py decline
    python simulate.py hazard
    python simulate.py normal --date 2026-06-01
"""

import csv
import json
import sys
import random
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import CSV_PATH, SIM_INTERVAL_SEC, SIM_DATE, EVENT_FILES, SCENARIO_FILES

# Sensor starting point for every scenario
BASELINE = {
    "bathroom_motion":              False,
    "toilet_pressure":              False,
    "bathroom_water_flow":          False,
    "bathroom_shower_water_temp":   20.0,
    "bathroom_humidity":            55.0,
    "bathroom_temperature":         22.0,
    "bathroom_fall":                False,
    "bed_pressure":                 True,
    "bedroom_motion":               False,
    "bedroom_lamp_plug":            False,
    "bedroom_temperature":          20.5,
    "living_motion":                False,
    "sofa_pressure":                False,
    "sofa_pressure_2":              False,
    "chair_pressure":               False,
    "tv_plug":                      False,
    "tv_volume":                    0,
    "entrance_motion":              False,
    "entrance_door":                True,
    "kitchen_motion":               False,
    "kitchen_temperature":          21.0,
    "stove_power":                  False,
    "smoke_detector":               False,
    "fridge_door":                  True,
    "kitchen_faucet":               True,
    "kitchen_medication_cabinet":   True,
}




def load_sensors():
    with open(CSV_PATH, newline="") as f:
        return {row["sensor_id"]: row for row in csv.DictReader(f)}


def add_noise(sensor_type, value):
    """
    Add a small random variation to continuous sensors (temperature, humidity)
    to make readings look realistic rather than perfectly flat.
    """
    if sensor_type == "temperature_sensor":
        return round(value + random.uniform(-0.4, 0.4), 1)
    if sensor_type == "humidity_sensor":
        return round(value + random.uniform(-1.5, 1.5), 1)
    return value  # boolean / string / integer sensors stay exact


def make_reading(sensor_id, value, timestamp, sensors):
    s = sensors[sensor_id]
    return {
        "sensor_id":   sensor_id,
        "sensor_type": s["sensor_type"],
        "room":        s["room"],
        "location":    s["location"],
        "timestamp":   timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "value":       value,
        "unit":        s["unit"],
    }


# Simulation logic 

def simulate(scenario, date_str):
    sensors = load_sensors()
    base_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    ev_path = Path(EVENT_FILES[scenario])
    if not ev_path.exists():
        print(f"Error: {ev_path} not found.")
        return

    ev_data = json.loads(ev_path.read_text())

    # Convert events to a lookup: second_offset → [(sensor_id, value), ...]
    event_map = defaultdict(list)
    for ev in ev_data["events"]:
        parts = list(map(int, ev["time"].split(":")))
        h, m, s = parts[0], parts[1], parts[2] if len(parts) > 2 else 0
        second = h * 3600 + m * 60 + s
        event_map[second].append((ev["sensor_id"], ev["value"]))

    # Start every sensor at its baseline value
    state = dict(BASELINE)

    readings = []
    TOTAL_SEC = 24 * 3600  # 86400 seconds in a day

    # Step through the full day in SIM_INTERVAL_SEC increments
    for sec in range(0, TOTAL_SEC, SIM_INTERVAL_SEC):

        exact_at_step: dict = {}
        for s in range(sec, sec + SIM_INTERVAL_SEC):
            for sensor_id, new_value in event_map.get(s, []):
                state[sensor_id] = new_value
                if sensor_id not in sensors:
                    continue
                if s != sec:
                    event_dt = base_dt + timedelta(seconds=s)
                    readings.append(make_reading(sensor_id, new_value, event_dt, sensors))
                else:
                    exact_at_step[sensor_id] = new_value

        # Sensors whose event fired exactly on this step use the exact value.
        dt = base_dt + timedelta(seconds=sec)
        for sensor_id, sensor_meta in sensors.items():
            if sensor_id in exact_at_step:
                value = exact_at_step[sensor_id]
            else:
                value = add_noise(sensor_meta["sensor_type"], state[sensor_id])
            readings.append(make_reading(sensor_id, value, dt, sensors))

    # Sort readings chronologically
    readings.sort(key=lambda r: r["timestamp"])

    # Write the scenario file
    output = {
        "scenario":         ev_data["scenario"],
        "description":      ev_data["description"],
        "date":             date_str,
        "sim_interval_sec": SIM_INTERVAL_SEC,
        "sensor_count":     len(sensors),
        "total_readings":   len(readings),
        "readings":         readings,
    }

    out_path = Path(SCENARIO_FILES[scenario])
    out_path.write_text(json.dumps(output, indent=2))

    steps = TOTAL_SEC // SIM_INTERVAL_SEC
    print(f"Scenario  : {ev_data['scenario']}")
    print(f"Interval  : every {SIM_INTERVAL_SEC}s  ({steps} time steps x {len(sensors)} sensors)")
    print(f"Readings  : {len(readings):,}")
    print(f"Saved to  : {out_path}")


# Entry point

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Simulate 24 h of sensor data for a scenario."
    )
    parser.add_argument(
        "scenario",
        choices=["normal", "decline", "hazard"],
        help="Scenario to simulate: normal | decline | hazard"
    )
    parser.add_argument(
        "--date",
        default=SIM_DATE,
        help=f"Simulation date YYYY-MM-DD (default: {SIM_DATE})"
    )
    args = parser.parse_args()

    simulate(args.scenario, args.date)
