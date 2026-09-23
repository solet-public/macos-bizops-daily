"""State-interface-backed ``agent_role_binding`` store (v10 Control #2).

The single WRITABLE resolution + compare-and-set authority for role ownership,
replacing the address-book ``agent_role`` entries (``peer_role_management``).
Every access goes through ``StateManagementInterface`` primitives — operator
mandate: NO raw SQL/DDL. The binding's ``TableSchema`` is declared + registered
in the plugin ``schema.py``; the cross-layer column contract lives in core
``ananta.llm.agent_messaging.role_binding`` (shared so the core read path —
``AgentMessagingService.list_silent_for_roles`` — and this write path can never
drift on the column names).

Identity model (KB ``03_inter_agent_messaging``):

* ``agent_instance_id`` — durable per-bridge UUID, NEW each subprocess.
* ``agent_session_id`` — the stable logical-session key the CAS self-refresh
  keys on to re-point a rotated ``agent_instance_id`` on reconnect WITHOUT a
  fresh explicit claim. One CAS, filtered on the session id alone, re-points
  every role that session holds.
* ``session_label`` — display-only, never a routing key.

This module is pure (no plugin/bridge imports, no live caller until the
Control #2.C cutover repoints ``peer_send_by_name`` / ``peer_claim_role`` /
``_attempt_role_self_refresh`` to it), so it is fully unit-smokeable against a
fake state.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ananta.llm.agent_messaging.role_binding import (
    AGENT_ROLE_BINDING_NAMESPACE,
    COL_AGENT_ID,
    COL_AGENT_INSTANCE_ID,
    COL_AGENT_SESSION_ID,
    COL_CLAIM_EPOCH,
    COL_CLAIMED_AT,
    COL_DESCRIPTION,
    COL_HOLDER_IDENTITY,
    COL_HOLDER_KIND,
    COL_MEMORY_ID,
    COL_ORIGIN,
    COL_PROPERTIES,
    COL_ROLE,
    COL_ROLE_CLASS,
    COL_SESSION_LABEL,
    HOLDER_KIND_INFERENCE_PROVIDER,
    HOLDER_KIND_SESSION,
    ROLE_ORIGIN_USER,
    TABLE_AGENT_ROLE_BINDING,
    TABLE_ROLE,
    TABLE_ROLE_BINDING,
    role_binding_external_id,
)
from ananta.llm.agent_messaging.state_results import (
    StateOperationError,
    is_completed,
    require_completed,
    require_records,
    require_updated,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)

# Platform-standard soft-delete column (schema_standardizer, INTEGER default 0).
# Resolution/enumeration filter on ``is_deleted = 0`` so a soft-deleted binding
# never resolves (Codex BLOCKER-4 defence-in-depth; release also hard-deletes).
_COL_IS_DELETED = "is_deleted"

# Sentinel ``agent_session_id`` for a backfilled (legacy) binding not yet
# claimed by a live session (Q2 backfill). A real session's CAS self-refresh
# (keyed on its OWN agent_session_id) never matches this, so the first explicit
# ``peer_claim_role`` replaces it — a legacy binding is never silently
# "recovered" by an unrelated session, but it still RESOLVES (so role sends
# queue for replay instead of rejecting).
UNCLAIMED_SESSION_ID = "__unclaimed__"

# The standard platform-UNIQUE conflict field (``role:{role}``).
_COL_EXTERNAL_ID = "external_id"

# Bounded attempt budget for the §5.1 claim CAS loop. A role under genuine
# contention converges in one or two iterations (INSERT loses → displace CAS
# wins); a higher count means a hot livelock or a PERSISTENT non-conflict write
# fault (a re-read that keeps finding no row while the INSERT keeps failing), so
# the loop fails loud with the last state-op detail rather than spinning.
_CLAIM_CAS_MAX_ATTEMPTS = 8


@dataclass(frozen=True, slots=True)
class ResolvedRole:
    """The routing target backing one role binding row.

    Canonical home (the binding store IS the resolution authority).
    ``peer_dispatch`` + the address-book ``peer_role_management`` shim import
    it from here so there is a single ``ResolvedRole`` type across the cutover.

    Role-model v4 (§4.3) adds ``holder_kind`` / ``holder_identity`` / the
    projected ``agent_session_id`` (the last makes §5.0's act-time re-check
    ``resolved.agent_session_id == mine`` constructible). For a
    ``holder_kind='session'`` binding ``agent_id`` / ``session_label`` are the
    typed-parsed session identity; a provider binding leaves them empty (§4.6).
    The v4 fields DEFAULT so the legacy ``agent_role_binding`` resolve path (which
    predates them) still constructs a valid value until the §9 cutover.
    """

    name: str
    agent_id: str
    agent_instance_id: str
    session_label: str
    holder_kind: str = HOLDER_KIND_SESSION
    agent_session_id: str = ""
    holder_identity: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HolderClaim:
    """The claimant's identity for a v4 ``role_binding`` claim (§5.1).

    Bundles the five holder fields threaded through the claim/displace CAS so the
    store helpers stay low-arity. ``holder_identity`` is a RAW dict (the postgres
    provider auto-serializes dict/list values to JSONB; the in-memory backend
    stores it as-is — either way ``_coerce_identity`` reads it back). For a
    ``holder_kind='session'`` claim it carries ``agent_id`` (+ optional
    ``session_label``); for a provider it carries ``provider_kind`` +
    ``provider_ref``. ``agent_session_id`` is the stable logical-session key the
    self-re-claim check and the reconnect CAS both key on — sourced from the
    claimant's live ``peer_binding`` row, NEVER from claim args (REL-07).
    """

    holder_kind: str
    holder_identity: dict[str, object]
    agent_instance_id: str
    agent_session_id: str
    session_label: str


class RoleBindingVacantError(Exception):
    """No ``agent_role_binding`` row exists for the requested role name."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"role_binding_vacant: no binding for role {name!r}")


