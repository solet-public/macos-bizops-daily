"""State-interface-backed ``held_authorization`` store (R1 held-authorization
queue, 2026-08-17, seat GO ruling).

Pure state-layer primitives over the ``held_authorization`` table declared in
``schema.py``. This is the mechanically-enumerable half of the R1 fix — the
platform-side backstop that does not depend on any seat remembering to write
anything, complementing the OBLIGATIONS running-log slot
(``ananta/knowledge_base/seat_running_log_convention.md``), which fixes the
same failure through seat discipline. Git-Controller calls
:func:`record_held_authorization` at the moment it refuses a peer's citation
of an authorization it cannot verify first-party in its own inbox — a moment
GC reaches mechanically every time, independent of any seat's memory. Retire
via :func:`retire_held_authorization`, either by GC (on receiving the
matching first-party authorization) or by the ``owed_by_role`` holder
(superseded/withdrawn work).

No raw SQL, no bare ``None`` filter values — every filter that means
"IS NULL" uses ``{"op": "is_null"}`` (a bare ``None`` compiles to a literal
``col = NULL`` comparison, which matches zero rows silently, forever).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import (
    StateOperationError,
    require_completed,
    require_records,
    require_updated,
)

from .schema import TABLE_HELD_AUTHORIZATION

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

_COL_ID = "id"
_COL_OWED_BY_ROLE = "owed_by_role"
_COL_REQUESTING_PEER = "requesting_peer"
_COL_RETIRED_AT = "retired_at"


def record_held_authorization(
    state: StateManagementInterface,
    *,
    requesting_peer: str,
    owed_by_role: str,
    branch_or_request_ref: str,
    reason: str,
) -> str:
    """Insert one open entry. Returns the new row's ``id``.

    A fresh INSERT every call, deliberately — this is a queue of REFUSAL
    EVENTS, not a one-row-per-request cache; the same request can be
    refused more than once across retries, and each refusal is its own
    obligation until something retires it. ``created_at`` is a
    platform-managed auto-timestamp (protected standard field) — the state
    service stamps it on insert; this store never sets it explicitly.
    """
    result = require_completed(
        state.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_HELD_AUTHORIZATION,
                "record": {
                    _COL_REQUESTING_PEER: requesting_peer,
                    _COL_OWED_BY_ROLE: owed_by_role,
                    "branch_or_request_ref": branch_or_request_ref,
                    "reason": reason,
                },
            },
        ),
        "insert held_authorization",
    )
    inner = result.get("result")
    row_id = inner.get("generated_id") if isinstance(inner, dict) else None
    if not isinstance(row_id, str) or not row_id:
        raise StateOperationError(
            "state insert held_authorization returned no string 'generated_id' "
            f"(got {row_id!r})",
        )
    return row_id


def list_held_authorizations(
    state: StateManagementInterface,
    *,
    owed_by_role: str | None = None,
    requesting_peer: str | None = None,
    include_retired: bool = False,
) -> list[dict[str, Any]]:
    """The "what is blocked on <role>?" answer. Open entries
    (``retired_at IS NULL``) by default; pass ``include_retired=True`` for
    the full history. Filters compose with AND — both are optional."""
    filters: dict[str, Any] = {}
    if owed_by_role is not None:
        filters[_COL_OWED_BY_ROLE] = owed_by_role
    if requesting_peer is not None:
        filters[_COL_REQUESTING_PEER] = requesting_peer
    if not include_retired:
        filters[_COL_RETIRED_AT] = {"op": "is_null"}
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_HELD_AUTHORIZATION, "filters": filters},
    )
    return require_records(result)


def retire_held_authorization(
    state: StateManagementInterface,
    *,
    entry_id: str,
    retired_reason: str,
    retired_by: str,
    retired_at: str,
) -> bool:
    """Set ``retired_at`` exactly once. Returns ``True`` if this call retired
    it, ``False`` if the entry does not exist or was already retired — the
    predicated ``retired_at IS NULL`` filter makes a double-retire a no-op
    rather than a silent overwrite of an earlier retirement's provenance."""
    result = state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_HELD_AUTHORIZATION,
            "filters": {_COL_ID: entry_id, _COL_RETIRED_AT: {"op": "is_null"}},
        },
        {
            _COL_RETIRED_AT: retired_at,
            "retired_reason": retired_reason,
            "retired_by": retired_by,
        },
    )
    return require_updated(result) == 1


__all__ = [
    "list_held_authorizations",
    "record_held_authorization",
    "retire_held_authorization",
]
