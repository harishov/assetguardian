"""Input sanitization for user-supplied text fields.

The report explicitly flags "No input sanitization — prompt injection via
asset tag/employee name fields" as an unaddressed security gap. This module
closes it: every free-text field that gets interpolated into an LLM prompt
or logged is length-capped, stripped of control characters, and checked for
common prompt-injection phrasing before use. It is defense-in-depth, not a
substitute for treating all model output as untrusted.
"""
import os
import re

MAX_FIELD_LENGTH = 128

# Employee IDs may be given as a staff ID or a corporate email address. An
# address on any other domain is rejected: the portal highlights it client-side,
# and this is the enforcement that survives a caller bypassing the page.
CORPORATE_EMAIL_DOMAIN = os.environ.get("CORPORATE_EMAIL_DOMAIN", "@ncs.com.sg").lower()

# Internal field names -> what the person filling in the form actually sees.
_FRIENDLY_NAMES = {
    "assetId": "The asset ID",
    "employeeId": "The employee ID",
    "s3Key": "One of the uploaded photos",
}

_INJECTION_PATTERNS = [
    re.compile(r"ignore (all|previous|prior) instructions", re.IGNORECASE),
    re.compile(r"system prompt", re.IGNORECASE),
    re.compile(r"you are now", re.IGNORECASE),
    re.compile(r"disregard (the )?(above|previous)", re.IGNORECASE),
    re.compile(r"<\s*/?\s*(system|assistant|user)\s*>", re.IGNORECASE),
]


class SuspiciousInputError(ValueError):
    """Rejected input.

    Carries two messages: the exception text is the technical one destined for
    CloudWatch, and `user_message` is the plain-English sentence shown in the
    portal. Anything without a user_message falls back to a generic sentence in
    the handler, so internal field names never reach the screen.
    """

    def __init__(self, message: str, user_message: str | None = None):
        super().__init__(message)
        self.user_message = user_message


def sanitize_field(value: str | None, *, field_name: str, max_length: int = MAX_FIELD_LENGTH) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SuspiciousInputError(f"{field_name} must be a string")

    # Strip control/non-printable characters.
    cleaned = "".join(ch for ch in value if ch.isprintable())
    cleaned = cleaned.strip()

    if len(cleaned) > max_length:
        raise SuspiciousInputError(
            f"{field_name} exceeds max length of {max_length}",
            user_message=f"{_FRIENDLY_NAMES.get(field_name, 'One of the details you entered')} is too long.",
        )

    for pattern in _INJECTION_PATTERNS:
        if pattern.search(cleaned):
            raise SuspiciousInputError(
                f"{field_name} contains a disallowed instruction-like phrase",
                user_message=(
                    f"{_FRIENDLY_NAMES.get(field_name, 'One of the details you entered')} "
                    "contains text that isn't allowed. Please enter it again."
                ),
            )

    return cleaned


def sanitize_asset_id(value: str | None) -> str | None:
    if value is None:
        return None
    v = sanitize_field(value, field_name="assetId", max_length=64)
    if v and not re.fullmatch(r"[A-Za-z0-9\-_]+", v):
        raise SuspiciousInputError(
            "assetId contains disallowed characters",
            user_message=(
                "Asset IDs and serial numbers can only contain letters, numbers, "
                "hyphens and underscores. Check for spaces or brackets."
            ),
        )
    return v


def sanitize_employee_id(value: str | None) -> str | None:
    if value is None:
        return None
    v = sanitize_field(value, field_name="employeeId", max_length=64)
    if v and not re.fullmatch(r"[A-Za-z0-9\-_.@]+", v):
        raise SuspiciousInputError(
            "employeeId contains disallowed characters",
            user_message=(
                "The employee ID can only contain letters, numbers, dots, hyphens "
                "and underscores — or your work email address."
            ),
        )
    if v and "@" in v and not v.lower().endswith(CORPORATE_EMAIL_DOMAIN):
        raise SuspiciousInputError(
            f"employeeId given as an email must be on the {CORPORATE_EMAIL_DOMAIN} domain",
            user_message=(
                f"Please use your {CORPORATE_EMAIL_DOMAIN} work email address, "
                "or your employee ID instead."
            ),
        )
    return v


def sanitize_s3_key(value: str | None) -> str | None:
    if value is None:
        return None
    v = sanitize_field(value, field_name="s3Key", max_length=512)
    if v and (".." in v or v.startswith("/")):
        raise SuspiciousInputError(
            "s3Key contains path-traversal characters",
            user_message="One of the uploaded photos couldn't be read. Please upload it again.",
        )
    return v
