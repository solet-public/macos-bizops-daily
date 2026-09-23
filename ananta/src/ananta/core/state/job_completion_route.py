"""Where a finished job's completion should be DELIVERED, read off its flow.

Companion to :mod:`ananta.core.state.job_completion_reach`, which records how a
completion *could* be reached. This module answers the next question: when the
originating dispatch named a durable role, the completion is pushed into that
role's inbox instead of being left for the drain.

**Why a push exists at all.** A ``solet call`` bridge mints a fresh session per
open, so a job's ``session_id`` is one-shot and useless for durable delivery.
A FLEET caller, though, exports ``AGENT_SESSION_ID``; the bridge rides it,
the server resolves it against the peer registry, and
``PlatformSurface._build_process_call_trigger_data`` stamps the resolved durable
role onto the flow as :data:`COMPLETION_ROUTE_ROLE_KEY`. That stamp is this
module's only input — recovered at completion time from the same flow row the
reach stamp already reads.

**Coverage is partial by construction.** Fleet callers only. A bare-shell CLI
call carries no key, resolves to no role, and leaves the flow unstamped; those
completions keep the pull path and the unreached marker. Any claim of full
coverage would be false.

**Presence is permission.** The writer omits the key entirely for a roleless
caller rather than writing "", so a present key always names a role that was
actually resolved. Nothing here guesses a destination.
"""

from __future__ import annotations

import json
import logging
from typing import Final

from ananta.core.domain.enums import ActionStatus
from ananta.core.domain.status import is_status_match
from ananta.interfaces.state_service_protocol import StateServiceProtocol

logger = logging.getLogger(__name__)

FLOWS_TABLE: Final[str] = "flows"
"""Core flow-ledger table carrying the routing stamp."""

COMPLETION_ROUTE_ROLE_KEY: Final[str] = "completion_route_role"
"""``trigger_data`` key naming the durable role to deliver this completion to.

Restated here rather than imported: the WRITER is
``agent_messaging_plugin.platform_surface`` (which owns the canonical
definition), and core does not import from a plugin. Same split, and the same
reason, as ``job_completion_reach.BRIDGE_PROCESS_CALL_TRIGGER``.
"""

COMPLETION_DELIVERY_PROCESS_KEY: Final[str] = (
    "plugin::agent_messaging_plugin::deliver_job_completion"
)
"""The verb that performs the delivery and stamps the delivered reach value.

Core submits it; it does NOT deliver inline. The outcome of a role dispatch is
only knowable where the dispatch happens, so the verb owns both the send and
the stamp that attests it — an action SUBMISSION would otherwise be recorded as
a delivery it cannot have observed.
"""


def read_completion_route_role(
    state_service: StateServiceProtocol, namespace: str, flow_id: str | None
) -> str:
    """The durable role this flow's completion routes to, or "" for none.

    Degrade-silent and loud in the log: an unreadable flow, an unparseable
    ``trigger_data``, or an absent key all mean "no push route", which falls
    back to the existing continuation + drain behaviour. A routing decision is
    never inferred from a failed read.
    """
    if not flow_id:
        return ""
    try:
        result = state_service.read_state(
            namespace=namespace,
            query={"table": FLOWS_TABLE, "filters": {"id": flow_id}, "limit": 1},
        )
    except Exception:  # noqa: BLE001 — no route is the safe answer; never break a completion
        logger.warning(
            "flow %s unreadable for completion routing; no push route", flow_id,
            exc_info=True,
        )
        return ""
    if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
        return ""
    data = result.get("data")
    if not isinstance(data, dict):
        return ""
    records = data.get("records")
    if not isinstance(records, list) or not records:
        return ""
    record = records[0]
    if not isinstance(record, dict):
        return ""
    trigger = _decode_trigger_data(record.get("trigger_data"))
    role = trigger.get(COMPLETION_ROUTE_ROLE_KEY)
    return role if isinstance(role, str) else ""


def _decode_trigger_data(raw: object) -> dict[str, object]:
    """``trigger_data`` as a dict — it is a TEXT column holding JSON."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("trigger_data is not valid JSON; no push route")
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def resolve_route(
    state_service: StateServiceProtocol,
    namespace: str,
    metadata: dict[str, object],
    status: str,
) -> tuple[str, str] | None:
    """``(role, flow_id)`` when this completion should be pushed, else None.

    Lives here rather than on ``AsyncJobManager`` for the reason its sibling
    module records: that class is the tree's standing maintainability-index
    debt, and this lane declined to deepen it.

    None covers every "no push" reason — a non-routable status, an unreadable
    job, a flow with no routing stamp — because they all take the same
    fallback: the pre-existing continuation. ``cancelled`` is deliberately NOT
    routable: it is terminal but has no continuation to replace, so pushing one
    would be new behaviour rather than a redirection of existing behaviour.
    """
    if status not in {"completed", "error"} or not metadata:
        return None
    flow_id = metadata.get("flow_id")
    if not isinstance(flow_id, str) or not flow_id:
        return None
    role = read_completion_route_role(state_service, namespace, flow_id)
    return (role, flow_id) if role else None


def build_delivery_action(
    *,
    role: str,
    job_id: str,
    provider_name: str,
    status: str,
    payload: dict[str, object] | None,
    flow_id: str,
    session_id: str | None,
) -> dict[str, object]:
    """The action definition that delivers this completion to ``role``.

    The envelope carries ``job_id``, the originating ``provider_name``
    (``plugin.verb``), the terminal ``status`` and the attached payload, so the
    recipient can act on the message itself. Names route, content binds: a
    notification that only said "a job finished" would force the reader into
    exactly the second lookup this lane exists to remove.
    """
    return {
        "name": f"deliver_job_completion_{job_id}",
        "description": f"Deliver job {job_id} completion to role {role}",
        "process_key": COMPLETION_DELIVERY_PROCESS_KEY,
        "process": {
            "provider_type": "plugin",
            "provider": "agent_messaging_plugin",
            "function_name": "deliver_job_completion",
        },
        "arguments": {
            "name": role,
            "job_id": job_id,
            "provider_name": provider_name,
            "status": status,
            "payload": payload or {},
        },
        "notes": f"Async job {job_id} {status} routed to role {role}",
        "flow_id": flow_id,
        **({"session_id": session_id} if session_id else {}),
    }
