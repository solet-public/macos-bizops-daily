"""Transport-provenance values owned by the messaging transport layer."""

from __future__ import annotations

from typing import Final

SENDER_PRINCIPAL_KIND_OAUTH_CLIENT: Final[str] = "oauth_client"
SENDER_PRINCIPAL_KIND_STDIO_AGENT: Final[str] = "stdio_agent"
SENDER_PRINCIPAL_KIND_SYSTEM: Final[str] = "system"
SENDER_PRINCIPAL_KIND_UNKNOWN: Final[str] = "unknown"

__all__ = [
    "SENDER_PRINCIPAL_KIND_OAUTH_CLIENT",
    "SENDER_PRINCIPAL_KIND_STDIO_AGENT",
    "SENDER_PRINCIPAL_KIND_SYSTEM",
    "SENDER_PRINCIPAL_KIND_UNKNOWN",
]
