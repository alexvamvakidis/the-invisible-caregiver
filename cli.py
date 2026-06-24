#!/usr/bin/env python3
"""
Terminal interface for The Invisible Caregiver.

Commands
--------
  python cli.py report [--scenario normal|decline|hazard] [--window-ms N]
     Run the LLM on the last hour of data and print a structured alert.

  python cli.py chat [--scenario normal|decline|hazard] [--window-ms N]
      Chat session where a caretaker can ask natural language questions about the last 24 hours.

  python cli.py summary [--scenario normal|decline|hazard] [--window-ms N]
      Print the raw sensor summary JSON (useful for debugging).

  python cli.py fetch [--out-hour FILE] [--out-day FILE]
      Pull live ThingsBoard data and write fetch_hour.json (1h) and
      fetch_day.json (24h) for offline debugging.

  python cli.py llm <file> [--mode report|chat] [--query TEXT]
      Run the LLM on any summary JSON file produced by 'fetch' or 'summary'.
      Default mode is 'report' (Safety Auditor).  Use --mode chat with --query
      to invoke the Narrator for a single question.

Without --scenario the data is pulled live from ThingsBoard.
With    --scenario a local JSON file is used (no ThingsBoard needed).

Default time windows (can be overridden with --window-ms):
  report  → 1 hour   (HISTORY_WINDOW_MS)
  chat    → 24 hours (NARRATOR_WINDOW_MS)
  summary → 1 hour
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from config.settings import SCENARIO_FILES, SENSOR_DATA_DIR, HISTORY_WINDOW_MS, NARRATOR_WINDOW_MS, ALL_SENSOR_KEYS, TB_URL
from llm.client import audit, narrate


#  Helpers 
def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def _annotate_ts(obj):
    """Recursively replace every 'ts' int field with a human-readable 'timestamp' string."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "ts" and isinstance(v, int):
                out["timestamp"] = _ms_to_iso(v)
            else:
                out[k] = _annotate_ts(v)
        return out
    if isinstance(obj, list):
        return [_annotate_ts(i) for i in obj]
    return obj


#  Scenario loader 

def _iso_to_ms(ts_str: str) -> int:
    """Convert an ISO-8601 timestamp string to a Unix millisecond integer."""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def _load_scenario_summary(name: str, window_ms: int | None = None) -> tuple[dict, int, int]:
    """
    Build a sensor summary from per-sensor JSON files in the scenario directory.
    Produces the same structure as data.collector.summarize() so both
    scenario mode and live mode feed identical data to the LLM.

    window_ms: take the last window_ms milliseconds of the scenario data
               (relative to the latest timestamp in the files, not wall-clock).
               None = all data.

    Returns (summary, window_start_ms, window_end_ms).
    """
    data_dir = Path(SCENARIO_FILES.get(name, ""))
    if not data_dir.exists():
        sys.exit(f"Error: scenario '{name}' not found at {data_dir}. "
                 f"Run 'python simulation/simulate.py {name}' first.")

    # First pass: collect all readings to find the max timestamp in the scenario.
    all_files_data: list[tuple[str, str, list]] = []
    max_ts = 0
    for json_file in sorted(data_dir.glob("*.json")):
        data = json.load(json_file.open())
        sensor_id = data["sensor_id"]
        room      = data.get("room", "unknown")
        readings  = data.get("readings", [])
        for r in readings:
            ts = _iso_to_ms(r["timestamp"])
            if ts > max_ts:
                max_ts = ts
        all_files_data.append((sensor_id, room, readings))

    window_end_ms   = max_ts
    window_start_ms = (max_ts - window_ms) if window_ms else 0

    # Second pass: filter readings to the window.
    raw: dict = {}
    for sensor_id, room, readings in all_files_data:
        entries = [
            {"ts": _iso_to_ms(r["timestamp"]), "value": r["value"]}
            for r in readings
            if _iso_to_ms(r["timestamp"]) >= window_start_ms
        ]
        if entries:
            (raw
             .setdefault(room, {})
             .setdefault(sensor_id, {})[sensor_id]) = entries

    from data.collector import summarize
    return summarize(raw, window_end_ms=window_end_ms), window_start_ms, window_end_ms


#  Live data loader
def _load_live_summary(window_ms: int) -> tuple[dict, int, int]:
    import time as _time2
    from data.collector import get_all_rooms, summarize
    now_ms = int(_time2.time() * 1000)
    raw = get_all_rooms(window_ms)
    return summarize(raw, window_end_ms=now_ms), now_ms - window_ms, now_ms


