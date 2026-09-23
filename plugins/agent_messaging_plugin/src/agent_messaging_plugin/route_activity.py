"""REL-05 model-activity route classification + the stamp middleware.

The consumption signal (§4.3) is: an owed IMPORTANT send counts as CONSUMED once
the recipient's session performs any MODEL-INITIATED bridge operation after the
emission. This module is the SINGLE source of truth for which bridge routes are
model-initiated (only a live turn can invoke them) vs forwarder/infrastructure
(the bridge subprocess polls them with no model turn). The two sets are an
EXHAUSTIVE partition of the bridge route table — smoke S4 iterates the ACTUAL
FastAPI routes and asserts every one is classified into exactly one set, so a
future route addition FAILS the gate until deliberately classified (kills the
drift class permanently: a mis-classified infra route silently kills the
insurance — R2).

F1 (existential): ``peer/register`` is INFRA, not model activity — the forwarder
auto-invokes it on every attach AND every reconnect with NO model turn
(``forwarder.open_bridge`` / ``_reconnect`` → ``_register_identity``). Had it
stamped, every deploy-flap reconnect would auto-consume every owed row, silently
killing the insurance for exactly the reconnect-strand class. The accepted cost:
a genuinely model-initiated ``/rename`` (which calls peer_register) does not
stamp — at most one redundant re-emit, the false-negative-only asymmetry §4.3
already accepts.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from .http_routes import API_PREFIX

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response

    from .bridge_sessions import BridgeSessionManager
    from .peer_registry import PeerRegistry

logger = logging.getLogger(__name__)

_P = API_PREFIX

# Model-initiated: only a live turn invokes these. Stamp last_model_activity_at.
MODEL_INITIATED_ROUTES: frozenset[str] = frozenset(
    {
        f"{_P}/{{bridge_id}}/process/search",
        f"{_P}/{{bridge_id}}/process/schema",
        f"{_P}/{{bridge_id}}/process/call",
        f"{_P}/{{bridge_id}}/process/result/{{action_id}}",
        f"{_P}/{{bridge_id}}/download/{{blob_id}}",
        f"{_P}/{{bridge_id}}/peer/list",
        f"{_P}/{{bridge_id}}/peer/send",
        f"{_P}/{{bridge_id}}/peer/send_by_name",
        f"{_P}/{{bridge_id}}/peer/inbox",
        f"{_P}/{{bridge_id}}/current_identity",
        f"{_P}/{{bridge_id}}/agent/thread/open",
        f"{_P}/{{bridge_id}}/agent/{{thread_id}}/send",
        f"{_P}/{{bridge_id}}/agent/{{thread_id}}/messages",
        f"{_P}/{{bridge_id}}/agent/{{thread_id}}/status",
        f"{_P}/{{bridge_id}}/agent/{{thread_id}}/close",
    },
)

# Forwarder / infrastructure: the bridge subprocess polls these with no model
# turn. NEVER stamp (F1 for peer/register; the drain/delivered pair is the
# repair loop; open/events/close/health are lifecycle).
INFRA_ROUTES: frozenset[str] = frozenset(
    {
        f"{_P}/open",
        f"{_P}/{{bridge_id}}/close",
        f"{_P}/{{bridge_id}}/events",
        f"{_P}/{{bridge_id}}/peer/register",
        # F1's sibling: the forwarder claims its standing role on open, on
        # reconnect, and whenever a steady-state re-assert finds the binding
        # gone — all with NO model turn. That claim used to travel the
        # MODEL_INITIATED ``/process/call`` route, so it stamped model activity
        # every ~176s forever and marked owed IMPORTANT wakes to an idle session
        # consumed. A genuinely model-initiated claim (the ``/rename`` skill)
        # still goes through ``/process/call`` and still stamps, which is
        # correct — the two transports exist precisely to tell those cases
        # apart. Both share one body; see :mod:`role_claim`.
        f"{_P}/{{bridge_id}}/peer/claim_role",
        f"{_P}/{{bridge_id}}/peer/drain",
        f"{_P}/{{bridge_id}}/peer/delivered",
        f"{_P}/health",
    },
)


def classify_route(path_template: str) -> str | None:
    """Return ``"model"`` / ``"infra"`` for a bridge route template, else ``None``.

    ``None`` means the template is not a classified bridge route (e.g. a
    streamable-transport mount or a default docs route) — S4 scopes its
    total-classification assertion to templates under :data:`API_PREFIX`.
    """
    if path_template in MODEL_INITIATED_ROUTES:
        return "model"
    if path_template in INFRA_ROUTES:
        return "infra"
    return None


_MODEL_ROUTE_REGEXES: tuple[re.Pattern[str], ...] = tuple(
    re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", tmpl) + "$")
    for tmpl in MODEL_INITIATED_ROUTES
)
_BRIDGE_ID_RE: re.Pattern[str] = re.compile(rf"^{re.escape(_P)}/(?P<bridge_id>[^/]+)/")


def is_model_initiated_path(concrete_path: str) -> bool:
    """True iff a concrete request path matches a model-initiated route template."""
    return any(rx.match(concrete_path) for rx in _MODEL_ROUTE_REGEXES)


def bridge_id_from_path(concrete_path: str) -> str | None:
    """Extract the ``bridge_id`` path segment from a bridge-scoped request path."""
    match = _BRIDGE_ID_RE.match(concrete_path)
    return match.group("bridge_id") if match is not None else None


def stamp_model_activity_for_bridge(
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    bridge_id: str,
) -> None:
    """Stamp the in-memory session (authoritative) + mirror to the binding.

    The in-memory ``BridgeSessionState`` stamp is what the drain-time
    consumption reconcile reads, so it is authoritative and always runs. The
    durable ``peer_binding`` mirror is best-effort (for the server-side sweep /
    diagnostics): a mirror fault is loud but never fails the model route.
    """
    bridge = bridge_manager.get(bridge_id)
    if bridge is None or bridge.agent_instance_id is None:
        return
    stamp = bridge.stamp_model_activity()
    try:
        peer_registry.stamp_model_activity(bridge.agent_instance_id, stamp)
    except Exception:  # noqa: BLE001 — the mirror is best-effort; the in-memory stamp is authoritative
        logger.warning(
            "model-activity mirror failed for bridge %s (in-memory stamp kept)",
            bridge_id,
            exc_info=True,
        )


def make_model_activity_middleware(
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
) -> Callable[
    [Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]
]:
    """Build the ASGI-http middleware that stamps model activity on model routes.

    Runs after the handler: if the request path is a model-initiated bridge
    route, stamp the calling bridge's model-activity timestamp. Every other route
    (forwarder/infra) is left untouched — the forwarder's perpetual polling must
    NOT read as the model turning. The invocation itself is the activity signal,
    so the stamp fires regardless of the handler's status code (a rejected call
    is still a call the model made); a stale/gone bridge simply no-ops.
    """

    async def model_activity_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        path = request.url.path
        if is_model_initiated_path(path):
            bridge_id = bridge_id_from_path(path)
            if bridge_id is not None:
                stamp_model_activity_for_bridge(
                    bridge_manager, peer_registry, bridge_id,
                )
        return response

    return model_activity_middleware


__all__ = [
    "INFRA_ROUTES",
    "MODEL_INITIATED_ROUTES",
    "bridge_id_from_path",
    "classify_route",
    "is_model_initiated_path",
    "make_model_activity_middleware",
    "stamp_model_activity_for_bridge",
]
