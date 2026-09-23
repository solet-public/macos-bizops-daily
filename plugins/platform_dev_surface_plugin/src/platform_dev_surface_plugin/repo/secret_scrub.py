"""Secret-shape scan + redaction (design §2.3 control 3, Q2 ruling).

Even inside the confined + denylisted boundary, a first-party source file can
still carry a credential-shaped token (a checked-in fixture, an example, an
accidental paste). The last gate scans RETURNED bytes for known credential
shapes (the security-scan phase-1 markers):

* ``read_file`` REFUSES the whole file on any hit (:class:`RepoSecretError`) —
  fail-closed; the caller can fall back to ``search`` for a redacted view.
* ``search`` REDACTS matched spans in each snippet with an explicit marker, so
  a match is still discoverable without exposing the token.
"""

from __future__ import annotations

import re

_REDACTION = "[REDACTED:secret-shape]"

# Credential shapes (design §2.3): Anthropic / generic sk- keys, AWS access
# keys, PEM private-key headers, bearer tokens, password=/api_key= assignments.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{12,}\b"),
    re.compile(r"-----BEGIN [A-Z0-9 ]+-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*"),
    re.compile(r"(?i)\bpassword\s*=\s*\S+"),
    re.compile(r"(?i)\bapi_key\s*=\s*\S+"),
)


def contains_secret(text: str) -> bool:
    """True if any credential shape appears in ``text`` (read_file refuse gate)."""
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def redact_secrets(text: str) -> str:
    """Replace every credential-shaped span in ``text`` with the redaction marker."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACTION, text)
    return text
