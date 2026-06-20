#!/usr/bin/env python3
"""
Generates 24 hours of sensor data for a chosen scenario.
Outputs one JSON file per sensor under:
    simulation/sensor_data/{scenario}/{sensor_id}.json

Emission strategy:
  - Physics/continuous sensors (temperature, humidity): emit every SIM_INTERVAL_SEC seconds.
  - Boolean sensors: emit on state-change OR every HEARTBEAT_SEC seconds (whichever comes first).
    This matches real IoT behaviour — no point recording 17 000 identical "False" readings.

Physics models (state held clean; noise only added to recorded value):
  - kitchen_temperature  : Newton-Heating toward 50 °C (τ=90 min) when stove on;
                           Newton cooling toward ambient (τ=90 min) when off.
  - bathroom_temperature : Rises toward 25 °C when shower on (τ=40 min);
                           cools when off (τ=40 min).
  - bedroom_temperature  : Sinusoidal diurnal curve ±0.8 °C (coolest 04:00, warmest 16:00).
  - bathroom_humidity    : Rises at 2 %/min during shower (cap 87 %);
                           decays with 15-min time constant after.
  - bathroom_shower_water_temp : Heats to 39 °C at 4 °C/min when flowing;
                                 cools to ambient at 3 °C/min when stopped.

sofa_pressure_2 is derived from sofa_pressure (both cushions respond together).

Usage:
    python simulate.py normal
    python simulate.py decline
    python simulate.py hazard
    python simulate.py normal --date 2026-06-01
"""

import csv
import json
import math
import sys
import random
import argparse
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import CSV_PATH, SIM_INTERVAL_SEC, SIM_DATE, EVENT_FILES, SENSOR_DATA_DIR


BASELINE = {
    "bathroom_motion":            False,
    "toilet_pressure":            False,
    "bathroom_water_flow":        False,
    "bathroom_shower_water_temp": 20.0,
    "bathroom_humidity":          55.0,
    "bathroom_temperature":       20.0,
    "bed_pressure":               True,
    "bedroom_motion":             False,
    "bedroom_lamp_plug":          False,
    "bedroom_temperature":        19.5,
    "living_motion":              False,
    "sofa_pressure":              False,
    "sofa_pressure_2":            False,
    "tv_plug":                    False,
    "entrance_motion":            False,
    "entrance_door":              True,
    "kitchen_motion":             False,
    "kitchen_temperature":        21.0,
    "stove_power":                False,
    "smoke_detector":             False,
    "fridge_door":                True,
    "kitchen_faucet":             True,
    "kitchen_medication_cabinet": True,
}

_SEEDS     = {"normal": 42, "decline": 43, "hazard": 44}
HEARTBEAT_SEC = 300  # boolean sensors re-publish their state every 5 minutes

PHYSICS_SENSORS = {
    "kitchen_temperature",
    "bathroom_temperature",
    "bedroom_temperature",
    "bathroom_humidity",
    "bathroom_shower_water_temp",
}


def load_sensors():
    with open(CSV_PATH, newline="") as f:
        return {row["sensor_id"]: row for row in csv.DictReader(f)}


def _diurnal(sec, base, amplitude):
    """Sinusoidal temperature: minimum at 04:00 (14 400 s), maximum at 16:00."""
    phase = 2 * math.pi * (sec - 14_400) / 86_400
    return base - amplitude * math.cos(phase)


def _compute_physics(sensor_id, current, state, interval, sec):
    """
    Advance a physics-driven sensor by one time step.
    Returns (new_state, recorded_value).

    new_state      — clean physics value stored in state (no noise).
    recorded_value — value written to JSON (small Gaussian noise added).

    Separating the two prevents noise from accumulating as a random walk
    across 17 000 steps.
    """
    if sensor_id == "kitchen_temperature":
        ambient = 21.0
        if state.get("stove_power"):
            # Newton-Heating: exponential approach toward 50 °C, τ = 90 min.
            # T(t) = T_ss − (T_ss − T₀) · e^(−t/τ)
            T_ss = 50.0
            tau  = 90 * 60
            new  = T_ss - (T_ss - current) * math.exp(-interval / tau)
        else:
            # Newton cooling toward ambient, same time constant.
            tau = 90 * 60
            new = ambient + (current - ambient) * math.exp(-interval / tau)
        return new, round(new + random.gauss(0, 0.12), 1)

    if sensor_id == "bathroom_temperature":
        ambient = 20.0
        tau = 40 * 60
        if state.get("bathroom_water_flow"):
            T_ss = 25.0
            new  = T_ss - (T_ss - current) * math.exp(-interval / tau)
        else:
            new = ambient + (current - ambient) * math.exp(-interval / tau)
        return new, round(new + random.gauss(0, 0.10), 1)

    if sensor_id == "bedroom_temperature":
        new = _diurnal(sec, 20.0, 0.8)
        return new, round(new + random.gauss(0, 0.08), 1)

    if sensor_id == "bathroom_humidity":
        ambient = 55.0
        if state.get("bathroom_water_flow"):
            new = min(current + (2.0 / 60) * interval, 87.0)
        else:
            new = max(ambient + (current - ambient) * math.exp(-interval / 900.0), ambient)
        return new, round(new + random.gauss(0, 0.4), 1)

    if sensor_id == "bathroom_shower_water_temp":
        if state.get("bathroom_water_flow"):
            new = min(current + (4.0 / 60) * interval, 39.0)
        else:
            new = max(current - (3.0 / 60) * interval, 20.0)
        return new, round(new + random.gauss(0, 0.25), 1)

    return current, current  # should not reach here


