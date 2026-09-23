"""Per-session inference vertex backed by an MCP-connected coding-agent.

Implements the `InferenceServiceAPI` ABC (Pattern B per
`workbench/2026-06-13_coding_agent_inference_interface_design_v4.md` §2).
Routes the two vertex-routed methods (`process_error`, `process_results`)
to a `bridge_delivery_*` event on the bound bridge so the coding-agent
session can act on the error/result context. `propose_name` is a
defense-in-depth stub raising `NotImplementedError` to satisfy the ABC
contract; the wrapper at `inference_service/__init__.py:280-282` calls
`propose_name` directly on the bound plugin (default_inference_plugin),
not through any per-session vertex, so the stub is unreachable in
production routing (v4 §2.3).

The provider is constructed once per stdio peer_register where the
caller passes `provides_inference=True`. It is owned by the
`AgentMessagingPlugin._inference_providers` sidecar keyed by
`agent_instance_id` (v4 §4); the wrapper resolves it via
`AgentMessagingPlugin.get_inference_provider(agent_instance_id)` (v4 §5).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from ananta.core.domain.enums import ActionStatus
from ananta.services.inference_service.interfaces.public import InferenceServiceAPI

if TYPE_CHECKING:
    from ananta.core.domain.types import ActionResult

    from .bridge_sessions import BridgeSessionManager


logger = logging.getLogger(__name__)


# Module-level RELOAD_SAFE marker — pure class adapter, no module-level
# mutable state, no background threads.
RELOAD_SAFE = True


_EVENT_TYPE_RESULT = "bridge_delivery_result"
_EVENT_TYPE_ERROR = "bridge_delivery_error"
# INF-02: a completion REQUEST carried to the sys:autonomic holder (its own
# typed event — distinct from the two vertex delivery events above so the
# session client can dispatch on it). The payload names the serve verb the
# holder calls back with the completion text.
_EVENT_TYPE_COMPLETION_REQUEST = "inference_completion_request"
SERVE_COMPLETION_PROCESS_KEY = (
    "plugin::agent_messaging_plugin::submit_autonomic_completion"
)


class SessionInferenceProvider(InferenceServiceAPI):
    """Inference vertex backed by an MCP-connected coding-agent session.

    Implements 2 vertex-routed methods (process_error, process_results)
    per ActionProcessor.vertex_methods at action_processor.py:800.
    `propose_name` is a defense-in-depth stub raising
    `NotImplementedError` to satisfy the ABC contract — never invoked
    under normal operation per v4 §2.3.
    """

    __slots__ = (
        "_bridge_id",
        "_agent_instance_id",
        "_agent_id",
        "_session_label",
        "_bridge_manager",
    )

    def __init__(
        self,
        *,
        bridge_id: str,
        agent_instance_id: str,
        agent_id: str,
        session_label: str | None,
        bridge_manager: BridgeSessionManager,
    ) -> None:
        self._bridge_id = bridge_id
        self._agent_instance_id = agent_instance_id
        self._agent_id = agent_id
        self._session_label = session_label
        self._bridge_manager = bridge_manager

    @property
    def bridge_id(self) -> str:
        return self._bridge_id

    @property
    def agent_instance_id(self) -> str:
        return self._agent_instance_id

    def process_error(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Forward the error context to the bound bridge as a delivery event."""
        return self._emit_bridge_event(
            event_type=_EVENT_TYPE_ERROR,
            params=params,
            state=state,
        )

    def process_results(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Forward the result context to the bound bridge as a delivery event."""
        return self._emit_bridge_event(
            event_type=_EVENT_TYPE_RESULT,
            params=params,
            state=state,
        )

    def forward_completion_request(
        self,
        *,
        request_id: str,
        purpose: str,
        messages: list[dict[str, str]],
        correlation: dict[str, str],
    ) -> None:
        """Carry one INF-02 completion request to this session's bridge.

        Satisfies the wrapper's ``CompletionForwarder`` structural contract.
        The event payload is self-contained: the holder answers by calling
        the named serve verb with ``request_id`` + its completion text. A
        bridge append fault propagates — the caller owns the durable
        re-queue (the row must never sit stamped to a holder that never
        heard about it).
        """
        content_payload: dict[str, Any] = {
            "request_id": request_id,
            "purpose": purpose,
            "messages": messages,
            "correlation": correlation,
            "serve_process_key": SERVE_COMPLETION_PROCESS_KEY,
        }
        meta: dict[str, object] = {
            "source": "agent_messaging_plugin",
            "event_type": _EVENT_TYPE_COMPLETION_REQUEST,
            "delivered_to_vertex": self._agent_instance_id,
            "agent_id": self._agent_id,
            "session_label": self._session_label,
        }
        self._bridge_manager.append_event(
            self._bridge_id,
            _EVENT_TYPE_COMPLETION_REQUEST,
            json.dumps(content_payload, default=str),
            meta,
        )

    def propose_name(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Defense-in-depth stub — see design v4 §2.2 + §2.3."""
        del params, state
        raise NotImplementedError(
            "SessionInferenceProvider.propose_name was called — "
            "propose_name is NOT vertex-routed in v1 design "
            "(workbench/2026-06-13_coding_agent_inference_interface_design_v4.md §2.2). "
            "If you see this error in the field, the routing chain at "
            "InferenceService.propose_name has changed and the design needs "
            "revisiting (D-IF9 promotion to vertex routing)."
        )

    def _emit_bridge_event(
        self,
        *,
        event_type: str,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        content_payload: dict[str, Any] = {
            "params": params,
            "state": state,
        }
        meta: dict[str, object] = {
            "source": "agent_messaging_plugin",
            "event_type": event_type,
            "delivered_to_vertex": self._agent_instance_id,
            "agent_id": self._agent_id,
            "session_label": self._session_label,
        }
        self._bridge_manager.append_event(
            self._bridge_id,
            event_type,
            json.dumps(content_payload, default=str),
            meta,
        )
        result: ActionResult = {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {
                "delivered_to_vertex": self._agent_instance_id,
                "event_type": event_type,
                "bridge_id": self._bridge_id,
            },
            "actions": [],
            "error": None,
            "timestamp": "",
        }
        return result


__all__ = ["SERVE_COMPLETION_PROCESS_KEY", "SessionInferenceProvider"]
