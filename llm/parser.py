import json, re

_SEVERITY_RANK = {"none": 0, "low": 1, "high": 2, "critical": 3}


def _fix_consistency(result: dict) -> dict:
    """
    Recompute alert and severity from the issues list so the model's
    self-reported values can never contradict the actual findings.
    """
    issues = result.get("issues", [])
    worst = "none"
    for issue in issues:
        sev = str(issue.get("severity", "")).lower()
        if _SEVERITY_RANK.get(sev, 0) > _SEVERITY_RANK[worst]:
            worst = sev
    result["severity"] = worst
    result["alert"] = worst in ("high", "critical")
    return result


def parse_audit_response(raw: str) -> dict:
    """
    Parse the Safety Auditor JSON response from the LLM.
    Expected schema: {alert, severity, issues, message}
    Falls back gracefully if the LLM wraps output in markdown or produces invalid JSON.
    """
    clean = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

    start = clean.find("{")
    end   = clean.rfind("}")
    if start != -1 and end != -1 and end > start:
        clean = clean[start : end + 1]
    elif start != -1:
        clean = clean[start:]

    try:
        result = json.loads(clean)
        parsed = {
            "alert":    bool(result.get("alert", False)),
            "severity": result.get("severity", "none"),
            "issues":   result.get("issues", []),
            "message":  result.get("message", ""),
            "raw":      raw,
        }
        return _fix_consistency(parsed)
    except (json.JSONDecodeError, ValueError):
        return {
            "alert":    False,
            "severity": "none",
            "issues":   [],
            "message":  "Could not parse LLM response.",
            "raw":      raw,
        }
