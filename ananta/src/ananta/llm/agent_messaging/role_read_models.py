"""Typed contracts for the per-instance, display-only role read boundary."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RoleReadPage:
    """A server-issued page.  Its token is deliberately opaque to clients."""

    token: str
    status: str
    item_count: int


@dataclass(frozen=True, slots=True)
class RoleReadAck:
    """Result of acknowledging a page after successful consumer output."""

    status: str
    receipt_count: int


@dataclass(frozen=True, slots=True)
class RoleReadReceiptResult:
    """Exact receipt lookup result used by the wake reconciliation client."""

    recipient_key: str
    role_row_id: str
    served: bool
