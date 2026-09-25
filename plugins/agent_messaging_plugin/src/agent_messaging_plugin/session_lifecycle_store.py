"""Fleet session-management Phase B, D1 (§3.2/§3.3, AMEND 2a/2b) — the
``managed_session`` lifecycle ledger + its append-only ``session_transition``
audit trail. The state-layer primitives the L1 verb surface (§4) is a thin
wrapper over.

TWO GUARDS, TWO FAILURE MODES (do not conflate them — this is the whole
point of the split):

* An ILLEGAL edge (a caller asking for a transition the §3.2 matrix does not
  allow, e.g. ``live`` straight to ``retired``) is a Python-level check
  against :data:`schema.LIFECYCLE_TRANSITIONS`, BEFORE any state write. It
  raises :class:`IllegalLifecycleTransitionError` — a caller bug, never
  retried.
* A LEGAL edge that LOSES A RACE (the sweep and the steward both moving the
  same row) is caught by the predicated ``update_state`` — ``rows_affected
  == 0`` means another writer moved the row first. It raises
  :class:`StaleLifecycleStateError` — the AMEND-2b error token
  ``stale_lifecycle_state``, a race outcome, not a caller bug.

If both checks fed the same error token, a test driving an illegal edge
would never exercise the CAS predicate at all — the exact
``reference_a_second_guard_makes_the_first_guards_legs_vacuous`` trap. Each
guard owns its own token; each has its own test.

ORDERING (ledger write BEFORE audit insert, never the reverse): the
predicated ``managed_session.lifecycle_state`` write happens FIRST; the
``session_transition`` audit row is inserted ONLY once that write reports
``rows_affected == 1``. Inserting the audit row first and losing the ledger
race after would record a transition that never happened — the audit trail
lying is worse than a crash between the two steps losing one audit row for a
transition that DID happen (a lost race here still leaves the transition
undocumented for THAT window, but never invents one).

``directed_by`` is the server-built ``CallContext`` principal
(``state.get("call_context")`` in every ``@platform_process`` method) — a
COARSE authorization discriminator (``operator`` / ``operator_equivalent`` /
``external`` / ``plugin``), NOT the spawning session's identity. It answers
"who was authorized to direct this," never "which peer requested it" — that
provenance lives in ``managed_session.spawned_by_instance_id`` /
``spawned_by_role`` (lineage columns, §7). Do not conflate the two.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import (
    require_completed,
    require_records,
    require_updated,
)
from ananta.services.state_service.bounded_read import (
    PagedReadError,
    ReadCeilingError,
    iter_table_rows,
)

from .schema import (
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_PARKED,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    LIFECYCLE_TRANSITIONS,
    SESSION_VISIBILITY_HEADLESS,
    TABLE_LANE_CHARTER,
    TABLE_MANAGED_DISPATCH,
    TABLE_MANAGED_SESSION,
    TABLE_SESSION_TRANSITION,
)
from .session_hosts import DEFAULT_AGENT_RUNTIME, OPERATOR_HOST, resolve_host_driver

if TYPE_CHECKING:
    from ananta.core.services.call_context import CallContext
    from ananta.interfaces.state_management_interface import StateManagementInterface

    from .bridge_sessions import BridgeSessionManager
    from .peer_registry import PeerRegistry

logger = logging.getLogger(__name__)

_COL_AGENT_INSTANCE_ID = "agent_instance_id"
_COL_IS_DELETED = "is_deleted"

#: Rows :func:`list_managed_sessions` will walk before refusing. NOT a claim that
#: the fleet ledger is small — it is one row per managed session ever spawned and
#: nothing prunes it, which is exactly how it crossed the 100-row cap (measured
#: 106 live rows, 2026-08-16). It is a claim about this call site: a fleet list
#: that has walked a million rows is not a list anyone can read, and the caller
#: wants a filter rather than a longer walk.
_MANAGED_SESSION_WALK_CEILING = 1_000_000

_MANAGED_SESSION_CEILING_REASON = (
    "one row per managed session ever spawned; the ledger is append-mostly and "
    "is not pruned (106 live rows measured 2026-08-16)."
)
_COL_LIFECYCLE_STATE = "lifecycle_state"

# These deliberately mirror bounded_read's ordered cursor contract. Managed
# sessions are lifecycle authority: an absent ``data.records`` envelope or a
# non-row member cannot be silently interpreted as proof that no active owner
# exists. Keep the strict decoder local rather than weakening the shared
# helper's compatibility contract for unrelated readers.
_MANAGED_SESSION_PAGE_ROWS = 100
_MANAGED_SESSION_ORDER_BY = [["created_at", "asc"], ["id", "asc"]]
_COL_CREATED_AT = "created_at"
_COL_ID = "id"

# Single source of truth (session_lifecycle_verbs.py's _rearm_report_by
# imports this rather than redefining it) — interim fixed window, pending
# the §6 rule 1 per-work_class config substrate (deferred to a later D-step).
DEFAULT_REPORT_BY_SECONDS = 300


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def format_directed_by(call_context: CallContext | None) -> str:
    """The server-built principal as an audit string — ``"kind:id"`` or
    ``"kind:plugin"``, whichever identifier the context actually carries.
    Empty when there is no context at all (never fabricated)."""
    if call_context is None:
        return ""
    ident = call_context.principal_id or call_context.calling_plugin or ""
    return f"{call_context.principal_kind}:{ident}" if ident else call_context.principal_kind


class SessionNotFoundError(Exception):
    """No ``managed_session`` row exists for the given ``agent_instance_id``."""

    def __init__(self, agent_instance_id: str) -> None:
        self.agent_instance_id = agent_instance_id
        super().__init__(f"session_not_found: no managed_session row for {agent_instance_id!r}")


class ManagedSessionFilterRequiredError(ValueError):
    """A default fleet-ledger read was attempted without a predicate."""


class IllegalLifecycleTransitionError(Exception):
    """The requested edge is not in the §3.2 transition matrix — a caller
    bug (asking for a transition that was never legal), never a race."""

    def __init__(self, from_state: str, to_state: str) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"illegal_lifecycle_transition: {from_state!r} -> {to_state!r} is not "
            "a legal edge in the §3.2 transition matrix.",
        )


class StaleLifecycleStateError(Exception):
    """The predicated write lost its race — another writer (sweep, steward,
    a concurrent verb call) already moved this row off ``from_state``. A
    legal edge, a lost race — never a caller bug, and never silently
    retried by this module (the CALLER decides whether to re-read + retry)."""

    def __init__(self, agent_instance_id: str, from_state: str, to_state: str) -> None:
        self.agent_instance_id = agent_instance_id
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"stale_lifecycle_state: {agent_instance_id!r} is no longer in "
            f"{from_state!r} (requested {from_state!r} -> {to_state!r}).",
        )


@dataclass(frozen=True, slots=True)
class ManagedSessionSpec:
    """The ``spawn_session`` ledger-row-before-host-dispatch record (§4)."""

    agent_instance_id: str
    lane_id: str
    brief_ref: str
    work_class: str
    budget_line: str
    host: str
    unit_id: str = ""
    dispatch_id: str = ""
    agent_runtime: str = DEFAULT_AGENT_RUNTIME
    spawned_by_instance_id: str = ""
    spawned_by_role: str = ""
    role_name: str = ""
    # W6 (#13 §44.3): the name the worker answers to on its own machine.
    # Empty means "derive it" — insert_managed_session falls back to
    # role_name then lane_id, the same order spawn_session resolves.
    local_name: str = ""
    # W4A item 3: an EXPLICIT operator choice to spawn onto a host whose
    # preflight found worker hooks unable to run. Default off.
    degraded_hooks_acknowledged: bool = False
    visibility: str = SESSION_VISIBILITY_HEADLESS
    model: str = ""
    effort: str = ""
    report_by_seconds: int = 0
    directed_by: str = ""
    provisioning_mode: str = "worktree"
    # The resolved source checkout used at spawn.  Retire reads this row, not
    # a live process environment, so a foreign-repository lane removes the
    # same worktree it provisioned.
    lane_repo_root: str = ""
    dispatch_kind: str = ""
    reviewed_report_vendor: str = ""
    pair_id: str = ""
    # Capability-floor audit trail (iss_63d91ca9): the caller's declared
    # scope_tags and every floor spawn_session actually applied (declared or
    # brief-detected), so "which floor governed this spawn's model" is a
    # ledger read, never a reconstruction from a commit trailer.
    scope_tags: tuple[str, ...] = ()
    capability_floors: list[dict[str, str]] = dataclasses.field(default_factory=list)
    difficulty_score: float = 0.0
    selection_receipt: dict[str, Any] = dataclasses.field(default_factory=dict)


def insert_managed_session(
    state: StateManagementInterface,
    spec: ManagedSessionSpec,
) -> dict[str, Any]:
    """Write the ``managed_session`` ledger row in ``spawning`` state BEFORE
    host dispatch (§4 — a half-failed spawn stays visible by construction).
    ``agent_instance_id`` is UNIQUE, so a caller retrying a failed dispatch
    with the SAME id would conflict here rather than double-insert; callers
    mint a fresh id per spawn attempt."""
    now = _now_iso()
    record: dict[str, Any] = {
        _COL_AGENT_INSTANCE_ID: spec.agent_instance_id,
        "lane_id": spec.lane_id,
        "brief_ref": spec.brief_ref,
        "unit_id": spec.unit_id,
        "work_class": spec.work_class,
        "budget_line": spec.budget_line,
        "host": spec.host,
        "dispatch_id": spec.dispatch_id,
        "agent_runtime": spec.agent_runtime,
        "spawned_by_instance_id": spec.spawned_by_instance_id,
        "spawned_by_role": spec.spawned_by_role,
        # W6: role_name was a ManagedSessionSpec field that this writer never
        # persisted and no caller ever passed — an inert knob. It is
        # load-bearing now (it is what the incumbent refusal names), so it is
        # written, and local_name is derived from it exactly as spawn_session
        # resolves it so a direct store caller cannot mint a different rule.
        "role_name": spec.role_name,
        "local_name": spec.local_name or spec.role_name or spec.lane_id,
        "degraded_hooks_acknowledged": spec.degraded_hooks_acknowledged,
        "visibility": spec.visibility,
        "model": spec.model,
        "effort": spec.effort,
        "capability_report": {},
        _COL_LIFECYCLE_STATE: LIFECYCLE_SPAWNING,
        # Empty until retirement attempts teardown.  The terminal outcome is
        # written atomically with terminated -> retired, never inferred from
        # the lifecycle state alone.
        "worktree_disposition": "",
        "last_transition_at": now,
        "directed_by": spec.directed_by,
        "provisioning_mode": spec.provisioning_mode,
        "lane_repo_root": spec.lane_repo_root,
        "dispatch_kind": spec.dispatch_kind,
        "reviewed_report_vendor": spec.reviewed_report_vendor,
        "pair_id": spec.pair_id,
        "scope_tags": list(spec.scope_tags),
        "capability_floors": list(spec.capability_floors),
        "difficulty_score": spec.difficulty_score,
        "selection_receipt": dict(spec.selection_receipt),
    }
    # Always persisted (even 0) — the WINDOW LENGTH itself, distinct from
    # "report_by" (the computed deadline below). This is what lets
    # _rearm_report_by (session_lifecycle_verbs.py) re-arm from the spawn's
    # OWN requested window instead of a hardcoded default on every later
    # report_alive/drive_session call.
    #
    # A4 Slice 0 (measured gap, not assumed): a caller-omitted
    # report_by_seconds (0) used to leave NON-operator rows with no
    # report_by at all until their first report_alive/drive_session call —
    # a spawn-to-first-report window invisible to sweep_overdue_sessions,
    # which reads report_by IS NULL as "no contract" (the correct read for
    # an operator row, which has none by design). Every non-operator host
    # now gets a contract from the moment the row is visible, defaulted to
    # the SAME fallback _rearm_report_by already uses, so the row's own
    # report_by_seconds stays self-describing for every later re-arm.
    effective_report_by_seconds = spec.report_by_seconds
    if not effective_report_by_seconds and spec.host != OPERATOR_HOST:
        effective_report_by_seconds = DEFAULT_REPORT_BY_SECONDS
    record["report_by_seconds"] = effective_report_by_seconds
    if effective_report_by_seconds:
        record["report_by"] = (
            datetime.now(UTC) + timedelta(seconds=effective_report_by_seconds)
        ).isoformat()
    require_completed(
        state.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MANAGED_SESSION, "record": record},
        ),
        "insert managed_session",
    )
    return record


def read_managed_session(
    state: StateManagementInterface,
    agent_instance_id: str,
) -> dict[str, Any]:
    """The live ``managed_session`` row, or :class:`SessionNotFoundError`."""
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_MANAGED_SESSION,
            "filters": {_COL_AGENT_INSTANCE_ID: agent_instance_id, _COL_IS_DELETED: 0},
        },
    )
    records = require_records(result)
    if not records:
        raise SessionNotFoundError(agent_instance_id)
    return records[0]


@dataclass(frozen=True, slots=True)
class LaneCharterSpec:
    """The seat-captured operator charter for a lane (fleet-watch-transport-
    migration phase 2 slice 6, design check-in ruling item 3) — driven
    byte-exact as a spawned worker's literal first turn."""

    lane_id: str
    charter_text: str
    captured_at: str
    brief_ref: str = ""
    directed_by: str = ""