def simulate(scenario, date_str):
    sensors = load_sensors()
    random.seed(_SEEDS.get(scenario, 42))

    base_dt = datetime.strptime(date_str, "%Y-%m-%d")

    ev_path = Path(EVENT_FILES[scenario])
    if not ev_path.exists():
        print(f"Error: {ev_path} not found.")
        return

    ev_data = json.loads(ev_path.read_text())

    # Build event lookup: elapsed_second → [(sensor_id, value), ...]
    event_map = defaultdict(list)
    for ev in ev_data["events"]:
        parts = list(map(int, ev["time"].split(":")))
        h, m = parts[0], parts[1]
        s = parts[2] if len(parts) > 2 else 0
        event_map[h * 3600 + m * 60 + s].append((ev["sensor_id"], ev["value"]))

    state    = dict(BASELINE)
    TOTAL_SEC = 86_400

    sensor_readings = {sid: [] for sid in sensors}

    # Heartbeat tracking for boolean sensors
    # Initialise last_emit_sec so the first step (sec=0) triggers an emit for all booleans.
    last_emit_sec = {sid: -HEARTBEAT_SEC for sid in sensors}
    last_emit_val = {}

    def emit(sensor_id, value, ts_str, sec):
        """Append a reading and update heartbeat trackers."""
        sensor_readings[sensor_id].append({"timestamp": ts_str, "value": value})
        last_emit_sec[sensor_id] = sec
        last_emit_val[sensor_id] = value

    for step_sec in range(0, TOTAL_SEC, SIM_INTERVAL_SEC):
        dt     = base_dt + timedelta(seconds=step_sec)
        ts_now = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        exact_at_step = {}

        # ── Process all events in [step_sec, step_sec + SIM_INTERVAL_SEC) ──────
        for s in range(step_sec, step_sec + SIM_INTERVAL_SEC):
            for sensor_id, new_value in event_map.get(s, []):
                if sensor_id not in sensors:
                    continue
                state[sensor_id] = new_value

                # Derived: sofa_pressure_2 always mirrors sofa_pressure
                if sensor_id == "sofa_pressure" and "sofa_pressure_2" in sensors:
                    state["sofa_pressure_2"] = new_value
                    if s != step_sec:
                        ts = (base_dt + timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%SZ")
                        emit("sofa_pressure_2", new_value, ts, s)
                    else:
                        exact_at_step["sofa_pressure_2"] = new_value

                if s != step_sec:
                    ts = (base_dt + timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    emit(sensor_id, new_value, ts, s)
                else:
                    exact_at_step[sensor_id] = new_value

        # ── Sample every sensor at the step boundary ──────────────────────────
        for sensor_id in sensors:
            if sensor_id in exact_at_step:
                # An event fired exactly on this step — always emit.
                emit(sensor_id, exact_at_step[sensor_id], ts_now, step_sec)

            elif sensor_id in PHYSICS_SENSORS:
                # Continuous sensor — emit every step, no heartbeat logic.
                new_state, value = _compute_physics(
                    sensor_id, state[sensor_id], state, SIM_INTERVAL_SEC, step_sec
                )
                state[sensor_id] = new_state
                sensor_readings[sensor_id].append({"timestamp": ts_now, "value": value})

            else:
                # Boolean sensor — emit only on change or heartbeat.
                value   = state[sensor_id]
                elapsed = step_sec - last_emit_sec.get(sensor_id, step_sec - HEARTBEAT_SEC)
                if value != last_emit_val.get(sensor_id) or elapsed >= HEARTBEAT_SEC:
                    emit(sensor_id, value, ts_now, step_sec)

    # ── Write one JSON file per sensor ────────────────────────────────────────
    out_dir = Path(SENSOR_DATA_DIR) / scenario
    out_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    for sensor_id, readings in sensor_readings.items():
        meta = sensors[sensor_id]
        readings.sort(key=lambda r: r["timestamp"])
        output = {
            "sensor_id":   sensor_id,
            "sensor_type": meta["sensor_type"],
            "room":        meta["room"],
            "location":    meta["location"],
            "unit":        meta["unit"],
            "scenario":    ev_data["scenario"],
            "description": ev_data["description"],
            "date":        date_str,
            "readings":    readings,
        }
        (out_dir / f"{sensor_id}.json").write_text(json.dumps(output, indent=2))
        total += len(readings)

    steps = TOTAL_SEC // SIM_INTERVAL_SEC
    bool_sensors   = len(sensors) - len(PHYSICS_SENSORS)
    physics_sensors = len(PHYSICS_SENSORS)
    print(f"Scenario  : {ev_data['scenario']}")
    print(f"Sensors   : {bool_sensors} boolean (on-change + 5-min heartbeat) | "
          f"{physics_sensors} continuous (every {SIM_INTERVAL_SEC}s)")
    print(f"Readings  : {total:,}  across {len(sensors)} files")
    print(f"Saved to  : simulation/sensor_data/{scenario}/")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Simulate 24 h of sensor data for a scenario."
    )
    parser.add_argument("scenario", choices=["normal", "decline", "hazard"])
    parser.add_argument(
        "--date",
        default=SIM_DATE,
        help=f"Simulation date YYYY-MM-DD (default: {SIM_DATE})",
    )
    args = parser.parse_args()
    simulate(args.scenario, args.date)
