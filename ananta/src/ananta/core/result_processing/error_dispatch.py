"""Shared process-level error-handler dispatcher.

Two error sources funnel through this module:

1. **Tool execution failure** — the plugin raised before producing a
   result.  The action row is marked ``failed`` elsewhere; the
   dispatcher's job is to hand the failure to the process-level error
   handler inference path.

2. **Result-contract violation** — the tool returned a successful result
   that failed the validation gate.  The action row stays ``completed``
   and its result row is preserved; the dispatcher writes a structured
   ``core__result_processing_violations`` row, then submits the
   process-level error handler with the violation payload.

Per handoff Section 11 the two paths must share one dispatcher so the
process-level error handler always sees the same shape.  Submission is
delegated through Protocol collaborators to keep the dispatcher free of
direct dependencies on
:class:`~ananta.core.actions.action_queue_poller.ActionQueuePoller`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from ananta.core.result_processing.contracts import (
    BridgeDeliveryContractViolationError,
    ResultContractViolationError,
    ValidatedBridgeDeliveryFailureContext,
)
from ananta.core.result_processing.coordinator import CompletedAction
from ananta.core.result_processing.enums import ErrorProcessorKind

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Collaborator Protocols
# ---------------------------------------------------------------------------


class ViolationRecorder(Protocol):
    """Write one ``core__result_processing_violations`` row."""

    def record(self, *, row: Mapping[str, object]) -> str: ...


class ProcessErrorInferenceSubmitter(Protocol):
    """Submit a process-level error handler inference action.

    Wraps the existing
    :meth:`ActionQueuePoller._route_failed_edge_to_inference` so the
    dispatcher can stay decoupled from AQP-specific concerns
    (circuit-breaker counters, template fetching, model-config wiring).
    """

    def submit_error_inference(
        self,
        *,
        error_message: str,
        process_key: str,
        failed_arguments: Mapping[str, object] | None,
        notes: str | None,
        session_id: str | None,
        flow_id: str | None,
        context_id: str | None,
        canonical_schema: Mapping[str, object] | None,
    ) -> bool: ...


class TokenBlocker(Protocol):
    """Block automatic happy-path advancement after a violation.

    The dispatcher signals the parent flow token that continuation must
    not occur — the error handler action is now responsible for
    recovery.
    """

    def block_token(self, *, flow_token_id: str | None) -> None: ...


class BridgeDeliveryFailureSubmitter(Protocol):
    """Submit a validated bridge-delivery failure payload.

    Implemented by
    :class:`ananta.core.result_processing.bridge_delivery.
    BridgeDeliveryDispatcher.dispatch_failure`.  Wired here so the
    error dispatcher can route execution failures with
    ``error_processor_kind = "bridge_delivery"`` to the bridge.
    """

    def dispatch_failure(
        self,
        *,
        completed: CompletedAction,
        validated: ValidatedBridgeDeliveryFailureContext,
        flow_token_id: str | None,
    ) -> None: ...


class BridgeDeliveryFailureContextBuilder(Protocol):
    """Build a :class:`ValidatedBridgeDeliveryFailureContext`.

    The AQP-side adapter supplies the validator + the structured error
    payload shape so the error dispatcher stays decoupled from the
    bridge contract internals.  Returns ``None`` when the bridge
    contract itself is violated — in that case the dispatcher must
    fall back to inference (the documented escape valve).
    """

    def build(
        self,
        *,
        completed: CompletedAction,
        error_message: str,
        failed_arguments: Mapping[str, object] | None,
        canonical_schema: Mapping[str, object] | None,
    ) -> ValidatedBridgeDeliveryFailureContext | None: ...


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResultProcessingErrorDispatcher:
    """Process-level error-handler routing for both error sources."""

    violation_recorder: ViolationRecorder
    inference_submitter: ProcessErrorInferenceSubmitter
    token_blocker: TokenBlocker
    bridge_failure_submitter: BridgeDeliveryFailureSubmitter
    bridge_failure_context_builder: BridgeDeliveryFailureContextBuilder

    def dispatch_contract_violation(
        self,
        *,
        completed: CompletedAction,
        violation: ResultContractViolationError | BridgeDeliveryContractViolationError,
        flow_token_id: str | None,
    ) -> None:
        """Record / route the violation per its type.

        :class:`ResultContractViolationError` — the standard result-
        contract path: write a violation row, submit the process-level
        error handler inference, block the parent token.

        :class:`BridgeDeliveryContractViolationError` — the bridge-
        contract escape valve: no violation row (the
        ``result_processing_violations`` table records result-contract
        violations only), submit inference against the originating
        user-process so the agent gets a human-readable explanation,
        block the parent token.  Always inference, regardless of the
        action's ``error_processor_kind``.
        """
        if isinstance(violation, BridgeDeliveryContractViolationError):
            self._dispatch_bridge_contract_violation(
                completed=completed,
                violation=violation,
                flow_token_id=flow_token_id,
            )
            return
        self._dispatch_result_contract_violation(
            completed=completed,
            violation=violation,
            flow_token_id=flow_token_id,
        )

    def _dispatch_result_contract_violation(
        self,
        *,
        completed: CompletedAction,
        violation: ResultContractViolationError,
        flow_token_id: str | None,
    ) -> None:
        details = violation.violation
        # ``id`` and ``created_at`` are platform-managed standard fields
        # — never set them here; the StateService write_state path
        # assigns the row id and stamps ``created_at``.
        violation_id = self.violation_recorder.record(row={
            "core__action_events_id": completed.action_id,
            "core__flows_id": completed.flow_id,
            "core__sessions_id": completed.session_id,
            "context_id": completed.context_id,
            "process_key": completed.process_key,
            "result_processor_kind": details.result_processor_kind.value,
            "invariant": details.invariant,
            "message": details.message,
            "expected_json": json.dumps(dict(details.expected), default=str),
            "observed_json": json.dumps(dict(details.observed), default=str),
        })
        logger.warning(
            "RESULT_CONTRACT_VIOLATION_RECORDED: violation_id=%s "
            "action=%s process=%s invariant=%s",
            violation_id,
            completed.action_id,
            completed.process_key,
            details.invariant,
        )

        error_message = _format_violation_message(details, violation_id)
        self.inference_submitter.submit_error_inference(
            error_message=error_message,
            process_key=completed.process_key,
            failed_arguments=dict(completed.parameters),
            notes=completed.notes,
            session_id=completed.session_id,
            flow_id=completed.flow_id,
            context_id=completed.context_id,
            canonical_schema=None,
        )
        self.token_blocker.block_token(flow_token_id=flow_token_id)

    def _dispatch_bridge_contract_violation(
        self,
        *,
        completed: CompletedAction,
        violation: BridgeDeliveryContractViolationError,
        flow_token_id: str | None,
    ) -> None:
        details = violation.violation
        logger.warning(
            "BRIDGE_DELIVERY_CONTRACT_VIOLATION: action=%s process=%s "
            "invariant=%s — routing to inference (escape valve)",
            completed.action_id,
            completed.process_key,
            details.invariant,
        )
        error_message = (
            f"BRIDGE_DELIVERY_CONTRACT_VIOLATION: {details.invariant}\n"
            f"{details.message}\n\n"
            f"Expected: {json.dumps(dict(details.expected), default=str)}\n"
            f"Observed: {json.dumps(dict(details.observed), default=str)}"
        )
        self.inference_submitter.submit_error_inference(
            error_message=error_message,
            process_key=completed.process_key,
            failed_arguments=dict(completed.parameters),
            notes=completed.notes,
            session_id=completed.session_id,
            flow_id=completed.flow_id,
            context_id=completed.context_id,
            canonical_schema=None,
        )
        self.token_blocker.block_token(flow_token_id=flow_token_id)

    def dispatch_execution_failure(
        self,
        *,
        error_message: str,
        process_key: str,
        failed_arguments: Mapping[str, object] | None,
        notes: str | None,
        session_id: str | None,
        flow_id: str | None,
        context_id: str | None,
        canonical_schema: Mapping[str, object] | None,
        completed: CompletedAction | None = None,
        flow_token_id: str | None = None,
    ) -> None:
        """Route a tool-execution failure through the shared dispatcher.

        Routes by ``error_processor_kind`` when *completed* is supplied:

        * ``INFERENCE`` (or unset, ``None``) — submit the process-level
          error handler inference (the historical path).
        * ``BRIDGE_DELIVERY`` — build a failure context via
          :attr:`bridge_failure_context_builder` and dispatch to the
          bridge.  If the builder returns ``None`` (bridge contract
          itself violated), fall back to inference so the agent still
          sees the failure.

        No violation row is written for execution failures: the
        violation table records *result-contract* violations only.  The
        action's status is set to ``failed`` by the caller (AQP's
        ``_mark_action_failed``); this method just submits the
        process-level error handler.  Token blocking for execution
        failures is owned by the caller (FRG token completion).
        """
        if (
            completed is not None
            and completed.error_processor_kind
            is ErrorProcessorKind.BRIDGE_DELIVERY
        ):
            built = self.bridge_failure_context_builder.build(
                completed=completed,
                error_message=error_message,
                failed_arguments=failed_arguments,
                canonical_schema=canonical_schema,
            )
            if built is not None:
                self.bridge_failure_submitter.dispatch_failure(
                    completed=completed,
                    validated=built,
                    flow_token_id=flow_token_id,
                )
                return
            logger.warning(
                "BRIDGE_DELIVERY_FAILURE_FALLBACK: action=%s process=%s "
                "bridge contract violated; routing failure through "
                "inference escape valve",
                completed.action_id,
                process_key,
            )
        self.inference_submitter.submit_error_inference(
            error_message=error_message,
            process_key=process_key,
            failed_arguments=failed_arguments,
            notes=notes,
            session_id=session_id,
            flow_id=flow_id,
            context_id=context_id,
            canonical_schema=canonical_schema,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_violation_message(
    details: object,
    violation_id: str,
) -> str:
    """Render a structured violation as a single error-handler prompt string.

    Includes the invariant id, the human-readable message, the
    serialized ``expected`` / ``observed`` payloads, and the violation
    row id so the recovery prompt can be audited.
    """
    # Late attribute access keeps the helper agnostic of the dataclass
    # type when used by ad-hoc tests; ``ResultContractViolationDetails``
    # always provides these fields.
    invariant = getattr(details, "invariant", "")
    message = getattr(details, "message", "")
    expected = dict(getattr(details, "expected", {}))
    observed = dict(getattr(details, "observed", {}))
    return (
        f"RESULT_CONTRACT_VIOLATION ({violation_id}): {invariant}\n"
        f"{message}\n\n"
        f"Expected: {json.dumps(expected, default=str)}\n"
        f"Observed: {json.dumps(observed, default=str)}"
    )
