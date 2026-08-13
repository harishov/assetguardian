"""Input sanitization for user-supplied text fields.

The report explicitly flags "No input sanitization — prompt injection via
asset tag/employee name fields" as an unaddressed security gap. This module
closes it: every free-text field that gets interpolated into an LLM prompt
or logged is length-capped, stripped of control characters, and checked for
common prompt-injection phrasing before use. It is defense-in-depth, not a
substitute for treating all model output as untrusted.
"""
import re

MAX_FIELD_LENGTH = 128

_INJECTION_PATTERNS = [
    re.compile(r"ignore (all|previous|prior) instructions", re.IGNORECASE),
    re.compile(r"system prompt", re.IGNORECASE),
    re.compile(r"you are now", re.IGNORECASE),
    re.compile(r"disregard (the )?(above|previous)", re.IGNORECASE),
    re.compile(r"<\s*/?\s*(system|assistant|user)\s*>", re.IGNORECASE),
]


class SuspiciousInputError(ValueError):
    pass


def sanitize_field(value: str | None, *, field_name: str, max_length: int = MAX_FIELD_LENGTH) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SuspiciousInputError(f"{field_name} must be a string")

    # Strip control/non-printable characters.
    cleaned = "".join(ch for ch in value if ch.isprintable())
    cleaned = cleaned.strip()

    if len(cleaned) > max_length:
        raise SuspiciousInputError(f"{field_name} exceeds max length of {max_length}")

    for pattern in _INJECTION_PATTERNS:
        if pattern.search(cleaned):
            raise SuspiciousInputError(
                f"{field_name} contains a disallowed instruction-like phrase"
            )

    return cleaned


def sanitize_asset_id(value: str | None) -> str | None:
    if value is None:
        return None
    v = sanitize_field(value, field_name="assetId", max_length=64)
    if v and not re.fullmatch(r"[A-Za-z0-9\-_]+", v):
        raise SuspiciousInputError("assetId contains disallowed characters")
    return v


def sanitize_employee_id(value: str | None) -> str | None:
    if value is None:
        return None
    v = sanitize_field(value, field_name="employeeId", max_length=64)
    if v and not re.fullmatch(r"[A-Za-z0-9\-_.@]+", v):
        raise SuspiciousInputError("employeeId contains disallowed characters")
    return v


def sanitize_s3_key(value: str | None) -> str | None:
    if value is None:
        return None
    v = sanitize_field(value, field_name="s3Key", max_length=512)
    if v and (".." in v or v.startswith("/")):
        raise SuspiciousInputError("s3Key contains path-traversal characters")
    return v
