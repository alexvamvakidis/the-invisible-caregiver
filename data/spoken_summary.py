"""
Translates raw sensor summary dicts into spoken-language narratives and saves them to JSON.
One descriptive sentence per sensor, incorporating carryover state from the previous window.
"""
import json
from datetime import datetime
from pathlib import Path

_REPORTS_DIR = Path(__file__).parent.parent / "reports"

# Per-sensor display name and value labels
SENSOR_CONFIG = {
    "bathroom_motion":            {"name": "bathroom motion sensor",    "true_label": "motion detected", "false_label": "no motion"},
    "toilet_pressure":            {"name": "toilet",                    "true_label": "occupied",        "false_label": "unoccupied"},
    "bathroom_water_flow":        {"name": "bathroom water flow",       "true_label": "running",         "false_label": "off"},
    "bathroom_shower_water_temp": {"name": "shower water temperature",  "unit": "°C"},
    "bathroom_temperature":       {"name": "bathroom temperature",      "unit": "°C"},
    "bathroom_humidity":          {"name": "bathroom humidity",         "unit": "%"},
    "bed_pressure":               {"name": "bed",                       "true_label": "occupied",        "false_label": "empty"},
    "bedroom_motion":             {"name": "bedroom motion sensor",     "true_label": "motion detected", "false_label": "no motion"},
    "bedroom_lamp_plug":          {"name": "bedroom lamp",              "true_label": "on",              "false_label": "off"},
    "bedroom_temperature":        {"name": "bedroom temperature",       "unit": "°C"},
    "living_motion":              {"name": "living room motion sensor", "true_label": "motion detected", "false_label": "no motion"},
    "sofa_pressure":              {"name": "sofa (left seat)",          "true_label": "occupied",        "false_label": "empty"},
    "sofa_pressure_2":            {"name": "sofa (right seat)",         "true_label": "occupied",        "false_label": "empty"},
    "tv_plug":                    {"name": "TV",                        "true_label": "on",              "false_label": "off"},
    "entrance_motion":            {"name": "entrance motion sensor",    "true_label": "motion detected", "false_label": "no motion"},
    "entrance_door":              {"name": "entrance door",             "true_label": "open",            "false_label": "closed"},
    "kitchen_motion":             {"name": "kitchen motion sensor",     "true_label": "motion detected", "false_label": "no motion"},
    "kitchen_temperature":        {"name": "kitchen temperature",       "unit": "°C"},
    "stove_power":                {"name": "stove",                     "true_label": "on",              "false_label": "off"},
    "smoke_detector":             {"name": "smoke detector",            "true_label": "triggered",       "false_label": "clear"},
    "fridge_door":                {"name": "fridge door",               "true_label": "open",            "false_label": "closed"},
    "kitchen_faucet":             {"name": "kitchen faucet",            "true_label": "running",         "false_label": "off"},
    "kitchen_medication_cabinet": {"name": "medication cabinet",        "true_label": "open",            "false_label": "closed"},
}

_CARRYOVER_STALENESS_MS = 70 * 60 * 1000


def _fmt_dur(sec) -> str:
    if sec is None:
        return "unknown duration"
    sec = int(sec)
    if sec < 60:
        return f"{sec} sec"
    m, s = divmod(sec, 60)
    return f"{m} min" if s == 0 else f"{m} min {s} sec"


