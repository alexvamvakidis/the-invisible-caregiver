"""
Full data pipeline: collect → carryover → spoken summary.

This is the single entry point for both the Safety Auditor (1-hour window)
and the Narrator (24-hour window).  Three data sources are supported:

  1. Live ThingsBoard — fetches real telemetry from the running Docker instance.
  2. Scenario files — loads pre-generated per-sensor JSON from simulation/.
     Run 'python simulation/simulate.py <normal|decline|hazard>' first.
  3. Pre-built summary JSON — a cached pipeline output written by 'cli.py fetch'.

The pipeline performs three steps every time:
  collect   — retrieve raw sensor readings and summarise boolean events +
               continuous-sensor statistics (min/max/avg) grouped by room.
  carryover — load the previous audit's active-sensor state and merge
               cross-window durations (e.g. stove was already on for 40 min
               before this window started).
  narrative — convert the summary into a human-readable spoken text grouped
               by room, ready to inject verbatim into an LLM prompt.

Callers receive (spoken_text, window_start_ms, window_end_ms).
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from config.settings import HISTORY_WINDOW_MS, SCENARIO_FILES
from data.collector import get_all_rooms, summarize
from data.carryover import clear_carryover, compute_carryover, load_carryover, save_carryover
from data.spoken_summary import format_as_text, render_spoken_summary, save_spoken_summary


# ── Collection helpers ────────────────────────────────────────────────────────

def _iso_to_ms(ts_str: str) -> int:
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def _collect_scenario(name: str, window_ms: int | None) -> tuple[dict, int, int]:
    """Load and summarize per-sensor JSON files from a local scenario directory."""
    data_dir = Path(SCENARIO_FILES.get(name, ""))
    if not data_dir.exists():
        sys.exit(
            f"Error: scenario '{name}' not found at {data_dir}. "
            f"Run 'python simulation/simulate.py {name}' first."
        )

    all_files_data: list[tuple[str, str, list]] = []
    max_ts = 0
    for json_file in sorted(data_dir.glob("*.json")):
        data = json.load(json_file.open())
        sensor_id = data["sensor_id"]
        room = data.get("room", "unknown")
        readings = data.get("readings", [])
        for r in readings:
            ts = _iso_to_ms(r["timestamp"])
            if ts > max_ts:
                max_ts = ts
        all_files_data.append((sensor_id, room, readings))

    window_end_ms = max_ts
    window_start_ms = (max_ts - window_ms) if window_ms else 0

    raw: dict = {}
    for sensor_id, room, readings in all_files_data:
        entries = [
            {"ts": _iso_to_ms(r["timestamp"]), "value": r["value"]}
            for r in readings
            if _iso_to_ms(r["timestamp"]) >= window_start_ms
        ]
        if entries:
            raw.setdefault(room, {}).setdefault(sensor_id, {})[sensor_id] = entries

    return summarize(raw, window_end_ms=window_end_ms), window_start_ms, window_end_ms


def _collect_file(path: str, window_ms: int) -> tuple[dict, int, int]:
    """Load a pre-built summary JSON file (from 'cli.py fetch' or demo/hourly/)."""
    p = Path(path)
    if not p.exists():
        sys.exit(f"Error: file not found: {path}")
    data = json.loads(p.read_text())

    # Derive the window from embedded event timestamps so demo files get the
    # correct window context in audit prompts (not the current wall-clock time).
    all_ts: list[int] = []
    for sensors in data.values():
        if not isinstance(sensors, dict):
            continue
        for sensor_data in sensors.values():
            if not isinstance(sensor_data, dict):
                continue
            for ev in sensor_data.get("events", []):
                if isinstance(ev, dict) and "ts" in ev:
                    ts = int(ev["ts"])
                    all_ts.append(ts)
                    dur = ev.get("duration_sec") or 0
                    all_ts.append(ts + int(dur) * 1000)

    if all_ts:
        window_start_ms = min(all_ts)
        window_end_ms   = max(all_ts)
    else:
        now_ms = int(time.time() * 1000)
        window_start_ms = now_ms - window_ms
        window_end_ms   = now_ms

    return data, window_start_ms, window_end_ms


def _collect_live(window_ms: int) -> tuple[dict, int, int]:
    """Fetch live telemetry from ThingsBoard."""
    now_ms = int(time.time() * 1000)
    raw = get_all_rooms(window_ms)
    return summarize(raw, window_end_ms=now_ms), now_ms - window_ms, now_ms


# ── Public entry point ────────────────────────────────────────────────────────

def run(
    *,
    scenario: str | None = None,
    file: str | None = None,
    window_ms: int = HISTORY_WINDOW_MS,
    update_state: bool = True,
) -> tuple[str, dict, int, int]:
    """
    Full data pipeline: collect → carryover → spoken summary.

    Parameters
    ----------
    scenario      : load from local simulation files (None = live ThingsBoard)
    file          : load from a pre-built summary JSON file (overrides scenario)
    window_ms     : telemetry window in milliseconds
    update_state  : True for audit (saves carryover + report); False for narrator

    Returns
    -------
    spoken_text       : plain-text narrative ready to inject into an LLM prompt
    window_start_ms   : window start as Unix milliseconds
    window_end_ms     : window end as Unix milliseconds
    """
    # 1. Collect
    if file:
        raw_summary, window_start_ms, window_end_ms = _collect_file(file, window_ms)
    elif scenario:
        raw_summary, window_start_ms, window_end_ms = _collect_scenario(scenario, window_ms)
    else:
        raw_summary, window_start_ms, window_end_ms = _collect_live(window_ms)

    # 2. Carryover — load previous state, compute new state for this window
    prev_carryover = load_carryover()
    new_carryover = compute_carryover(raw_summary, window_start_ms, window_end_ms, prev_carryover)
    if update_state:
        has_something = bool(new_carryover.get("active_sensors")) or new_carryover.get("medication_taken_today")
        if has_something:
            save_carryover(new_carryover)
        else:
            clear_carryover()

    # 3. Spoken summary — previous carryover merges cross-window durations into the narrative
    narrative = render_spoken_summary(raw_summary, window_start_ms, window_end_ms, prev_carryover)
    spoken_text = format_as_text(narrative)

    # 4. Persist report for audit runs
    if update_state:
        save_spoken_summary(narrative, window_end_ms)

    return spoken_text, window_start_ms, window_end_ms
