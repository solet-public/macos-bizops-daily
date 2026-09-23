"""Bridge-delivery dispatcher.

Submits an EDGE_SINK ``deliver_result`` or ``deliver_error`` action
against the originating bridge plugin so the raw structured payload
flows back to the MCP caller (or any other channel originator)
without going through inference.

Per handoff 2026-05-10 Section 9 the dispatcher must not:

* parse plans or inspect WBS / Joseki state;
* call inference;
* generate prose from the payload;
* choose among multiple originators;
* reach into ``post_message`` — that surface is reserved for prose
  channel messages.

Recursion safety: the submitted ``deliver_*`` action is declared
EDGE_SINK by the originating plugin (it carries no result_processor
and no error_processor).  When AQP completes that action there is no
further result-processing dispatch.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from ananta.core.result_processing.contracts import (
    ValidatedBridgeDeliveryContext,
    ValidatedBridgeDeliveryFailureContext,
)
from ananta.core.result_processing.coordinator import CompletedAction
from ananta.core.result_processing.deterministic_continuation import (
    ActionSubmissionService,
)
from ananta.core.state.execution_token_context import result_processor_context

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BridgeDeliveryDispatcher:
    """Submit ``deliver_result`` / ``deliver_error`` actions for bridge delivery.

    Stateless; safe to share across actions.  Reuses
    :class:`ActionSubmissionService` from the deterministic processor —
    the submission path is identical (ActionFactory + standard pipeline
    + recorder).
    """

    submission_service: ActionSubmissionService

    def dispatch_success(
        self,
        *,
        completed: CompletedAction,
        validated: ValidatedBridgeDeliveryContext,
        flow_token_id: str | None,
    ) -> None:
        """Deliver a validated success payload to the originating bridge.

        Submits ``plugin::<originator>::deliver_result`` inside
        ``result_processor_context(flow_token_id)`` so the new action
        inherits FRG parent-token ancestry the same way deterministic
        continuation and inference dispatch do.
        """
        target = validated.target
        action_definition = self._build_action_definition(
            completed=completed,
            target_process_key=target.deliver_result_process_key,
            payload_key="result_payload",
            payload=validated.result_data,
            target=target,
            source_process_key=completed.process_key,
        )
        self._submit(
            action_definition=action_definition,
            completed=completed,
            flow_token_id=flow_token_id,
            label="dispatch_success",
            target_process_key=target.deliver_result_process_key,
        )

    def dispatch_failure(
        self,
        *,
        completed: CompletedAction,
        validated: ValidatedBridgeDeliveryFailureContext,
        flow_token_id: str | None,
    ) -> None:
        """Deliver a validated failure payload to the originating bridge."""
        target = validated.target
        action_definition = self._build_action_definition(
            completed=completed,
            target_process_key=target.deliver_error_process_key,
            payload_key="error_payload",
            payload=validated.error_payload,
            target=target,
            source_process_key=completed.process_key,
        )
        self._submit(
            action_definition=action_definition,
            completed=completed,
            flow_token_id=flow_token_id,
            label="dispatch_failure",
            target_process_key=target.deliver_error_process_key,
        )

    def _build_action_definition(
        self,
        *,
        completed: CompletedAction,
        target_process_key: str,
        payload_key: str,
        payload: Mapping[str, object],
        target: object,  # BridgeDeliveryTarget (avoid cycle; only attrs read)
        source_process_key: str,
    ) -> dict[str, object]:
        """Materialize the ``deliver_*`` action definition.

        The submitted action is EDGE_SINK so it carries no
        ``result_processor`` / ``error_processor`` and no
        ``*_processor_kind`` (the originator plugin declares it
        terminal).  Routing IDs are inherited from the completed
        action.
        """
        bridge_id = getattr(target, "bridge_id", "")
        return {
            "process_key": target_process_key,
            "arguments": {
                payload_key: dict(payload),
                "source_process_key": source_process_key,
                "bridge_id": bridge_id,
            },
            "notes": f"Bridge delivery for {source_process_key}",
            "session_id": completed.session_id,
            "flow_id": completed.flow_id,
            "context_id": completed.context_id,
        }

    def _submit(
        self,
        *,
        action_definition: Mapping[str, object],
        completed: CompletedAction,
        flow_token_id: str | None,
        label: str,
        target_process_key: str,
    ) -> None:
        logger.info(
            "BRIDGE_DELIVERY_%s: completed_action=%s source_pk=%s "
            "target_pk=%s",
            label.upper(),
            completed.action_id,
            completed.process_key,
            target_process_key,
        )
        with result_processor_context(flow_token_id):
            submitted_id = self.submission_service.submit_action(
                action_definition=action_definition,
                parent_action_id=completed.action_id,
            )
        logger.info(
            "BRIDGE_DELIVERY_%s: queued %s",
            label.upper(),
            submitted_id,
        )
