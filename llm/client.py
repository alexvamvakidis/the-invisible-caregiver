import json
import time
import requests
from datetime import datetime
from pathlib import Path
from llm.prompts import SAFETY_AUDITOR_SYSTEM, AUDIT_USER, NARRATOR_SYSTEM, NARRATOR_USER
from llm.parser import parse_audit_response
from data.carryover import compute_carryover, save_carryover, load_carryover, format_carryover_section
from data.spoken_summary import render_spoken_summary, format_as_text
from config.settings import LLM_HOST, LLM_MODEL, MAX_TOKENS

_ROOT = Path(__file__).parent.parent
_AUDIT_PROMPT_PATH = _ROOT / "last_audit_prompt.json"

# Carryover is stale if the previous window ended more than 70 min before this window's start.
_CARRYOVER_STALENESS_MS = 70 * 60 * 1000


def _call(system: str, messages: list, json_mode: bool = False) -> tuple[str, list]:
    """Send messages to Ollama. Returns (reply_text, updated_messages)."""
    payload_messages = [{"role": "system", "content": system}] + messages
    body = {
        "model": LLM_MODEL,
        "stream": False,
        "options": {"num_predict": MAX_TOKENS},
        "messages": payload_messages,
    }
    if json_mode:
        body["format"] = "json"
    response = requests.post(f"{LLM_HOST}/api/chat", json=body, timeout=300)
    response.raise_for_status()
    assistant_msg = response.json()["message"]
    return assistant_msg["content"], messages + [assistant_msg]


def _tz_label() -> str:
    """Return a human-readable timezone label, e.g. 'EEST (UTC+3)'."""
    local_dt = datetime.now().astimezone()
    offset   = local_dt.utcoffset().total_seconds()
    sign     = "+" if offset >= 0 else "-"
    h, m     = divmod(int(abs(offset)), 3600)
    m //= 60
    offset_str = f"UTC{sign}{h}" if m == 0 else f"UTC{sign}{h}:{m:02d}"
    name = local_dt.tzname() or offset_str
    return f"{name} ({offset_str})" if name != offset_str else offset_str


def _build_window_context(window_start_ms: int, window_end_ms: int) -> dict:
    """Build the time-context dict that is injected into every LLM prompt."""
    duration_sec = (window_end_ms - window_start_ms) // 1000
    return {
        "window_start":     datetime.fromtimestamp(window_start_ms / 1000).strftime("%Y-%m-%d %H:%M:%S"),
        "window_end":       datetime.fromtimestamp(window_end_ms   / 1000).strftime("%Y-%m-%d %H:%M:%S"),
        "timezone":         _tz_label(),
        "duration_minutes": duration_sec // 60,
        "duration_seconds": duration_sec % 60,
    }


def audit(
    summary: dict,
    window_start_ms: int | None = None,
    window_end_ms:   int | None = None,
    debug: bool = False,
) -> dict:
    """
    Requirement A — Safety Auditor (cron every 60 min).
    Input:  1-hour sensor summary organised by room.
    Output: structured alert dict {alert, severity, issues, message, summary}.
    """
    total_sensors = sum(len(v) for v in summary.values())
    if total_sensors == 0:
        return {
            "alert":    False,
            "severity": "none",
            "issues":   [],
            "message":  "No sensor data available. Publish readings to ThingsBoard first.",
            "summary":  {},
        }

    now_ms = int(time.time() * 1000)
    if window_end_ms is None:
        window_end_ms = now_ms
    if window_start_ms is None:
        window_start_ms = window_end_ms - 3_600_000

    window_ctx = _build_window_context(window_start_ms, window_end_ms)

    carryover = load_carryover()
    if carryover and abs(carryover.get("window_end_ts", 0) - window_start_ms) > _CARRYOVER_STALENESS_MS:
        carryover = None  # stale — gap between windows is too large

    narrative     = render_spoken_summary(summary, window_start_ms, window_end_ms, carryover)
    narrative_txt = format_as_text(narrative)

    user_text = AUDIT_USER.format(
        window_context=json.dumps(window_ctx, indent=2),
        summary=narrative_txt,
    )

    prompt_payload = {
        "called_at": datetime.now().isoformat(),
        "window_context": window_ctx,
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SAFETY_AUDITOR_SYSTEM},
            {"role": "user",   "content": user_text},
        ],
    }
    _AUDIT_PROMPT_PATH.write_text(json.dumps(prompt_payload, indent=2, ensure_ascii=False), encoding="utf-8")

    if debug:
        print("=== SYSTEM PROMPT ===")
        print(SAFETY_AUDITOR_SYSTEM)
        print("=== USER PROMPT ===")
        print(user_text)
        print("=== END PROMPTS ===\n")

    raw, _ = _call(SAFETY_AUDITOR_SYSTEM, [{"role": "user", "content": user_text}], json_mode=True)

    if debug:
        print(f"=== RAW RESPONSE ({len(raw)} chars) ===")
        print(repr(raw))
        print("=== END RAW ===\n")

    result = parse_audit_response(raw)
    save_carryover(compute_carryover(summary, window_start_ms, window_end_ms, prev_carryover=carryover))
    return result


def narrate(
    summary: dict,
    query: str,
    history: list | None = None,
    window_start_ms: int | None = None,
    window_end_ms:   int | None = None,
) -> tuple[str, list]:
    """
    Requirement B — The Narrator (on-demand caretaker chat).
    Input:  24-hour sensor summary + caretaker's question + optional prior history.
    Output: (reply_text, updated_history) — pass updated_history into the next call.
    """
    total_sensors = sum(len(v) for v in summary.values())
    if total_sensors == 0:
        msg = (
            "I'm sorry, but I don't have any sensor data available for the last 24 hours. "
            "Please make sure the sensors are active and publishing readings, then try again."
        )
        return msg, history or []

    now_ms = int(time.time() * 1000)
    if window_end_ms is None:
        window_end_ms = now_ms
    if window_start_ms is None:
        window_start_ms = window_end_ms - 86_400_000

    window_ctx    = _build_window_context(window_start_ms, window_end_ms)
    narrative_txt = format_as_text(render_spoken_summary(summary, window_start_ms, window_end_ms))

    if not history:
        first_msg = NARRATOR_USER.format(
            window_context=json.dumps(window_ctx, indent=2),
            summary=narrative_txt,
            query=query,
        )
        messages = [{"role": "user", "content": first_msg}]
    else:
        messages = history + [{"role": "user", "content": query}]

    reply, updated_history = _call(NARRATOR_SYSTEM, messages)
    return reply, updated_history
