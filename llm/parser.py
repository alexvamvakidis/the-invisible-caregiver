import json, re


def parse_audit_response(raw: str) -> dict:
    """
    Parse the Safety Auditor JSON response from the LLM.
    Expected schema: {alert, severity, issues, message}
    Falls back gracefully if the LLM wraps output in markdown or produces invalid JSON.
    """
    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    clean = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

    # Extract the JSON object: from the first { to the last }
    start = clean.find("{")
    end   = clean.rfind("}")
    if start != -1 and end != -1 and end > start:
        clean = clean[start : end + 1]
    elif start != -1:
        clean = clean[start:]

    try:
        result = json.loads(clean)
        # Normalise: ensure all expected keys exist with safe defaults
        return {
            "alert":    bool(result.get("alert", False)),
            "severity": result.get("severity", "none"),
            "issues":   result.get("issues", []),
            "message":  result.get("message", ""),
        }
    except (json.JSONDecodeError, ValueError):
        return {
            "alert":    False,
            "severity": "none",
            "issues":   [],
            "message":  "Could not parse LLM response.",
            "raw":      raw,
            "summary":  {},
        }
