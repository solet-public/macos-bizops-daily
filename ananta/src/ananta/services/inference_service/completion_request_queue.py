"""Durable completion-request queue operations (INF-02).

The queue mechanics behind ``InferenceService.submit_completion_request``,
extracted so the service class stays a thin routing wrapper (the same shape
as :mod:`~ananta.services.inference_service.deferred_vertex_queue`, which
this deliberately mirrors — SEPARATE table, shared MECHANISM, per the
Reviewer-A INF-02 rider).

Lifecycle primitives (all predicated-CAS via ``update_state`` rows-affected —
that IS the compare-and-set per the state-interface mandate):

- :func:`insert_completion_request` — one durable ``pending`` row, unassigned.
- :func:`stamp_for_forward` — CAS unassigned→stamped; the winner (and only
  the winner) emits the forward event, so a submit racing a drain cannot
  double-forward one request.
- :func:`serve_completion_request` — CAS pending→served; idempotent (a second
  serve reports ``already_served``, never a double resume) and typed on an
  unknown request.
- :func:`requeue_stale_assignment` — CAS stamped→unassigned (attempts+1) for
  holder-death / serve-timeout; fails terminally (loud) at the attempts cap.
  A completion must never hang pending forever on a dead holder.

The store dependency is STRUCTURAL (:class:`CompletionRequestStore`) so the
real ``StateManagementInterface`` and the offline smoke fakes both satisfy
it — a mismatch fails LOUD rather than silently dropping requests.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable
from uuid import uuid4

from ananta.core.domain.timestamps import to_naive_utc
from ananta.error_handling import FrameworkError
from ananta.llm.agent_messaging.state_results import (
    require_completed,
    require_records,
    require_updated,
)
from ananta.services.inference_service.completion_request_schema import (
    COL_ATTEMPTS,
    COL_CORRELATION,
    COL_FAILURE_REASON,
    COL_FORWARDED_AT,
    COL_HOLDER_AGENT_INSTANCE_ID,
    COL_IS_DELETED,
    COL_MESSAGES,
    COL_PURPOSE,
    COL_REQUEST_ID,
    COL_RESULT_TEXT,
    COL_RESUME_PROCESS_KEY,
    COL_STATUS,
    ID_PREFIX_COMPLETION_REQUEST,
    INFERENCE_COMPLETION_REQUEST_NAMESPACE,
    MAX_REQUEUE_ATTEMPTS,
    MESSAGES_PAYLOAD_MAX_CHARS,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SERVED,
    TABLE_INFERENCE_COMPLETION_REQUEST,
)

logger = logging.getLogger(__name__)

# Empty-string sentinel for "no holder" — the state interface's
# ``query_state``/``update_state`` filters are equality-only, and an
# equality filter cannot match SQL NULL, so unassigned rows carry '' in
# ``holder_agent_instance_id`` (and ``forwarded_at``) instead of NULL.
UNASSIGNED_HOLDER = ""

# Serve verdict tokens (the serve verb surfaces these verbatim).
SERVE_SERVED = "served"
SERVE_ALREADY_SERVED = "already_served"
SERVE_ALREADY_FAILED = "already_failed"
SERVE_UNKNOWN_REQUEST = "unknown_request"

REQUEUE_REQUEUED = "requeued"
REQUEUE_FAILED_TERMINAL = "failed_terminal"
REQUEUE_LOST_RACE = "lost_race"


@runtime_checkable
class CompletionForwarder(Protocol):
    """A session vertex that can carry a completion request to its holder.

    Satisfied structurally by ``SessionInferenceProvider`` (agent_messaging)
    — the routing wrapper duck-checks the resolved autonomic provider
    against this; a holder whose provider lacks the capability routes the
    request to the durable queue instead (loud), never silently drops it.
    """

    @property
    def agent_instance_id(self) -> str: ...

    def forward_completion_request(
        self,
        *,
        request_id: str,
        purpose: str,
        messages: list[dict[str, str]],
        correlation: dict[str, str],
    ) -> None: ...


@runtime_checkable
class CompletionRequestStore(Protocol):
    """The state-interface surface the durable completion queue needs."""

    def write_state(self, namespace: str, data: dict[str, object]) -> object: ...

    def query_state(self, namespace: str, filters: dict[str, object]) -> object: ...

    def update_state(
        self,
        namespace: str,
        query: dict[str, object],
        updates: dict[str, object],
    ) -> object: ...

    def delete_records(self, namespace: str, query: dict[str, object]) -> object: ...


def require_completion_request_store(state_service: object) -> CompletionRequestStore:
    """The state surface for the durable queue; fail LOUD if absent/wrong.

    The durable queue is a hard dependency of the autonomic completion
    path — a missing/incompatible state service must NOT silently drop
    requests.
    """
    if not isinstance(state_service, CompletionRequestStore):
        raise FrameworkError(
            "InferenceService durable completion-request queue requires a "
            "state_service exposing write_state/query_state/update_state; "
            f"got {type(state_service).__name__}",
        )
    return state_service


def serialize_messages(messages: list[dict[str, str]]) -> str:
    """Serialize + size-cap the ``messages`` payload (typed, never truncated).

    A truncated prompt is a corrupted completion, so an over-cap payload is
    a typed submit-time rejection the caller propagates loud.
    """
    payload = json.dumps(messages)
    if len(payload) > MESSAGES_PAYLOAD_MAX_CHARS:
        raise FrameworkError(
            "completion request messages payload exceeds "
            f"{MESSAGES_PAYLOAD_MAX_CHARS} chars ({len(payload)}) — refusing "
            "to enqueue a truncated prompt; compact the requesting context",
        )
    return payload


def insert_completion_request(
    store: CompletionRequestStore,
    *,
    purpose: str,
    resume_process_key: str,
    correlation: dict[str, str],
    messages: list[dict[str, str]],
) -> str:
    """Insert one durable ``pending`` unassigned request row; return its id.

    ``request_id`` is minted here (``icr-<uuid4>``) — fresh per submit, so
    the UNIQUE index never conflicts and the write's contract is success
    (``require_completed``, fail loud).
    """
    request_id = f"{ID_PREFIX_COMPLETION_REQUEST}-{uuid4().hex}"
    require_completed(
        store.write_state(
            INFERENCE_COMPLETION_REQUEST_NAMESPACE,
            {
                "table": TABLE_INFERENCE_COMPLETION_REQUEST,
                "record": {
                    COL_REQUEST_ID: request_id,
                    COL_PURPOSE: purpose,
                    COL_RESUME_PROCESS_KEY: resume_process_key,
                    COL_CORRELATION: json.dumps(correlation),
                    COL_MESSAGES: serialize_messages(messages),
                    COL_STATUS: STATUS_PENDING,
                    COL_HOLDER_AGENT_INSTANCE_ID: UNASSIGNED_HOLDER,
                    COL_FORWARDED_AT: "",
                    COL_ATTEMPTS: 0,
                    COL_RESULT_TEXT: "",
                    COL_FAILURE_REASON: "",
                },
            },
        ),
        "insert completion request",
    )
    return request_id


def stamp_for_forward(
    store: CompletionRequestStore,
    *,
    request_id: str,
    holder_agent_instance_id: str,
) -> bool:
    """CAS unassigned→stamped; True iff THIS caller won the forward.

    The winner emits the forward event; a loser (concurrent drain vs
    submit) does nothing — one request is never forwarded twice while
    stamped.
    """
    updated = require_updated(
        store.update_state(
            INFERENCE_COMPLETION_REQUEST_NAMESPACE,
            {
                "table": TABLE_INFERENCE_COMPLETION_REQUEST,
                "filters": {
                    COL_REQUEST_ID: request_id,
                    COL_STATUS: STATUS_PENDING,
                    COL_HOLDER_AGENT_INSTANCE_ID: UNASSIGNED_HOLDER,
                },
            },
            {
                COL_HOLDER_AGENT_INSTANCE_ID: holder_agent_instance_id,
                COL_FORWARDED_AT: datetime.now(UTC).isoformat(),
            },
        ),
    )
    return updated == 1


def clear_stamp_after_failed_forward(
    store: CompletionRequestStore,
    *,
    request_id: str,
    holder_agent_instance_id: str,
) -> None:
    """Return a just-stamped row to unassigned after the forward emission failed.

    The bridge append raced the holder's disconnect — the row must not sit
    stamped to a holder that never heard about it. No attempts increment:
    the request was never actually in flight.
    """
    updated = require_updated(
        store.update_state(
            INFERENCE_COMPLETION_REQUEST_NAMESPACE,
            {
                "table": TABLE_INFERENCE_COMPLETION_REQUEST,
                "filters": {
                    COL_REQUEST_ID: request_id,
                    COL_STATUS: STATUS_PENDING,
                    COL_HOLDER_AGENT_INSTANCE_ID: holder_agent_instance_id,
                },
            },
            {
                COL_HOLDER_AGENT_INSTANCE_ID: UNASSIGNED_HOLDER,
                COL_FORWARDED_AT: "",
            },
        ),
    )
    if updated != 1:
        # The row moved under us (served/requeued concurrently) — loud,
        # nothing to repair: the CAS lifecycle already owns it.
        logger.warning(
            "completion request %s: stamp-clear after failed forward "
            "matched no row (moved concurrently)", request_id,
        )


def serve_completion_request(
    store: CompletionRequestStore,
    *,
    request_id: str,
    result_text: str,
) -> tuple[str, dict[str, object] | None]:
    """CAS pending→served; return ``(verdict, row)``.

    ``served`` → the caller submits the resume continuation (exactly once —
    the CAS win is the idempotency gate). ``already_served`` /
    ``already_failed`` / ``unknown_request`` → typed rejection, no resume.
    """
    updated = require_updated(
        store.update_state(
            INFERENCE_COMPLETION_REQUEST_NAMESPACE,
            {
                "table": TABLE_INFERENCE_COMPLETION_REQUEST,
                "filters": {COL_REQUEST_ID: request_id, COL_STATUS: STATUS_PENDING},
            },
            {COL_STATUS: STATUS_SERVED, COL_RESULT_TEXT: result_text},
        ),
    )
    row = read_completion_request(store, request_id=request_id)
    if updated == 1:
        return SERVE_SERVED, row
    if row is None:
        return SERVE_UNKNOWN_REQUEST, None
    status = str(row.get(COL_STATUS) or "")
    if status == STATUS_FAILED:
        return SERVE_ALREADY_FAILED, row
    return SERVE_ALREADY_SERVED, row


def requeue_stale_assignment(
    store: CompletionRequestStore,
    *,
    row: dict[str, object],
    reason: str,
) -> str:
    """CAS one stamped pending row back to unassigned (attempts+1), or fail it.

    Predicated on the AS-READ holder stamp + forwarded_at so a concurrent
    serve/requeue wins cleanly (``lost_race``). At ``MAX_REQUEUE_ATTEMPTS``
    the row fails TERMINALLY (loud) instead of cycling forever.
    """
    request_id = str(row.get(COL_REQUEST_ID) or "")
    holder = str(row.get(COL_HOLDER_AGENT_INSTANCE_ID) or "")
    forwarded_at = str(row.get(COL_FORWARDED_AT) or "")
    attempts = row.get(COL_ATTEMPTS)
    attempts = attempts if isinstance(attempts, int) else 0
    filters = {
        COL_REQUEST_ID: request_id,
        COL_STATUS: STATUS_PENDING,
        COL_HOLDER_AGENT_INSTANCE_ID: holder,
        COL_FORWARDED_AT: forwarded_at,
    }
    if attempts + 1 >= MAX_REQUEUE_ATTEMPTS:
        failure_reason = (
            f"requeue attempts exhausted ({attempts + 1}/{MAX_REQUEUE_ATTEMPTS}) "
            f"— last holder {holder!r}, {reason}"
        )
        updated = require_updated(
            store.update_state(
                INFERENCE_COMPLETION_REQUEST_NAMESPACE,
                {"table": TABLE_INFERENCE_COMPLETION_REQUEST, "filters": filters},
                {
                    COL_STATUS: STATUS_FAILED,
                    COL_ATTEMPTS: attempts + 1,
                    COL_FAILURE_REASON: failure_reason,
                },
            ),
        )
        if updated != 1:
            return REQUEUE_LOST_RACE
        logger.error(
            "completion request %s FAILED terminally: %s (purpose=%s)",
            request_id, failure_reason, row.get(COL_PURPOSE),
        )
        return REQUEUE_FAILED_TERMINAL
    updated = require_updated(
        store.update_state(
            INFERENCE_COMPLETION_REQUEST_NAMESPACE,
            {"table": TABLE_INFERENCE_COMPLETION_REQUEST, "filters": filters},
            {
                COL_HOLDER_AGENT_INSTANCE_ID: UNASSIGNED_HOLDER,
                COL_FORWARDED_AT: "",
                COL_ATTEMPTS: attempts + 1,
            },
        ),
    )
    if updated != 1:
        return REQUEUE_LOST_RACE
    logger.warning(
        "completion request %s re-queued (%s): holder %r never served "
        "(attempt %d/%d) — the next sys:autonomic claim's drain re-serves it",
        request_id, reason, holder, attempts + 1, MAX_REQUEUE_ATTEMPTS,
    )
    return REQUEUE_REQUEUED


def read_completion_request(
    store: CompletionRequestStore,
    *,
    request_id: str,
) -> dict[str, object] | None:
    """Read one live request row by id, or ``None``."""
    rows = require_records(
        store.query_state(
            INFERENCE_COMPLETION_REQUEST_NAMESPACE,
            {
                "table": TABLE_INFERENCE_COMPLETION_REQUEST,
                "filters": {COL_REQUEST_ID: request_id, COL_IS_DELETED: 0},
            },
        ),
    )
    return rows[0] if rows else None


def pending_unassigned_requests(
    store: CompletionRequestStore,
) -> list[dict[str, object]]:
    """All live ``pending`` unassigned rows (the first-claim drain's input)."""
    return require_records(
        store.query_state(
            INFERENCE_COMPLETION_REQUEST_NAMESPACE,
            {
                "table": TABLE_INFERENCE_COMPLETION_REQUEST,
                "filters": {
                    COL_STATUS: STATUS_PENDING,
                    COL_HOLDER_AGENT_INSTANCE_ID: UNASSIGNED_HOLDER,
                    COL_IS_DELETED: 0,
                },
            },
        ),
    )


def pending_stamped_requests(
    store: CompletionRequestStore,
) -> list[dict[str, object]]:
    """All live ``pending`` rows with a holder stamp (sweep/requeue input).

    Equality-only filters cannot express ``holder != ''``, so this
    enumerates pending rows and filters in code — the pending set is small
    (planning turns in flight), never a hot path.
    """
    rows = require_records(
        store.query_state(
            INFERENCE_COMPLETION_REQUEST_NAMESPACE,
            {
                "table": TABLE_INFERENCE_COMPLETION_REQUEST,
                "filters": {COL_STATUS: STATUS_PENDING, COL_IS_DELETED: 0},
            },
        ),
    )
    return [
        row for row in rows
        if str(row.get(COL_HOLDER_AGENT_INSTANCE_ID) or "") != UNASSIGNED_HOLDER
    ]


def forwarded_before(row: dict[str, object], *, cutoff_iso: str) -> bool:
    """Was this stamped row forwarded before ``cutoff_iso`` (serve-timeout)?

    Compares the forward timestamp by VALUE via ``to_naive_utc``, NOT by ISO-8601
    spelling. This table's ``forwarded_at`` is a ``ColumnType.TEXT`` column
    (``completion_request_schema.py``), so the aware ``datetime.now(UTC).isoformat()``
    stamp is preserved verbatim and a lexical compare against the aware cutoff
    happens to sort right today — but that correctness is INCIDENTAL to the column
    type. Coercing both sides to a naive-UTC VALUE makes the compare correct
    regardless of naive/aware spelling (F-AISLOP), robust to a future column-type
    change, and consistent with the sibling ``deferred_vertex_queue.forwarded_before``
    sweep — WITHOUT falsely claiming to mirror it.

    A missing / empty stamp returns True (surface-on-anomaly): a stamped row ALWAYS
    carries a forward time, so a missing one is an anomaly that must not pin the
    request — requeue it (bounded, it caps out to a loud terminal ``failed``). A
    present-but-unparseable stamp is a genuine corruption — ``to_naive_utc`` raises
    (fail loud); the sweeper tick catches it, logs, and retries. It is unreachable
    via the ``isoformat()`` / ``""`` writers.
    """
    forwarded_at = row.get(COL_FORWARDED_AT)
    if not (isinstance(forwarded_at, str) and forwarded_at):
        return True
    return to_naive_utc(forwarded_at) < to_naive_utc(cutoff_iso)


# The standard soft-delete/audit column the ``SchemaStandardizer`` auto-injects on
# every table (so it is NOT declared in ``completion_request_schema.py``); it
# carries the terminal-flip timestamp the GC ages ``served``/``failed`` rows by.
COL_UPDATED_AT = "updated_at"


def _terminal_row_aged(row: dict[str, object], *, cutoff_iso: str) -> bool:
    """Is this terminal (``served``/``failed``) row older than ``cutoff_iso``?

    Compares ``updated_at`` (the terminal-flip timestamp) by VALUE via
    ``to_naive_utc``, mirroring ``deferred_vertex_queue.terminal_row_aged`` /
    ``forwarded_vertex_reconcile.terminal_row_aged`` in spirit — WITHOUT
    importing across the plugin boundary (each table's queue module owns its
    own comparator, same convention as ``forwarded_before`` above).

    A missing / empty ``updated_at`` returns False (never-delete-on-unknown-age):
    a DESTRUCTIVE GC must never reap a row whose age it cannot read. A
    present-but-unparseable stamp is a genuine corruption — ``to_naive_utc``
    raises (fail loud); the GC tick catches it, logs, and retries.
    """
    updated = row.get(COL_UPDATED_AT)
    if not (isinstance(updated, str) and updated):
        return False
    return to_naive_utc(updated) < to_naive_utc(cutoff_iso)


def gc_terminal_completion_requests(
    store: CompletionRequestStore,
    *,
    terminal_gc_after_seconds: int,
) -> int:
    """Hard-delete aged terminal (``served``/``failed``) completion-request rows.

    INF-08 retention rider — mirrors
    ``forwarded_vertex_reconcile.ForwardedVertexReconciler.gc_terminal_rows``'
    ``gc_terminal_rows``/``terminal_gc_after_seconds`` shape for the INF-02
    ``core__inference_completion_request`` queue, so terminal rows here do not
    grow unbounded either. Scoped to the two TERMINAL statuses only —
    ``pending`` rows (assigned or not) are live work and are never reaped
    here, matching the deferred-vertex queue's own terminal-only GC.

    Callers must self-isolate around this the same way
    ``ForwardedVertexReconciler`` does (never raises here so a caller wiring
    this into a sweeper tick still needs its own try/except per the existing
    convention) — kept bare since this module has no sweeper-tick composition
    seam of its own; the wiring lives with whichever caller schedules it.

    Returns the number of rows reaped.
    """
    cutoff = (
        datetime.now(UTC) - timedelta(seconds=terminal_gc_after_seconds)
    ).isoformat()
    reaped: list[str] = []
    for status in (STATUS_SERVED, STATUS_FAILED):
        rows = require_records(
            store.query_state(
                INFERENCE_COMPLETION_REQUEST_NAMESPACE,
                {
                    "table": TABLE_INFERENCE_COMPLETION_REQUEST,
                    "filters": {COL_STATUS: status, COL_IS_DELETED: 0},
                },
            ),
        )
        for row in rows:
            if not _terminal_row_aged(row, cutoff_iso=cutoff):
                continue
            request_id = str(row.get(COL_REQUEST_ID) or "")
            if request_id:
                reaped.append(request_id)
    for request_id in reaped:
        require_completed(
            store.delete_records(
                INFERENCE_COMPLETION_REQUEST_NAMESPACE,
                {
                    "table": TABLE_INFERENCE_COMPLETION_REQUEST,
                    "filters": {COL_REQUEST_ID: request_id},
                    "soft_delete": False,
                },
            ),
            "hard-delete completion request",
        )
    if reaped:
        logger.info(
            "INF-08 terminal-row GC: reaped %d aged completion-request rows",
            len(reaped),
        )
    return len(reaped)


__all__ = [
    "REQUEUE_FAILED_TERMINAL",
    "REQUEUE_LOST_RACE",
    "REQUEUE_REQUEUED",
    "SERVE_ALREADY_FAILED",
    "SERVE_ALREADY_SERVED",
    "SERVE_SERVED",
    "SERVE_UNKNOWN_REQUEST",
    "UNASSIGNED_HOLDER",
    "CompletionForwarder",
    "CompletionRequestStore",
    "clear_stamp_after_failed_forward",
    "forwarded_before",
    "gc_terminal_completion_requests",
    "insert_completion_request",
    "pending_stamped_requests",
    "pending_unassigned_requests",
    "read_completion_request",
    "requeue_stale_assignment",
    "require_completion_request_store",
    "serialize_messages",
    "serve_completion_request",
]
