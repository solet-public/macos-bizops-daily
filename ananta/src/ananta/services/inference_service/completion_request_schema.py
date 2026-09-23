"""Durable schema for the InferenceService completion-request queue (INF-02).

One ``core``-namespace table, ``inference_completion_request``, owned by
:class:`~ananta.services.inference_service.InferenceService` (a core service →
a ``core__*`` table). It is the durable half of the autonomic-routed completion
surface: a planning completion request carries its own assembled ``messages``
(no §D.4.1 cold-context assembly on this surface), is forwarded to the live
``sys:autonomic`` holder when one exists, and otherwise waits unassigned —
durable DEFER, NO loss — until the first-claim drain re-serves it.

This table deliberately MIRRORS ``core__inference_deferred_vertex`` (schema.py)
but is a SEPARATE table with its own per-type handler: the Reviewer-A INF-02
rider is explicit — share the Trigger-1 drain MECHANISM, not the table; no
union-typed rows.

Standard fields (``id``, ``created_at``, ``updated_at``, ``is_deleted`` …) are
auto-injected by the ``SchemaStandardizer`` and MUST NOT be declared here;
``created_at`` gives the FIFO order the first-claim drain forwards in.

Status lifecycle (predicated-CAS via ``update_state`` rows-affected):

- ``pending`` + empty holder stamp — unassigned durable request (vacancy DEFER)
- ``pending`` + holder stamp + ``forwarded_at`` — in flight with a holder
- ``served`` + ``result_text`` — terminal; the serve verb submits the resume
- ``failed`` + ``failure_reason`` — terminal, loud (requeue attempts exhausted)

A completion must never hang pending forever on a dead holder: holder-death
and serve-timeout transitions clear the stamp (re-queue, attempts+1) so the
next claim's drain re-serves, failing terminally only at the attempts cap.
"""

from __future__ import annotations

from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    IndexDefinition,
    SchemaDefinition,
    TableSchema,
)

# InferenceService is a core service → its durable table lives in ``core``.
INFERENCE_COMPLETION_REQUEST_NAMESPACE = "core"
TABLE_INFERENCE_COMPLETION_REQUEST = "inference_completion_request"
ID_PREFIX_COMPLETION_REQUEST = "icr"

COL_REQUEST_ID = "request_id"
COL_PURPOSE = "purpose"
COL_RESUME_PROCESS_KEY = "resume_process_key"
COL_CORRELATION = "correlation"
COL_MESSAGES = "messages"
COL_STATUS = "status"
COL_HOLDER_AGENT_INSTANCE_ID = "holder_agent_instance_id"
COL_FORWARDED_AT = "forwarded_at"
COL_ATTEMPTS = "attempts"
COL_RESULT_TEXT = "result_text"
COL_FAILURE_REASON = "failure_reason"
# Standard auto-injected soft-delete column — filtered on to enumerate live rows.
COL_IS_DELETED = "is_deleted"

STATUS_PENDING = "pending"
STATUS_SERVED = "served"
STATUS_FAILED = "failed"

# The serialized ``messages`` payload cap. Planning contexts are
# snapshot-compacted upstream so a legitimate request stays far below this;
# an over-cap payload is a typed submit-time rejection (never a silent
# truncation — a truncated prompt is a corrupted completion).
MESSAGES_PAYLOAD_MAX_CHARS = 262_144

# Re-queue ceiling: a request re-queued this many times without a served
# transition fails terminally (loud). Keeps the never-hang-forever guarantee
# honest without letting a poisoned request cycle indefinitely.
MAX_REQUEUE_ATTEMPTS = 5


