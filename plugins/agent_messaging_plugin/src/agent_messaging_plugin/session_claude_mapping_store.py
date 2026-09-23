"""T1 usage-capture lane (2026-08-05, workbench
the 2026-08-05 usage-capture ruling) — the state-layer primitives
over ``session_claude_mapping`` (schema.py). A worker's Claude Code
session_id rotates over its lifetime (/clear, /resume) without touching its
managed_session row (ONE-TO-MANY, ruling's key constraint), so this table
accumulates one row per observed firing rather than one row per worker.

Idempotent by construction: :func:`upsert_session_claude_mapping` conflicts
on (agent_instance_id, claude_session_id, captured_at) — exactly the triple
the file-per-firing spool's own filename encodes — so re-ingesting a spool
file that survived a crash before its post-write delete upserts the SAME
row rather than duplicating it (ruling 2026-08-05, Q1(b)).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_completed, require_records

from .schema import TABLE_SESSION_CLAUDE_MAPPING

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

_COL_AGENT_INSTANCE_ID = "agent_instance_id"
_COL_IS_DELETED = "is_deleted"
_CONFLICT_COLUMNS = ["agent_instance_id", "claude_session_id", "captured_at"]


def upsert_session_claude_mapping(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    claude_session_id: str,
    captured_at: str,
    capture_source: str,
) -> None:
    """Idempotent upsert of one observed firing. Conflicts on the same
    triple the spool filename encodes, so a crash-then-retry ingestion of
    the same not-yet-deleted spool file never duplicates the row."""
    require_completed(
        state.upsert_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_SESSION_CLAUDE_MAPPING,
                "record": {
                    _COL_AGENT_INSTANCE_ID: agent_instance_id,
                    "claude_session_id": claude_session_id,
                    "captured_at": captured_at,
                    "capture_source": capture_source,
                },
                "conflict_columns": _CONFLICT_COLUMNS,
            },
        ),
        "upsert session_claude_mapping",
    )


def list_session_claude_mappings(
    state: StateManagementInterface, agent_instance_id: str,
) -> list[dict[str, Any]]:
    """All live mapping rows for one worker, oldest-observation-order is
    NOT guaranteed by this call (callers needing order sort by
    ``captured_at`` themselves) — every firing this worker's lifetime has
    ever recorded, the ONE-TO-MANY rotation history in full."""
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_CLAUDE_MAPPING,
            "filters": {_COL_AGENT_INSTANCE_ID: agent_instance_id, _COL_IS_DELETED: 0},
        },
    )
    return require_records(result)


__all__ = [
    "list_session_claude_mappings",
    "upsert_session_claude_mapping",
]