def resolve_role_binding(
    state: StateManagementInterface, name: str,
) -> ResolvedRole:
    """Resolve a role name to its current routing target (single state read).

    §9 CUTOVER: DELEGATES to :func:`resolve_role_binding_v4` — the live resolution
    authority is now the v4 ``role_binding`` table (readers + writer flipped
    together, §9 step 5). Kept as the stable entry point so every caller
    (``peer_send_by_name``, the inference vertex resolver via
    ``resolve_role_to_instance``, ``peer_claim_role``'s prior-capture) follows the
    flip without a call-site change. It MUST delegate rather than repoint the table:
    v4 stores ``agent_id`` in ``holder_identity`` JSON (no top-level column) and
    discriminates provider holders, so only the typed §4.6 parse resolves correctly.
    Raises :class:`RoleBindingVacantError` when vacant; a malformed row cannot occur
    on the live path (excluded at migration via ``_legacy_row_is_migratable``).
    """
    return resolve_role_binding_v4(state, name)


class RoleBindingMalformedError(Exception):
    """A ``role_binding`` row failed the typed per-``holder_kind`` parse (§4.6)."""

    def __init__(self, name: str, detail: str) -> None:
        self.name = name
        super().__init__(f"role_binding_malformed: role {name!r}: {detail}")


def _require_str(value: object, field_name: str, name: str) -> str:
    parsed = value if isinstance(value, str) else ""
    if not parsed:
        raise RoleBindingMalformedError(name, f"missing/empty required field {field_name!r}")
    return parsed


def _coerce_identity(name: str, identity_raw: object) -> dict[str, object]:
    """``holder_identity`` may arrive as a dict (Postgres JSONB / in-memory) or a
    JSON string (SQLite JSON→TEXT). Fail loud on a non-JSON string."""
    if isinstance(identity_raw, dict):
        return identity_raw
    if isinstance(identity_raw, str) and identity_raw:
        try:
            parsed = json.loads(identity_raw)
        except (ValueError, TypeError) as exc:
            raise RoleBindingMalformedError(
                name, f"holder_identity is not valid JSON: {exc}",
            ) from exc
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parse_holder_identity(
    name: str, holder_kind: str, identity_raw: object,
) -> tuple[str, str, dict[str, object]]:
    """Typed, fail-loud per-``holder_kind`` parse of ``holder_identity`` (§4.6).

    Returns ``(agent_id, session_label, identity_dict)`` — the session-facing
    fields projected onto ResolvedRole (empty for a provider). Never a silent
    ``.get(field,'')``: a missing REQUIRED field raises
    :class:`RoleBindingMalformedError`; an unknown ``holder_kind`` is FATAL (never
    falls through to ``session``).
    """
    identity = _coerce_identity(name, identity_raw)
    if holder_kind == HOLDER_KIND_SESSION:
        agent_id = _require_str(identity.get("agent_id"), "holder_identity.agent_id", name)
        session_label = str(identity.get("session_label") or "")
        return agent_id, session_label, identity
    if holder_kind == HOLDER_KIND_INFERENCE_PROVIDER:
        _require_str(identity.get("provider_kind"), "holder_identity.provider_kind", name)
        _require_str(identity.get("provider_ref"), "holder_identity.provider_ref", name)
        return "", "", identity
    raise RoleBindingMalformedError(name, f"unknown holder_kind {holder_kind!r}")


def resolve_role_binding_v4(
    state: StateManagementInterface, name: str,
) -> ResolvedRole:
    """Resolve a role via the v4 ``role_binding`` table — one read + typed parse.

    §4.3 (one ``query_state`` on ``role_binding`` by ``external_id``, filtered
    ``is_deleted=0``) + §4.6 (typed, fail-loud per-``holder_kind`` parse). Projects
    ``holder_kind`` + ``holder_identity`` + the top-level ``agent_session_id``
    onto ResolvedRole so §5.0's act-time re-check is constructible. Raises
    :class:`RoleBindingVacantError` when vacant, :class:`RoleBindingMalformedError`
    on a bad row.

    NOT yet the live resolution authority — the atomic cutover of the live callers
    (``resolve_role_binding`` + the core readers + the refresh writer) from
    ``agent_role_binding`` to ``role_binding`` is the §9 migration (slice-D). This
    function is built + smoked now; the switch flips later.
    """
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_ROLE_BINDING,
            "filters": {
                _COL_EXTERNAL_ID: role_binding_external_id(name),
                _COL_IS_DELETED: 0,
            },
        },
    )
    records = require_records(result)
    if not records:
        raise RoleBindingVacantError(name)
    row = records[0]
    holder_kind = str(row.get(COL_HOLDER_KIND) or HOLDER_KIND_SESSION)
    agent_id, session_label, identity = _parse_holder_identity(
        name, holder_kind, row.get(COL_HOLDER_IDENTITY),
    )
    return ResolvedRole(
        name=name,
        agent_id=agent_id,
        agent_instance_id=str(row.get(COL_AGENT_INSTANCE_ID) or ""),
        session_label=session_label,
        holder_kind=holder_kind,
        agent_session_id=str(row.get(COL_AGENT_SESSION_ID) or ""),
        holder_identity=identity,
    )


def list_roles_for_agent_instance(
    state: StateManagementInterface, agent_instance_id: str,
) -> list[str]:
    """Return role names currently bound to ``agent_instance_id``.

    This is the reverse lookup needed by transport identity tools:
    roles are derived from ``agent_role_binding`` rows whose
    ``agent_instance_id`` equals the current peer instance, never from
    display-only ``session_label``.
    """
    if not agent_instance_id:
        return []
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            # §9 CUTOVER: the reverse-lookup (roles_held / current_identity) reads v4.
            "table": TABLE_ROLE_BINDING,
            "filters": {
                COL_AGENT_INSTANCE_ID: agent_instance_id,
                _COL_IS_DELETED: 0,
            },
        },
    )
    roles: list[str] = []
    for row in require_records(result):
        role = row.get(COL_ROLE)
        if isinstance(role, str) and role:
            roles.append(role)
    return sorted(roles)


