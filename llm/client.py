import json
import requests
from llm.prompts import CARE_PLAN, AUDIT_USER, NARRATOR
from llm.parser import parse_audit_response
from config.settings import LLM_HOST, LLM_MODEL, MAX_TOKENS
from llm.formatter import format_for_llm

def _call(system: str, user: str) -> str:
    response = requests.post(
        f"{LLM_HOST}/api/chat",
        json={
            "model": LLM_MODEL,
            "stream": False,
            "options": {
                "num_predict": MAX_TOKENS,
                "think": False      # ← disable thinking mode
            },
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
        },
        timeout=300,
    )
    response.raise_for_status()
    return response.json()["message"]["content"]


def audit(summary: dict) -> dict:
    """
    Safety Auditor — called by cron every hour.
    Returns a structured alert dict.
    """
    total_sensors = sum(len(v) for v in summary.values())
    if total_sensors == 0:
        return {
            "alert":    False,
            "severity": "none",
            "issues":   [],
            "message":  "No sensor data available yet. Publish readings to ThingsBoard first.",
        }

    formatted = format_for_llm(summary)
    user = AUDIT_USER.format(summary=formatted)
    raw = _call(CARE_PLAN, user)
    print(f"RAW RESPONSE:\n{raw}\n")
    return parse_audit_response(raw)


def narrate(summary: dict, query: str) -> str:
    """
    Narrator — called on demand by the caretaker chat interface.
    Returns a conversational string.
    """
    formatted = format_for_llm(summary)
    user = (
        "24-hour sensor activity report:\n"
        + formatted
        + f"\n\nCaretaker question: {query}"
    )
    return _call(NARRATOR, user)