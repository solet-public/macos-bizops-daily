"""Fleet session-management Phase B, D1 (§2) — the AMEND-4b cardinality gate.

Wraps :func:`role_binding.claim_role_binding_v4` (the per-ROLE CAS) with a
per-SESSION uniqueness row (:mod:`schema`'s ``session_role_claim`` table) so
"every session holds at most one named role" (the net invariant, §2) is a
uniqueness FACT — won via ``upsert_state(on_conflict='do_nothing')`` BEFORE
the binding CAS — rather than the TOCTOU check-then-claim the draft shipped
(AMEND 4b).

TRUST MODEL — read this before touching anything here (Dawn ruling,
2026-08-03, exercising the Architect memo's own pre-authorized deferral
clause, memo AMEND 4c): ``CallContext``
(``ananta.core.services.call_context``) carries only
``{calling_plugin, principal_kind, principal_id}`` — a coarse authorization
discriminator, NOT a per-session identity. There is no server-built source
for a claimant's OWN ``agent_session_id`` at D1. This gate therefore keys on
``agent_session_id`` at the SAME trust level the existing ``role_binding`` CAS
already operates at: caller-supplied, filled from the registry via
``peer_registry.agent_session_id_for_instance`` ONLY when the caller leaves it
empty (REL-07(1)), never validated against a caller-SUPPLIED value. This is
ZERO REGRESSION versus today and genuinely closes the TOCTOU race for an
honestly-identified session claiming two roles concurrently — it is NOT
hardened against a forged ``agent_instance_id``/``agent_session_id`` pair (the
measured ``reference_peer_claim_role_trusts_caller_asserted_instance_id``
trap). NEVER describe this gate as "enforced" without that caveat — say what
it does not enforce, not just what it does (a green lies four ways).

Crash-safety (Architect ratification, arm-a0cd684f9317, folded into the spec
as §2 rules 3-4):

* Every PREDICATED write in this module that loses its predicate (0 rows
  affected — another writer moved the row first) makes the caller RE-ENTER
  THE TOP of the bounded claim loop, mirroring
  ``role_binding.claim_role_binding_v4``'s own ``_CLAIM_CAS_MAX_ATTEMPTS``
  convention. The ONE exception: the displacer's delete of the LOSER's row
  (:func:`delete_session_role_claim_if_still_holds`) is a BENIGN no-op at 0 —
  the loser's own next claim self-repairs via branch (iii) below, so nobody
  needs to retry on its behalf.
* Rule-3 lane handoff (an explicit release-then-claim for a role CHANGE)
  releases the OLD ``role_binding`` row STRICTLY BEFORE deleting the session's
  own ``session_role_claim`` row (never the reverse — row-first + a crash
  between would leave the old binding standing while a fresh INSERT (which
  never consults old bindings) lets a second claim through, a double-claim).
  Binding-first crash residue is exactly branch (iii)'s stale-orphan shape,
  which self-repairs safely on the next claim attempt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import (
    require_completed,
    require_deleted,
    require_records,
)

from .schema import (
    TABLE_ROLE_BINDING,
    TABLE_SESSION_ROLE_CLAIM,
    session_role_claim_external_id,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)

_COL_EXTERNAL_ID = "external_id"
_COL_IS_DELETED = "is_deleted"
_COL_AGENT_SESSION_ID = "agent_session_id"
_COL_HELD_ROLE = "held_role"
_COL_AGENT_INSTANCE_ID = "agent_instance_id"
_COL_CLAIMED_AT = "claimed_at"
_COL_ROLE = "role"

# Bounded attempt budget for the outer (session-key + binding) claim loop —
# same convention/order-of-magnitude as role_binding.py's per-role CAS loop.
_GATE_MAX_ATTEMPTS = 8


class CardinalityConflictError(Exception):
    """The claimant already holds a DIFFERENT role whose binding still names
    it — the net invariant refuses a second concurrent role without an
    explicit release first (rule 3 lane handoff), never an ambient displace.

    NOT a race outcome (unlike a lost predicate) — this is a genuine policy
    refusal and propagates immediately, never retried by the bounded loop.
    """

    def __init__(self, agent_session_id: str, held_role: str, requested_role: str) -> None:
        self.agent_session_id = agent_session_id
        self.held_role = held_role
        self.requested_role = requested_role
        super().__init__(
            f"cardinality_conflict: session {agent_session_id!r} already holds "
            f"role {held_role!r} (binding still names it); release it first "
            f"before claiming {requested_role!r}.",
        )


class SessionRoleClaimContendedError(Exception):
    """The bounded outer claim loop did not converge — persistent contention
    (unlikely for a per-session key) or a genuine repeated lost-predicate
    race. Fails loud with the attempt count rather than spinning forever."""

    def __init__(self, agent_session_id: str, requested_role: str, attempts: int) -> None:
        super().__init__(
            f"session_role_claim_contended: session {agent_session_id!r} claiming "
            f"{requested_role!r} did not converge in {attempts} attempts.",
        )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def read_session_role_claim(
    state: StateManagementInterface, agent_session_id: str,
) -> dict[str, object] | None:
    """Read a session's OWN ``session_role_claim`` row, or ``None`` if absent.
    Public so callers outside this module (``retire_session``'s cleanup step)
    can read ``held_role`` before predicate-deleting against it."""
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_ROLE_CLAIM,
            "filters": {
                _COL_EXTERNAL_ID: session_role_claim_external_id(agent_session_id),
                _COL_IS_DELETED: 0,
            },
        },
    )
    records = require_records(result)
    return records[0] if records else None


def _binding_still_names_principal(
    state: StateManagementInterface, *, role_name: str, agent_session_id: str,
) -> bool:
    """Does the LIVE ``role_binding`` row for ``role_name`` still name this
    session as holder? Used to disambiguate a stale orphan (iii) from a
    genuine second-role attempt (ii) when the session-key row's ``held_role``
    disagrees with the currently requested role."""
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_ROLE_BINDING,
            "filters": {_COL_ROLE: role_name, _COL_IS_DELETED: 0},
        },
    )
    records = require_records(result)
    if not records:
        return False
    return str(records[0].get(_COL_AGENT_SESSION_ID) or "") == agent_session_id


def _insert_session_role_claim(
    state: StateManagementInterface,
    *,
    agent_session_id: str,
    requested_role: str,
    agent_instance_id: str,
) -> bool:
    """First-claim INSERT race primitive. Returns ``True`` iff THIS call won
    the insert (``on_conflict='do_nothing'``) — the platform-UNIQUE
    ``external_id`` device, same shape as ``role_binding``'s first-claim."""
    result = require_completed(
        state.upsert_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_SESSION_ROLE_CLAIM,
                "record": {
                    _COL_EXTERNAL_ID: session_role_claim_external_id(agent_session_id),
                    _COL_AGENT_SESSION_ID: agent_session_id,
                    _COL_HELD_ROLE: requested_role,
                    _COL_AGENT_INSTANCE_ID: agent_instance_id,
                    _COL_CLAIMED_AT: _now_iso(),
                },
                "conflict_columns": [_COL_EXTERNAL_ID],
                "on_conflict": "do_nothing",
            },
        ),
        "upsert session_role_claim",
    )
    inserted = result.get("result")
    return bool(inserted.get("inserted")) if isinstance(inserted, dict) else False


