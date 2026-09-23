"""Stable identities for immutable per-instance role display receipts."""

from __future__ import annotations

import hashlib


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def receipt_external_id(
    recipient_key: str, agent_instance_id: str, role_row_id: str,
) -> str:
    return f"receipt:{_hash(recipient_key, agent_instance_id, role_row_id)}"


def watermark_external_id(recipient_key: str, agent_instance_id: str) -> str:
    return f"watermark:{_hash(recipient_key, agent_instance_id)}"