def sole_role_for_reply_address(
    state: StateManagementInterface | None, agent_instance_id: str,
) -> str:
    """The sender's role when it is usable as a durable REPLY ADDRESS, else ``""``.

    Distinct from ``platform_surface._role_for_instance`` **on purpose, and the two
    must not be merged.** That one answers "tag this flow with a role" and takes
    ``sorted(roles)[0]`` — arbitrary-but-deterministic, and a wrong answer costs a
    log line. This one answers "where should a reply be SENT", where the same
    alphabetical pick would silently route replies to a role the sender was not
    acting in: a wrong-RECIPIENT delivery, strictly worse than the stale-but-honest
    instance reply-to it would replace.

    So a multi-role holder yields ``""`` and the caller keeps the instance
    reply-to: *stale but correct-session* beats *durable but wrong-session*. The
    real fix — an optional sender-declared acting role, validated against the roles
    actually held, because only the sender knows which role it acted in — is filed
    as DEF-3 and deliberately NOT done here.

    Degrade-silent: provenance must never break a dispatch, so any lookup fault
    yields ``""`` (the pre-existing instance reply-to), never an exception.
    """
    if state is None or not agent_instance_id:
        return ""
    try:
        roles = list_roles_for_agent_instance(state, agent_instance_id)
    except Exception:  # noqa: BLE001 — a reply-address hint never breaks a send
        logger.warning(
            "reply-address role lookup failed for agent_instance_id=%s; "
            "falling back to the instance reply-to",
            agent_instance_id,
            exc_info=True,
        )
        return ""
    if len(roles) == 1:
        return roles[0]
    if roles:
        # Named, not counted: whoever builds DEF-3 needs the population, and a
        # bare "2 roles" line would make them go find out which.
        logger.info(
            "agent_instance_id=%s holds %d roles %r — using the instance "
            "reply-to, not an arbitrary role (DEF-3)",
            agent_instance_id,
            len(roles),
            roles,
        )
    return ""


class RoleClaimContendedError(Exception):
    """The §5.1 claim CAS did not converge within the bounded attempt budget.

    This means persistent contention (many sessions racing one role) or a
    no-tombstone invariant breach where ``ON CONFLICT DO NOTHING`` keeps seeing a
    soft-deleted row hidden from the live-row re-read. Genuine provider faults
    fail immediately as :class:`StateOperationError`.
    """

    def __init__(self, name: str, attempts: int, last_detail: str) -> None:
        self.name = name
        super().__init__(
            f"role_claim_contended: role {name!r} did not converge in "
            f"{attempts} attempts; last state-op detail: {last_detail}",
        )


def _holder_fields(claim: HolderClaim, *, claim_epoch: int) -> dict[str, object]:
    """The mutable holder columns SET by an INSERT record or a CAS update."""
    return {
        COL_HOLDER_KIND: claim.holder_kind,
        COL_HOLDER_IDENTITY: claim.holder_identity,
        COL_AGENT_INSTANCE_ID: claim.agent_instance_id,
        COL_AGENT_SESSION_ID: claim.agent_session_id,
        COL_CLAIM_EPOCH: claim_epoch,
        COL_CLAIMED_AT: _now_iso(),
    }


def _binding_record(name: str, claim: HolderClaim) -> dict[str, object]:
    """The full ``role_binding`` INSERT record for a fresh (epoch-0) claim."""
    return {
        _COL_EXTERNAL_ID: role_binding_external_id(name),
        COL_ROLE: name,
        **_holder_fields(claim, claim_epoch=0),
    }


def _outcome(
    action: str, name: str, agent_instance_id: str, prior: ResolvedRole | None,
) -> dict[str, object]:
    """Uniform claim outcome envelope (``prior`` drives the §5.4 notify)."""
    return {
        "action": action,
        "name": name,
        "agent_instance_id": agent_instance_id,
        "prior": prior,
    }


def _require_inserted(result: object) -> bool:
    """Bool ``inserted`` from a completed DO-NOTHING upsert; fail loud otherwise."""
    data = require_completed(result, "upsert role_binding")
    inner = data.get("result")
    inserted = inner.get("inserted") if isinstance(inner, dict) else None
    if not isinstance(inserted, bool):
        raise StateOperationError(
            "state upsert role_binding returned no bool 'inserted' "
            f"(got {inserted!r})",
        )
    return inserted


def _read_binding_row(
    state: StateManagementInterface, external_id: str,
) -> dict[str, object] | None:
    """Read the live ``role_binding`` row (``is_deleted=0``) or ``None`` if vacant.

    Uses ``require_records`` so a NON-completed query (a genuine provider fault)
    raises ``StateOperationError`` and propagates — only a completed-but-empty
    query maps to ``None`` (vacant). This is the disambiguation the §5.1 loop
    depends on: vacant → retry the INSERT; fault → fail loud.
    """
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_ROLE_BINDING,
            "filters": {_COL_EXTERNAL_ID: external_id, _COL_IS_DELETED: 0},
        },
    )
    records = require_records(result)
    return records[0] if records else None


def _row_epoch(row: dict[str, object]) -> int:
    """The current ``claim_epoch`` of a binding row (0 if unset/non-int)."""
    epoch = row.get(COL_CLAIM_EPOCH)
    return epoch if isinstance(epoch, int) else 0


