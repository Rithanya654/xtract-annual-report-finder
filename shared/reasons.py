def clean_not_found_reason(reason: str | None) -> str:
    """Return a dashboard-friendly one-line reason while logs keep raw detail."""
    text = (reason or "").strip()
    if not text:
        return "No acceptable annual report found"

    lowered = text.lower()
    timed_out = "search timed out" in lowered

    if "no balance sheet found" in lowered:
        return "Balance Sheet not found"
    if "ifrs 17" in lowered and "cash flow" in lowered:
        return "IFRS 17 detected but Cash Flow statement not found"
    if "wrong company" in lowered:
        return "Search timed out; last candidate was wrong company" if timed_out else "Candidate PDF was for the wrong company"
    if "wrong year" in lowered:
        return "Search timed out; last candidate was wrong year" if timed_out else "Candidate PDF was for the wrong year"
    if "domain does not match" in lowered:
        return "Candidate PDF was from a different website"
    if "unreadable" in lowered:
        return "PDF unreadable; contents could not be verified"
    if "too short" in lowered:
        return "PDF text too short to verify"
    if "search backend unstable" in lowered:
        return "Search backend temporarily unstable"
    if timed_out:
        return "Search timed out before finding an acceptable report"
    if "no acceptable pdf candidate" in lowered:
        return "No acceptable annual report found"

    for prefix in ("rejected:", "local:", "not found:"):
        while text.lower().startswith(prefix):
            text = text[len(prefix):].strip()
    return text[:160] or "No acceptable annual report found"