def _repair_session_role_claim(
    state: StateManagementInterface,
    *,
    agent_session_id: str,
    stale_held_role: str,
    requested_role: str,
    agent_instance_id: str,
) -> bool:
    """Branch (iii): predicated self-repair of a STALE orphan row — the prior
    ``held_role`` no longer has a live binding naming this session, so this
    session is free to repoint its OWN row to the newly requested role.

    Predicated on ``held_role`` still equalling ``stale_held_role`` (a
    compare-and-set on the read just taken); ``rows_affected == 0`` means
    another writer moved this row first — a lost race, NOT a fault. Per the
    Architect ratification, the caller must NOT fall through on that outcome;
    it re-enters the top of the bounded outer loop.
    """
    result = state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_ROLE_CLAIM,
            "filters": {
                _COL_EXTERNAL_ID: session_role_claim_external_id(agent_session_id),
                _COL_HELD_ROLE: stale_held_role,
                _COL_IS_DELETED: 0,
            },
        },
        {
            _COL_HELD_ROLE: requested_role,
            _COL_AGENT_INSTANCE_ID: agent_instance_id,
            _COL_CLAIMED_AT: _now_iso(),
        },
    )
    data = require_completed(result, "update session_role_claim (self-repair)")
    inner = data.get("result")
    updated = inner.get("updated") if isinstance(inner, dict) else None
    return isinstance(updated, int) and updated == 1


_GateVerdict = Literal["won", "retry"]


