import json
from datetime import datetime
from pathlib import Path

CARRYOVER_PATH = Path(__file__).parent.parent / "audit_carryover.json"

# Sensor ID → the value that means "active / in a concerning state"
# These are sensors whose cumulative ON/OPEN duration matters across hour boundaries.
TRACKED_SENSORS = {
    "stove_power":                True,   # True = stove is ON
    "bathroom_water_flow":        True,   # True = water is running
    "kitchen_faucet":             True,   # True = water is running
    "fridge_door":                True,   # True = door is open
    "entrance_door":              True,   # True = door is open
    "toilet_pressure":            True,   # True = occupied (fall risk)
    "kitchen_medication_cabinet": True,   # True = opened (track taken-today flag)
}


def compute_carryover(
    summary: dict,
    window_start_ms: int,
    window_end_ms: int,
    prev_carryover: dict | None = None,
) -> dict:
    """
    Extract sensors still in an active/concerning state at the end of the current window.
    Both timestamps are Unix milliseconds.

    prev_carryover: the carryover saved at the end of the previous window.
      - If a sensor was already tracked there AND its first event in this window is still
        active (meaning it never turned off), the original active_since_ts is preserved
        and already_active_sec accumulates across both windows.
      - If the sensor is no longer active at the end of this window, its entry is dropped.
    """
    # Build a quick lookup: sensor_id → prev entry
    prev_by_sensor: dict = {}
    if prev_carryover:
        for s in prev_carryover.get("active_sensors", []):
            prev_by_sensor[s["sensor_id"]] = s

    active_sensors = []
    medication_taken = False

    for room, sensors in summary.items():
        for sensor_id, data in sensors.items():
            if sensor_id not in TRACKED_SENSORS:
                continue
            if data.get("type") != "boolean":
                continue

            active_value = TRACKED_SENSORS[sensor_id]

            # Medication: track whether it was opened at any point during this window.
            if sensor_id == "kitchen_medication_cabinet":
                for ev in data.get("events", []):
                    if ev["value"] == active_value:
                        medication_taken = True
                        break
                continue

            # For all other tracked sensors: is it currently in the active state?
            if data.get("current") != active_value:
                # State changed — entry is implicitly dropped (not added below).
                continue

            events = data.get("events", [])

            # Find the last transition INTO the active state.
            last_active_event = None
            for ev in reversed(events):
                if ev["value"] == active_value:
                    last_active_event = ev
                    break
            if last_active_event is None:
                continue

            # If the sensor was in prev_carryover AND its first event in this window is
            # already active (it never turned off between windows), accumulate the full
            # continuous duration from the original activation time.
            prev_entry = prev_by_sensor.get(sensor_id)
            first_event_is_active = (
                events and str(events[0]["value"]).lower() in ("true", "1")
            )
            if prev_entry and first_event_is_active and last_active_event is events[0]:
                # Sensor was continuously active since the previous window.
                active_since_ts  = prev_entry["active_since_ts"]
                already_active_sec = (window_end_ms - active_since_ts) // 1000
            else:
                active_since_ts  = last_active_event["ts"]
                already_active_sec = (window_end_ms - last_active_event["ts"]) // 1000

            active_sensors.append({
                "sensor_id": sensor_id,
                "room": room,
                "active_since_ts": active_since_ts,
                "active_since_local": datetime.fromtimestamp(
                    active_since_ts / 1000
                ).strftime("%H:%M:%S"),
                "already_active_sec": already_active_sec,
            })

    return {
        "window_start_ts":        window_start_ms,
        "window_end_ts":          window_end_ms,
        "window_start_local":     datetime.fromtimestamp(window_start_ms / 1000).strftime("%Y-%m-%d %H:%M:%S"),
        "window_end_local":       datetime.fromtimestamp(window_end_ms   / 1000).strftime("%Y-%m-%d %H:%M:%S"),
        "active_sensors":         active_sensors,
        "medication_taken_today": medication_taken,
    }


def clear_carryover() -> None:
    if CARRYOVER_PATH.exists():
        CARRYOVER_PATH.unlink()


def save_carryover(state: dict) -> None:
    CARRYOVER_PATH.write_text(json.dumps(state, indent=2))


def load_carryover() -> dict | None:
    if not CARRYOVER_PATH.exists():
        return None
    try:
        return json.loads(CARRYOVER_PATH.read_text())
    except Exception:
        return None


def format_carryover_section(carryover: dict | None) -> str:
    """Format carryover state as a prompt section to inject into AUDIT_USER."""
    if not carryover:
        return ""

    lines = [
        "== CARRYOVER FROM PREVIOUS HOUR ==",
        f"Previous window: {carryover.get('window_start_local', 'unknown')} → {carryover.get('window_end_local', 'unknown')}",
        "",
        "If a sensor listed below is still active (same state) at the start of this window,",
        "ADD its already_active_sec to the total continuous duration when evaluating",
        "any time-based rule (stove ON >60 min, water running >30 min, fridge open >10 min, etc.).",
        "",
    ]

    sensors = carryover.get("active_sensors", [])
    if sensors:
        lines.append("Sensors still active at end of previous window:")
        for s in sensors:
            lines.append(
                f"  - {s['sensor_id']} ({s['room']}): active since {s['active_since_local']}, "
                f"already active for {s['already_active_sec']} sec "
                f"({s['already_active_sec'] // 60} min {s['already_active_sec'] % 60} sec)"
            )
    else:
        lines.append("No sensors were in an active state at end of previous window.")

    med = carryover.get("medication_taken_today")
    if med is True:
        lines.append(
            "  - kitchen_medication_cabinet: already opened earlier today — "
            "do NOT flag MISSED_MEDICATION for this window."
        )
    else:
        lines.append(
            "  - kitchen_medication_cabinet: NOT yet opened today."
        )

    lines.append("=================================")
    lines.append("")
    return "\n".join(lines)