def capture_lane_charter(
    state: StateManagementInterface,
    spec: LaneCharterSpec,
) -> dict[str, Any]:
    """Insert-only capture: ALWAYS writes a NEW ``lane_charter`` row, never
    updates a prior one — the exact ``session_transition`` append-only
    shape. A later charter for the same ``lane_id`` supersedes by recency
    (:func:`resolve_lane_charter` reads the latest row by ``captured_at``
    desc); there is no update path for ``charter_text`` anywhere in this
    codebase, which is what makes "the stored words are write-once" true
    rather than merely documented."""
    if not spec.lane_id:
        raise ValueError("capture_lane_charter requires a non-empty lane_id.")
    if not spec.charter_text:
        raise ValueError("capture_lane_charter requires non-empty charter_text.")
    record: dict[str, Any] = {
        "lane_id": spec.lane_id,
        "charter_text": spec.charter_text,
        "brief_ref": spec.brief_ref,
        "captured_at": spec.captured_at,
        "directed_by": spec.directed_by,
    }
    require_completed(
        state.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_LANE_CHARTER, "record": record},
        ),
        "insert lane_charter",
    )
    return record


@dataclass(frozen=True, slots=True)
class LaneCharterRecord:
    """The latest captured charter for a lane, as :func:`resolve_lane_charter`
    resolves it — widened beyond a bare ``charter_text`` string (2026-08-06,
    phase-3 charter-rider provenance framing) so a caller can build the
    provenance frame (captured_at + brief_ref) around the verbatim body
    without a second read."""

    charter_text: str
    captured_at: str
    brief_ref: str