def _win_session_role_claim(
    state: StateManagementInterface,
    *,
    agent_session_id: str,
    requested_role: str,
    agent_instance_id: str,
) -> _GateVerdict:
    """ONE attempt at the AMEND-4b win-before-CAS gate (Dawn ruling gap 2).

    Returns ``"won"`` when this call may proceed to the per-role binding CAS;
    ``"retry"`` when the caller must re-enter the top of the bounded outer
    loop (a lost predicate — NOT a fault). Raises
    :class:`CardinalityConflictError` for a genuine second-role attempt
    (branch ii) — that is a policy refusal, never retried.
    """
    if _insert_session_role_claim(
        state,
        agent_session_id=agent_session_id,
        requested_role=requested_role,
        agent_instance_id=agent_instance_id,
    ):
        return "won"
    row = read_session_role_claim(state, agent_session_id)
    if row is None:
        # Conflict reported, but the row is gone by the time we re-read —
        # another writer raced us out from under. Not a fault; retry.
        return "retry"
    held_role = str(row.get(_COL_HELD_ROLE) or "")
    if held_role == requested_role:
        # Branch (i): idempotent refresh — this session already holds exactly
        # the role it is asking for again (e.g. a SessionStart re-claim).
        return "won"
    if _binding_still_names_principal(
        state, role_name=held_role, agent_session_id=agent_session_id,
    ):
        # Branch (ii): a genuine second NAMED role while the first is still
        # live — refused, resolvable only via the rule-3 release-first path.
        raise CardinalityConflictError(agent_session_id, held_role, requested_role)
    # Branch (iii): stale orphan — the row's held_role has no live binding
    # naming this session (a prior crash between binding-release and
    # session-key-row-delete, or between a displacer's binding CAS and its
    # loser-row cleanup). Self-repair; a lost predicate here retries the
    # outer loop rather than falling through (Architect ratification #1).
    if _repair_session_role_claim(
        state,
        agent_session_id=agent_session_id,
        stale_held_role=held_role,
        requested_role=requested_role,
        agent_instance_id=agent_instance_id,
    ):
        return "won"
    return "retry"


def delete_session_role_claim_if_still_holds(
    state: StateManagementInterface, *, agent_session_id: str, expected_held_role: str,
) -> bool:
    """Displacer cleanup: predicated-delete the LOSER's session-key row, iff
    it still names the role that was just displaced (Dawn ruling gap 1).

    Returns whether the delete actually matched. A ``False`` return (0 rows —
    the row was already gone, or already repointed to a different role) is a
    BENIGN no-op by design (Architect ratification #1's one exception): the
    loser's own next claim self-repairs via branch (iii) above, so nobody
    needs to retry on its behalf. HARD-delete only (``soft_delete=False``) —
    the same no-tombstone requirement as ``release_role_binding_v4``: a soft
    tombstone would deadlock the loser's own future INSERT race primitive.
    """
    result = state.delete_records(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_ROLE_CLAIM,
            "filters": {
                _COL_EXTERNAL_ID: session_role_claim_external_id(agent_session_id),
                _COL_HELD_ROLE: expected_held_role,
            },
            "soft_delete": False,
        },
    )
    return require_deleted(result) == 1


@dataclass(frozen=True, slots=True)
class CardinalityGatedClaim:
    """The outer-loop entry point's inputs, bundled to keep the loop body
    readable — mirrors ``role_binding.HolderClaim``'s low-arity intent."""

    agent_session_id: str
    requested_role: str
    agent_instance_id: str


def win_cardinality_gate(
    state: StateManagementInterface, gated: CardinalityGatedClaim,
) -> None:
    """Bounded outer loop: win the AMEND-4b session-key gate before the
    caller runs the per-role binding CAS. Raises
    :class:`CardinalityConflictError` (policy refusal, not retried) or
    :class:`SessionRoleClaimContendedError` (loop exhausted — persistent
    contention or a repeated lost race) — either propagates loud; a normal
    return means the caller may proceed to ``claim_role_binding_v4``.
    """
    for _ in range(_GATE_MAX_ATTEMPTS):
        verdict = _win_session_role_claim(
            state,
            agent_session_id=gated.agent_session_id,
            requested_role=gated.requested_role,
            agent_instance_id=gated.agent_instance_id,
        )
        if verdict == "won":
            return
    raise SessionRoleClaimContendedError(
        gated.agent_session_id, gated.requested_role, _GATE_MAX_ATTEMPTS,
    )


__all__ = [
    "CardinalityConflictError",
    "CardinalityGatedClaim",
    "SessionRoleClaimContendedError",
    "delete_session_role_claim_if_still_holds",
    "win_cardinality_gate",
]