def _cas_write(
    state: StateManagementInterface,
    *,
    external_id: str,
    expected_epoch: int,
    updates: dict[str, object],
) -> int:
    """Predicated CAS: SET ``updates`` WHERE external_id + claim_epoch + is_deleted=0.

    Returns rows-affected — ``1`` = won (this session held the expected epoch),
    ``0`` = another session moved the epoch under us (caller re-reads + retries).
    The ``is_deleted=0`` predicate never matches a tombstone, but the no-tombstone
    invariant (a ``role_binding`` row is hard-deleted on release — the v4 release
    wrapper lands in slice-D) means one never exists to begin with.
    """
    result = state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_ROLE_BINDING,
            "filters": {
                _COL_EXTERNAL_ID: external_id,
                COL_CLAIM_EPOCH: expected_epoch,
                _COL_IS_DELETED: 0,
            },
        },
        updates,
    )
    return require_updated(result)


def _prior_from_row(name: str, row: dict[str, object]) -> ResolvedRole:
    """A LENIENT ResolvedRole over a displaced holder's row for §5.4 notify.

    Routing keys (``agent_session_id`` / ``agent_instance_id``) come straight off
    row COLUMNS so they survive even a malformed ``holder_identity``; ``agent_id``
    / ``session_label`` are best-effort off the identity JSON. A malformed prior
    must never block a legitimate displace (the write path stays robust — the bad
    row is a pre-existing data problem, not the claimant's fault).
    """
    try:
        identity = _coerce_identity(name, row.get(COL_HOLDER_IDENTITY))
    except RoleBindingMalformedError:
        identity = {}
    return ResolvedRole(
        name=name,
        agent_id=str(identity.get("agent_id") or ""),
        agent_instance_id=str(row.get(COL_AGENT_INSTANCE_ID) or ""),
        session_label=str(identity.get("session_label") or ""),
        holder_kind=str(row.get(COL_HOLDER_KIND) or HOLDER_KIND_SESSION),
        agent_session_id=str(row.get(COL_AGENT_SESSION_ID) or ""),
        holder_identity=identity,
    )


def _attempt_claim_existing(
    state: StateManagementInterface,
    *,
    name: str,
    row: dict[str, object],
    claim: HolderClaim,
) -> dict[str, object] | None:
    """One CAS attempt against an EXISTING live row; ``None`` = lost, caller retries.

    A self-re-claim (the live holder's ``agent_session_id`` equals the claimant's)
    is an IDEMPOTENT refresh — re-point the instance/label in place, NO epoch bump,
    NO handover (finding REL-07(2): kills the self-notify noise). Any other holder
    is a displace — CAS bumps ``claim_epoch`` E→E+1 and returns the PRIOR holder
    (captured this iteration, tied to epoch E) so the caller notifies exactly the
    session it displaced.
    """
    epoch = _row_epoch(row)
    is_self = bool(claim.agent_session_id) and (
        str(row.get(COL_AGENT_SESSION_ID) or "") == claim.agent_session_id
    )
    new_epoch = epoch if is_self else epoch + 1
    if _cas_write(
        state,
        external_id=role_binding_external_id(name),
        expected_epoch=epoch,
        updates=_holder_fields(claim, claim_epoch=new_epoch),
    ) != 1:
        return None
    if is_self:
        return _outcome("refreshed", name, claim.agent_instance_id, None)
    return _outcome("displaced", name, claim.agent_instance_id, _prior_from_row(name, row))


def claim_role_binding_v4(
    state: StateManagementInterface, *, name: str, claim: HolderClaim,
) -> dict[str, object]:
    """Race-safe claim/displace of the v4 ``role_binding`` row (§5.1).

    First-claim uses ``upsert_state(on_conflict='do_nothing')`` as the race
    primitive: the ``external_id`` UNIQUE constraint means exactly one
    concurrent first-claim reports ``inserted=True``. A conflict reports the
    completed, non-error ``inserted=False`` shape, so the loser RE-READS to
    disambiguate without logging an expected ``UniqueViolation`` traceback. A
    live holder → displace via a predicated CAS on ``claim_epoch``; a
    self-re-claim (holder's ``agent_session_id`` == the claimant's) is an
    idempotent refresh, never a displace. ``rows_affected==0`` on the CAS means
    another session moved the epoch under us → re-read + retry (bounded + loud).

    NO-TOMBSTONE INVARIANT (load-bearing): a released ``role_binding`` row MUST be
    HARD-deleted (``soft_delete=False``). The v4 release wrapper that enforces this
    on ``role_binding`` lands in slice-D (the legacy ``release_role_binding`` today
    hard-deletes only the ``agent_role_binding`` table — the same discipline,
    established there). The INSERT race primitive REQUIRES it — the ``external_id``
    UNIQUE index IGNORES ``is_deleted``, so a soft-delete tombstone would make the
    upsert conflict on a DEAD row while both the ``is_deleted=0`` re-read AND
    the ``is_deleted=0`` CAS hide it → a permanently deadlocked slot. The
    migration also filters ``is_deleted=1`` (never copy tombstones from
    ``agent_role_binding``).

    Returns ``_outcome(...)``: ``action`` is ``'claimed'`` (fresh INSERT),
    ``'displaced'`` (took it from another session — ``prior`` set), or
    ``'refreshed'`` (idempotent self-re-claim — ``prior`` None). Raises
    :class:`RoleClaimContendedError` on bounded-loop exhaustion (with the last
    contention detail); a genuine upsert or query fault propagates immediately as
    ``StateOperationError``.

    This is the live claim authority after the §9 cutover.
    """
    external_id = role_binding_external_id(name)
    last_detail = "upsert conflict followed by a missing or concurrently moved row"
    for _ in range(_CLAIM_CAS_MAX_ATTEMPTS):
        insert_result = state.upsert_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_ROLE_BINDING,
                "record": _binding_record(name, claim),
                "conflict_columns": [_COL_EXTERNAL_ID],
                "on_conflict": "do_nothing",
            },
        )
        if _require_inserted(insert_result):
            return _outcome("claimed", name, claim.agent_instance_id, None)
        row = _read_binding_row(state, external_id)
        if row is None:
            continue
        outcome = _attempt_claim_existing(state, name=name, row=row, claim=claim)
        if outcome is not None:
            return outcome
    raise RoleClaimContendedError(name, _CLAIM_CAS_MAX_ATTEMPTS, last_detail)


