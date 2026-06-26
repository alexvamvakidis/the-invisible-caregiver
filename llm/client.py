import json
import time
import requests
from datetime import datetime
from pathlib import Path
from llm.prompts import SAFETY_AUDITOR_SYSTEM, AUDIT_USER, NARRATOR_SYSTEM, NARRATOR_USER
from llm.parser import parse_audit_response
from config.settings import LLM_HOST, LLM_MODEL, MAX_TOKENS

_ROOT = Path(__file__).parent.parent
_AUDIT_PROMPT_PATH = _ROOT / "last_audit_prompt.json"


def _call(system: str, messages: list, json_mode: bool = False) -> tuple[str, list]:
    """Send messages to Ollama. Returns (reply_text, updated_messages)."""
    payload_messages = [{"role": "system", "content": system}] + messages
    body = {
        "model":    LLM_MODEL,
        "stream":   False,
        "think":    False,
        "options":  {"num_predict": MAX_TOKENS},
        "messages": payload_messages,
    }
    if json_mode:
        body["format"] = "json"
    response = requests.post(f"{LLM_HOST}/api/chat", json=body, timeout=300)
    response.raise_for_status()
    assistant_msg = response.json()["message"]
    return assistant_msg["content"], messages + [assistant_msg]


def _tz_label() -> str:
    local_dt = datetime.now().astimezone()
    offset = local_dt.utcoffset().total_seconds()
    sign = "+" if offset >= 0 else "-"
    h, m = divmod(int(abs(offset)), 3600)
    m //= 60
    offset_str = f"UTC{sign}{h}" if m == 0 else f"UTC{sign}{h}:{m:02d}"
    name = local_dt.tzname() or offset_str
    return f"{name} ({offset_str})" if name != offset_str else offset_str


def _build_window_context(window_start_ms: int, window_end_ms: int) -> dict:
    duration_sec = (window_end_ms - window_start_ms) // 1000
    return {
        "window_start":     datetime.fromtimestamp(window_start_ms / 1000).strftime("%Y-%m-%d %H:%M:%S"),
        "window_end":       datetime.fromtimestamp(window_end_ms   / 1000).strftime("%Y-%m-%d %H:%M:%S"),
        "timezone":         _tz_label(),
        "duration_minutes": duration_sec // 60,
        "duration_seconds": duration_sec % 60,
    }


def audit(
    spoken_text: str,
    window_start_ms: int,
    window_end_ms: int,
    debug: bool = False,
) -> dict:
    """
    Requirement A — Safety Auditor (cron every 60 min).
    Input:  spoken-text narrative produced by data.pipeline.run().
    Output: structured alert dict {alert, severity, issues, message}.
    """
    if not spoken_text.strip():
        return {
            "alert":    False,
            "severity": "none",
            "issues":   [],
            "message":  "No sensor data available. Publish readings to ThingsBoard first.",
        }

    window_ctx = _build_window_context(window_start_ms, window_end_ms)
    user_text = AUDIT_USER.format(
        window_start=window_ctx["window_start"],
        window_end=window_ctx["window_end"],
        summary=spoken_text,
    )

    _AUDIT_PROMPT_PATH.write_text(
        json.dumps({
            "called_at":      datetime.now().isoformat(),
            "window_context": window_ctx,
            "model":          LLM_MODEL,
            "messages": [
                {"role": "system", "content": SAFETY_AUDITOR_SYSTEM},
                {"role": "user",   "content": user_text},
            ],
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if debug:
        print("=== SYSTEM PROMPT ===")
        print(SAFETY_AUDITOR_SYSTEM)
        print("=== USER PROMPT ===")
        print(user_text)
        print("=== END PROMPTS ===\n")

    raw, _ = _call(SAFETY_AUDITOR_SYSTEM, [{"role": "user", "content": user_text}], json_mode=False)

    if debug:
        print(f"=== RAW RESPONSE ({len(raw)} chars) ===")
        print(repr(raw))
        print("=== END RAW ===\n")

    return parse_audit_response(raw)


def narrate(
    spoken_text: str,
    query: str,
    history: list | None = None,
    window_start_ms: int | None = None,
    window_end_ms: int | None = None,
) -> tuple[str, list]:
    """
    Requirement B — The Narrator (on-demand caretaker chat).
    Input:  spoken-text narrative produced by data.pipeline.run() + caretaker's question.
    Output: (reply_text, updated_history) — pass updated_history into the next call.
    """
    if not spoken_text.strip():
        return (
            "I'm sorry, but I don't have any sensor data available for this period. "
            "Please make sure the sensors are active and publishing readings, then try again.",
            history or [],
        )

    now_ms = int(time.time() * 1000)
    if window_end_ms is None:
        window_end_ms = now_ms
    if window_start_ms is None:
        window_start_ms = window_end_ms - 86_400_000

    window_ctx = _build_window_context(window_start_ms, window_end_ms)

    if not history:
        first_msg = NARRATOR_USER.format(
            window_start=window_ctx["window_start"],
            window_end=window_ctx["window_end"],
            timezone=window_ctx["timezone"],
            summary=spoken_text,
            query=query,
        )
        messages = [{"role": "user", "content": first_msg}]
    else:
        messages = history + [{"role": "user", "content": query}]

    reply, updated_history = _call(NARRATOR_SYSTEM, messages)
    return reply, updated_history
