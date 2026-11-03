"""Secret redaction. Applied to every string before it is written to the store.

Two layers:
  1. Pattern-based: well-known key/token formats and `name=value` assignments
     whose name looks secret.
  2. Environment-based: literal values of env vars in the hook's environment
     whose *names* look secret (so a real key leaked verbatim is caught even if
     its format is unknown).
"""
from __future__ import annotations

import os
import re

_SECRET_NAME = r"[A-Za-z0-9_\-]*(?:api[_\-]?key|secret|token|passw(?:or)?d|credential|auth[_\-]?key|private[_\-]?key)[A-Za-z0-9_\-]*"

# (compiled pattern, replacement). Replacements never contain quotes or
# backslashes, so they are safe inside any string.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED:PRIVATE_KEY]"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "[REDACTED:ANTHROPIC_KEY]"),
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"), "[REDACTED:API_KEY]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"), "[REDACTED:GITHUB_TOKEN]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "[REDACTED:GITHUB_TOKEN]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED:AWS_KEY_ID]"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), "[REDACTED:SLACK_TOKEN]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "[REDACTED:GOOGLE_KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "[REDACTED:JWT]"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=\-]{16,}"), r"\1[REDACTED]"),
    # user:password@ in URLs
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^/\s:@\"']+:)[^@\s/\"']+(@)"), r"\1[REDACTED]\2"),
    # NAME=value / "name": "value" / name: value where NAME looks secret
    (re.compile(r"(?i)\b(" + _SECRET_NAME + r")([\"']?\s*[:=]\s*[\"']?)([^\s\"'<>\\,;]{8,})"), r"\1\2[REDACTED]"),
]

_env_secrets: list[str] | None = None


def _load_env_secrets() -> list[str]:
    global _env_secrets
    if _env_secrets is None:
        name_re = re.compile(r"(?i)(key|token|secret|passw|credential|auth)")
        vals = {
            v for k, v in os.environ.items()
            if name_re.search(k) and len(v) >= 12 and not v.startswith("/")
        }
        _env_secrets = sorted(vals, key=len, reverse=True)
    return _env_secrets


def redact(text: str) -> str:
    if not text:
        return text
    for v in _load_env_secrets():
        if v in text:
            text = text.replace(v, "[REDACTED:ENV]")
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


def redact_obj(obj):
    """Recursively redact every string inside a JSON-like object."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    return obj