def resolve_lane_charter(
    state: StateManagementInterface,
    lane_id: str,
) -> LaneCharterRecord | None:
    """The latest ``lane_charter`` row for ``lane_id``, or ``None`` if none
    is on file (an ordinary lane with no captured charter — never a fault).
    The ``(captured_at desc, id desc)`` order mirrors the platform's own
    latest-row precedent (``agent_messaging/repository.py``'s peer-thread
    lookup) — ``query_ordered``'s >=2-order-col contract forces a
    deterministic pick among same-instant captures rather than leaving the
    tie undefined."""
    if not lane_id:
        return None
    result = state.query_ordered(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_LANE_CHARTER,
            "filters": {"lane_id": lane_id},
            "order_by": [["captured_at", "desc"], ["id", "desc"]],
            "limit": 1,
        },
    )
    records = require_records(result)
    if not records:
        return None
    row = records[0]
    return LaneCharterRecord(
        charter_text=str(row.get("charter_text") or ""),
        captured_at=str(row.get("captured_at") or ""),
        brief_ref=str(row.get("brief_ref") or ""),
    )


def _iter_managed_session_rows(
    state: StateManagementInterface,
    *,
    filters: dict[str, Any],
    ceiling: int,
    reason: str,
) -> Iterator[dict[str, Any]]:
    """The one soft-delete-aware, complete managed-session keyset walk."""
    query_filters = dict(filters)
    requested_is_deleted = query_filters.pop(_COL_IS_DELETED, 0)
    include_deleted = requested_is_deleted != 0
    if include_deleted:
        query_filters[_COL_IS_DELETED] = requested_is_deleted
    after: list[object] | None = None
    seen = 0
    while True:
        query: dict[str, object] = {
            "table": TABLE_MANAGED_SESSION,
            "filters": query_filters,
            "order_by": _MANAGED_SESSION_ORDER_BY,
            "limit": _MANAGED_SESSION_PAGE_ROWS,
            "include_deleted": include_deleted,
        }
        if after is not None:
            query["after"] = after
        records = _complete_managed_session_page(
            state.query_ordered(AGENT_ROLE_BINDING_NAMESPACE, query),
        )
        if not records:
            return
        for record in records:
            seen += 1
            if seen > ceiling:
                raise ReadCeilingError(
                    f"paged walk of table {TABLE_MANAGED_SESSION!r} passed its declared ceiling "
                    f"of {ceiling} rows, so it was refused rather than run to completion. "
                    f"The ceiling was justified as: {reason}",
                )
            yield record
        if len(records) < _MANAGED_SESSION_PAGE_ROWS:
            return
        last = records[-1]
        if _COL_CREATED_AT not in last or _COL_ID not in last:
            raise PagedReadError(
                f"paged read of table {TABLE_MANAGED_SESSION!r}: a row is missing the cursor "
                f"columns {_COL_CREATED_AT!r}/{_COL_ID!r}, so the walk cannot advance "
                f"without risking skipped or repeated rows: {last!r}",
            )
        after = [last[_COL_CREATED_AT], last[_COL_ID]]


def _complete_managed_session_page(result: object) -> list[dict[str, Any]]:
    """Decode lifecycle authority pages without discarding incomplete evidence."""
    records = _managed_session_page_records(result)
    if not all(_managed_session_cursor_is_complete(record) for record in records):
        raise PagedReadError(
            f"paged read of table {TABLE_MANAGED_SESSION!r}: "
            f"a row has missing cursor values {_COL_CREATED_AT!r}/{_COL_ID!r}: {records!r}",
        )
    return records


def _managed_session_page_records(result: object) -> list[dict[str, Any]]:
    """Require the completed result envelope and its homogeneous row list."""
    if not isinstance(result, dict):
        raise PagedReadError(
            f"paged read of table {TABLE_MANAGED_SESSION!r}: "
            f"state result is not a dict: {result!r}",
        )
    if str(result.get("action_status", "")) != "completed":
        raise PagedReadError(
            f"paged read of table {TABLE_MANAGED_SESSION!r} did not complete "
            f"(action_status={result.get('action_status')!r}): {result!r}",
        )
    data = result.get("data")
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list):
        raise PagedReadError(
            f"paged read of table {TABLE_MANAGED_SESSION!r}: "
            f"data.records is not a list: {records!r}",
        )
    if not all(isinstance(record, dict) for record in records):
        raise PagedReadError(
            f"paged read of table {TABLE_MANAGED_SESSION!r}: "
            f"data.records contains a non-row: {records!r}",
        )
    return records


def _managed_session_cursor_is_complete(record: dict[str, Any]) -> bool:
    """A full page may advance only from durable, nonempty cursor columns."""
    return all(str(record.get(column) or "").strip() for column in (_COL_CREATED_AT, _COL_ID))


def iter_managed_sessions_unbounded(
    state: StateManagementInterface,
    *,
    reason: str,
) -> Iterator[dict[str, Any]]:
    """Deliberately walk the whole managed-session ledger.

    The name and required reason make full-ledger consent visible at every call
    site. ``_MANAGED_SESSION_WALK_CEILING`` remains a last-resort safety fuse;
    "unbounded" distinguishes this from the ordinary filter-required API, not
    from the repository-wide paged-read ceiling discipline.
    """
    if not reason.strip():
        raise ValueError("iter_managed_sessions_unbounded requires a non-empty reason")
    yield from _iter_managed_session_rows(
        state,
        filters={},
        ceiling=_MANAGED_SESSION_WALK_CEILING,
        reason=f"{reason} {_MANAGED_SESSION_CEILING_REASON}",
    )