def holds_role(
    state: StateManagementInterface, name: str, agent_session_id: str,
) -> bool:
    """§5.0 act-time ownership re-check: does ``agent_session_id`` STILL hold ``name``?

    Resolution answers "who holds this role NOW", but a routing decision made at
    resolve time can be stale by ACT time (another session displaced the holder in
    between). The platform guarantee is that a holder RE-CHECKS ownership at the
    moment it acts, comparing the live binding's stable ``agent_session_id`` to
    its own. An empty session id is never an identity, so it never matches; a
    vacant role → ``False`` (nobody holds it, so neither do I). A malformed
    binding row is NOT swallowed — it propagates :class:`RoleBindingMalformedError`
    so a real data fault surfaces rather than silently reading as "not held".
    """
    if not agent_session_id:
        return False
    try:
        resolved = resolve_role_binding_v4(state, name)
    except RoleBindingVacantError:
        return False
    return resolved.agent_session_id == agent_session_id


def upsert_role_entity(
    state: StateManagementInterface,
    *,
    name: str,
    origin: str = ROLE_ORIGIN_USER,
    description: str = "",
    properties: dict[str, object] | None = None,
    memory_id: str = "",
) -> None:
    """Idempotent upsert of the first-class ``role`` ENTITY (§4.1, §5.5).

    Entity-first (§5.5): the claim upserts this BEFORE the binding-CAS, so a lost
    binding-CAS leaves at most a harmless orphan entity (resolve never reads it,
    §4.3). Keyed on ``external_id="role:{name}"`` (one row per role name). The
    entity holds the extensible/discoverable identity; ``memory_id`` links the
    §7 ingest (empty until ingested — best-effort, never gates the claim).
    """
    record: dict[str, object] = {
        _COL_EXTERNAL_ID: role_binding_external_id(name),
        COL_ROLE: name,
        COL_ORIGIN: origin,
        COL_DESCRIPTION: description,
        COL_PROPERTIES: json.dumps(properties or {}),
    }
    if memory_id:
        record[COL_MEMORY_ID] = memory_id
    require_completed(
        state.upsert_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_ROLE,
                "record": record,
                "conflict_columns": [_COL_EXTERNAL_ID],
            },
        ),
        "upsert role entity",
    )


class RoleClassConflictError(Exception):
    """A governance act tried to legislate ``name`` with a role_class DIFFERENT
    from the one already on record — never silently reassigned.

    §3.1 Q1 corollary: class-assignment is a governance act, never an ambient
    side-effect, so a conflicting re-run is a genuine policy refusal (the same
    posture ``session_lifecycle_verbs._validate_spawn_role``'s
    ``role_class_conflict`` already takes at spawn time), not idempotency.
    """

    def __init__(self, name: str, existing_class: str, requested_class: str) -> None:
        self.name = name
        self.existing_class = existing_class
        self.requested_class = requested_class
        super().__init__(
            f"role_class_conflict: role {name!r} is already legislated "
            f"role_class={existing_class!r}, not the requested {requested_class!r}.",
        )


def _read_role_entity_row(
    state: StateManagementInterface, external_id: str,
) -> dict[str, object] | None:
    records = require_records(
        state.query_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_ROLE, "filters": {_COL_EXTERNAL_ID: external_id}},
        ),
    )
    return records[0] if records else None


def legislate_role_class(
    state: StateManagementInterface,
    *,
    name: str,
    role_class: str,
    directed_by: str,
    brief_ref: str,
) -> str:
    """Governance-act creation of the ``role`` entity row with ``role_class``
    stamped AT BIRTH — the ONE sanctioned path that assigns an authority-
    carrying class (§3.1 Q1: claim-time is enforce-by-class, never
    class-assignment; ``upsert_role_entity``'s record deliberately excludes
    ``role_class`` so ordinary claim traffic structurally cannot touch it —
    the only other writer is ``role_class_backfill.py``'s one-shot, same-shaped
    targeted ``update_state``).

    Race-safe first-write: an INSERT race primitive
    (``on_conflict='do_nothing'``, the same device ``claim_role_binding_v4``
    uses for its first-claim). A lost race re-reads the live row: the SAME
    class already on record is an idempotent no-op re-run
    (``'already_legislated'``); a DIFFERENT class raises
    :class:`RoleClassConflictError` rather than silently reassigning it.

    ``directed_by`` / ``brief_ref`` are best-effort provenance on the row's
    ``description``/``properties`` (NOT the durable audit trail — those two
    columns are overwritten by every subsequent ordinary claim's
    ``upsert_role_entity(state, name=name)`` call, which is pre-existing D1
    behavior, out of this act's scope to change); the loud log line below and
    the platform's own action/session-ledger record of this call are the
    actual audit trail (the settled ruling: "audit is the record").

    Returns ``'legislated'`` (fresh) or ``'already_legislated'`` (idempotent
    re-run).
    """
    external_id = role_binding_external_id(name)
    record = {
        _COL_EXTERNAL_ID: external_id,
        COL_ROLE: name,
        COL_ROLE_CLASS: role_class,
        COL_ORIGIN: ROLE_ORIGIN_USER,
        COL_DESCRIPTION: f"legislated role_class={role_class}",
        COL_PROPERTIES: json.dumps(
            {"directed_by": directed_by, "brief_ref": brief_ref, "legislated_at": _now_iso()},
        ),
    }
    insert_result = require_completed(
        state.upsert_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_ROLE,
                "record": record,
                "conflict_columns": [_COL_EXTERNAL_ID],
                "on_conflict": "do_nothing",
            },
        ),
        "legislate role entity",
    )
    inner = insert_result.get("result")
    inserted = bool(inner.get("inserted")) if isinstance(inner, dict) else False
    if inserted:
        logger.warning(
            "GOVERNANCE ACT: role %r legislated role_class=%r directed_by=%r "
            "brief_ref=%r", name, role_class, directed_by, brief_ref,
        )
        return "legislated"
    existing = _read_role_entity_row(state, external_id)
    existing_class = str((existing or {}).get(COL_ROLE_CLASS) or "")
    if existing_class == role_class:
        logger.info(
            "GOVERNANCE ACT: role %r already legislated role_class=%r — "
            "idempotent re-run, directed_by=%r brief_ref=%r",
            name, role_class, directed_by, brief_ref,
        )
        return "already_legislated"
    raise RoleClassConflictError(name, existing_class, role_class)


