#!/usr/bin/env python3
"""
Verification checks for scenario and ThingsBoard data.

  python verify.py events   [--scenario normal|decline|hazard]
  python verify.py tb       [--scenario normal|decline|hazard]
  python verify.py types    [--scenario normal|decline|hazard]
  python verify.py all      [--scenario normal|decline|hazard]
"""

import argparse
import json
import sys
from pathlib import Path

from config.settings import SCENARIO_FILES, EVENT_FILES, SENSOR_KEYS_BY_ROOM

ALL_SENSOR_IDS = {sid for ids in SENSOR_KEYS_BY_ROOM.values() for sid in ids}

UNIT_VALIDATORS = {
    "boolean": lambda v: isinstance(v, bool),
    "celsius": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "watt":    lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "kwh":     lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "%rh":     lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "state":   lambda v: isinstance(v, str),
    "string":  lambda v: isinstance(v, str),
}


def _load_json(path) -> dict:
    p = Path(path)
    if not p.exists():
        sys.exit(f"File not found: {path}")
    with open(p) as f:
        return json.load(f)


# Check if events happen in scenario

def _time_only(timestamp: str) -> str:
    """Extract HH:MM:SS from an ISO timestamp or normalise a bare HH:MM string."""
    t = timestamp.split("T")[-1].rstrip("Z")  # "07:05:00" or already "07:05"
    return t if t.count(":") == 2 else t + ":00"


def check_events_in_scenario(name: str) -> dict:
    """
    Verify that every sensor_id, value, and time in the events file
    appears in the scenario readings. Time is compared as HH:MM:SS only,
    ignoring the date part of scenario timestamps.
    """
    scenario = _load_json(SCENARIO_FILES[name])
    events   = _load_json(EVENT_FILES[name])

    # Build a set of (sensor_id, time_only, value) from scenario for fast lookup
    scenario_index = {
        (r["sensor_id"], _time_only(r["timestamp"]), r["value"])
        for r in scenario.get("readings", [])
    }

    missing = []
    for ev in events.get("events", []):
        key = (ev["sensor_id"], _time_only(ev["time"]), ev["value"])
        if key not in scenario_index:
            missing.append({"sensor_id": ev["sensor_id"], "time": ev["time"], "value": ev["value"]})

    return {
        "ok":     len(missing) == 0,
        "missing": missing,
    }



# Check if thingsboard telemetry matches events

def check_thingsboard_vs_events(name: str) -> dict:
    """
    For each event in the events file whose sensor_id is tracked in ThingsBoard,
    verify that the expected value appears somewhere in the live telemetry.
    Uses a 24-hour window to cover a full simulated day.
    """
    import time as _time
    from data.collector import get_token, get_device_id_map, get_telemetry

    events_data = _load_json(EVENT_FILES[name])
    events      = events_data.get("events", [])

    token     = get_token()
    device_map = get_device_id_map(token)

    matched, missing, skipped = [], [], []

    # Group events by sensor so we fetch telemetry once per device
    by_sensor: dict = {}
    for ev in events:
        sid = ev["sensor_id"]
        by_sensor.setdefault(sid, []).append(ev)
        

    for sid, evs in by_sensor.items():
        if sid not in ALL_SENSOR_IDS:
            skipped.append({"sensor_id": sid, "reason": "not in SENSOR_KEYS_BY_ROOM"})
            continue

        device_id = device_map.get(sid)
        if device_id is None:
            skipped.append({"sensor_id": sid, "reason": "not registered in ThingsBoard"})
            continue

        telemetry = get_telemetry(device_id, [sid], token, window_ms=86_400_000)
        tb_values = {str(r["value"]).lower() for r in telemetry.get(sid, [])}

        for ev in evs:
            expected = str(ev["value"]).lower()
            entry = {"sensor_id": sid, "time": ev["time"], "expected": ev["value"]}
            if expected in tb_values:
                matched.append(entry)
            else:
                missing.append(entry)

    return {
        "ok":      len(missing) == 0,
        "matched": matched,
        "missing": missing,
        "skipped": skipped,
    }


#  Check sensor values in scenario match their declared unit 

def check_sensor_types(name: str) -> dict:
    """
    Verify that each reading's value is consistent with its declared unit,
    and that every sensor_id in the scenario is present in SENSOR_KEYS_BY_ROOM.
    """
    scenario = _load_json(SCENARIO_FILES[name])
    readings = scenario.get("readings", [])

    type_errors_seen = {}   
    unknown_sensors  = set()

    for r in readings:
        sid   = r["sensor_id"]
        unit  = r.get("unit", "")
        value = r["value"]

        if sid not in ALL_SENSOR_IDS:
            unknown_sensors.add(sid)

        validator = UNIT_VALIDATORS.get(unit)
        if validator and not validator(value) and sid not in type_errors_seen:
            type_errors_seen[sid] = {
                "sensor_id":  sid,
                "unit":       unit,
                "value_type": type(value).__name__,
                "example":    value,
            }

    type_errors = list(type_errors_seen.values())

    return {
        "ok":              len(type_errors) == 0 and len(unknown_sensors) == 0,
        "type_errors":     type_errors,
        "unknown_sensors": sorted(unknown_sensors),
    }


#  Pretty printer 

def _print_result(title: str, result: dict) -> None:
    status = "PASS" if result["ok"] else "FAIL"
    print(f"\n[{status}] {title}")
    print("-" * (len(title) + 8))
    for field, val in result.items():
        if field == "ok":
            continue
        if isinstance(val, list) and val:
            print(f"  {field} ({len(val)}):")
            for item in val:
                print(f"    • {item}")
        elif isinstance(val, list):
            print(f"  {field}: (none)")
        else:
            print(f"  {field}: {val}")


#  CLI 
def main() -> None:
    parser = argparse.ArgumentParser(description="Verify scenario and ThingsBoard data")
    parser.add_argument(
        "check",
        choices=["events", "tb", "types", "all"],
        help="Which check to run",
    )
    parser.add_argument(
        "--scenario",
        choices=["normal", "decline", "hazard"],
        default="normal",
    )
    args = parser.parse_args()
    name = args.scenario

    checks = {
        "events": ("Events → Scenario coverage",  check_events_in_scenario),
        "tb":     ("ThingsBoard vs Events",        check_thingsboard_vs_events),
        "types":  ("Sensor type correctness",      check_sensor_types),
    }

    to_run = list(checks.items()) if args.check == "all" else [(args.check, checks[args.check])]

    all_ok = True
    for key, (title, fn) in to_run:
        result = fn(name)
        _print_result(f"[{name}] {title}", result)
        if not result["ok"]:
            all_ok = False

    print()
    if len(to_run) > 1:
        print("Overall:", "ALL PASSED" if all_ok else "SOME CHECKS FAILED")

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