#  Commands
def cmd_clear(scenario: str | None) -> None:
    """Delete all ThingsBoard telemetry for every sensor (or only those in a scenario)."""
    import requests
    from data.collector import get_token, get_device_id_map

    if scenario:
        data_dir = Path(SCENARIO_FILES.get(scenario, ""))
        if not data_dir.exists():
            sys.exit(f"Error: scenario '{scenario}' not found at {data_dir}.")
        sensor_ids = [
            json.loads(f.read_text())["sensor_id"]
            for f in sorted(data_dir.glob("*.json"))
        ]
    else:
        sensor_ids = list(ALL_SENSOR_KEYS)

    label = f"scenario '{scenario}'" if scenario else "all sensors"
    print(f"Clearing ThingsBoard telemetry for {label} ({len(sensor_ids)} sensor(s)) …")

    token      = get_token()
    device_map = get_device_id_map(token)
    headers    = {"Authorization": f"Bearer {token}"}
    cleared, skipped = 0, 0

    for sensor_id in sensor_ids:
        device_id = device_map.get(sensor_id)
        if not device_id:
            skipped += 1
            continue
        r = requests.delete(
            f"{TB_URL}/api/plugins/telemetry/DEVICE/{device_id}/timeseries/delete",
            headers=headers,
            params={"keys": sensor_id, "deleteAllDataForKeys": "true"},
        )
        if r.ok:
            cleared += 1
        else:
            print(f"  Warning: could not clear {sensor_id}: {r.status_code}")

    print(f"Done — cleared {cleared}, skipped {skipped} (not registered in ThingsBoard).")


def cmd_fetch(
    do_hour: bool = True,
    do_day: bool = True,
    out_hour: str = "fetch_hour.json",
    out_day: str = "fetch_day.json",
) -> None:
    import time as _time_fetch
    from data.collector import get_all_rooms, summarize

    now_ms = int(_time_fetch.time() * 1000)

    if do_hour:
        print("Fetching 1-hour window from ThingsBoard…")
        summary_hour = _annotate_ts(summarize(get_all_rooms(HISTORY_WINDOW_MS), window_end_ms=now_ms))
        Path(out_hour).write_text(json.dumps(summary_hour, indent=2))
        print(f"  → {out_hour}")

    if do_day:
        print("Fetching 24-hour window from ThingsBoard…")
        summary_day = _annotate_ts(summarize(get_all_rooms(NARRATOR_WINDOW_MS), window_end_ms=now_ms))
        Path(out_day).write_text(json.dumps(summary_day, indent=2))
        print(f"  → {out_day}")

def _load_summary(file: str | None, scenario: str | None, window_ms: int,
                  label: str) -> tuple[dict, int, int]:
    """Returns (summary, window_start_ms, window_end_ms)."""
    if file:
        import time as _time2
        path = Path(file)
        if not path.exists():
            sys.exit(f"Error: file not found: {file}")
        print(f"Loading sensor data from {file}…")
        now_ms = int(_time2.time() * 1000)
        return json.loads(path.read_text()), now_ms - window_ms, now_ms
    print(f"Fetching sensor data ({label})…")
    if scenario:
        return _load_scenario_summary(scenario, window_ms)
    return _load_live_summary(window_ms)


def cmd_report(scenario: str | None, window_ms: int, file: str | None = None,
               show_raw: bool = False, debug: bool = False) -> None:
    """Requirement A — Safety Auditor (last 1 hour)."""
    summary, window_start_ms, window_end_ms = _load_summary(file, scenario, window_ms, "last 1 hour")

    # Generate and save spoken-language narrative before running the audit.
    from data.carryover import load_carryover
    from data.spoken_summary import render_spoken_summary, save_spoken_summary
    carryover = load_carryover()
    narrative = render_spoken_summary(summary, window_start_ms, window_end_ms, carryover)
    out_path  = save_spoken_summary(narrative, window_end_ms)
    print(f"Sensor narrative saved -> reports/{out_path.name}\n")

    print("Running safety audit…\n")
    result = audit(summary, window_start_ms=window_start_ms, window_end_ms=window_end_ms, debug=debug)

    if show_raw:
        print("[raw LLM output]")
        print(result.get("raw", ""))
        print()

    alert    = result.get("alert", False)
    severity = result.get("severity", "none")
    issues   = result.get("issues", [])
    message  = result.get("message", "")

    tag = f"[{'ALERT' if alert else 'OK'}] severity={severity}"
    print(tag)
    print("-" * len(tag))
    if issues:
        for issue in issues:
            rule     = issue.get("rule", "?")
            sev      = issue.get("severity", "?").upper()
            detail   = issue.get("detail", "")
            print(f"  • [{sev}] {rule}: {detail}")
    print()
    print(message)


def cmd_chat(scenario: str | None, window_ms: int, file: str | None = None, show_raw: bool = False) -> None:
    """Requirement B — The Narrator (last 24 hours, on-demand)."""
    summary, window_start_ms, window_end_ms = _load_summary(file, scenario, window_ms, "last 24 hours")
    print("Ready. Ask a question about Mary or type 'quit' to exit.\n")
    print("Example questions:")
    print("  Did Mom have a healthy morning?")
    print("  How was Mom's day today?")
    print("  Did my Mom forget to take her medication today?\n")

    history: list = []
    while True:
        try:
            query = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not query or query.lower() in {"quit", "exit", "q"}:
            break

        response, history = narrate(summary, query, history,
                                    window_start_ms=window_start_ms, window_end_ms=window_end_ms)

        if show_raw:
            print(f"[raw] {repr(response)}")

        print(f"\nCaregiver: {response}\n")