def ingest_role_entity(
    memory_service: object | None,
    *,
    name: str,
    origin: str = ROLE_ORIGIN_USER,
    description: str = "",
    properties: dict[str, object] | None = None,
) -> str | None:
    """Best-effort memory ingest of the role entity (§7) — the ONE borrowed
    address-book trait (discoverability), so ``recall``/search answers
    "who or what is ``<role>``".

    NEVER gates a claim (P8): a missing service, a disabled toggle, or a
    ``remember`` fault returns ``None`` (the caller leaves ``memory_id`` empty and
    the binding stays authoritative). Off the resolve hot path (§4.3). ``name`` is
    an OPAQUE, operator-defined string — never enumerated/special-cased.
    """
    if memory_service is None:
        return None
    remember = getattr(memory_service, "remember", None)
    if not callable(remember):
        return None
    props = json.dumps(properties or {})
    content = f"role {name!r} (origin={origin}). {description}".strip()
    try:
        result = remember(
            content=f"{content} properties={props}",
            tags=["role", f"origin:{origin}", f"role:{name}"],
        )
    except Exception:  # noqa: BLE001 — ingest is best-effort; a fault must never gate the claim
        return None
    if isinstance(result, dict):
        memory_id = result.get("memory_id")
        return memory_id if isinstance(memory_id, str) and memory_id else None
    return None


def refresh_role_binding_cas(
    state: StateManagementInterface,
    *,
    agent_session_id: str,
    new_agent_instance_id: str,
) -> int:
    """Reconnect self-refresh — re-point every role this session holds in one CAS.

    Filtered on the stable ``agent_session_id`` ALONE (no role name): on
    reconnect a bridge's ``agent_instance_id`` rotates but its
    ``agent_session_id`` is stable, so a single predicated ``update_state``
    re-points EVERY binding that session holds to the new instance id. Returns
    the rows-affected count (``0`` = this session holds no binding → the caller
    falls through to an explicit claim). An empty / sentinel session id is NOT
    an identity — fail closed (return ``0`` without touching the table) so a
    no-carrier session can never CAS-match a real or backfilled binding.
    """
    if not agent_session_id or agent_session_id == UNCLAIMED_SESSION_ID:
        return 0
    result = state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            # §9 CUTOVER: reconnect self-refresh CASes the v4 table (readers + writer
            # move together). Filter on the stable agent_session_id; re-point ONLY the
            # rotated agent_instance_id (+ claimed_at). session_label is NOT a v4 column
            # — it lives in holder_identity JSON (§4.6) and does not rotate on reconnect,
            # so there is nothing to re-point. A provider holder has no session id so it
            # never matches — no-op, correct.
            "table": TABLE_ROLE_BINDING,
            "filters": {COL_AGENT_SESSION_ID: agent_session_id},
        },
        {
            COL_AGENT_INSTANCE_ID: new_agent_instance_id,
            COL_CLAIMED_AT: _now_iso(),
        },
    )
    return require_updated(result)


def release_role_binding(
    state: StateManagementInterface, name: str,
) -> dict[str, object]:
    """HARD-delete the single ``role:{name}`` binding (``peer_release_role``).

    ``soft_delete=False`` is REQUIRED (Codex BLOCKER-4): ``delete_records``
    defaults to a SOFT delete (``is_deleted=1``), and ``query_state`` does not
    exclude soft-deleted rows, so a soft release would still RESOLVE + enumerate
    — i.e. not release at all. The one-shot backfill marker (Control #2 B5) then
    prevents a released role from being re-seeded on a later boot.
    """
    require_completed(
        state.delete_records(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_AGENT_ROLE_BINDING,
                "filters": {_COL_EXTERNAL_ID: role_binding_external_id(name)},
                "soft_delete": False,
            },
        ),
        "delete agent_role_binding",
    )
    return {"released": True, "name": name}


def release_role_binding_v4(
    state: StateManagementInterface, name: str,
) -> dict[str, object]:
    """§9/§6.1 v4 release — HARD-delete the single ``role:{name}`` ``role_binding`` row.

    ``soft_delete=False`` is REQUIRED (carry-forward (a) / the no-tombstone invariant
    §5.1): the ``external_id`` UNIQUE index IGNORES ``is_deleted``, so a soft-delete
    tombstone would deadlock the slot (the INSERT conflicts on the dead row while the
    ``is_deleted=0`` re-read AND CAS both hide it). Mirrors the legacy
    ``release_role_binding`` but on ``TABLE_ROLE_BINDING`` — the §9 cutover repoints
    ``peer_release_role`` here.
    """
    require_completed(
        state.delete_records(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_ROLE_BINDING,
                "filters": {_COL_EXTERNAL_ID: role_binding_external_id(name)},
                "soft_delete": False,
            },
        ),
        "delete role_binding (v4)",
    )
    return {"released": True, "name": name}


