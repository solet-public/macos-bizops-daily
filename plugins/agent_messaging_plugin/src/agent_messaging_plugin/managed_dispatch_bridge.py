"""F1 compatibility reads and corrections for durable managed dispatches."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_records

from .managed_dispatch_errors import DispatchError
from .schema import TABLE_MANAGED_DISPATCH, TABLE_MANAGED_DISPATCH_EVENT

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

MANAGED_DISPATCH_PAGE_DEFAULT = 100
MANAGED_DISPATCH_PAGE_MAX = 250


def _page_limit(value: int | str | None) -> int:
    if value is None or isinstance(value, bool):
        raise DispatchError("page_limit_invalid", "limit must be an integer.")
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise DispatchError("page_limit_invalid", "limit must be an integer.") from exc
    if not 1 <= limit <= MANAGED_DISPATCH_PAGE_MAX:
        raise DispatchError(
            "page_limit_invalid",
            f"limit must be between 1 and {MANAGED_DISPATCH_PAGE_MAX}.",
        )
    return limit


def _page_after(first: object, row_id: object, *, first_name: str) -> list[str] | None:
    first_text = str(first or "").strip()
    id_text = str(row_id or "").strip()
    if bool(first_text) != bool(id_text):
        raise DispatchError(
            "page_cursor_invalid",
            f"{first_name} and after_id must be supplied together.",
        )
    return [first_text, id_text] if first_text else None


def _json_safe_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for field, value in result.items():
        if isinstance(value, datetime):
            parsed = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
            result[field] = parsed.isoformat()
    return result


def _page_result(
    rows: list[dict[str, Any]],
    *,
    limit: int,
    first_name: str,
) -> tuple[list[dict[str, Any]], bool, dict[str, str] | None]:
    page = rows[:limit]
    truncated = len(rows) > limit
    if not truncated:
        return page, False, None
    last = page[-1]
    first_value = last.get(first_name)
    row_id = last.get("id")
    if first_value in (None, "") or row_id in (None, ""):
        raise DispatchError(
            "page_cursor_missing",
            f"A returned row lacks cursor fields {first_name!r}/'id'.",
        )
    return page, True, {first_name: str(first_value), "id": str(row_id)}


def _read_page(
    state: StateManagementInterface,
    *,
    table: str,
    filters: Mapping[str, object],
    first_name: str,
    limit: int | str | None,
    after_first: object,
    after_id: object,
) -> tuple[list[dict[str, Any]], bool, dict[str, str] | None]:
    page_limit = _page_limit(limit)
    query: dict[str, object] = {
        "table": table,
        "filters": dict(filters),
        "order_by": [[first_name, "asc"], ["id", "asc"]],
        "limit": page_limit + 1,
        "unbounded": True,
    }
    after = _page_after(after_first, after_id, first_name=first_name)
    if after is not None:
        query["after"] = after
    rows = [
        _json_safe_row(row)
        for row in require_records(
            state.query_ordered(AGENT_ROLE_BINDING_NAMESPACE, query)
        )
    ]
    return _page_result(rows, limit=page_limit, first_name=first_name)


def managed_dispatch_inventory(
    state: StateManagementInterface,
    *,
    state_name: str = "",
    limit: int | str | None = MANAGED_DISPATCH_PAGE_DEFAULT,
    after_created_at: object = None,
    after_id: object = None,
) -> dict[str, Any]:
    """Return one complete, tie-safe page of managed-dispatch inventory."""
    page, truncated, next_cursor = _read_page(
        state,
        table=TABLE_MANAGED_DISPATCH,
        filters={"state": state_name} if state_name else {},
        first_name="created_at",
        limit=limit,
        after_first=after_created_at,
        after_id=after_id,
    )
    return {
        "dispatches": page,
        "returned": len(page),
        "truncated": truncated,
        "next_cursor": next_cursor,
    }


def managed_dispatch_events(
    state: StateManagementInterface,
    *,
    dispatch_id: str = "",
    event_kind: str = "",
    accepted: object = None,
    limit: int | str | None = MANAGED_DISPATCH_PAGE_DEFAULT,
    after_event_at: object = None,
    after_id: object = None,
) -> dict[str, Any]:
    """Return one complete, tie-safe page of append-only dispatch events."""
    if accepted is not None and not isinstance(accepted, bool):
        raise DispatchError("accepted_filter_invalid", "accepted must be a boolean.")
    filters: dict[str, object] = {}
    for name, value in (("dispatch_id", dispatch_id), ("event_kind", event_kind)):
        if value:
            filters[name] = value
    if accepted is not None:
        filters["accepted"] = accepted
    page, truncated, next_cursor = _read_page(
        state,
        table=TABLE_MANAGED_DISPATCH_EVENT,
        filters=filters,
        first_name="event_at",
        limit=limit,
        after_first=after_event_at,
        after_id=after_id,
    )
    return {
        "events": page,
        "returned": len(page),
        "truncated": truncated,
        "next_cursor": next_cursor,
    }


def all_managed_dispatch_events(
    state: StateManagementInterface,
    dispatch_id: str,
) -> list[dict[str, Any]]:
    """Exhaust the public event-page contract for one status aggregate."""
    events: list[dict[str, Any]] = []
    after_event_at: object = None
    after_id: object = None
    while True:
        page = managed_dispatch_events(
            state,
            dispatch_id=dispatch_id,
            limit=MANAGED_DISPATCH_PAGE_MAX,
            after_event_at=after_event_at,
            after_id=after_id,
        )
        events.extend(page["events"])
        cursor = page["next_cursor"]
        if cursor is None:
            return events
        after_event_at = cursor["event_at"]
        after_id = cursor["id"]


def _require_text(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise DispatchError(f"{field}_required", f"{field} is required.")
    return text


def _require_legacy_expired(row: Mapping[str, Any], action: str) -> None:
    if str(row["state"]) != "expired":
        raise DispatchError(
            f"{action}_not_allowed",
            f"{action.replace('_', ' ').title()} requires legacy expired state.",
        )


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_completion_evidence(
    contract: Mapping[str, Any],
    evidence: object,
    verdict: object,
    *,
    prefix: str,
) -> dict[str, Any]:
    from .session_lifecycle_store import validate_completion_evidence  # noqa: PLC0415

    return validate_completion_evidence(contract, evidence, verdict, prefix=prefix)


def legacy_completion_updates(
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
    observed_at: datetime,
    _actor_role: str,
) -> dict[str, Any]:
    """Accept independently verified work stranded by legacy TTL expiry."""
    _require_legacy_expired(row, "legacy_completion")
    artifact = Path(str(row["expected_path"]))
    if not artifact.is_file():
        raise DispatchError("completion_artifact_missing", "Expected artifact does not exist.")
    artifact_sha256 = _require_text(payload.get("artifact_sha256"), "artifact_sha256")
    if hashlib.sha256(artifact.read_bytes()).hexdigest() != artifact_sha256:
        raise DispatchError("completion_hash_mismatch", "Completion artifact hash is wrong.")
    contract_sha256 = _require_text(
        payload.get("completion_contract_sha256"),
        "completion_contract_sha256",
    )
    if contract_sha256 != str(row["completion_contract_sha256"]):
        raise DispatchError("completion_contract_mismatch", "Completion contract digest is wrong.")
    contract = row.get("completion_contract")
    if not isinstance(contract, Mapping):
        raise DispatchError("completion_contract_invalid", "Stored completion contract is invalid.")
    worker_completion = payload.get("worker_completion")
    if not isinstance(worker_completion, Mapping):
        raise DispatchError(
            "legacy_worker_completion_required",
            "Structured worker_completion evidence is required.",
        )
    worker_evidence = _validate_completion_evidence(
        contract,
        worker_completion.get("evidence"),
        worker_completion.get("verdict"),
        prefix="completion",
    )
    worker_digest = _canonical_sha256(worker_evidence)
    acceptance = payload.get("acceptance_evidence")
    if not isinstance(acceptance, Mapping):
        raise DispatchError("acceptance_evidence_required", "Acceptance evidence is required.")
    acceptance_evidence = _validate_completion_evidence(
        contract,
        acceptance.get("evidence"),
        acceptance.get("verdict"),
        prefix="acceptance",
    )
    if _require_text(
        acceptance.get("worker_evidence_sha256"),
        "worker_evidence_sha256",
    ) != worker_digest:
        raise DispatchError(
            "acceptance_worker_evidence_mismatch",
            "Acceptance did not bind the exact worker evidence projection.",
        )
    return {
        "state": "completed",
        "completion_reported_at": observed_at.isoformat(),
        "completion_accepted_at": observed_at.isoformat(),
        "reported_artifact_sha256": artifact_sha256,
        "reported_completion_evidence": worker_evidence,
        "reported_completion_verdict": str(worker_completion["verdict"]),
        "acceptance_evidence": {
            "evidence": acceptance_evidence,
            "verdict": str(acceptance["verdict"]),
            "worker_evidence_sha256": worker_digest,
        },
        "terminal_reason": "verified_legacy_completion",
        "next_required_action": "none",
        "responsible_role": "",
    }


def _require_attempt_disposition(
    row: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> None:
    disposition = _require_text(evidence.get("attempt_disposition"), "attempt_disposition")
    current_attempt = str(row.get("current_agent_instance_id") or "")
    if current_attempt and (
        disposition != "confirmed_dead" or str(row.get("host_liveness") or "") != "dead"
    ):
        raise DispatchError(
            "legacy_live_attempt_unresolved",
            "A current legacy attempt requires durable confirmed-dead liveness evidence.",
        )
    if not current_attempt and disposition != "absent":
        raise DispatchError(
            "legacy_attempt_evidence_invalid",
            "A legacy dispatch with no current attempt requires attempt_disposition='absent'.",
        )


def _require_work_evidence(evidence: Mapping[str, Any]) -> None:
    disposition = _require_text(evidence.get("work_disposition"), "work_disposition")
    if disposition not in {"none_to_preserve", "preserved"}:
        raise DispatchError(
            "legacy_work_evidence_invalid",
            "work_disposition must be 'none_to_preserve' or 'preserved'.",
        )
    refs = evidence.get("evidence_refs")
    valid_refs = isinstance(refs, list) and bool(refs) and all(
        isinstance(value, str) and value.strip() for value in refs
    )
    if not valid_refs:
        raise DispatchError(
            "legacy_evidence_refs_invalid",
            "legacy expiry cancellation requires non-empty evidence_refs.",
        )


def legacy_cancel_updates(
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
    _observed_at: datetime,
    _actor_role: str,
) -> dict[str, Any]:
    """Cancel one legacy expiry only with durable no-live-work evidence."""
    _require_legacy_expired(row, "legacy_cancel")
    reason = _require_text(payload.get("reason"), "reason")
    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping):
        raise DispatchError(
            "legacy_expiry_evidence_required",
            "Structured legacy-expiry evidence is required.",
        )
    _require_attempt_disposition(row, evidence)
    _require_work_evidence(evidence)
    return {
        "state": "cancelled",
        "terminal_reason": f"legacy_expiry_cancelled: {reason}",
        "next_required_action": "none",
        "responsible_role": "",
    }


__all__ = [
    "MANAGED_DISPATCH_PAGE_DEFAULT",
    "MANAGED_DISPATCH_PAGE_MAX",
    "all_managed_dispatch_events",
    "legacy_cancel_updates",
    "legacy_completion_updates",
    "managed_dispatch_events",
    "managed_dispatch_inventory",
]