def list_managed_sessions(
    state: StateManagementInterface,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Materialise managed-session rows matching a required predicate.

    Empty filters refuse. A caller that genuinely needs the entire append-mostly
    ledger must opt in through :func:`iter_managed_sessions_unbounded`, where the
    full walk and its reason are visible at the call site.

    The soft-delete override is preserved: ``is_deleted=1`` opts into deleted
    rows and applies that value explicitly, while the ordinary path relies on
    ``iter_table_rows``' default live-row predicate.
    """
    if not filters:
        raise ManagedSessionFilterRequiredError(
            "list_managed_sessions requires a non-empty filter; use "
            "iter_managed_sessions_unbounded with a reason for a deliberate full-ledger walk"
        )
    return list(
        _iter_managed_session_rows(
            state,
            filters=filters,
            ceiling=_MANAGED_SESSION_WALK_CEILING,
            reason=_MANAGED_SESSION_CEILING_REASON,
        )
    )


def list_managed_sessions_bounded(
    state: StateManagementInterface,
    filters: dict[str, Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Return at most ``limit`` matches, refusing when match ``limit + 1`` exists."""
    if not filters:
        raise ManagedSessionFilterRequiredError(
            "list_managed_sessions_bounded requires a non-empty filter"
        )
    return list(
        _iter_managed_session_rows(
            state,
            filters=filters,
            ceiling=limit,
            reason=(
                f"list_sessions has a hard result limit of {limit}; the caller must narrow "
                "its fleet predicate rather than receive a truncated roster."
            ),
        )
    )


def read_managed_sessions_page(
    state: StateManagementInterface,
    filters: dict[str, Any],
    *,
    limit: int,
    after: list[str] | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Read one tie-safe ascending managed-session page.

    ``managed_session`` is append-mostly, so an operator roster must page
    rather than make a claim about a maximum live population.  The cursor is
    the immutable ``(created_at, id)`` pair used by :func:`iter_table_rows`;
    keeping the id tie-break prevents same-timestamp rows from disappearing at
    a page boundary.
    """
    if not filters:
        raise ManagedSessionFilterRequiredError(
            "read_managed_sessions_page requires a non-empty filter"
        )
    query: dict[str, Any] = {
        "table": TABLE_MANAGED_SESSION,
        "filters": filters,
        "order_by": [["created_at", "asc"], ["id", "asc"]],
        # The extra row proves that a next page exists.  A caller may request
        # up to 250 rows, above the state service's default 100-row page cap,
        # so this explicit flag is consent to this bounded page, not a scan.
        "limit": limit + 1,
        "unbounded": True,
    }
    if after is not None:
        query["after"] = after
    rows = [
        dict(row)
        for row in require_records(
            state.query_ordered(AGENT_ROLE_BINDING_NAMESPACE, query)
        )
    ]
    return rows[:limit], len(rows) > limit


_SPAWN_AGENT_SESSION_ID_PREFIX = "ases-"
"""``tmux_adapter.py``/``headless_adapter.py`` both mint
``agent_session_id = f"ases-{agent_instance_id}"`` for every ``spawn_session``
lineage, exactly once, never re-derived (both adapters' own comments say so
verbatim). Confirmed by direct read of both construction sites (only two
spawn/host-driver paths in this repo) — spawn/registration-gaps findings,
2026-08-08, "the embedding IS guaranteed by construction, for spawn lineage
only." An operator-launched session's ``agent_session_id`` (e.g. the seat's
own ``ases-<epoch>-<pid>-<random>``, minted independently in ``~/.zshrc``)
can share the prefix but does not recover an existing spawn row. The
registration route treats that explicit no-match signal as the trigger for an
honest no-contract operator inventory row."""


def _recover_spawn_instance_id(agent_session_id: str) -> str | None:
    """The spawn-time ``agent_instance_id`` this ``agent_session_id`` embeds,
    or ``None`` if it doesn't have the spawn-lineage shape at all. A pure
    string-parse of a DETERMINISTIC, guaranteed-by-construction embedding
    (see :data:`_SPAWN_AGENT_SESSION_ID_PREFIX`) — recovers the exact
    original key, never a fuzzy or ambiguous match."""
    if not agent_session_id.startswith(_SPAWN_AGENT_SESSION_ID_PREFIX):
        return None
    recovered = agent_session_id[len(_SPAWN_AGENT_SESSION_ID_PREFIX) :]
    return recovered or None


def backfill_registration(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    agent_id: str,
    agent_session_id: str,
) -> bool:
    """The registration-hook fix (§3.2/§5, Dawn ruling arm-11511b07): a
    ``managed_session`` row spawned through ``spawn_session`` carries
    ``agent_session_id``/``agent_id`` as NULL until the spawned process
    actually registers with the platform — nothing previously wrote them.
    Call this from ``http_routes.peer_register_route`` right after
    ``peer_registry.register`` succeeds, for EVERY registration (not just the
    first): returns ``False`` when no ``managed_session`` row exists for
    either lookup key, so the registration route can create the separate
    no-contract operator inventory row. Otherwise returns ``True`` after an
    unconditional backfill of the identity columns (self-correcting across
    reconnects, mirroring the state-table self-refresh pattern this route
    already runs). The ``spawning -> live`` lifecycle edge fires ONLY the
    first time (guarded by the row still being ``spawning``); a later
    reconnect of an already-``live``/``idle`` session must never re-fire it
    or clobber a lifecycle_state a sweep/steward has since moved on.

    **Fallback reconciliation (spawn/registration-gaps fix, 2026-08-08,
    coordinator-seat ruling — guards below are conditions, not suggestions):** a
    ``watch``-hosted worker deliberately registers under a DIFFERENT id than
    its spawn-time ``agent_instance_id`` (``_resolve_watch_identity``,
    ``local_cli/cli.py`` — a real, separate requirement, REL-07 reconnect
    survival). When the primary lookup misses, recover the spawn-time id
    from ``agent_session_id`` (guaranteed-by-construction, see
    :func:`_recover_spawn_instance_id`) and retry — but ONLY as a fallback
    (guard 1: never the primary path), ONLY when the recovered row is STILL
    ``spawning`` (guard 2: a row that already backfilled belongs to a
    lineage that completed — a later registration must never re-key it),
    and FAILING LOUD rather than silently guessing when the recovered row
    exists but isn't still spawning (guard 3). The documented genuine no-op
    (no row under either id — an operator-launched session with no spawn
    lineage) stays exactly as quiet as before (guard 4) — recovery failing
    to resolve ANY row is indistinguishable from, and handled identically
    to, never having attempted recovery at all.
    """
    try:
        row = read_managed_session(state, agent_instance_id)
        matched_instance_id = agent_instance_id
    except SessionNotFoundError:
        recovered_id = _recover_spawn_instance_id(agent_session_id)
        if recovered_id is None:
            return False
        try:
            row = read_managed_session(state, recovered_id)
        except SessionNotFoundError:
            return False
        if str(row.get(_COL_LIFECYCLE_STATE) or "") != LIFECYCLE_SPAWNING:
            logger.warning(
                "backfill_registration: %s registered with agent_session_id %s, "
                "which recovers spawn id %s -- but that row is no longer "
                "'spawning' (lifecycle_state=%s). Refusing to re-key a "
                "completed lineage; row left untouched.",
                agent_instance_id,
                agent_session_id,
                recovered_id,
                row.get(_COL_LIFECYCLE_STATE),
            )
            return True
        matched_instance_id = recovered_id
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_MANAGED_SESSION,
            "filters": {_COL_AGENT_INSTANCE_ID: matched_instance_id, _COL_IS_DELETED: 0},
        },
        {"agent_session_id": agent_session_id, "agent_id": agent_id},
    )
    # W4A: registration is the exact event the watchdog was waiting for, so a
    # registration that arrives LATE clears the mark rather than leaving a row
    # that permanently reads as deaf. Cleared loudly, not silently: a worker
    # that registered late is a different story from one that never did, and
    # the next reader needs to be able to tell them apart.
    if row.get("registration_overdue_at"):
        logger.warning(
            "registration watchdog: %s registered LATE -- it was marked "
            "registration-overdue at %s (%s) and has now completed "
            "registration. Clearing the mark; the delay itself was real.",
            matched_instance_id,
            row.get("registration_overdue_at"),
            row.get("registration_overdue_reason"),
        )
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MANAGED_SESSION,
                "filters": {_COL_AGENT_INSTANCE_ID: matched_instance_id, _COL_IS_DELETED: 0},
            },
            {"registration_overdue_at": None, "registration_overdue_reason": ""},
        )
    if str(row.get(_COL_LIFECYCLE_STATE) or "") != LIFECYCLE_SPAWNING:
        return True
    try:
        transition_lifecycle_state(
            state,
            agent_instance_id=matched_instance_id,
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="registration_hook",
            reason="first registration after spawn",
        )
    except StaleLifecycleStateError:
        # Lost the race (e.g. the spawned process crashed and something else
        # already moved the row to 'terminated' before this registration
        # landed) -- the identity backfill above already happened; the
        # lifecycle edge is this function's second job, not its only one.
        logger.info(
            "backfill_registration: %s lost the spawning->live race (already "
            "moved on) -- identity columns still backfilled.",
            matched_instance_id,
        )
    return True


def set_host_ref(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    host_ref: str,
) -> None:
    """Persist the adapter's ``spawn()`` return value (§5) -- previously
    discarded except in the verb's own response dict, so ``session_status``
    could never show it and a later ``terminate``/``driver_channel`` call had
    no way to find the process it was supposed to act on."""
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_MANAGED_SESSION,
            "filters": {_COL_AGENT_INSTANCE_ID: agent_instance_id, _COL_IS_DELETED: 0},
        },
        {"host_ref": host_ref},
    )


def persist_first_turn_evidence(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    dispatch_id: str,
    source: str,
    delivered: bool,
    error: str,
    observed_at: datetime,
) -> None:
    """Persist the spawn result fields that were previously response-only."""
    updated = require_updated(
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MANAGED_SESSION,
                "filters": {
                    _COL_AGENT_INSTANCE_ID: agent_instance_id,
                    _COL_IS_DELETED: 0,
                },
            },
            {
                "dispatch_id": dispatch_id,
                "first_turn_source": source,
                "first_turn_delivered": delivered,
                "first_turn_error": error,
                "first_turn_at": observed_at.astimezone(UTC).isoformat(),
            },
        ),
    )
    if updated != 1:
        raise SessionNotFoundError(agent_instance_id)


def persist_session_liveness(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    liveness: str,
    detail: str,
    observed_at: datetime,
) -> None:
    """Persist alive/dead/unknown without treating probe faults as death."""
    if liveness not in {"alive", "dead", "unknown"}:
        raise ValueError(f"invalid host liveness {liveness!r}")
    updated = require_updated(
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MANAGED_SESSION,
                "filters": {
                    _COL_AGENT_INSTANCE_ID: agent_instance_id,
                    _COL_IS_DELETED: 0,
                },
            },
            {
                "host_liveness": liveness,
                "host_liveness_observed_at": observed_at.astimezone(UTC).isoformat(),
                "host_liveness_detail": detail,
                "last_reconciled_at": observed_at.astimezone(UTC).isoformat(),
            },
        ),
    )
    if updated != 1:
        raise SessionNotFoundError(agent_instance_id)


def mark_registration_overdue(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    reason: str,
    observed_at: datetime,
) -> None:
    """W4A: stamp the registration-overdue FIELDS on a still-``spawning`` row.

    Deliberately NOT a :func:`transition_lifecycle_state` call, and the
    reasoning is the whole design call — see
    :func:`session_sweep.sweep_unregistered_spawning_sessions`. The row is
    still genuinely in the spawn phase AND is now registration-overdue; those
    are two facts, and a lifecycle state can only hold one of them. A new
    state would also have to be WRITTEN by somebody, and the defining property
    of this failure is that nobody is home — so it would encode "the platform
    noticed", not a lifecycle fact about the session.

    Predicated on the row still being ``spawning`` so a registration or a
    terminate landing in the race window is never overwritten with a mark that
    is already false. Idempotent by the same predicate plus the null check in
    the sweep: the FIRST observation's timestamp is the one that survives, so
    the field answers "since when", not "as of the last tick".
    """
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_MANAGED_SESSION,
            "filters": {
                _COL_AGENT_INSTANCE_ID: agent_instance_id,
                _COL_IS_DELETED: 0,
                _COL_LIFECYCLE_STATE: LIFECYCLE_SPAWNING,
            },
        },
        {
            "registration_overdue_at": observed_at.isoformat(),
            "registration_overdue_reason": reason,
        },
    )


def transition_lifecycle_state(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    from_state: str,
    to_state: str,
    directed_by: str,
    reason: str = "",
    recorded_fields: Mapping[str, object] | None = None,
) -> None:
    """Predicated ``lifecycle_state`` write (§3.2 matrix) + the AMEND-2a
    append-only audit insert. Raises :class:`IllegalLifecycleTransitionError`
    (illegal edge, checked BEFORE any write) or
    :class:`StaleLifecycleStateError` (legal edge, lost the CAS race — the
    ledger write itself, not the audit insert, is what raises). A successful
    call has ALREADY inserted the audit row when it returns.
    """
    if to_state not in LIFECYCLE_TRANSITIONS.get(from_state, frozenset()):
        raise IllegalLifecycleTransitionError(from_state, to_state)
    now = _now_iso()
    transition_record: dict[str, object] = {
        _COL_LIFECYCLE_STATE: to_state,
        "last_transition_at": now,
        "directed_by": directed_by,
    }
    if recorded_fields:
        transition_record.update(recorded_fields)
    updated = require_updated(
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MANAGED_SESSION,
                "filters": {
                    _COL_AGENT_INSTANCE_ID: agent_instance_id,
                    _COL_LIFECYCLE_STATE: from_state,
                    _COL_IS_DELETED: 0,
                },
            },
            transition_record,
        ),
    )
    if updated != 1:
        raise StaleLifecycleStateError(agent_instance_id, from_state, to_state)
    # AMEND 2a: audit AFTER the ledger write succeeds — never document a
    # transition that lost its race. Insert-only; concurrent writers never
    # contend on this table.
    require_completed(
        state.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_SESSION_TRANSITION,
                "record": {
                    _COL_AGENT_INSTANCE_ID: agent_instance_id,
                    "from_state": from_state,
                    "to_state": to_state,
                    "directed_by": directed_by,
                    "reason": reason,
                    "occurred_at": now,
                },
            },
        ),
        "insert session_transition",
    )


# Coordination-efficiency contract validation and supervision live beside the
# lifecycle persistence they govern. Imports of the dispatch state machine stay
# call-local so that state machine can use this module's row primitives without
# a module cycle.
_DISPATCH_EVIDENCE_STATUSES = frozenset({"pass", "fail", "skipped", "not_applicable"})
_DISPATCH_SKIP_STATUSES = frozenset({"skipped", "not_applicable"})


def _dispatch_text(value: object, field: str) -> str:
    from .managed_dispatch import _require_text  # noqa: PLC0415

    return _require_text(value, field)


def _dispatch_error(code: str, message: str) -> Exception:
    from .managed_dispatch import DispatchError  # noqa: PLC0415

    return DispatchError(code, message)


def _completion_obligation(item: object) -> tuple[str, frozenset[str]]:
    if not isinstance(item, Mapping):
        raise _dispatch_error(
            "completion_contract_invalid",
            "Each completion evidence obligation must be an object.",
        )
    evidence_id = _dispatch_text(item.get("id"), "completion_evidence_id")
    allowed = item.get("allowed_statuses")
    if not isinstance(allowed, list) or not allowed:
        raise _dispatch_error(
            "completion_contract_invalid",
            f"Evidence obligation {evidence_id!r} needs allowed_statuses.",
        )
    statuses = frozenset(str(value) for value in allowed)
    if not statuses <= _DISPATCH_EVIDENCE_STATUSES:
        raise _dispatch_error(
            "completion_contract_invalid",
            f"Evidence obligation {evidence_id!r} has invalid statuses.",
        )
    return evidence_id, statuses


def _validate_completion_verdicts(contract: Mapping[str, Any]) -> None:
    verdicts = contract.get("allowed_verdicts")
    if (
        not isinstance(verdicts, list)
        or not verdicts
        or not all(isinstance(value, str) and value.strip() for value in verdicts)
    ):
        raise _dispatch_error(
            "completion_contract_invalid",
            "completion_contract.allowed_verdicts must be a non-empty string list.",
        )


def completion_obligations(
    contract: Mapping[str, Any],
) -> dict[str, frozenset[str]]:
    raw = contract.get("evidence_obligations")
    if not isinstance(raw, list) or not raw:
        raise _dispatch_error(
            "completion_contract_invalid",
            "completion_contract.evidence_obligations must be a non-empty list.",
        )
    obligations: dict[str, frozenset[str]] = {}
    for item in raw:
        evidence_id, statuses = _completion_obligation(item)
        if evidence_id in obligations:
            raise _dispatch_error(
                "completion_contract_invalid",
                f"Duplicate completion evidence obligation {evidence_id!r}.",
            )
        obligations[evidence_id] = statuses
    _validate_completion_verdicts(contract)
    return obligations


def validate_completion_evidence(
    contract: Mapping[str, Any],
    evidence: object,
    verdict: object,
    *,
    prefix: str,
) -> dict[str, Any]:
    obligations = completion_obligations(contract)
    if not isinstance(evidence, Mapping):
        raise _dispatch_error(
            f"{prefix}_evidence_required",
            "Structured evidence is required.",
        )
    missing = sorted(set(obligations) - set(evidence))
    if missing:
        raise _dispatch_error(
            f"{prefix}_evidence_incomplete",
            f"Missing completion evidence obligations: {missing}.",
        )
    normalized: dict[str, Any] = {}
    for evidence_id, allowed in obligations.items():
        item = evidence[evidence_id]
        if not isinstance(item, Mapping):
            raise _dispatch_error(
                f"{prefix}_evidence_invalid",
                f"Evidence {evidence_id!r} must be an object.",
            )
        status = _dispatch_text(item.get("status"), f"{evidence_id}_status")
        if status not in allowed:
            raise _dispatch_error(
                f"{prefix}_evidence_status_invalid",
                f"Evidence {evidence_id!r} status {status!r} is not allowed.",
            )
        if status in _DISPATCH_SKIP_STATUSES:
            _dispatch_text(item.get("reason"), f"{evidence_id}_reason")
        normalized[evidence_id] = dict(item)
    verdict_text = _dispatch_text(verdict, "verdict")
    if verdict_text not in {str(value) for value in contract["allowed_verdicts"]}:
        raise _dispatch_error(
            f"{prefix}_verdict_invalid",
            "Verdict is outside the contract.",
        )
    return normalized


_REQUIRED_DISPATCH_SPEC_TEXT = (
    "dispatch_id",
    "lane_id",
    "role_name",
    "role_class",
    "work_class",
    "budget_line",
    "brief_ref",
    "brief_sha256",
    "expected_path",
    "model",
    "effort",
    "agent_runtime",
    "host",
    "visibility",
    "local_name",
    "permission_mode",
    "transport",
    "spawned_by_instance_id",
    "spawned_by_role",
    "directed_by",
    "uptake_due_at",
    "report_by",
    "watchdog_due_at",
)


def _validate_required_dispatch_spec(spec: Any) -> None:
    for field in _REQUIRED_DISPATCH_SPEC_TEXT:
        _dispatch_text(getattr(spec, field), field)
    if not spec.completion_contract:
        raise _dispatch_error(
            "completion_contract_required",
            "completion_contract is required.",
        )
    completion_obligations(spec.completion_contract)
    if not spec.allowed_hosts:
        raise _dispatch_error("allowed_hosts_required", "allowed_hosts is required.")
    if spec.report_by_seconds < 0:
        raise _dispatch_error(
            "spawn_window_invalid",
            "Spawn report windows cannot be negative.",
        )


def _validate_dispatch_digest(value: str, field: str) -> None:
    if len(value) != 64:
        raise _dispatch_error(
            f"{field}_invalid",
            f"{field} must be a SHA-256 hex digest.",
        )
    try:
        int(value, 16)
    except ValueError as exc:
        raise _dispatch_error(
            f"{field}_invalid",
            f"{field} must be a SHA-256 hex digest.",
        ) from exc


def _validate_dispatch_brief(spec: Any) -> tuple[Path, str]:
    from .managed_dispatch import _file_sha256  # noqa: PLC0415

    _validate_dispatch_digest(spec.brief_sha256, "brief_sha256")
    brief = Path(spec.brief_ref)
    if not brief.is_file():
        raise _dispatch_error(
            "brief_not_found",
            f"brief_ref does not exist: {brief}",
        )
    measured = _file_sha256(brief)
    if measured != spec.brief_sha256:
        raise _dispatch_error(
            "brief_digest_mismatch",
            "brief_ref does not match brief_sha256.",
        )
    return brief, measured


def _validate_dispatch_deadlines(spec: Any, now: datetime) -> None:
    deadlines = {
        field: _dispatch_deadline({field: getattr(spec, field)}, field)
        for field in ("uptake_due_at", "report_by", "watchdog_due_at")
    }
    if any(value <= now for value in deadlines.values()):
        raise _dispatch_error(
            "deadline_not_future",
            "Every dispatch deadline must be future.",
        )


def validate_dispatch_spec(spec: Any, now: datetime) -> tuple[Path, str]:
    _validate_required_dispatch_spec(spec)
    brief = _validate_dispatch_brief(spec)
    _validate_dispatch_deadlines(spec, now)
    return brief


# Coordination-efficiency supervision lives beside the lifecycle persistence
# it reconciles. Imports of the dispatch state machine stay call-local so the
# state machine can use this module's row primitives without a module cycle.
_MANAGED_DISPATCH_WALK_CEILING = 1_000_000
_SupervisionCondition = tuple[str, str, str]


def _dispatch_deadline(row: Mapping[str, Any], field: str) -> datetime:
    from .managed_dispatch import _parse_persisted_utc  # noqa: PLC0415

    return _parse_persisted_utc(row[field], field)


def _failed_start_dispatch_condition(
    row: Mapping[str, Any], _now: datetime
) -> _SupervisionCondition | None:
    if str(row["state"]) == "failed_start":
        return (
            "failed_start_decision_required",
            "decide_retry_or_cancel",
            str(row["spawned_by_role"]),
        )
    return None


def _unknown_liveness_dispatch_condition(
    row: Mapping[str, Any], now: datetime
) -> _SupervisionCondition | None:
    if str(row.get("host_liveness") or "") != "unknown":
        return None
    escalation = str(row.get("liveness_escalation_due_at") or "")
    if escalation and now >= _dispatch_deadline(
        {"liveness_escalation_due_at": escalation},
        "liveness_escalation_due_at",
    ):
        return (
            "liveness_unknown_escalation",
            "investigate_or_decide_retry",
            str(row["spawned_by_role"]),
        )
    next_probe = str(row.get("next_liveness_probe_at") or "")
    if next_probe and now >= _dispatch_deadline(
        {"next_liveness_probe_at": next_probe},
        "next_liveness_probe_at",
    ):
        return (
            "liveness_reprobe_due",
            "reprobe_current_attempt",
            str(row["spawned_by_role"]),
        )
    return None


def _watchdog_dispatch_condition(
    row: Mapping[str, Any], now: datetime
) -> _SupervisionCondition | None:
    if str(row["state"]) != "active":
        return None
    if row.get("watchdog_fired_at"):
        return None
    if now >= _dispatch_deadline(row, "watchdog_due_at"):
        return (
            "watchdog_overdue",
            "perform_watchdog_review",
            str(row["spawned_by_role"]),
        )
    return None


def _blocker_dispatch_condition(
    row: Mapping[str, Any], now: datetime
) -> _SupervisionCondition | None:
    if (
        str(row["state"]) == "blocked_internal"
        and row.get("decision_due_at")
        and now >= _dispatch_deadline(row, "decision_due_at")
    ):
        return (
            "blocker_decision_overdue",
            "resolve_internal_blocker",
            str(row["blocker_owner"]),
        )
    return None


def _completion_dispatch_condition(
    row: Mapping[str, Any], _now: datetime
) -> _SupervisionCondition | None:
    if str(row["state"]) == "completion_reported":
        return (
            "completion_acceptance_pending",
            "validate_and_accept_completion",
            str(row["spawned_by_role"]),
        )
    return None


def _uptake_dispatch_condition(
    row: Mapping[str, Any], now: datetime
) -> _SupervisionCondition | None:
    if str(row["state"]) in {
        "preparing",
        "uptake_pending",
        "uptake_uncertain",
    } and now >= _dispatch_deadline(row, "uptake_due_at"):
        return (
            "uptake_overdue",
            "decide_uptake_recovery",
            str(row["spawned_by_role"]),
        )
    return None


def _milestone_dispatch_condition(
    row: Mapping[str, Any], now: datetime
) -> _SupervisionCondition | None:
    if str(row["state"]) == "active" and now >= _dispatch_deadline(row, "report_by"):
        return (
            "milestone_overdue",
            "request_worker_milestone",
            str(row["spawned_by_role"]),
        )
    return None


_DISPATCH_CONDITION_DETECTORS = (
    _failed_start_dispatch_condition,
    _unknown_liveness_dispatch_condition,
    _blocker_dispatch_condition,
    _completion_dispatch_condition,
    _uptake_dispatch_condition,
    _milestone_dispatch_condition,
    _watchdog_dispatch_condition,
)


def managed_dispatch_condition(
    row: Mapping[str, Any], now: datetime
) -> _SupervisionCondition | None:
    conditions = _managed_dispatch_conditions(row, now)
    return conditions[0] if conditions else None


def _managed_dispatch_conditions(
    row: Mapping[str, Any], now: datetime
) -> tuple[_SupervisionCondition, ...]:
    terminal_states = {"completed", "cancelled", "expired", "failed_start"}
    if str(row["state"]) in terminal_states - {"failed_start"}:
        return ()
    conditions: list[_SupervisionCondition] = []
    for detector in _DISPATCH_CONDITION_DETECTORS:
        condition = detector(row, now)
        if condition is None:
            continue
        conditions.append(condition)
        if condition[0] == "failed_start_decision_required":
            return (condition,)
    return tuple(conditions)


def _all_managed_dispatch_rows(
    state: StateManagementInterface,
) -> list[dict[str, Any]]:
    return list(
        iter_table_rows(
            state,
            namespace=AGENT_ROLE_BINDING_NAMESPACE,
            table=TABLE_MANAGED_DISPATCH,
            filters={},
            ceiling=_MANAGED_DISPATCH_WALK_CEILING,
            reason="managed dispatch supervisor must evaluate every owed row",
        )
    )


def _apply_dispatch_supervision_condition(
    state: StateManagementInterface,
    row: Mapping[str, Any],
    *,
    condition: _SupervisionCondition,
    clock: datetime,
) -> tuple[dict[str, Any], bool]:
    from .managed_dispatch import (  # noqa: PLC0415
        _find_event,
        _update_dispatch,
        _write_event,
    )

    name, action, owner = condition
    if name == "milestone_overdue":
        # Watchdog bookkeeping advances ``version`` without changing the
        # milestone episode.  Only attempt/report progress may re-key it.
        event_id = ":".join(
            (
                "supervision",
                name,
                str(int(row["attempt_number"])),
                _dispatch_deadline(row, "report_by").isoformat(),
                str(row.get("last_milestone_at") or "initial"),
            )
        )
    else:
        event_id = f"supervision:{name}:{int(row['version'])}"
    notice_emitted = _find_event(state, str(row["dispatch_id"]), event_id) is None
    current = dict(row)
    if name == "watchdog_overdue":
        current = _update_dispatch(
            state,
            row,
            {
                "watchdog_fired_at": clock.isoformat(),
                "next_required_action": action,
                "responsible_role": owner,
            },
        )
    if notice_emitted:
        _write_event(
            state,
            dispatch_id=str(current["dispatch_id"]),
            event_id=event_id,
            event_kind="supervision_notice",
            attempt_agent_instance_id=str(current.get("current_agent_instance_id") or ""),
            actor_role="platform-supervisor",
            actor_instance_id="",
            prior_version=(
                int(current["version"]) - 1
                if name == "watchdog_overdue"
                else int(current["version"])
            ),
            observed_at=clock,
            payload={
                "condition": name,
                "next_required_action": action,
                "responsible_role": owner,
            },
            accepted=True,
        )
    return current, notice_emitted


def _supervision_projections(
    state: StateManagementInterface,
    row: Mapping[str, Any],
    *,
    clock: datetime,
) -> list[tuple[dict[str, Any], bool]]:
    conditions = _managed_dispatch_conditions(row, clock)
    if not conditions:
        return []

    current = dict(row)
    applied: dict[str, tuple[dict[str, Any], bool]] = {}
    application_order = sorted(
        conditions,
        key=lambda condition: condition[0] != "watchdog_overdue",
    )
    for condition in application_order:
        current, notice_emitted = _apply_dispatch_supervision_condition(
            state,
            current,
            condition=condition,
            clock=clock,
        )
        name, action, owner = condition
        applied[name] = (
            {
                "dispatch_id": str(current["dispatch_id"]),
                "condition": name,
                "next_required_action": action,
                "responsible_role": owner,
                "notice_emitted": notice_emitted,
                "state": str(current["state"]),
            },
            notice_emitted,
        )
    return [applied[condition[0]] for condition in conditions]


def supervise_managed_dispatches(
    state: StateManagementInterface,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate every dispatch while isolating malformed persisted rows."""
    from .managed_dispatch import DispatchError  # noqa: PLC0415

    clock = (now or datetime.now(UTC)).astimezone(UTC)
    conditions: list[dict[str, Any]] = []
    malformed_rows: list[dict[str, str]] = []
    notices_emitted = 0
    rows = _all_managed_dispatch_rows(state)
    for row in rows:
        try:
            projections = _supervision_projections(state, row, clock=clock)
        except (DispatchError, KeyError, TypeError, ValueError) as exc:
            malformed_rows.append(
                {
                    "dispatch_id": str(row.get("dispatch_id") or ""),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            logger.error(
                "managed dispatch row %s is malformed; continuing sweep: %s",
                row.get("dispatch_id"),
                exc,
            )
            continue
        for condition, emitted in projections:
            conditions.append(condition)
            notices_emitted += int(emitted)
    return {
        "evaluated": len(rows),
        "conditions": conditions,
        "notices_emitted": notices_emitted,
        "malformed_rows": malformed_rows,
    }


def _probe_managed_attempt(row: Mapping[str, Any]) -> tuple[str, str]:
    """Return alive/dead/unknown; unsupported or faulted probes are unknown."""
    host_ref = str(row.get("host_ref") or "")
    if not host_ref:
        return "unknown", "host_ref unavailable"
    try:
        driver, _resolved_host = resolve_host_driver(
            str(row.get("host") or ""),
            str(row.get("agent_runtime") or DEFAULT_AGENT_RUNTIME),
        )
        alive = driver.alive(host_ref)
    except Exception as exc:  # noqa: BLE001 — probe faults are unknown, never dead
        return "unknown", f"{type(exc).__name__}: {exc}"
    if alive:
        return "alive", "driver.alive returned true"
    return "dead", "driver.alive returned false"


def _reconcile_managed_attempt(
    state: StateManagementInterface,
    row: Mapping[str, Any],
    *,
    now: datetime,
) -> str:
    from .managed_dispatch import (  # noqa: PLC0415
        DispatchError,
        mark_dispatch_worker_lost,
        record_dispatch_liveness,
    )

    agent_instance_id = str(row.get("agent_instance_id") or "")
    dispatch_id = str(row.get("dispatch_id") or "")
    liveness, detail = _probe_managed_attempt(row)
    persist_session_liveness(
        state,
        agent_instance_id=agent_instance_id,
        liveness=liveness,
        detail=detail,
        observed_at=now,
    )
    try:
        record_dispatch_liveness(
            state,
            dispatch_id=dispatch_id,
            liveness=liveness,
            detail=detail,
            observed_at=now,
        )
    except DispatchError as exc:
        logger.info(
            "managed dispatch %s liveness projection skipped for %s: %s",
            dispatch_id,
            agent_instance_id,
            exc,
        )
        return liveness
    if liveness != "dead":
        return liveness
    try:
        transition_lifecycle_state(
            state,
            agent_instance_id=agent_instance_id,
            from_state=str(row.get("lifecycle_state") or ""),
            to_state=LIFECYCLE_TERMINATED,
            directed_by="platform:managed_dispatch_supervisor",
            reason="native host definitively absent",
        )
    except StaleLifecycleStateError:
        logger.info(
            "managed attempt %s lost its host-death transition race",
            agent_instance_id,
        )
    try:
        mark_dispatch_worker_lost(
            state,
            dispatch_id=dispatch_id,
            agent_instance_id=agent_instance_id,
            observed_at=now,
            detail=detail,
        )
    except DispatchError as exc:
        logger.info(
            "managed dispatch %s host-death transition skipped: %s",
            dispatch_id,
            exc,
        )
    return liveness


def sweep_managed_dispatches(
    state: StateManagementInterface,
    *,
    peer_registry: PeerRegistry | None = None,
    bridge_manager: BridgeSessionManager | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reconcile every nonterminal attempt, then supervise every dispatch."""
    from .managed_dispatch import EVENT_MANAGED_DISPATCH_NOTICE  # noqa: PLC0415
    from .session_lifecycle_verbs import (  # noqa: PLC0415
        notify_steward_of_managed_dispatch,
    )

    clock = (now or datetime.now(UTC)).astimezone(UTC)
    counts = {"alive": 0, "dead": 0, "unknown": 0}
    evaluated = 0
    notices_delivered = 0
    for lifecycle_state in (
        LIFECYCLE_SPAWNING,
        LIFECYCLE_LIVE,
        LIFECYCLE_IDLE,
        LIFECYCLE_OVERDUE,
        LIFECYCLE_PARKED,
    ):
        for row in list_managed_sessions(state, {"lifecycle_state": lifecycle_state}):
            if not row.get("dispatch_id"):
                continue
            evaluated += 1
            outcome = _reconcile_managed_attempt(state, row, now=clock)
            counts[outcome] += 1
            if outcome == "dead" and notify_steward_of_managed_dispatch(
                state,
                peer_registry=peer_registry,
                bridge_manager=bridge_manager,
                dispatch_id=str(row["dispatch_id"]),
                condition="worker_lost",
                next_required_action="decide_retry_or_cancel",
                event_type=EVENT_MANAGED_DISPATCH_NOTICE,
            ):
                notices_delivered += 1
    supervision = supervise_managed_dispatches(state, now=clock)
    for condition in supervision["conditions"]:
        if condition["notice_emitted"] and notify_steward_of_managed_dispatch(
            state,
            peer_registry=peer_registry,
            bridge_manager=bridge_manager,
            dispatch_id=str(condition["dispatch_id"]),
            condition=str(condition["condition"]),
            next_required_action=str(condition["next_required_action"]),
            event_type=EVENT_MANAGED_DISPATCH_NOTICE,
        ):
            notices_delivered += 1
    return {
        "attempts_evaluated": evaluated,
        **counts,
        "dispatches_evaluated": supervision["evaluated"],
        "conditions": supervision["conditions"],
        "notices_emitted": supervision["notices_emitted"],
        "notices_delivered": notices_delivered,
    }


__all__ = [
    "IllegalLifecycleTransitionError",
    "LaneCharterRecord",
    "LaneCharterSpec",
    "ManagedSessionFilterRequiredError",
    "ManagedSessionSpec",
    "SessionNotFoundError",
    "StaleLifecycleStateError",
    "backfill_registration",
    "capture_lane_charter",
    "completion_obligations",
    "format_directed_by",
    "insert_managed_session",
    "iter_managed_sessions_unbounded",
    "list_managed_sessions",
    "list_managed_sessions_bounded",
    "managed_dispatch_condition",
    "persist_first_turn_evidence",
    "persist_session_liveness",
    "read_managed_session",
    "resolve_lane_charter",
    "set_host_ref",
    "supervise_managed_dispatches",
    "sweep_managed_dispatches",
    "transition_lifecycle_state",
    "validate_completion_evidence",
    "validate_dispatch_spec",
]