def get_inference_completion_request_schema() -> SchemaDefinition:
    """Declarative schema for ``core__inference_completion_request`` (state-interface).

    Registered via ``CoreSchemaDefinitions.get_all_core_schemas`` (deferred
    import, matching the deferred-vertex pattern) so it is created at startup.
    """
    return SchemaDefinition(
        namespace=INFERENCE_COMPLETION_REQUEST_NAMESPACE,
        tables={
            TABLE_INFERENCE_COMPLETION_REQUEST: TableSchema(
                table_name=TABLE_INFERENCE_COMPLETION_REQUEST,
                id_prefix=ID_PREFIX_COMPLETION_REQUEST,
                description=(
                    "INF-02 autonomic-routed completion requests (durable "
                    "request/response queue). One row per request_id; "
                    "forwarded to the live sys:autonomic holder or held "
                    "unassigned until the first-claim drain re-serves it."
                ),
                columns={
                    COL_REQUEST_ID: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description=(
                            "Idempotency + serve key (icr-…). UNIQUE: the "
                            "serve CAS and the resume continuation both key "
                            "on this."
                        ),
                    ),
                    COL_PURPOSE: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description=(
                            "The requesting surface's completion purpose "
                            "(e.g. playbook_planning) — observability + "
                            "per-purpose resume dispatch audit."
                        ),
                    ),
                    COL_RESUME_PROCESS_KEY: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description=(
                            "The consumer's resume process key (e.g. "
                            "service_interface::thinking_service::"
                            "resume_thinking_completion). The serve verb "
                            "submits this as a deterministic continuation — "
                            "agent_messaging stays consumer-agnostic."
                        ),
                    ),
                    COL_CORRELATION: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description=(
                            "JSON correlation payload the consumer needs to "
                            "resume (session/flow/context ids). Opaque to "
                            "the queue; platform-owned, never caller-forged "
                            "at resume time."
                        ),
                    ),
                    COL_MESSAGES: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description=(
                            "JSON-serialized messages payload (the request "
                            "carries its own assembled prompt — no "
                            "cold-context assembly on this surface). "
                            "Size-capped at submit."
                        ),
                    ),
                    COL_STATUS: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        check=(
                            f"{COL_STATUS} IN ('{STATUS_PENDING}', "
                            f"'{STATUS_SERVED}', '{STATUS_FAILED}')"
                        ),
                        description=(
                            "pending → served/failed via predicated-CAS "
                            "(update_state rows-affected)."
                        ),
                    ),
                    COL_HOLDER_AGENT_INSTANCE_ID: ColumnDefinition(
                        type=ColumnType.TEXT,
                        description=(
                            "The sys:autonomic holder the request was "
                            "forwarded to; NULL/empty while unassigned. "
                            "Cleared (re-queue) on holder-death and "
                            "serve-timeout."
                        ),
                    ),
                    COL_FORWARDED_AT: ColumnDefinition(
                        type=ColumnType.TEXT,
                        description=(
                            "ISO-8601 forward timestamp — the serve-timeout "
                            "sweep measures the serve window from this."
                        ),
                    ),
                    COL_ATTEMPTS: ColumnDefinition(
                        type=ColumnType.INTEGER,
                        not_null=True,
                        description=(
                            "Re-queue counter (holder-death / serve-timeout). "
                            "Fails terminally at MAX_REQUEUE_ATTEMPTS."
                        ),
                    ),
                    COL_RESULT_TEXT: ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="The served completion text (terminal).",
                    ),
                    COL_FAILURE_REASON: ColumnDefinition(
                        type=ColumnType.TEXT,
                        description=(
                            "Why the request failed terminally (attempts "
                            "exhausted / operator action). NULL otherwise."
                        ),
                    ),
                },
                indexes=[
                    IndexDefinition(
                        name="idx_inference_completion_request_request_id",
                        columns=[COL_REQUEST_ID],
                        unique=True,
                    ),
                    IndexDefinition(
                        name="idx_inference_completion_request_status",
                        columns=[COL_STATUS],
                    ),
                    IndexDefinition(
                        name="idx_inference_completion_request_holder",
                        columns=[COL_HOLDER_AGENT_INSTANCE_ID],
                    ),
                ],
            ),
        },
    )
