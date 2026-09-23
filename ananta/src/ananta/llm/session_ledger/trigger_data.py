"""Shared helper for building ``process_call`` trigger_data with authenticated principal.

Spec §14.8. Used by ``platform_surface.py`` and ``mcp_streamable/dispatch.py``
in M5 so both surfaces serialize ``AuthenticatedPrincipal`` into trigger_data
identically. Lives in ``session_ledger`` because the ledger's authz-bearing
service-process methods are the first consumers; other future authenticated
services can import from here.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from ananta.llm.session_ledger.types import AuthenticatedPrincipal


def build_process_call_trigger_data(
    *,
    principal: AuthenticatedPrincipal,
    bridge_id: str,
    session_id: str,
    process_key: str,
    reason: str,
    source_namespace: str,
    deliver_result_process_key: str,
    deliver_error_process_key: str,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the canonical trigger_data dict for an authenticated process_call.

    The shape is the platform-side contract that
    ``action_processor._inject_session_context`` reads to lift
    ``authenticated_principal`` onto the service-handler ``state`` dict.
    Callers MUST construct this from a verified bridge claim, never from
    caller-supplied parameters.
    """
    trigger: dict[str, Any] = {
        "source_namespace": source_namespace,
        "bridge_id": bridge_id,
        "session_id": session_id,
        "process_key": process_key,
        "reason": reason,
        "bridge_plugin_namespace": source_namespace,
        "deliver_result_process_key": deliver_result_process_key,
        "deliver_error_process_key": deliver_error_process_key,
        "authenticated_principal": dataclasses.asdict(principal),
    }
    if extras:
        trigger.update(extras)
    return trigger


def extract_authenticated_principal(
    state: dict[str, Any],
) -> AuthenticatedPrincipal:
    """Reconstruct ``AuthenticatedPrincipal`` from the service-handler state dict.

    Service-process handlers use this to read the verified caller identity.
    Raises ``PermissionError`` when the state dict has no authenticated
    principal — fail-closed, never fall back to caller-supplied identity.
    """
    raw = state.get("authenticated_principal")
    if not isinstance(raw, dict):
        raise PermissionError(
            "process invoked without authenticated_principal in state — "
            "service handler refusing to act on caller-supplied identity"
        )
    client_id = raw.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise PermissionError(
            "authenticated_principal missing non-empty client_id — refusing to proceed"
        )
    return AuthenticatedPrincipal(
        client_id=client_id,
        agent_id=str(raw.get("agent_id", "")),
        agent_instance_id=str(raw.get("agent_instance_id", "")),
        bridge_id=str(raw.get("bridge_id", "")),
        session_id=str(raw.get("session_id", "")),
    )


__all__ = [
    "build_process_call_trigger_data",
    "extract_authenticated_principal",
]