#  Entry point 

def main() -> None:
    parser = argparse.ArgumentParser(
        description="The Invisible Caregiver — terminal interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python cli.py report --scenario hazard\n"
            "  python cli.py report --file fetch_hour.json\n"
            "  python cli.py chat   --scenario decline\n"
            "  python cli.py chat   --file fetch_day.json\n"
            "  python cli.py report                          # live ThingsBoard, last 1h\n"
            "  python cli.py chat                            # live ThingsBoard, last 24h\n"
            "  python cli.py summary --scenario normal\n"
            "  python cli.py fetch                           # → fetch_hour.json + fetch_day.json\n"
            "  python cli.py fetch --hour                    # → fetch_hour.json only\n"
            "  python cli.py fetch --day                     # → fetch_day.json only\n"
            "  python cli.py fetch --hour --out-hour h.json\n"
            "  python cli.py llm fetch_hour.json             # audit\n"
            "  python cli.py llm fetch_day.json --mode chat --query 'Did Mom eat today?'\n"
            "  python cli.py clear                           # wipe all ThingsBoard telemetry\n"
            "  python cli.py clear --scenario hazard         # wipe only hazard scenario sensors\n"
        ),
    )
    parser.add_argument(
        "command",
        choices=["report", "chat", "summary", "fetch", "llm", "clear"],
        help=(
            "'report' — Safety Auditor; "
            "'chat' — Narrator Q&A; "
            "'summary' — print raw sensor data; "
            "'fetch' — dump ThingsBoard data to JSON files; "
            "'llm' — run LLM on a JSON file; "
            "'clear' — delete all telemetry from ThingsBoard"
        ),
    )
    parser.add_argument(
        "--scenario",
        choices=["normal", "decline", "hazard"],
        default=None,
        help="Use a local scenario file instead of live ThingsBoard data (report/chat/summary)",
    )
    parser.add_argument(
        "--file",
        default=None,
        metavar="FILE",
        help="Load a JSON summary file directly (report/chat) — skips ThingsBoard and scenario",
    )
    parser.add_argument(
        "--window-ms",
        type=int,
        default=None,
        help=(
            "Override the telemetry window in milliseconds. "
            f"Defaults: report/summary={HISTORY_WINDOW_MS} ms (1 h), "
            f"chat={NARRATOR_WINDOW_MS} ms (24 h)"
        ),
    )
    # fetch options
    parser.add_argument(
        "--hour",
        action="store_true",
        help="Fetch only the 1-hour window (fetch command)",
    )
    parser.add_argument(
        "--day",
        action="store_true",
        help="Fetch only the 24-hour window (fetch command)",
    )
    parser.add_argument(
        "--out-hour",
        default="fetch_hour.json",
        metavar="FILE",
        help="Output file for the 1-hour fetch (default: fetch_hour.json)",
    )
    parser.add_argument(
        "--out-day",
        default="fetch_day.json",
        metavar="FILE",
        help="Output file for the 24-hour fetch (default: fetch_day.json)",
    )
    # llm options
    parser.add_argument(
        "llm_file",
        nargs="?",
        default=None,
        metavar="FILE",
        help="JSON summary file to feed to the LLM (required for 'llm' command)",
    )
    parser.add_argument(
        "--mode",
        choices=["report", "chat"],
        default="report",
        help="LLM mode: 'report' (Safety Auditor) or 'chat' (Narrator) — used with 'llm' command",
    )
    parser.add_argument(
        "--query",
        default=None,
        metavar="TEXT",
        help="Question for the Narrator when --mode chat (required with --mode chat)",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print the raw LLM output before parsing (useful for debugging stuck responses)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print the full prompts sent to the LLM and the raw response with byte count",
    )

    args = parser.parse_args()

    if args.command == "report":
        window = args.window_ms if args.window_ms is not None else HISTORY_WINDOW_MS
        cmd_report(args.scenario, window, file=args.file, show_raw=args.raw, debug=args.debug)
    elif args.command == "chat":
        window = args.window_ms if args.window_ms is not None else NARRATOR_WINDOW_MS
        cmd_chat(args.scenario, window, file=args.file, show_raw=args.raw)
    elif args.command == "fetch":
        # if neither flag given, fetch both
        do_hour = args.hour or not args.day
        do_day  = args.day  or not args.hour
        cmd_fetch(do_hour, do_day, args.out_hour, args.out_day)
    elif args.command == "clear":
        cmd_clear(args.scenario)

if __name__ == "__main__":
    main()
