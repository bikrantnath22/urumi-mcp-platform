"""Non-sensitive provider diagnostics. Never return raw provider error text."""
import math
import re


def rate_headers(response):
    allowed = (
        "x-ratelimit-limit-requests", "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens",
    )
    return {key: value for key in allowed if (value := response.headers.get(key))
            and len(value) <= 80 and re.fullmatch(r"[\d.\-a-zA-Z ]+", value)}


def rejection_details(response):
    try:
        error = response.json().get("error", {})
        message = str(error.get("message", "")).lower()
    except (ValueError, AttributeError, TypeError):
        message = ""
    reason = "provider_rate_limit"
    for label, pattern in (
        ("tokens_per_minute", r"tokens per minute|\btpm\b"),
        ("tokens_per_day", r"tokens per day|\btpd\b"),
        ("requests_per_minute", r"requests per minute|\brpm\b"),
        ("requests_per_day", r"requests per day|\brpd\b"),
    ):
        if re.search(pattern, message):
            reason = label
            break
    details = {"reason": reason, "headers": rate_headers(response)}
    for key in ("limit", "used", "requested"):
        match = re.search(r"\b" + key + r"\s*:?\s*(\d{1,12})\b", message)
        if match:
            details[key] = int(match.group(1))
    return details


def retry_seconds(response):
    try:
        value = float(response.headers.get("retry-after", "30"))
        return max(1, math.ceil(value)) if math.isfinite(value) and value <= 604800 else 30
    except (TypeError, ValueError, OverflowError):
        return 30
