"""How a finished job's completion can be reached, stamped onto the job row.

Measured 2026-08-14: a job completion never returns over the bridge that
dispatched it. ``AsyncJobManager._handle_completion_actions`` submits the
plugin's configured continuation — every async plugin in this tree names
``service_interface::inference_service::process_results`` / ``process_error``
— whose only egress is a ``post_message`` into an IO channel. A flow created
by ``bridge_process_call`` (the local CLI and every direct ``/process/call``)
has no such channel, so its completion is reachable ONLY by reading the job
row back. ``service_interface::job_service::list_unreached_job_completions``
is the reader that finds those rows; this module writes what it reads.

The stamp names that measured condition and nothing more — which kind of
dispatch created the flow. It is never a claim about who was listening.

Kept out of ``async_job_manager.py`` deliberately: that module is a
pre-existing radon-MI C file (8.70 at 028adabb1, measured before this work),
and adding to it would deepen a debt this lane did not create.
"""

from __future__ import annotations

import json
import logging

from ananta.core.domain.enums import ActionStatus
from ananta.core.domain.status import is_status_match
from ananta.interfaces.state_service_protocol import StateServiceProtocol

logger = logging.getLogger(__name__)

FLOWS_TABLE = "flows"
"""Core flow-ledger table read to classify a job's originating dispatch."""

BRIDGE_PROCESS_CALL_TRIGGER = "bridge_process_call"
"""``trigger_type`` stamped by the bridge's ``/process/call`` surface.

Set in ``PlatformSurface._submit_process_call`` — the local ``solet call`` CLI
and every direct MCP ``process_call`` create their flow this way.
"""

COMPLETION_REACH_KEY = "completion_reach"
"""Job-metadata key carrying the completion-reach stamp (values below)."""

REACH_BRIDGE_DISPATCH_NO_RETURN_PATH = "bridge_dispatch_no_return_path"
"""The job's flow came from a bridge dispatch, which has no completion channel."""

REACH_CHANNEL_FLOW = "channel_flow"
"""The job's flow came from a channel a completion continuation can post to."""

REACH_ROLE_INBOX_DELIVERED = "role_inbox_delivered"
"""The completion was handed to the durable role-addressed inbox (Lane W).

Written ONLY after a measured successful hand-off to the persist-first role
dispatch — never on submission alone. A hand-off that fails falls back to
:data:`REACH_BRIDGE_DISPATCH_NO_RETURN_PATH`, so a lost push always leaves the
drain marker behind and a reader can never lose both.

``list_unreached_job_completions`` needs no change to accommodate this value:
its predicate is exact equality against
:data:`REACH_BRIDGE_DISPATCH_NO_RETURN_PATH`, not a presence test, so a
delivered row drops out of the drain by construction.
"""


def classify_reach(trigger_type: str) -> str:
    """Map a flow's ``trigger_type`` onto a completion-reach value."""
    if trigger_type == BRIDGE_PROCESS_CALL_TRIGGER:
        return REACH_BRIDGE_DISPATCH_NO_RETURN_PATH
    return REACH_CHANNEL_FLOW


def read_flow_trigger_type(
    state_service: StateServiceProtocol, namespace: str, flow_id: str
) -> str | None:
    """The flow's ``trigger_type``, or None when the row cannot be read."""
    result = state_service.read_state(
        namespace=namespace,
        query={"table": FLOWS_TABLE, "filters": {"id": flow_id}, "limit": 1},
    )
    if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
        return None
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    records = data.get("records")
    if not isinstance(records, list) or not records:
        return None
    record = records[0]
    if not isinstance(record, dict):
        return None
    trigger_type = record.get("trigger_type")
    return trigger_type if isinstance(trigger_type, str) else None


def write_job_metadata(
    state_service: StateServiceProtocol,
    namespace: str,
    table: str,
    job_id: str,
    metadata: dict[str, object],
) -> None:
    """Persist a job's metadata column, serialized as ``create_job`` writes it."""
    result = state_service.update_state(
        namespace=namespace,
        query={"table": table, "filters": {"id": job_id}},
        updates={"metadata": json.dumps(metadata)},
    )
    if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
        raise RuntimeError(f"metadata update did not complete: {result!r}")


def record_completion_reach(
    state_service: StateServiceProtocol,
    namespace: str,
    table: str,
    job_id: str,
    metadata: dict[str, object],
) -> None:
    """Stamp ``metadata[COMPLETION_REACH_KEY]`` for a job reaching terminal state.

    Best-effort BY DESIGN, and loud: a stamp that cannot be written must not
    block the delivery it describes, so a failure logs and leaves the key
    ABSENT. Absent means unmeasured — never read it as reachable.

    Callers stamp BEFORE submitting the completion continuation, so that a job
    whose continuation raises (the doctrine's second ledger gap: a row that
    reads completed while nothing was submitted) is still findable.
    """
    try:
        flow_id = metadata.get("flow_id")
        if not isinstance(flow_id, str) or not flow_id:
            logger.info(
                "job %s has no flow_id in metadata; completion reach not stamped",
                job_id,
            )
            return
        trigger_type = read_flow_trigger_type(state_service, namespace, flow_id)
        if trigger_type is None:
            logger.warning(
                "flow %s for job %s is unreadable; completion reach left unstamped",
                flow_id,
                job_id,
            )
            return
        write_job_metadata(
            state_service,
            namespace,
            table,
            job_id,
            {**metadata, COMPLETION_REACH_KEY: classify_reach(trigger_type)},
        )
    except Exception:  # noqa: BLE001 — never block a completion on its own stamp
        logger.error(
            "failed to stamp completion reach for job %s; the key stays absent "
            "(unmeasured), delivery continues",
            job_id,
            exc_info=True,
        )
