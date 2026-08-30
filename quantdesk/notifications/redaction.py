from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


REDACTION_MARKER = "[REDACTED]"
SENSITIVE_KEY_PARTS = (
    "sendkey",
    "send_key",
    "secret",
    "token",
    "authorization",
    "cookie",
)

_SERVER_CHAN_URL_RE = re.compile(
    r"https?://sctapi\.ftqq\.com/[^\s/'\"<>]+\.send",
    flags=re.IGNORECASE,
)
_SERVER_CHAN_KEY_RE = re.compile(r"\bSCT[A-Za-z0-9_=-]{8,}\b")
_NAMED_SECRET_RE = re.compile(
    r"(?i)\b(sendkey|send_key|secret|token|authorization|cookie)\s*[=:]\s*[^\s,;'\"}]+"
)


def redact_text(value: object, *, known_secrets: Iterable[str] = ()) -> str:
    text = str(value)
    secrets = sorted(
        {str(item) for item in known_secrets if str(item)},
        key=len,
        reverse=True,
    )
    for secret in secrets:
        text = text.replace(secret, REDACTION_MARKER)
    text = _SERVER_CHAN_URL_RE.sub(
        f"https://sctapi.ftqq.com/{REDACTION_MARKER}.send", text
    )
    text = _SERVER_CHAN_KEY_RE.sub(REDACTION_MARKER, text)
    text = _NAMED_SECRET_RE.sub(
        lambda match: f"{match.group(1)}={REDACTION_MARKER}", text
    )
    return text


def _sensitive_key(value: object) -> bool:
    key = str(value).casefold().replace("-", "_")
    return any(part in key for part in SENSITIVE_KEY_PARTS)


def redact_payload(value: Any, *, known_secrets: Iterable[str] = ()) -> Any:
    secrets = tuple(str(item) for item in known_secrets if str(item))
    if isinstance(value, BaseException):
        return redact_text(type(value).__name__, known_secrets=secrets)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            safe_key = redact_text(key, known_secrets=secrets)
            result[safe_key] = (
                REDACTION_MARKER
                if _sensitive_key(key)
                else redact_payload(item, known_secrets=secrets)
            )
        return result
    if isinstance(value, tuple):
        return tuple(redact_payload(item, known_secrets=secrets) for item in value)
    if isinstance(value, list):
        return [redact_payload(item, known_secrets=secrets) for item in value]
    if isinstance(value, set):
        return sorted(
            (redact_payload(item, known_secrets=secrets) for item in value),
            key=repr,
        )
    if isinstance(value, str):
        return redact_text(value, known_secrets=secrets)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(value, known_secrets=secrets)


__all__ = [
    "REDACTION_MARKER",
    "SENSITIVE_KEY_PARTS",
    "redact_payload",
    "redact_text",
]
