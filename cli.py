#!/usr/bin/env python3
"""
Terminal interface for The Invisible Caregiver.

Commands
--------
  python cli.py report [--scenario normal|decline|hazard]
      Run the LLM safety audit and print the result.

  python cli.py chat [--scenario normal|decline|hazard]
      Start an interactive Q&A session with the narrator.

Without --scenario the data is pulled live from ThingsBoard.
With    --scenario a local JSON file is used (no ThingsBoard needed).
"""

import argparse
import json
import sys
from pathlib import Path

from config.settings import SCENARIO_FILES, HISTORY_WINDOW_MS
from llm.client import audit, narrate


# Scenario loader

def _load_scenario_summary(name: str) -> dict:
    """Build a sensor summary dict from a local scenario JSON file,
    matching the output format of collector.summarize()."""
    path = SCENARIO_FILES.get(name)
    if not path or not Path(path).exists():
        sys.exit(f"Error: scenario '{name}' not found at {path}")

    with open(path) as f:
        data = json.load(f)

    # Group readings by room → sensor_id → list of {value, timestamp}
    raw: dict = {}
    for r in data.get("readings", []):
        room      = r.get("room", "unknown")
        sensor_id = r.get("sensor_id", "unknown")
        raw.setdefault(room, {}).setdefault(sensor_id, []).append({
            "value": r.get("value"),
            "ts":    r.get("timestamp", ""),
        })

    summary: dict = {}
    for room, sensors in raw.items():
        summary[room] = {}
        for sid, entries in sensors.items():
            if not entries:
                continue

            first_val = entries[0]["value"]

            # Continuous sensor
            if isinstance(first_val, (int, float)) and not isinstance(first_val, bool):
                floats = [e["value"] for e in entries]
                summary[room][sid] = {
                    "type": "continuous",
                    "min":  min(floats),
                    "max":  max(floats),
                    "avg":  round(sum(floats) / len(floats), 2),
                }

            # Boolean/state sensor — extract transitions with timestamps
            else:
                events, prev = [], None
                for e in entries:
                    if e["value"] != prev:
                        events.append({"value": e["value"], "time": e["ts"]})
                        prev = e["value"]

                # Annotate with duration between transitions
                annotated = []
                for i, ev in enumerate(events):
                    duration_sec = None
                    if i + 1 < len(events):
                        try:
                            from datetime import datetime, timezone
                            t1 = datetime.fromisoformat(ev["time"].replace("Z", "+00:00"))
                            t2 = datetime.fromisoformat(events[i+1]["time"].replace("Z", "+00:00"))
                            duration_sec = int((t2 - t1).total_seconds())
                        except Exception:
                            pass
                    annotated.append({
                        "value":        ev["value"],
                        "time":         ev["time"],
                        "duration_sec": duration_sec,
                    })

                summary[room][sid] = {
                    "type":    "boolean",
                    "current": entries[-1]["value"],
                    "events":  annotated,
                }

    return summary


# Live data loader 

def _load_live_summary(window_ms=None) -> dict:
    from data.collector import get_all_rooms, summarize
    raw = get_all_rooms(window_ms)
    return  summarize(raw)


# Commands 

def cmd_summary(scenario: str | None, window_ms: int | None = None) -> None:
    print("Fetching sensor data…")
    summary = _load_scenario_summary(scenario) if scenario else _load_live_summary(window_ms)
    print(json.dumps(summary, indent=2))


def cmd_report(scenario: str | None, window_ms: int | None = HISTORY_WINDOW_MS) -> None:
    print("Fetching sensor data…")
    summary = _load_scenario_summary(scenario) if scenario else _load_live_summary(window_ms)

    print("Running safety audit…\n")
    result = audit(summary)

    alert    = result.get("alert", False)
    severity = result.get("severity", "none")
    issues   = result.get("issues", [])
    message  = result.get("message", "")

    tag = f"[{'ALERT' if alert else 'OK'}] severity={severity}"
    print(tag)
    print("-" * len(tag))
    if issues:
        for issue in issues:
            print(f"  • {issue}")
    print()
    print(message)


def cmd_chat(scenario: str | None, window_ms: int | None = None) -> None:
    print("Fetching sensor data…")
    summary = _load_scenario_summary(scenario) if scenario else _load_live_summary(window_ms)
    print("Ready. Type your question or 'quit' to exit.\n")

    while True:
        try:
            query = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not query or query.lower() in {"quit", "exit", "q"}:
            break

        response = narrate(summary, query)
        print(f"\nCaregiver: {response}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="The Invisible Caregiver — terminal interface"
    )
    parser.add_argument(
        "command",
        choices=["report", "chat", "summary"],
        help="'report' for safety audit, 'chat' for Q&A, 'summary' to print sensor data",

    )
    parser.add_argument(
        "--scenario",
        choices=["normal", "decline", "hazard"],
        default=None,
        help="Use a local scenario file instead of live ThingsBoard data",
    )
    parser.add_argument(
        "--window-ms",
        type=int,
        default=HISTORY_WINDOW_MS,
        help=f"How far back to pull telemetry data (ms, default {HISTORY_WINDOW_MS})",
    )
    
    args = parser.parse_args()

    if args.command == "report":
        cmd_report(args.scenario, args.window_ms)
    elif args.command == "summary":
        cmd_summary(args.scenario, args.window_ms)
    else:
        cmd_chat(args.scenario, args.window_ms)


if __name__ == "__main__":
    main()
