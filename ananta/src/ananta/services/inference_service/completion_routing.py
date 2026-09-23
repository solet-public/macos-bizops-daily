"""INF-02 completion-request routing (session-PRIMARY precedence).

The routing half behind ``InferenceService.submit_completion_request``,
extracted so the service class stays a thin wrapper (the same shape as
:mod:`~ananta.services.inference_service.deferred_vertex_queue` /
:mod:`~ananta.services.inference_service.completion_request_queue`):

1. live ``sys:autonomic`` holder with the forward capability → durable row,
   stamp-CAS, forward event — verdict ``session``;
2. no live holder + operator-enabled provider fallback + bound provider →
   verdict ``provider_fallback`` (the CALLER runs its own synchronous
   provider path — prompt/model policy stays the consumer's);
3. otherwise → durable unassigned row — verdict ``deferred``; the
   first-claim drain re-serves it. NEVER silently dropped, NEVER an
   implicit local-model fallback (the ◆R2 asymmetry, completion edition).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ananta.services.inference_service.completion_request_queue import (
    CompletionForwarder,
    clear_stamp_after_failed_forward,
    insert_completion_request,
    read_completion_request,
    stamp_for_forward,
)
from ananta.services.inference_service.completion_request_schema import (
    COL_HOLDER_AGENT_INSTANCE_ID,
    COL_STATUS,
    STATUS_PENDING,
)
from ananta.services.inference_service.vertex_resolver import VertexRouting

if TYPE_CHECKING:
    from ananta.services.inference_service.completion_request_queue import (
        CompletionRequestStore,
    )
    from ananta.services.inference_service.vertex_resolver import VertexResolution

logger = logging.getLogger(__name__)

# Routing verdict tokens. The consumer's contract per token:
# SESSION  — forwarded to the live sys:autonomic holder; return a deferred
#            ActionResult and await the resume continuation.
# DEFERRED — durably queued unassigned (no live holder / forward failed /
#            holder lacks the forward capability); same consumer contract —
#            the first-claim drain re-serves it.
# PROVIDER_FALLBACK — the operator explicitly enabled the provider fallback
#            AND a provider is bound: the CALLER runs its existing
#            synchronous provider path.
COMPLETION_ROUTED_SESSION = "session"
COMPLETION_ROUTED_DEFERRED = "deferred"
COMPLETION_ROUTED_PROVIDER_FALLBACK = "provider_fallback"

# The bound provider serves autonomic-routed completions ONLY when the
# operator opts in ("the API model in this role needs to be an optional
# component" — config default OFF). Same env-var config surface as
# ANANTA_INFERENCE_PROVIDER.
COMPLETION_PROVIDER_FALLBACK_ENV = "ANANTA_COMPLETION_PROVIDER_FALLBACK"
_ENV_TRUE_VALUES = frozenset({"1", "true", "yes"})


def completion_provider_fallback_enabled() -> bool:
    """Is the optional bound-provider fallback for completions opted in?"""
    import os

    raw = os.environ.get(COMPLETION_PROVIDER_FALLBACK_ENV, "")
    return raw.strip().lower() in _ENV_TRUE_VALUES


def route_completion_request(
    *,
    resolution: VertexResolution,
    store: CompletionRequestStore,
    provider_bound: bool,
    purpose: str,
    messages: list[dict[str, str]],
    resume_process_key: str,
    correlation: dict[str, str],
) -> dict[str, object]:
    """Route one completion request per the session-PRIMARY precedence."""
    if resolution.routing is VertexRouting.PROVIDER:
        forwarder = resolution.provider
        if isinstance(forwarder, CompletionForwarder):
            return _forward_to_holder(
                forwarder,
                store=store,
                purpose=purpose,
                messages=messages,
                resume_process_key=resume_process_key,
                correlation=correlation,
            )
        logger.warning(
            "completion request (purpose=%s): sys:autonomic holder's "
            "provider lacks forward_completion_request — queuing durable "
            "unassigned instead", purpose,
        )
    elif completion_provider_fallback_enabled() and provider_bound:
        logger.info(
            "completion request (purpose=%s): no live sys:autonomic holder; "
            "operator-enabled provider fallback serves it synchronously (%s)",
            purpose, COMPLETION_PROVIDER_FALLBACK_ENV,
        )
        return {"routing": COMPLETION_ROUTED_PROVIDER_FALLBACK}
    request_id = insert_completion_request(
        store,
        purpose=purpose,
        resume_process_key=resume_process_key,
        correlation=correlation,
        messages=messages,
    )
    logger.warning(
        "completion request %s (purpose=%s) DEFERRED durably: no live "
        "sys:autonomic holder — re-served on the next claim's drain",
        request_id, purpose,
    )
    return {"routing": COMPLETION_ROUTED_DEFERRED, "request_id": request_id}


def _forward_to_holder(
    forwarder: CompletionForwarder,
    *,
    store: CompletionRequestStore,
    purpose: str,
    messages: list[dict[str, str]],
    resume_process_key: str,
    correlation: dict[str, str],
) -> dict[str, object]:
    """Persist + stamp + forward one request to the live holder.

    Durability FIRST: the row lands before the forward event, so a bridge
    append racing the holder's disconnect loses nothing — the stamp is
    cleared and the row waits unassigned for the drain.
    """
    holder = forwarder.agent_instance_id
    request_id = insert_completion_request(
        store,
        purpose=purpose,
        resume_process_key=resume_process_key,
        correlation=correlation,
        messages=messages,
    )
    if not stamp_for_forward(
        store, request_id=request_id, holder_agent_instance_id=holder,
    ):
        # Reviewer-A F1: the serve-timeout sweeper runs on its OWN thread
        # and its drain enumerates ALL unassigned pending rows — including
        # this fresh one in the window between insert and this CAS. A lost
        # stamp means a concurrent drain already forwarded the request; the
        # request is owned and in flight — report honestly, NEVER raise
        # (raising here failed the planning turn on a self-healing race).
        return _lost_stamp_verdict(store, request_id=request_id, purpose=purpose)
    try:
        forwarder.forward_completion_request(
            request_id=request_id,
            purpose=purpose,
            messages=messages,
            correlation=correlation,
        )
    except Exception:  # noqa: BLE001 — forward fault → durable DEFER, never a lost request
        clear_stamp_after_failed_forward(
            store, request_id=request_id, holder_agent_instance_id=holder,
        )
        logger.warning(
            "completion request %s (purpose=%s): forward to holder %s "
            "FAILED — re-queued unassigned for the next claim's drain",
            request_id, purpose, holder, exc_info=True,
        )
        return {"routing": COMPLETION_ROUTED_DEFERRED, "request_id": request_id}
    logger.info(
        "completion request %s (purpose=%s) forwarded to sys:autonomic "
        "holder %s", request_id, purpose, holder,
    )
    return {
        "routing": COMPLETION_ROUTED_SESSION,
        "request_id": request_id,
        "holder_agent_instance_id": holder,
    }


def _lost_stamp_verdict(
    store: CompletionRequestStore,
    *,
    request_id: str,
    purpose: str,
) -> dict[str, object]:
    """The verdict after a concurrent drain won the fresh row's stamp (F1).

    Re-read the row: still pending + stamped → the drain forwarded it to a
    live holder, so the submit reports ``session`` with THAT holder;
    anything else (served already, re-queued, unreadable) → ``deferred`` —
    the durable queue owns the request either way, nothing is lost and
    nothing is double-forwarded.
    """
    row = read_completion_request(store, request_id=request_id)
    if row is not None:
        holder = str(row.get(COL_HOLDER_AGENT_INSTANCE_ID) or "")
        if str(row.get(COL_STATUS) or "") == STATUS_PENDING and holder:
            logger.info(
                "completion request %s (purpose=%s): a concurrent drain "
                "stamped + forwarded it to %s before the submit path could — "
                "reporting session", request_id, purpose, holder,
            )
            return {
                "routing": COMPLETION_ROUTED_SESSION,
                "request_id": request_id,
                "holder_agent_instance_id": holder,
            }
    logger.warning(
        "completion request %s (purpose=%s): lost the fresh-row stamp and "
        "the row is not stamped-pending on re-read — the durable queue owns "
        "it (drain/serve lifecycle)", request_id, purpose,
    )
    return {"routing": COMPLETION_ROUTED_DEFERRED, "request_id": request_id}


__all__ = [
    "COMPLETION_PROVIDER_FALLBACK_ENV",
    "COMPLETION_ROUTED_DEFERRED",
    "COMPLETION_ROUTED_PROVIDER_FALLBACK",
    "COMPLETION_ROUTED_SESSION",
    "completion_provider_fallback_enabled",
    "route_completion_request",
]
