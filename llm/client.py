import json
import time
import requests
from datetime import datetime
from llm.prompts import SAFETY_AUDITOR_SYSTEM, AUDIT_USER, NARRATOR_SYSTEM, NARRATOR_USER
from llm.parser import parse_audit_response
from data.carryover import compute_carryover, save_carryover, load_carryover, format_carryover_section
from config.settings import LLM_HOST, LLM_MODEL, MAX_TOKENS


def _call(system: str, messages: list) -> tuple[str, list]:
    """Send messages to Ollama. Returns (reply_text, updated_messages)."""
    payload_messages = [{"role": "system", "content": system}] + messages
    response = requests.post(
        f"{LLM_HOST}/api/chat",
        json={
            "model": LLM_MODEL,
            "stream": False,
            "options": {"num_predict": MAX_TOKENS},
            "messages": payload_messages,
        },
        timeout=300,
    )
    response.raise_for_status()
    assistant_msg = response.json()["message"]
    return assistant_msg["content"], messages + [assistant_msg]


def audit(summary: dict) -> dict:
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

    window_end_ts = time.time()
    carryover = load_carryover()
    user_text = AUDIT_USER.format(
        carryover_section=format_carryover_section(carryover),
        summary=json.dumps(summary, indent=2),
    )
    raw, _ = _call(SAFETY_AUDITOR_SYSTEM, [{"role": "user", "content": user_text}])
    result = parse_audit_response(raw)
    save_carryover(compute_carryover(summary, window_end_ts))
    return result
     


def narrate(summary: dict, query: str, history: list | None = None) -> tuple[str, list]:
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

    if not history:
        # First turn: inject the sensor summary as context in the opening user message
        first_msg = NARRATOR_USER.format(
            summary=json.dumps(summary, indent=2),
            query=query,
        )
        messages = [{"role": "user", "content": first_msg}]
    else:
        # Subsequent turns: just append the new question
        messages = history + [{"role": "user", "content": query}]

    reply, updated_history = _call(NARRATOR_SYSTEM, messages)
    return reply, updated_history
