"""Durable NO-LOSS deferred-vertex queue operations (INF-01 §D.9).

The queue mechanics behind ``InferenceService``'s DEFER verdict, extracted
so the service class stays a thin routing wrapper: one row per ``flow_id``
in ``core__inference_deferred_vertex`` (idempotent upsert — a re-defer of
the same flow overwrites, never duplicates), enumerable by role for the
sub-slice-2 first-claim drain and the SUB-05 re-drive. Deferrals survive a
restart; the pre-INF-01 in-memory last-writer-per-role register lost N−1.

The store dependency is STRUCTURAL (``DeferredVertexStore``) so the real
``StateManagementInterface`` and the offline smoke fakes both satisfy it —
a mismatch fails LOUD rather than silently dropping deferrals.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ananta.core.domain.enums import ActionStatus
from ananta.core.domain.timestamps import to_naive_utc
from ananta.error_handling import FrameworkError
from ananta.llm.agent_messaging.state_results import require_completed, require_records
from ananta.services.inference_service.schema import (
    COL_AGENT_INSTANCE_ID,
    COL_ATTEMPTS,
    COL_FLOW_ID,
    COL_FORWARDED_AT,
    COL_METHOD,
    COL_ROLE,
    COL_STATE,
    INFERENCE_DEFERRED_VERTEX_NAMESPACE,
    METHOD_PROCESS_ERROR,
    METHOD_PROCESS_RESULTS,
    STATE_DEFERRED,
    STATE_FAILED,
    STATE_FORWARDED,
    TABLE_INFERENCE_DEFERRED_VERTEX,
)
from ananta.services.state_service.bounded_read import iter_table_rows

if TYPE_CHECKING:
    from ananta.core.domain.types import ActionResult
    from ananta.services.inference_service.vertex_resolver import (
        VertexProvider,
        VertexResolution,
    )

logger = logging.getLogger(__name__)


_DEFERRED_VERTEX_QUEUE_CEILING = 1_000_000
_DEFERRED_VERTEX_QUEUE_CEILING_REASON = (
    "the durable deferred-vertex queue is paged for complete sweep, drain, and "
    "observability reads while the explicit ceiling keeps an unexpected runaway "
    "queue loud"
)


@runtime_checkable
class DeferredVertexStore(Protocol):
    """The state-interface surface the durable deferred-vertex queue needs."""

    def upsert_state(self, namespace: str, data: dict[str, object]) -> object: ...

    def query_state(self, namespace: str, filters: dict[str, object]) -> object: ...

    def query_ordered(self, namespace: str, data: dict[str, object]) -> object: ...

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object],
    ) -> object: ...

    def delete_records(self, namespace: str, query: dict[str, object]) -> object: ...


def require_deferred_vertex_store(state_service: object) -> DeferredVertexStore:
    """The state surface for the durable queue; fail LOUD if absent/wrong.

    The durable NO-LOSS queue is a hard dependency of the DEFER path — a
    missing/incompatible state service must NOT silently drop deferrals.
    """
    if not isinstance(state_service, DeferredVertexStore):
        raise FrameworkError(
            "InferenceService durable deferred-vertex queue requires a "
            "state_service exposing upsert_state/query_state; got "
            f"{type(state_service).__name__}",
        )
    return state_service


def record_deferred_vertex(
    store: DeferredVertexStore,
    *,
    is_error: bool,
    resolution: VertexResolution,
    flow_id: str | None,
) -> ActionResult:
    """Record a deferred vertex durably (loud) and return a no-op ActionResult.

    The flow is explicitly bound to a session that is absent right now;
    DEFER rather than run the default model (◆R2 — never silent-Qwen an
    explicitly-bound vertex). Mirrors ``SessionInferenceProvider``'s no-op
    ActionResult shape (``COMPLETED`` + empty ``actions``), which the poller
    already handles for the live-provider path — so the deferred flow
    TERMINATES cleanly (loudly logged + durably recorded), not parked pending.

    N2: ``flow_id`` is the re-drive key. A DEFER with NO ``flow_id`` is
    UNRECOVERABLE (nothing to RESUBMIT) and cannot key the durable row, so it
    is logged LOUD and dropped from the queue — still returning the no-op so
    the flow terminates. Honest per the §D.9 guardrail: the unacceptable
    state is SILENT loss; this is loud.
    """
    method = METHOD_PROCESS_ERROR if is_error else METHOD_PROCESS_RESULTS
    logger.warning(
        "inference vertex DEFERRED (%s): flow=%s bound to role=%r "
        "instance=%r has no live provider — recording deferral, NOT "
        "falling back to the default model (Phase 5 ◆R2)",
        method,
        flow_id,
        resolution.role,
        resolution.agent_instance_id,
    )
    if flow_id is None:
        logger.warning(
            "inference vertex DEFERRED with NO flow_id (role=%r instance=%r "
            "method=%s) — UNRECOVERABLE (no re-drive key), dropped from the "
            "durable queue; the flow still terminates cleanly",
            resolution.role,
            resolution.agent_instance_id,
            method,
        )
    else:
        _record_row(
            store,
            role=resolution.role,
            agent_instance_id=resolution.agent_instance_id,
            method=method,
            flow_id=flow_id,
        )
    return {
        "action_status": ActionStatus.COMPLETED.value,
        "data": {
            "vertex_deferred": True,
            "role": resolution.role,
            "agent_instance_id": resolution.agent_instance_id,
            "flow_id": flow_id,
        },
        "actions": [],
        "error": None,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def _record_row(
    store: DeferredVertexStore,
    *,
    role: str | None,
    agent_instance_id: str | None,
    method: str,
    flow_id: str,
) -> None:
    """Upsert one durable DEFERRED (no-holder) row (idempotent on ``flow_id``).

    ``state='deferred'`` and ``forwarded_at`` is cleared (a re-defer of a row
    that was previously ``forwarded`` — e.g. a vacancy-fill flip — nulls the
    stamp). ``attempts`` is deliberately OMITTED from the record so the
    ON-CONFLICT update PRESERVES it (monotone per flow occupancy); a first
    insert takes the schema default 0.
    """
    result = store.upsert_state(
        INFERENCE_DEFERRED_VERTEX_NAMESPACE,
        {
            "table": TABLE_INFERENCE_DEFERRED_VERTEX,
            "record": {
                COL_ROLE: role,
                COL_AGENT_INSTANCE_ID: agent_instance_id,
                COL_METHOD: method,
                COL_FLOW_ID: flow_id,
                COL_STATE: STATE_DEFERRED,
                COL_FORWARDED_AT: None,
            },
            "conflict_columns": [COL_FLOW_ID],
        },
    )
    # Fail LOUD if the durable write did not land — a swallowed upsert
    # followed by a "recorded" no-op is the precise silent-loss bug class
    # ``require_completed`` guards (state_results.py).
    require_completed(result, "upsert")


def record_forwarded_vertex(
    store: DeferredVertexStore,
    *,
    is_error: bool,
    role: str | None,
    holder_agent_instance_id: str | None,
    flow_id: str | None,
    now_iso: str,
) -> None:
    """Mint a durable FORWARDED-outstanding row BEFORE the fire-and-forget forward.

    INF-06 reliability carve: the Surface-1 action-decode forward
    (``_dispatch_to_provider`` → ``provider.process_*`` → ``_emit_bridge_event``)
    returns ``COMPLETED`` + ``actions: []`` so the flow terminates synchronously
    at forward time. This row is the durable "the holder owes me a
    self-execution" marker that OUTLIVES the completed flow, so a holder death /
    serve-timeout re-drives instead of stalling silently.

    Durability-FIRST + fail-LOUD (§8-bis Q1a rider): the caller mints via THIS
    (which raises on a non-landed upsert) BEFORE emitting the forward; a mint
    failure must degrade to a durable defer or raise — NEVER fire-and-forget.
    A forward with NO ``flow_id`` is UNRECOVERABLE (no re-drive key) — loud +
    raise (N2 analog), never a silent anchorless forward.

    ``state='forwarded'``, ``forwarded_at=now``; ``attempts`` OMITTED (preserved
    monotone across a re-forward of the same flow).
    """
    method = METHOD_PROCESS_ERROR if is_error else METHOD_PROCESS_RESULTS
    if flow_id is None:
        raise FrameworkError(
            "inference vertex FORWARD with NO flow_id "
            f"(role={role!r} holder={holder_agent_instance_id!r} method={method}) "
            "— UNRECOVERABLE (no serve-anchor re-drive key); refusing to emit an "
            "anchorless fire-and-forget forward (INF-06 reliability, N2 analog).",
        )
    result = store.upsert_state(
        INFERENCE_DEFERRED_VERTEX_NAMESPACE,
        {
            "table": TABLE_INFERENCE_DEFERRED_VERTEX,
            "record": {
                COL_ROLE: role,
                COL_AGENT_INSTANCE_ID: holder_agent_instance_id,
                COL_METHOD: method,
                COL_FLOW_ID: flow_id,
                COL_STATE: STATE_FORWARDED,
                COL_FORWARDED_AT: now_iso,
            },
            "conflict_columns": [COL_FLOW_ID],
        },
    )
    require_completed(result, "upsert")


def mint_forwarded_or_degrade(
    store: DeferredVertexStore,
    *,
    resolution: VertexResolution,
    is_error: bool,
    flow_id: str | None,
    now_iso: str,
    degrade: Callable[..., ActionResult],
) -> ActionResult | None:
    """Mint the forwarded serve-anchor BEFORE the forward; degrade a recoverable
    mint failure to a durable DEFER (§8-bis Q1a — never an anchorless
    fire-and-forget forward).

    Returns the ``degrade`` ActionResult when a RECOVERABLE mint failure fired (a
    flow_id-absent forward → ``FrameworkError``) so the caller returns it instead
    of forwarding; returns ``None`` when the mint landed (the caller emits the
    forward). A store-write failure propagates the ``StateOperationError`` loud —
    degrading to the SAME broken store would only re-fail — and still never
    fire-and-forget, since it raises BEFORE the forward.
    """
    try:
        record_forwarded_vertex(
            store, is_error=is_error, role=resolution.role,
            holder_agent_instance_id=resolution.agent_instance_id,
            flow_id=flow_id, now_iso=now_iso,
        )
    except FrameworkError:
        logger.warning(
            "INF-06: forwarded-vertex serve-anchor mint FAILED (flow=%s holder=%r) "
            "— degrading to durable DEFER, NOT an anchorless fire-and-forget forward",
            flow_id, resolution.agent_instance_id, exc_info=True,
        )
        return degrade(is_error=is_error, resolution=resolution, flow_id=flow_id)
    return None


def forward_with_serve_anchor(
    store: DeferredVertexStore,
    *,
    resolution: VertexResolution,
    is_error: bool,
    params: dict[str, Any],
    state: dict[str, Any],
    now_iso: str,
    degrade: Callable[..., ActionResult],
) -> ActionResult:
    """Route a PROVIDER-verdict resolution to its session vertex: mint the
    serve-anchor, THEN emit the forward (or the degrade).

    N6 (Rev-C): a PROVIDER verdict ALWAYS carries a provider — fail LOUD if the
    ``InferenceProviderResolver`` invariant is violated rather than silently
    returning ``None`` (which would fall through to the default model, the exact
    silent-Qwen path the governing rule forbids).

    Bundles the mint-before-forward ordering so the INF-06 durability-FIRST
    invariant is structural, not merely conventional: the serve-anchor row is
    always minted — or a recoverable failure degraded to a durable DEFER, or a
    store failure raised — BEFORE ``provider.process_*`` fires. A forward is never
    emitted anchorless. ``_emit_bridge_event`` returns COMPLETED + actions:[] so
    the forwarded flow terminates synchronously; the durable row OUTLIVES it as
    the "holder owes a self-execution" marker the sweep / drain re-drive.
    """
    provider: VertexProvider | None = resolution.provider
    if provider is None:
        raise FrameworkError(
            "VertexRouting.PROVIDER with no provider — "
            "InferenceProviderResolver invariant violated",
        )
    flow_id_raw = state.get("flow_id")
    flow_id = flow_id_raw if isinstance(flow_id_raw, str) and flow_id_raw else None
    degraded = mint_forwarded_or_degrade(
        store, resolution=resolution, is_error=is_error, flow_id=flow_id,
        now_iso=now_iso, degrade=degrade,
    )
    if degraded is not None:
        return degraded
    if is_error:
        return provider.process_error(params, state)
    return provider.process_results(params, state)


def hard_delete_flows(store: DeferredVertexStore, flow_ids: list[str]) -> None:
    """HARD-delete rows by ``flow_id`` — frees the unique flow_id slot.

    Shared by the first-claim drain's successful-re-drive path and the terminal
    'failed'-row GC sweep. A soft delete would leave the unique flow_id occupied
    and block a clean future re-defer, so ``soft_delete=False`` is load-bearing.
    """
    for flow_id in flow_ids:
        require_completed(
            store.delete_records(
                INFERENCE_DEFERRED_VERTEX_NAMESPACE,
                {
                    "table": TABLE_INFERENCE_DEFERRED_VERTEX,
                    "filters": {COL_FLOW_ID: flow_id},
                    "soft_delete": False,
                },
            ),
            "hard-delete deferred vertex",
        )


def forwarded_before(row: dict[str, object], *, cutoff_iso: str) -> bool:
    """Was this ``forwarded`` row stamped before ``cutoff_iso`` (serve-timeout)?

    Compares the forward timestamp by VALUE via ``to_naive_utc``, NOT by ISO-8601
    spelling. ``forwarded_at`` is a ``timestamp without time zone`` column
    (``schema.py`` ``ColumnType.DATETIME``), so a value written aware at forward
    time round-trips NAIVE (offset stripped) while ``cutoff_iso`` is the tz-aware
    ``datetime.now(UTC).isoformat()`` — a lexical compare of a naive spelling
    against an aware one mis-orders at the boundary (F-AISLOP; the ``to_naive_utc``
    bug class). Coerce both sides to one naive-UTC VALUE, then compare.

    A missing / empty stamp returns True (surface-on-anomaly): a ``forwarded``-state
    row ALWAYS carries a forward stamp (the writer mints it), so a missing one is an
    anomaly that must NOT pin the flow un-swept forever — re-drive it (bounded, it
    caps out to a loud terminal ``failed``). This SWEEP default is deliberately the
    OPPOSITE of the GC ``terminal_row_aged`` never-delete-on-unknown-age default:
    a re-drive is recoverable, a delete is not. A present-but-unparseable stamp is a
    genuine corruption — ``to_naive_utc`` raises (fail loud); the sweeper tick
    catches it, logs, and retries. It is unreachable via the DATETIME writer.
    """
    stamp = row.get(COL_FORWARDED_AT)
    if not (isinstance(stamp, str) and stamp):
        return True
    return to_naive_utc(stamp) < to_naive_utc(cutoff_iso)


def live_rows_in_state(
    store: DeferredVertexStore, *, state: str,
) -> list[dict[str, object]]:
    """Every live (``is_deleted=0``) row in ``state`` — the sweep/drain input."""
    return list(iter_table_rows(
        store,
        namespace=INFERENCE_DEFERRED_VERTEX_NAMESPACE,
        table=TABLE_INFERENCE_DEFERRED_VERTEX,
        filters={COL_STATE: state},
        ceiling=_DEFERRED_VERTEX_QUEUE_CEILING,
        reason=_DEFERRED_VERTEX_QUEUE_CEILING_REASON,
    ))


def increment_attempts(store: DeferredVertexStore, *, flow_id: str) -> None:
    """Increment a forwarded row's monotone ``attempts`` at sweep-fire (loud).

    A predicated in-place bump (never reset). The caller reads the row's prior
    ``attempts`` to decide re-drive-vs-terminal BEFORE calling this.
    """
    row = _read_row(store, flow_id=flow_id)
    prior = _int_or_zero(row.get(COL_ATTEMPTS) if row else None)
    require_completed(
        store.update_state(
            INFERENCE_DEFERRED_VERTEX_NAMESPACE,
            {"table": TABLE_INFERENCE_DEFERRED_VERTEX, "filters": {COL_FLOW_ID: flow_id}},
            {COL_ATTEMPTS: prior + 1},
        ),
        "increment deferred-vertex attempts",
    )


def mark_terminal_failed(store: DeferredVertexStore, *, flow_id: str) -> None:
    """Flip a row to the terminal ``failed`` state (re-drive attempts cap hit).

    A durable stall RECORD (vs today's stall-with-nothing) — the sweep skips
    ``failed`` rows; a GC sweep hard-deletes them once aged.
    """
    require_completed(
        store.update_state(
            INFERENCE_DEFERRED_VERTEX_NAMESPACE,
            {"table": TABLE_INFERENCE_DEFERRED_VERTEX, "filters": {COL_FLOW_ID: flow_id}},
            {COL_STATE: STATE_FAILED},
        ),
        "mark deferred-vertex terminal-failed",
    )


def _read_row(
    store: DeferredVertexStore, *, flow_id: str,
) -> dict[str, object] | None:
    result = store.query_state(
        INFERENCE_DEFERRED_VERTEX_NAMESPACE,
        {"table": TABLE_INFERENCE_DEFERRED_VERTEX, "filters": {COL_FLOW_ID: flow_id}},
    )
    rows = list(require_records(result))
    return rows[0] if rows else None


def _int_or_zero(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def attempts_of(row: dict[str, object]) -> int:
    """The row's monotone ``attempts`` (0 if absent/malformed)."""
    return _int_or_zero(row.get(COL_ATTEMPTS))


def deferred_vertices_snapshot(
    store: DeferredVertexStore,
) -> dict[str, dict[str, object]]:
    """Snapshot of the durable queue keyed by ``flow_id`` (observability).

    Every deferred flow is distinct, so nothing collapses (the pre-INF-01
    role-keyed shape held only the last flow per role). This is the hook the
    sub-slice-2 vacancy-fill drain + the SUB-05 re-drive read.
    """
    snapshot: dict[str, dict[str, object]] = {}
    for row in iter_table_rows(
        store,
        namespace=INFERENCE_DEFERRED_VERTEX_NAMESPACE,
        table=TABLE_INFERENCE_DEFERRED_VERTEX,
        filters={},
        ceiling=_DEFERRED_VERTEX_QUEUE_CEILING,
        reason=_DEFERRED_VERTEX_QUEUE_CEILING_REASON,
    ):
        flow_id = row.get(COL_FLOW_ID)
        if not isinstance(flow_id, str):
            continue
        snapshot[flow_id] = {
            "role": row.get(COL_ROLE),
            "agent_instance_id": row.get(COL_AGENT_INSTANCE_ID),
            "method": row.get(COL_METHOD),
            "flow_id": flow_id,
        }
    return snapshot


__all__ = [
    "DeferredVertexStore",
    "attempts_of",
    "deferred_vertices_snapshot",
    "forward_with_serve_anchor",
    "forwarded_before",
    "hard_delete_flows",
    "increment_attempts",
    "live_rows_in_state",
    "mark_terminal_failed",
    "mint_forwarded_or_degrade",
    "record_deferred_vertex",
    "record_forwarded_vertex",
    "require_deferred_vertex_store",
]