def session_claim_requires_session_id(holder_kind: str, agent_session_id: str) -> bool:
    """carry-forward (c) (§4.5.3/§11): does a durable claim lack its required session id?

    A ``holder_kind='session'`` claim MUST carry a non-empty ``agent_session_id`` — the
    reconnect CAS (§5.2) and the auto-assignment succession (§D.9) both key on it. The
    pre-cutover 'no worse than pre-fix' fallback (empty allowed) DIES at the §9 cutover.
    Returns ``True`` when the claim must be REJECTED; ``peer_claim_role`` wires this at
    the flip (Phase 2).
    """
    return holder_kind == HOLDER_KIND_SESSION and not agent_session_id


def _migrated_binding_record(name: str, legacy_row: dict[str, object]) -> dict[str, object]:
    """A v4 ``role_binding`` INSERT record built from a legacy ``agent_role_binding`` row.

    Every migrated holder is ``holder_kind='session'`` (legacy rows are all sessions);
    ``holder_identity`` carries the session's ``{agent_id, session_label}`` (§4.6), the
    two identity columns are copied verbatim, and ``claim_epoch`` starts at 0.
    """
    agent_id = str(legacy_row.get(COL_AGENT_ID) or "")
    session_label = str(legacy_row.get(COL_SESSION_LABEL) or "")
    return {
        _COL_EXTERNAL_ID: role_binding_external_id(name),
        COL_ROLE: name,
        COL_HOLDER_KIND: HOLDER_KIND_SESSION,
        COL_HOLDER_IDENTITY: {"agent_id": agent_id, "session_label": session_label},
        COL_AGENT_INSTANCE_ID: str(legacy_row.get(COL_AGENT_INSTANCE_ID) or ""),
        COL_AGENT_SESSION_ID: str(legacy_row.get(COL_AGENT_SESSION_ID) or ""),
        COL_CLAIM_EPOCH: 0,
        COL_CLAIMED_AT: str(legacy_row.get(COL_CLAIMED_AT) or "") or _now_iso(),
    }


def _migrate_role_entity(
    state: StateManagementInterface, name: str, memory_service: object | None,
) -> None:
    """Upsert the ``role`` entity for a migrated name + best-effort memory ingest (§7)."""
    memory_id = ingest_role_entity(memory_service, name=name) or ""
    upsert_role_entity(state, name=name, memory_id=memory_id)


def _legacy_row_is_migratable(row: dict[str, object]) -> bool:
    """A legacy row yields a PARSE-VALID v4 session binding iff it has a non-empty
    ``agent_id`` (§4.6 requires it for a ``holder_kind='session'`` holder). A row
    without one would throw ``RoleBindingMalformedError`` on the live v4 hot path
    post-cutover, so it is SKIPPED at migrate — fail-loud belongs HERE, never at live
    resolve. The role becomes vacant/re-claimable rather than a live-routing throw.
    Migrate skips it (loud) + parity EXCLUDES it (same predicate), so a malformed row
    never gates the cutover nor reaches routing.
    """
    return bool(str(row.get(COL_AGENT_ID) or ""))


def migrate_agent_role_binding_to_v4(
    state: StateManagementInterface, memory_service: object | None = None,
) -> dict[str, object]:
    """§9.3 idempotent migration copy: ``agent_role_binding`` → ``role`` + ``role_binding``.

    Reads every NON-deleted legacy row (``is_deleted=0`` — carry-forward (b): a
    soft-deleted tombstone is NEVER copied, or the v4 no-tombstone invariant (§5.1)
    is violated at birth). For each: upsert the first-class ``role`` entity (§5.5,
    best-effort memory ingest — never gates) + INSERT the ``role_binding`` row via
    ``write_state``. The INSERT is idempotent = ``ON CONFLICT DO NOTHING`` — a
    ``write_state`` UNIQUE conflict on ``external_id`` means the row is already
    migrated → SKIP, so the copy is re-runnable at every green readiness. Runs BEFORE
    the reader/writer cutover (§9 step 5). No raw SQL.

    A non-completed write is DISAMBIGUATED by re-reading the v4 row: present →
    ``skipped_conflict`` (idempotent, expected on re-run); absent → ``skipped_rejected``
    (a genuine write fault — schema drift / constraint — logged LOUD, and parity will
    gate the cutover). Returns
    ``{source_rows, copied, skipped_conflict, skipped_rejected, skipped_malformed}``.
    """
    rows = require_records(
        state.query_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_AGENT_ROLE_BINDING, "filters": {_COL_IS_DELETED: 0}},
        ),
    )
    copied = 0
    skipped_conflict = 0
    skipped_rejected = 0
    skipped_malformed = 0
    for row in rows:
        name = str(row.get(COL_ROLE) or "")
        if not name:
            continue
        if not _legacy_row_is_migratable(row):
            skipped_malformed += 1
            logger.warning(
                "slice-D migration: SKIP malformed legacy role %r (empty agent_id — "
                "not a valid v4 session holder; it becomes vacant/re-claimable rather "
                "than a live-routing throw).",
                name,
            )
            continue
        _migrate_role_entity(state, name, memory_service)
        insert_result = state.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_ROLE_BINDING, "record": _migrated_binding_record(name, row)},
        )
        if is_completed(insert_result):
            copied += 1
        elif _read_binding_row(state, role_binding_external_id(name)) is not None:
            # A non-completed write whose v4 row EXISTS is an idempotent UNIQUE
            # conflict on external_id (already migrated) — the re-runnable path
            # (Control #2). Expected on every re-run at green readiness.
            skipped_conflict += 1
        else:
            # A non-completed write with NO v4 row is a genuine write REJECTION
            # (schema drift / a constraint) — NOT idempotency. Parity will fail this
            # row and gate the cutover; log LOUD so the next live failure is
            # diagnosable from a name + the raw result, never silently bucketed.
            skipped_rejected += 1
            logger.error(
                "slice-D migration: role %r REJECTED by the role_binding write and no "
                "v4 row exists — a genuine write fault (schema drift / constraint), NOT "
                "an idempotent conflict. Result: %s",
                name,
                insert_result,
            )
    return {
        "source_rows": len(rows),
        "copied": copied,
        "skipped_conflict": skipped_conflict,
        "skipped_rejected": skipped_rejected,
        "skipped_malformed": skipped_malformed,
    }


