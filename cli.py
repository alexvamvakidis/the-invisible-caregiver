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

from config.settings import SCENARIO_FILES, SENSOR_DATA_DIR, HISTORY_WINDOW_MS, NARRATOR_WINDOW_MS
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


def _load_scenario_summary(name: str, window_ms: int | None = None) -> dict:
    """
    Build a sensor summary from per-sensor JSON files in the scenario directory.
    Produces the same structure as data.collector.summarize() so both
    scenario mode and live mode feed identical data to the LLM.

    window_ms: only include readings whose timestamp falls within the last
               window_ms milliseconds relative to now (UTC).  None = all data.
    """
    import time as _time
    data_dir = Path(SCENARIO_FILES.get(name, ""))
    if not data_dir.exists():
        sys.exit(f"Error: scenario '{name}' not found at {data_dir}. "
                 f"Run 'python simulation/simulate.py {name}' first.")

    now_ms     = int(_time.time() * 1000)
    start_ms   = (now_ms - window_ms) if window_ms else 0

    # Build raw in the exact format collector.get_all_rooms() produces:
    # { room: { sensor_id: { sensor_id: [{ts: ms_int, value: v}, ...] } } }
    raw: dict = {}
    for json_file in sorted(data_dir.glob("*.json")):
        data = json.load(json_file.open())
        sensor_id = data["sensor_id"]
        room      = data.get("room", "unknown")
        entries   = [
            {"ts": _iso_to_ms(r["timestamp"]), "value": r["value"]}
            for r in data.get("readings", [])
            if _iso_to_ms(r["timestamp"]) >= start_ms
        ]
        if entries:
            (raw
             .setdefault(room, {})
             .setdefault(sensor_id, {})[sensor_id]) = entries

    from data.collector import summarize
    return summarize(raw)


#  Live data loader 
def _load_live_summary(window_ms: int) -> dict:
    from data.collector import get_all_rooms, summarize
    raw = get_all_rooms(window_ms)
    return summarize(raw)


#  Commands 
def cmd_fetch(
    do_hour: bool = True,
    do_day: bool = True,
    out_hour: str = "fetch_hour.json",
    out_day: str = "fetch_day.json",
) -> None:
    from data.collector import get_all_rooms, summarize

    if do_hour:
        print("Fetching 1-hour window from ThingsBoard…")
        summary_hour = _annotate_ts(summarize(get_all_rooms(HISTORY_WINDOW_MS)))
        Path(out_hour).write_text(json.dumps(summary_hour, indent=2))
        print(f"  → {out_hour}")

    if do_day:
        print("Fetching 24-hour window from ThingsBoard…")
        summary_day = _annotate_ts(summarize(get_all_rooms(NARRATOR_WINDOW_MS)))
        Path(out_day).write_text(json.dumps(summary_day, indent=2))
        print(f"  → {out_day}")

def _load_summary(file: str | None, scenario: str | None, window_ms: int, label: str) -> dict:
    if file:
        path = Path(file)
        if not path.exists():
            sys.exit(f"Error: file not found: {file}")
        print(f"Loading sensor data from {file}…")
        return json.loads(path.read_text())
    print(f"Fetching sensor data ({label})…")
    return _load_scenario_summary(scenario, window_ms) if scenario else _load_live_summary(window_ms)


def cmd_report(scenario: str | None, window_ms: int, file: str | None = None) -> None:
    """Requirement A — Safety Auditor (last 1 hour)."""
    summary = _load_summary(file, scenario, window_ms, "last 1 hour")

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

    if "raw" in result:
        print("\n[debug] raw LLM output:")
        print(result["raw"])


def cmd_chat(scenario: str | None, window_ms: int, file: str | None = None, show_raw: bool = False) -> None:
    """Requirement B — The Narrator (last 24 hours, on-demand)."""
    summary = _load_summary(file, scenario, window_ms, "last 24 hours")
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

        response, history = narrate(summary, query, history)

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
        ),
    )
    parser.add_argument(
        "command",
        choices=["report", "chat", "summary", "fetch", "llm"],
        help=(
            "'report' — Safety Auditor; "
            "'chat' — Narrator Q&A; "
            "'summary' — print raw sensor data; "
            "'fetch' — dump ThingsBoard data to JSON files; "
            "'llm' — run LLM on a JSON file"
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
        "file",
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

    args = parser.parse_args()

    if args.command == "report":
        window = args.window_ms if args.window_ms is not None else HISTORY_WINDOW_MS
        cmd_report(args.scenario, window, file=args.file)
    elif args.command == "chat":
        window = args.window_ms if args.window_ms is not None else NARRATOR_WINDOW_MS
        cmd_chat(args.scenario, window, file=args.file, show_raw=args.raw)
    elif args.command == "fetch":
        # if neither flag given, fetch both
        do_hour = args.hour or not args.day
        do_day  = args.day  or not args.hour
        cmd_fetch(do_hour, do_day, args.out_hour, args.out_day)

if __name__ == "__main__":
    main()