def _ts_to_time(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%H:%M:%S")


def _val_label(sensor_id: str, value) -> str:
    cfg = SENSOR_CONFIG.get(sensor_id, {})
    if str(value).lower() in ("true", "1"):
        return cfg.get("true_label", "active")
    return cfg.get("false_label", "inactive")


def _render_boolean(sensor_id: str, data: dict, carryover_sec: int | None) -> str:
    cfg    = SENSOR_CONFIG.get(sensor_id, {})
    name   = cfg.get("name", sensor_id.replace("_", " "))
    events = data.get("events", [])

    if not events:
        return f"{name}: no activity recorded in this window"

    parts     = []
    start_idx = 0

    # If carryover says sensor was active and the first event confirms the same active state,
    # prepend a carry-over phrase and merge the durations.
    if carryover_sec and carryover_sec > 0:
        first_active = str(events[0]["value"]).lower() in ("true", "1")
        if first_active:
            lbl       = _val_label(sensor_id, events[0]["value"])
            first_dur = events[0].get("duration_sec")
            if first_dur is not None:
                total = carryover_sec + first_dur
                parts.append(
                    f"already {lbl} from previous window ({_fmt_dur(carryover_sec)} carry-over), "
                    f"{lbl} for a total of {_fmt_dur(total)} in this window"
                )
            else:
                parts.append(f"already {lbl} from previous window ({_fmt_dur(carryover_sec)} carry-over)")
            start_idx = 1

    for i in range(start_idx, len(events)):
        ev  = events[i]
        lbl = _val_label(sensor_id, ev["value"])
        ts  = _ts_to_time(ev["ts"])
        dur = ev.get("duration_sec")

        if i == 0:
            # First event, no carryover applied
            if dur is not None:
                parts.append(f"was {lbl} at {ts} for {_fmt_dur(dur)}")
            else:
                parts.append(f"was {lbl} at {ts} until end of window")
        else:
            if dur is not None:
                parts.append(f"then {lbl} for {_fmt_dur(dur)}")
            else:
                parts.append(f"then {lbl} at {ts} until end of window")

    return f"{name}: " + ", ".join(parts)


def _render_continuous(sensor_id: str, data: dict) -> str:
    cfg  = SENSOR_CONFIG.get(sensor_id, {})
    name = cfg.get("name", sensor_id.replace("_", " "))
    unit = cfg.get("unit", "")
    mn, mx, avg = data.get("min"), data.get("max"), data.get("avg")
    if mn is None:
        return f"{name}: no readings"
    return f"{name}: ranged from {mn}{unit} to {mx}{unit} (average {avg}{unit})"


def _get_carryover_sec(sensor_id: str, carryover: dict | None) -> int | None:
    if not carryover:
        return None
    for s in carryover.get("active_sensors", []):
        if s["sensor_id"] == sensor_id:
            return s.get("already_active_sec")
    return None


def render_spoken_summary(
    summary: dict,
    window_start_ms: int,
    window_end_ms: int,
    carryover: dict | None = None,
) -> dict:
    """
    Produce a spoken-language JSON dict from a sensor summary.
    carryover from the previous window is merged into the first event of each affected sensor.
    """
    if carryover and abs(carryover.get("window_end_ts", 0) - window_start_ms) > _CARRYOVER_STALENESS_MS:
        carryover = None  # stale — gap between windows is too large

    rooms = {}
    for room, sensors in summary.items():
        room_out = {}
        for sensor_id, data in sensors.items():
            carry_sec = _get_carryover_sec(sensor_id, carryover)
            if data.get("type") == "boolean":
                room_out[sensor_id] = _render_boolean(sensor_id, data, carry_sec)
            elif data.get("type") == "continuous":
                room_out[sensor_id] = _render_continuous(sensor_id, data)
        if room_out:
            rooms[room] = room_out

    # Build a plain-language list of what was carried over
    carry_notes = []
    if carryover:
        for s in carryover.get("active_sensors", []):
            carry_notes.append(
                f"{s['sensor_id']} was active since {s['active_since_local']} "
                f"({_fmt_dur(s['already_active_sec'])} carry-over into this window)"
            )
        if carryover.get("medication_taken_today"):
            carry_notes.append("medication cabinet was already opened in a previous window today")

    return {
        "window_start":    datetime.fromtimestamp(window_start_ms / 1000).strftime("%Y-%m-%d %H:%M:%S"),
        "window_end":      datetime.fromtimestamp(window_end_ms   / 1000).strftime("%Y-%m-%d %H:%M:%S"),
        "carryover_notes": carry_notes if carry_notes else None,
        "rooms":           rooms,
    }


def format_as_text(narrative: dict) -> str:
    """
    Convert the spoken summary dict into a plain-text block suitable for LLM input.
    Groups sentences by room in uppercase headers.
    """
    lines = []
    carry = narrative.get("carryover_notes")
    if carry:
        lines.append("== CARRY-OVER FROM PREVIOUS WINDOW ==")
        for note in carry:
            lines.append(f"  {note}")
        lines.append("")

    for room, sensors in narrative.get("rooms", {}).items():
        lines.append(f"{room.upper().replace('_', ' ')}:")
        for _sid, sentence in sensors.items():
            lines.append(f"  {sentence}")
        lines.append("")

    return "\n".join(lines).rstrip()


def save_spoken_summary(narrative: dict, window_end_ms: int | None = None) -> Path:
    """Save the spoken summary to reports/<timestamp>.json. Returns the path written."""
    _REPORTS_DIR.mkdir(exist_ok=True)
    if window_end_ms:
        ts_str = datetime.fromtimestamp(window_end_ms / 1000).strftime("%Y-%m-%d_%H-%M-%S")
    else:
        ts_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = _REPORTS_DIR / f"report_{ts_str}.json"
    path.write_text(json.dumps(narrative, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