def verify_migration_parity(state: StateManagementInterface) -> dict[str, object]:
    """§9.4 ``external_id`` parity proof — GATES the cutover (a mismatch fails it loud).

    Every NON-deleted legacy ``external_id`` must have a matching ``role_binding`` row.
    The v4 table legitimately holds MORE rows (system slots born in v4 + explicit claims
    during the copy window), so parity is a SUBSET check (legacy ⊆ v4), NOT
    count-equality. ``ok=False`` means a legacy binding is GENUINELY absent from v4 (not
    mere agi-staleness, which self-heals §9.1) → the caller MUST fail the cutover loud.
    Returns ``{ok, legacy_count, v4_count, missing}``.
    """
    legacy_ids = {
        str(r.get(_COL_EXTERNAL_ID) or "")
        for r in require_records(
            state.query_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {"table": TABLE_AGENT_ROLE_BINDING, "filters": {_COL_IS_DELETED: 0}},
            ),
        )
        if _legacy_row_is_migratable(r)
    }
    v4_ids = {
        str(r.get(_COL_EXTERNAL_ID) or "")
        for r in require_records(
            state.query_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {"table": TABLE_ROLE_BINDING, "filters": {_COL_IS_DELETED: 0}},
            ),
        )
    }
    missing = sorted(legacy_ids - v4_ids)
    return {
        "ok": not missing,
        "legacy_count": len(legacy_ids),
        "v4_count": len(v4_ids),
        "missing": missing,
    }


# Dedicated one-shot marker for the §9 cutover migration — NOT the backfill's key
# (a marker over an incomplete migration is worse than none; kept separate).
_MIGRATION_MARKER_KEY = "slice_d_cutover_migration_done"


class CutoverParityError(RuntimeError):
    """The §9 cutover parity proof FAILED — a legacy binding is absent from v4.

    Raised from the readiness gate so green REFUSES to serve (blue keeps serving) —
    the safe failure mode is 'green won't come up', never 'green serves a
    half-migrated v4 table'. Recovery = re-run the idempotent migrate until parity
    passes, then the flip proceeds.
    """

    def __init__(self, parity: dict[str, object], migrate_result: dict[str, object]) -> None:
        self.parity = parity
        super().__init__(
            f"§9 cutover parity FAILED — legacy bindings absent from v4: "
            f"{parity.get('missing')} (migrate={migrate_result}). Re-run the "
            f"idempotent migrate until parity passes.",
        )


def _cutover_migration_done(state: StateManagementInterface) -> bool:
    """True once the one-shot cutover-migration marker was durably set (post-parity)."""
    data = require_completed(
        state.get_key_value(AGENT_ROLE_BINDING_NAMESPACE, _MIGRATION_MARKER_KEY),
        "get cutover migration marker",
    )
    return bool(data.get("found"))


def run_cutover_migration_at_readiness(
    state: StateManagementInterface, memory_service: object | None = None,
) -> dict[str, object]:
    """§9 one-shot cutover migration for GREEN READINESS — a PRE-SERVE HARD GATE.

    Marker-gated (dedicated key): a prior successful run returns ``already_done``
    without re-copying. Otherwise: idempotent copy (malformed rows skipped+logged,
    tombstones filtered) → ``external_id`` PARITY PROOF → a parity FAILURE raises
    :class:`CutoverParityError` so the readiness hook aborts (green never serves,
    blue keeps serving; re-run converges). The marker is set ONLY after parity
    passes — the migrate→parity→[re-run]→flip loop IS the §9 explicit-claim
    quiesce-equivalent (a claim racing the copy window → parity fails → re-run).
    Returns ``{status, migrate, parity}``.
    """
    if _cutover_migration_done(state):
        return {"status": "already_done"}
    migrate_result = migrate_agent_role_binding_to_v4(state, memory_service)
    parity = verify_migration_parity(state)
    if not parity.get("ok"):
        raise CutoverParityError(parity, migrate_result)
    require_completed(
        state.set_key_value(AGENT_ROLE_BINDING_NAMESPACE, _MIGRATION_MARKER_KEY, "done"),
        "set cutover migration marker",
    )
    return {"status": "completed", "migrate": migrate_result, "parity": parity}


def _now_iso() -> str:
    """ISO-8601 UTC ``claimed_at`` timestamp."""
    return datetime.now(UTC).isoformat()


__all__ = [
    "UNCLAIMED_SESSION_ID",
    "CutoverParityError",
    "HolderClaim",
    "ResolvedRole",
    "RoleBindingMalformedError",
    "RoleBindingVacantError",
    "RoleClaimContendedError",
    "RoleClassConflictError",
    "claim_role_binding_v4",
    "holds_role",
    "ingest_role_entity",
    "list_roles_for_agent_instance",
    "migrate_agent_role_binding_to_v4",
    "refresh_role_binding_cas",
    "release_role_binding",
    "release_role_binding_v4",
    "resolve_role_binding",
    "resolve_role_binding_v4",
    "run_cutover_migration_at_readiness",
    "legislate_role_class",
    "session_claim_requires_session_id",
    "upsert_role_entity",
    "verify_migration_parity",
]
