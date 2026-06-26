import json, logging, re

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {"none": 0, "low": 1, "high": 2, "critical": 3}
_VALID_RULES = {"C1", "C2", "C3", "C4", "H1", "H2", "H3", "L1", "L2a", "L2b", "L3"}
_RULE_SEVERITY = {
    "C1": "critical", "C2": "critical",
    "C3": "high",     "C4": "high",  "H3": "high",
    "H1": "low",      "H2": "low",
    "L1": "low",      "L2a": "low",  "L2b": "low",  "L3": "low",
}
_NEGATION_PATTERNS = re.compile(
    r"does not (fire|apply|trigger|constitute|meet|occur)|"
    r"did not (fire|apply|trigger|meet|occur)|"
    r"is not (met|triggered|applicable|violated|running)|"
    r"not met|cannot fire|not violated|not a violation|not triggered|"
    r"bypassed|skipped|excluded|not applicable|no violation|"
    r"this does not|was not running|condition.{0,20}not|"
    r"state is.{0,10}off|shows.{0,10}off|state.{0,10}was off|"
    r"no (running|flow|water)|water.*was off|off.*not running|"
    r"outside.{0,20}(window|range|target)|window.{0,20}outside|"
    r"no false alarm|not the fridge|is not.{0,20}(fridge|door|sensor)|"
    r"correctly secured|not applicable to|does not apply to|"
    r"is incorrect|non-existent|incorrect or|rule.{0,20}(incorrect|not exist)|"
    r"potential rule|this rule.{0,30}(not|does not|incorrect)",
    re.IGNORECASE,
)


def _filter_issues(issues: list) -> list:
    """Remove hallucinated rules (invalid IDs) and 'bypassed' issues where the
    model's own detail/evidence text says the rule doesn't apply."""
    clean = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        rule = str(issue.get("rule", "")).strip().upper().lstrip("[").rstrip("]")
        if rule not in _VALID_RULES:
            logger.info("parse_audit_response: dropping invalid rule %r", rule)
            continue
        detail = str(issue.get("detail", ""))
        evidence = str(issue.get("evidence", ""))
        if _NEGATION_PATTERNS.search(detail) or _NEGATION_PATTERNS.search(evidence):
            logger.info("parse_audit_response: dropping self-negated issue %r", rule)
            continue
        # Enforce the correct severity per rule definition
        issue["severity"] = _RULE_SEVERITY.get(rule, issue.get("severity", "low"))
        clean.append(issue)
    return clean


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
    Falls back gracefully if the LLM wraps output in markdown, think-blocks, or produces invalid JSON.
    """
    clean = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    clean = re.sub(r"```(?:json)?", "", clean).strip().rstrip("`").strip()

    start = clean.find("{")
    if start == -1:
        logger.warning("parse_audit_response: no JSON object found. raw=%r", raw[:300])
        return {"alert": False, "severity": "none", "issues": [], "message": "Could not parse LLM response.", "raw": raw}

    try:
        result, _ = json.JSONDecoder().raw_decode(clean, start)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("parse_audit_response: JSON decode failed (%s). raw=%r", exc, raw[:300])
        return {"alert": False, "severity": "none", "issues": [], "message": "Could not parse LLM response.", "raw": raw}

    if not isinstance(result, dict):
        logger.warning("parse_audit_response: top-level JSON is not a dict. raw=%r", raw[:300])
        return {"alert": False, "severity": "none", "issues": [], "message": "Could not parse LLM response.", "raw": raw}

    issues_raw = result.get("issues") or []
    if not isinstance(issues_raw, list):
        issues_raw = []

    parsed = {
        "alert":    bool(result.get("alert", False)),
        "severity": result.get("severity", "none"),
        "issues":   _filter_issues(issues_raw),
        "message":  result.get("message", ""),
        "raw":      raw,
    }
    return _fix_consistency(parsed)
