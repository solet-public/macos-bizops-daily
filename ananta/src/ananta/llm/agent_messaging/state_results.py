"""Strict extraction of ``StateManagementInterface`` ActionResult envelopes (v10).

A state op returns an ``ActionResult`` (a dict envelope) and does NOT raise on a
provider error — it returns ``action_status != 'completed'``. Treating that as
an empty/zero success is the bug class Codex CODE-review BLOCKER-1 caught: a
failed durable ``upsert`` followed by live emission + a success response; a Q1
role section reporting ``status=OK`` + empty on a DB fault; a claim/release
reporting success on a failed write.

These helpers FAIL LOUD (fast-fail, no defensive swallow) on a non-completed
status, and extract the CORRECT provider path — verified against the postgres +
rds providers:

* **queries** (``query_state`` / ``query_ordered``) → ``data.records`` (flat).
* **mutations** (``update_state`` / ``upsert_state`` / ``delete_records``) →
  ``data.result.{updated, upserted, deleted}`` (NESTED under ``result``). The
  pre-fix code read ``data.updated`` directly, so a successful CAS reported 0 in
  production (the in-memory smoke fakes returned the flat shape and masked it).
"""

from __future__ import annotations

_COMPLETED = "completed"


class StateOperationError(RuntimeError):
    """A ``StateManagementInterface`` op did not complete (provider error result).

    Raised instead of silently treating a failed state op as empty/zero so the
    caller fails loud — and, for the ``peer_inbox`` role section, is caught by
    the Q1 fault-domain boundary (→ ``role_section_status=ERROR``) rather than
    masquerading as an empty-but-healthy section.
    """


def _completed_data(result: object, op: str) -> dict[str, object]:
    """Return ``data`` of a COMPLETED ActionResult; raise on any other status."""
    if not isinstance(result, dict):
        raise StateOperationError(f"state {op}: result is not a dict: {result!r}")
    status = str(result.get("action_status", ""))
    if status != _COMPLETED:
        raise StateOperationError(
            f"state {op} did not complete (action_status={status!r}): {result!r}",
        )
    data = result.get("data")
    return data if isinstance(data, dict) else {}


def require_records(result: object) -> list[dict[str, object]]:
    """Records from a COMPLETED ``query_state`` / ``query_ordered`` (``data.records``)."""
    data = _completed_data(result, "query")
    records = data.get("records")
    if not isinstance(records, list):
        return []
    return [row for row in records if isinstance(row, dict)]


def require_updated(result: object) -> int:
    """Rows-affected from a COMPLETED ``update_state`` (``data.result.updated``).

    NESTED under ``result`` — reading ``data.updated`` (the pre-fix path) yields
    0 against the real providers even on a successful CAS.
    """
    data = _completed_data(result, "update")
    inner = data.get("result")
    updated = inner.get("updated") if isinstance(inner, dict) else None
    return updated if isinstance(updated, int) else 0


def require_deleted(result: object) -> int:
    """Rows-affected from a COMPLETED ``delete_records`` (``data.result.deleted``).

    NESTED under ``result``, same shape family as :func:`require_updated`. The
    predicated-delete callers (a displacer pruning the loser's session-key row)
    need the count to distinguish "matched and removed" from "predicate no
    longer held" (0 — a benign lost race, not a fault).
    """
    data = _completed_data(result, "delete")
    inner = data.get("result")
    deleted = inner.get("deleted") if isinstance(inner, dict) else None
    return deleted if isinstance(deleted, int) else 0


def is_completed(result: object) -> bool:
    """True iff ``result`` is a COMPLETED ActionResult envelope (non-raising).

    The companion to :func:`require_completed` for the ONE call site that must
    inspect the status WITHOUT raising on a non-completed result: the role-model
    v4 §5.1 first-claim INSERT. ``write_state`` on a UNIQUE conflict returns a
    non-completed envelope (the postgres provider raises ``psycopg.UniqueViolation``,
    which ``write_state``'s broad ``except`` catches into an error result — it
    does NOT propagate), and the claim disambiguates conflict-vs-fault by
    RE-READING. ``require_completed`` would raise on that expected conflict, so
    the first-claim path predicates on this instead.
    """
    return isinstance(result, dict) and str(result.get("action_status", "")) == _COMPLETED


def require_completed(result: object, op: str) -> dict[str, object]:
    """Assert a state op COMPLETED; return its ``data``; raise loudly otherwise.

    For writes whose contract is success (``upsert`` / ``delete`` / ``set_key_value``)
    the durability guarantee depends on the write actually landing, so a
    non-completed status must propagate, never be swallowed. Returns the
    completed ``data`` dict so a caller that DOES need a field (e.g. the
    ``get_key_value`` ``found`` flag for the one-shot backfill marker) can read
    it off the same fail-loud check.
    """
    return _completed_data(result, op)


__all__ = [
    "StateOperationError",
    "is_completed",
    "require_completed",
    "require_records",
    "require_updated",
]
