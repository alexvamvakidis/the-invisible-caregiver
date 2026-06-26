#!/usr/bin/env python3
"""
Terminal interface for The Invisible Caregiver.

Commands
--------
  python cli.py report [--scenario normal|decline|hazard] [--file FILE] [--window-ms N]
      Safety Auditor — collect data, compute carryover, audit with the LLM.

  python cli.py chat [--scenario normal|decline|hazard] [--file FILE] [--window-ms N]
      Narrator Q&A — collect 24-hour data and open an interactive chat session.

  python cli.py fetch [--hour] [--day] [--out-hour FILE] [--out-day FILE]
      Run the pipeline and write the spoken narrative to text files.

Without --scenario / --file the data is pulled live from ThingsBoard.
"""

import argparse
import time
from pathlib import Path

from config.settings import HISTORY_WINDOW_MS, NARRATOR_WINDOW_MS
from data.pipeline import run as pipeline_run
from llm.client import audit, narrate


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_report(
    scenario: str | None,
    window_ms: int,
    file: str | None = None,
    show_raw: bool = False,
    debug: bool = False,
) -> None:
    spoken_text, window_start_ms, window_end_ms = pipeline_run(
        scenario=scenario, file=file, window_ms=window_ms, update_state=True
    )

    print("Just a moment...")
    _t0 = time.time()
    result = audit(spoken_text, window_start_ms, window_end_ms, debug=debug)
    _elapsed = time.time() - _t0

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
            rule   = issue.get("rule", "?")
            sev    = issue.get("severity", "?").upper()
            detail = issue.get("detail", "")
            print(f"  • [{sev}] {rule}: {detail}")
    print()
    print(message)
    _m, _s = divmod(int(_elapsed), 60)
    print(f"\n[replied in {f'{_m}m {_s}s' if _m else f'{_s}s'}]")


def cmd_chat(
    scenario: str | None,
    window_ms: int,
    file: str | None = None,
    show_raw: bool = False,
) -> None:
    print("Just a moment...")

    spoken_text, window_start_ms, window_end_ms = pipeline_run(
        scenario=scenario, file=file, window_ms=window_ms, update_state=False
    )

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

        _t0 = time.time()
        response, history = narrate(spoken_text, query, history, window_start_ms, window_end_ms)
        _elapsed = time.time() - _t0

        if show_raw:
            print(f"[raw] {repr(response)}")

        _m, _s = divmod(int(_elapsed), 60)
        print(f"\nCaregiver: {response}\n")
        print(f"[replied in {f'{_m}m {_s}s' if _m else f'{_s}s'}]")


def cmd_fetch(
    do_hour: bool = True,
    do_day: bool = True,
    out_hour: str = "fetch_hour.txt",
    out_day: str = "fetch_day.txt",
    show_raw: bool = False,
    debug: bool = False,
) -> None:
    if do_hour:
        print("Fetching 1-hour pipeline narrative…")
        spoken_text, window_start_ms, window_end_ms = pipeline_run(
            window_ms=HISTORY_WINDOW_MS, update_state=False
        )
        text = spoken_text if spoken_text.strip() else "(no sensor data in this window)"
        Path(out_hour).write_text(text, encoding="utf-8")
        print(f"  → {out_hour}\n")

        print("Running safety audit…\n")
        result = audit(spoken_text, window_start_ms, window_end_ms, debug=debug)

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
                rule   = issue.get("rule", "?")
                sev    = issue.get("severity", "?").upper()
                detail = issue.get("detail", "")
                print(f"  • [{sev}] {rule}: {detail}")
        print()
        print(message)

    if do_day:
        print("Fetching 24-hour pipeline narrative…")
        spoken_text, _ws, _we = pipeline_run(window_ms=NARRATOR_WINDOW_MS, update_state=False)
        text = spoken_text if spoken_text.strip() else "(no sensor data in this window)"
        Path(out_day).write_text(text, encoding="utf-8")
        print(f"  → {out_day}\n")
        print(text)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="The Invisible Caregiver — terminal interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python cli.py report --scenario hazard\n"
            "  python cli.py report                    # live ThingsBoard, last 1h\n"
            "  python cli.py chat   --scenario decline\n"
            "  python cli.py chat                      # live ThingsBoard, last 24h\n"
            "  python cli.py fetch                     # → fetch_hour.txt + fetch_day.txt\n"
            "  python cli.py fetch --hour              # → fetch_hour.txt only\n"
            "  python cli.py fetch --day               # → fetch_day.txt only\n"
        ),
    )
    parser.add_argument(
        "command",
        choices=["report", "chat", "fetch"],
        help=(
            "'report' — Safety Auditor; "
            "'chat' — Narrator Q&A; "
            "'fetch' — run the pipeline and write spoken narratives to files"
        ),
    )
    parser.add_argument(
        "--scenario",
        choices=["normal", "decline", "hazard"],
        default=None,
        help="Use a local scenario file instead of live ThingsBoard data",
    )
    parser.add_argument(
        "--file",
        default=None,
        metavar="FILE",
        help="Load a pre-built JSON summary file — skips ThingsBoard and scenario",
    )
    parser.add_argument(
        "--window-ms",
        type=int,
        default=None,
        help=(
            "Override the telemetry window in milliseconds. "
            f"Defaults: report={HISTORY_WINDOW_MS} ms (1 h), "
            f"chat={NARRATOR_WINDOW_MS} ms (24 h)"
        ),
    )
    parser.add_argument("--hour", action="store_true", help="Fetch only the 1-hour window")
    parser.add_argument("--day",  action="store_true", help="Fetch only the 24-hour window")
    parser.add_argument("--out-hour", default="fetch_hour.txt", metavar="FILE",
                        help="Output file for the 1-hour fetch (default: fetch_hour.txt)")
    parser.add_argument("--out-day",  default="fetch_day.txt",  metavar="FILE",
                        help="Output file for the 24-hour fetch (default: fetch_day.txt)")
    parser.add_argument("--raw",   action="store_true", help="Print raw LLM output before parsing")
    parser.add_argument("--debug", action="store_true", help="Print full prompts and raw response")

    args = parser.parse_args()

    if args.command == "report":
        window = args.window_ms if args.window_ms is not None else HISTORY_WINDOW_MS
        cmd_report(args.scenario, window, file=args.file, show_raw=args.raw, debug=args.debug)
    elif args.command == "chat":
        window = args.window_ms if args.window_ms is not None else NARRATOR_WINDOW_MS
        cmd_chat(args.scenario, window, file=args.file, show_raw=args.raw)
    elif args.command == "fetch":
        do_hour = args.hour or not args.day
        do_day  = args.day  or not args.hour
        cmd_fetch(do_hour, do_day, args.out_hour, args.out_day, show_raw=args.raw, debug=args.debug)


if __name__ == "__main__":
    main()
