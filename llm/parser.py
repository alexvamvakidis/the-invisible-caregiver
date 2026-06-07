import json, re

def parse_audit_response(raw: str) -> dict:
    clean = re.sub(r"```json|```", "", raw).strip()
    try:
        result = json.loads(clean)
        assert "alert" in result and "severity" in result
        return result
    except (json.JSONDecodeError, AssertionError):
        return {
            "alert": False,
            "severity": "none",
            "issues": [],
            "message": "Failed to parse LLM response.",
            "raw": raw
        }