"""Exact-receipt wake reconciliation; failures preserve every spool line."""

from __future__ import annotations

import json
from typing import Any

from .client import BridgeCallError, BridgeClient, resolve_base_url

_PROCESS = "plugin::agent_messaging_plugin::peer_role_read_receipts"


def reconcile_role_receipts(
    *, agent_session_id: str, agent_instance_id: str, lines: list[str],
) -> tuple[list[str], str | None]:
    """Suppress only recognised role lines with an acknowledged exact receipt."""
    recognised = _recognised_pairs(lines)
    if not recognised:
        return lines, None
    try:
        served = _lookup_served(
            agent_session_id, agent_instance_id, set(recognised.values()),
        )
    except Exception as exc:  # noqa: BLE001 - preserve is the safety direction
        return lines, f"wake receipt lookup failed ({exc}); preserving all lines"
    return [line for index, line in enumerate(lines) if recognised.get(index) not in served], None


def _recognised_pairs(lines: list[str]) -> dict[int, tuple[str, str]]:
    return {
        index: pair for index, line in enumerate(lines)
        if (pair := _role_pair(line)) is not None
    }


def _lookup_served(
    agent_session_id: str, agent_instance_id: str, expected: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    candidates = [{"recipient_key": key, "role_row_id": row_id} for key, row_id in expected]
    base_url = resolve_base_url()
    with BridgeClient(base_url, caller_agent_session_id=agent_session_id) as client:
        dispatched = client.call_and_wait(
            _PROCESS, {"agent_session_id": agent_session_id, "candidates": candidates},
            reason="wake: exact role display receipt reconciliation",
        )
    return _served_result(dispatched, agent_instance_id, expected)


def _served_result(
    dispatched: dict[str, Any], agent_instance_id: str,
    expected: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    results = _validated_results(dispatched, agent_instance_id)
    returned = {_result_pair(item) for item in results}
    if not expected.issubset(returned):
        raise BridgeCallError("receipt lookup omitted one or more candidates")
    return {_result_pair(item) for item in results if item.get("served") is True}


def _validated_results(
    dispatched: dict[str, Any], agent_instance_id: str,
) -> list[dict[str, Any]]:
    outcome = dispatched.get("result")
    if not isinstance(outcome, dict) or outcome.get("action_status") != "completed":
        raise BridgeCallError("receipt lookup did not complete")
    data = outcome.get("data")
    if not isinstance(data, dict) or data.get("agent_instance_id") != agent_instance_id:
        raise BridgeCallError("receipt lookup response identity is invalid")
    return _result_objects(data.get("results"))


def _result_objects(raw_results: object) -> list[dict[str, Any]]:
    if not isinstance(raw_results, list):
        raise BridgeCallError("receipt lookup response has malformed results")
    if not all(isinstance(item, dict) for item in raw_results):
        raise BridgeCallError("receipt lookup response has malformed result item")
    return [item for item in raw_results if isinstance(item, dict)]


def _role_pair(line: str) -> tuple[str, str] | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    metadata = _line_metadata(payload)
    if not isinstance(metadata, dict) or metadata.get("recipient_kind") != "role":
        return None
    return _metadata_pair(metadata)


def _line_metadata(payload: dict[str, Any]) -> Any:
    if payload.get("watch") == "event":
        event = payload.get("event")
        return event.get("meta") if isinstance(event, dict) else None
    if payload.get("watch") == "inbox" and payload.get("section") == "role_entries":
        entry = payload.get("entry")
        message = entry.get("message") if isinstance(entry, dict) else None
        return message.get("metadata") if isinstance(message, dict) else None
    return None


def _metadata_pair(metadata: dict[str, Any]) -> tuple[str, str] | None:
    key, row_id = metadata.get("recipient_key"), metadata.get("role_row_id")
    return (key, row_id) if isinstance(key, str) and key and isinstance(row_id, str) and row_id else None


def _result_pair(item: dict[str, Any]) -> tuple[str, str]:
    key, row_id = item.get("recipient_key"), item.get("role_row_id")
    if not isinstance(key, str) or not isinstance(row_id, str):
        return "", ""
    return key, row_id
