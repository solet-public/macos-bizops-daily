"""Result-processing coordinator.

Owns step-level dispatch for *successful* tool results:

1. read the persisted ``result_processor_kind`` on the completed action;
2. run common-success validation
   (:func:`ananta.core.result_processing.contracts.validate_common_success`);
3. route :class:`~ananta.core.result_processing.contracts.ResultContractViolationError`
   to the process-level error handler;
4. dispatch valid results by
   :class:`~ananta.core.result_processing.enums.ResultProcessorKind`:

   * ``INFERENCE`` → delegate to the existing ``process_results`` path.
   * ``DETERMINISTIC_CONTINUATION`` → resolve deterministic-continuation
     context, validate (Section 9 invariants), and submit the next
     action via the deterministic processor.

The coordinator owns no policy of its own.  Collaborators are passed in
as :class:`typing.Protocol` instances so the module stays decoupled from
:mod:`ananta.core.actions.action_queue_poller`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ananta.core.plans.types import ParsedPlan
from ananta.core.result_processing.contracts import (
    BridgeDeliveryContractViolationError,
    BridgeDeliverySuccessInput,
    CommonSuccessInput,
    DeterministicContinuationInput,
    FlowTriggerDataReader,
    ProcessRegistryProbe,
    ResultContractViolationError,
    ValidatedBridgeDeliveryContext,
    ValidatedDeterministicContinuation,
    ValidatedResultProcessingContext,
    validate_bridge_delivery_success,
    validate_common_success,
    validate_deterministic_continuation,
)
from ananta.core.result_processing.enums import (
    ErrorProcessorKind,
    ResultProcessorKind,
)

# ---------------------------------------------------------------------------
# Coordinator value objects
# ---------------------------------------------------------------------------


class DispatchOutcome(StrEnum):
    """What the coordinator did with a successful tool result.

    The AQP uses the outcome to drive parent-token completion:
    ``CONTRACT_VIOLATION_DISPATCHED`` is treated as ``blocks_continuation``
    so happy-path advancement does not fire after a violation.
    ``DETERMINISTIC_PLAN_COMPLETE`` (JOS-07) is the terminal step of an
    already-finished plan — a normal happy-path ending, not a violation, so
    it does NOT block continuation.
    """

    INFERENCE_DISPATCHED = "inference_dispatched"
    DETERMINISTIC_SUBMITTED = "deterministic_submitted"
    CONTRACT_VIOLATION_DISPATCHED = "contract_violation_dispatched"
    BRIDGE_DELIVERY_DISPATCHED = "bridge_delivery_dispatched"
    DETERMINISTIC_PLAN_COMPLETE = "deterministic_plan_complete"


@dataclass(frozen=True, slots=True)
class CompletedAction:
    """Snapshot of a completed action passed to the coordinator.

    The caller (AQP) hydrates this from ``core__action_events`` plus the
    stored result row; the coordinator treats it as read-only.
    """

    action_id: str
    process_key: str
    parameters: Mapping[str, object]
    notes: str | None
    result_processor: object | None
    error_processor: object | None
    result_processor_kind: ResultProcessorKind | None
    result_processor_target: str | None
    session_id: str | None
    flow_id: str | None
    context_id: str | None
    # ``error_processor_kind`` is appended with a None default so older
    # call sites (verifier scripts predating bridge delivery) keep
    # working with positional / partial kwarg construction.  New
    # call sites should pass it explicitly.
    error_processor_kind: ErrorProcessorKind | None = None


@dataclass(frozen=True, slots=True)
class DeterministicResolvedContext:
    """All deterministic-validation inputs that depend on platform state.

    Built by :class:`DeterministicContextResolver`; consumed by the
    coordinator to construct
    :class:`~ananta.core.result_processing.contracts.DeterministicContinuationInput`.
    """

    active_plan: ParsedPlan
    focused_wbs: ParsedPlan | None
    wbs_id: str | None
    work_product_run_id: str | None
    required_args_by_process: Mapping[str, frozenset[str]]
    owned_arg_slots_by_process: Mapping[str, frozenset[str]]
    allowed_result_field_sources: Mapping[str, str]


# ---------------------------------------------------------------------------
# Collaborator Protocols
# ---------------------------------------------------------------------------


class InferenceDispatcher(Protocol):
    """Delegate for the ``INFERENCE`` dispatch branch.

    The concrete implementation is the existing ``process_results``
    template-driven submission path on
    :class:`~ananta.core.actions.action_queue_poller.ActionQueuePoller`;
    the coordinator only knows the Protocol shape.
    """

    def dispatch_inference(
        self,
        *,
        completed: CompletedAction,
        validated: ValidatedResultProcessingContext,
        flow_token_id: str | None,
    ) -> None: ...


class DeterministicContextResolver(Protocol):
    """Build a :class:`DeterministicResolvedContext` for a completed action.

    Reads platform state (focused memory, knowledge base, process
    registry, work-product runtime).  No mutation, no inference.
    """

    def resolve(
        self,
        *,
        completed: CompletedAction,
    ) -> DeterministicResolvedContext: ...


class DeterministicSubmitter(Protocol):
    """Submit a validated deterministic continuation.

    Implemented by
    :class:`ananta.core.result_processing.deterministic_continuation.
    DeterministicContinuationProcessor`.
    """

    def submit(
        self,
        *,
        completed: CompletedAction,
        continuation: ValidatedDeterministicContinuation,
        flow_token_id: str | None,
    ) -> None: ...


class BridgeDeliverySuccessSubmitter(Protocol):
    """Submit a validated bridge-delivery success payload.

    Implemented by
    :class:`ananta.core.result_processing.bridge_delivery.
    BridgeDeliveryDispatcher.dispatch_success`.
    """

    def dispatch_success(
        self,
        *,
        completed: CompletedAction,
        validated: ValidatedBridgeDeliveryContext,
        flow_token_id: str | None,
    ) -> None: ...


class ContractViolationErrorDispatcher(Protocol):
    """Route a contract violation to the process-level error handler.

    Two violation flavors:

    * :class:`ResultContractViolationError` — common-success or
      deterministic-continuation contract failures.  Recorded in
      ``core__result_processing_violations`` and routed to inference
      for the originating user-process's error handler.
    * :class:`BridgeDeliveryContractViolationError` — bridge-delivery
      contract failures (missing trigger_data, unregistered
      ``deliver_*`` processes, session mismatch).  Always routed to
      inference regardless of ``error_processor_kind`` (the documented
      escape valve in handoff 2026-05-10 Section 8).
    """

    def dispatch_violation(
        self,
        *,
        completed: CompletedAction,
        violation: ResultContractViolationError | BridgeDeliveryContractViolationError,
        flow_token_id: str | None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SuccessfulResultCoordinator:
    """Top-level dispatch for successful tool results.

    Frozen / slotted: the coordinator is stateless once constructed.
    """

    inference_dispatcher: InferenceDispatcher
    deterministic_context_resolver: DeterministicContextResolver
    deterministic_submitter: DeterministicSubmitter
    bridge_delivery_submitter: BridgeDeliverySuccessSubmitter
    trigger_data_reader: FlowTriggerDataReader
    process_registry_probe: ProcessRegistryProbe
    error_dispatcher: ContractViolationErrorDispatcher

    def handle_successful_result(
        self,
        *,
        completed: CompletedAction,
        result: Mapping[str, object],
        plugin_returned_actions: Sequence[Mapping[str, object]],
        flow_token_id: str | None,
    ) -> DispatchOutcome:
        """Dispatch a successful tool result and report what happened.

        :param completed: action snapshot
        :param result: raw result payload (typed ``Mapping`` here; the
            common-success validator confirms shape at the boundary)
        :param plugin_returned_actions: actions the plugin emitted as
            part of its return (Pattern 6a); must be processed before
            the coordinator is invoked
        :param flow_token_id: FRG parent token for child-action ancestry
        :returns: :class:`DispatchOutcome` describing which branch ran.
            The caller uses the outcome to gate parent-token completion
            so contract violations propagate as ``blocks_continuation``.
        """
        try:
            validated = _validate_common(completed, result, plugin_returned_actions)
        except ResultContractViolationError as exc:
            self.error_dispatcher.dispatch_violation(
                completed=completed,
                violation=exc,
                flow_token_id=flow_token_id,
            )
            return DispatchOutcome.CONTRACT_VIOLATION_DISPATCHED

        kind = validated.result_processor_kind
        if kind is ResultProcessorKind.INFERENCE:
            self.inference_dispatcher.dispatch_inference(
                completed=completed,
                validated=validated,
                flow_token_id=flow_token_id,
            )
            return DispatchOutcome.INFERENCE_DISPATCHED
        if kind is ResultProcessorKind.BRIDGE_DELIVERY:
            return self._handle_bridge_delivery(
                completed, validated, flow_token_id,
            )
        return self._handle_deterministic(completed, validated, flow_token_id)

    # ── deterministic path ─────────────────────────────────────────

    def _handle_deterministic(
        self,
        completed: CompletedAction,
        validated: ValidatedResultProcessingContext,
        flow_token_id: str | None,
    ) -> DispatchOutcome:
        resolved = self.deterministic_context_resolver.resolve(completed=completed)
        det_input = _build_deterministic_input(completed, validated, resolved)
        try:
            continuation = validate_deterministic_continuation(det_input)
        except ResultContractViolationError as exc:
            self.error_dispatcher.dispatch_violation(
                completed=completed,
                violation=exc,
                flow_token_id=flow_token_id,
            )
            return DispatchOutcome.CONTRACT_VIOLATION_DISPATCHED
        if continuation is None:
            # JOS-07: the active plan is legitimately finished — the
            # terminal step's own completion has no next step to submit.
            # Not a violation; do not route to the error dispatcher.
            return DispatchOutcome.DETERMINISTIC_PLAN_COMPLETE
        self.deterministic_submitter.submit(
            completed=completed,
            continuation=continuation,
            flow_token_id=flow_token_id,
        )
        return DispatchOutcome.DETERMINISTIC_SUBMITTED

    # ── bridge-delivery path ───────────────────────────────────────

    def _handle_bridge_delivery(
        self,
        completed: CompletedAction,
        validated: ValidatedResultProcessingContext,
        flow_token_id: str | None,
    ) -> DispatchOutcome:
        """Validate the bridge-delivery contract and submit deliver_result.

        Bridge-delivery contract violations route to the inference
        path through the same error dispatcher; the dispatcher
        recognizes :class:`BridgeDeliveryContractViolationError` and
        forces the INFERENCE escape valve regardless of the action's
        ``error_processor_kind``.
        """
        bridge_input = BridgeDeliverySuccessInput(
            action_id=completed.action_id,
            action_process_key=completed.process_key,
            completed_parameters=completed.parameters,
            result_data=validated.result_data,
            action_session_id=completed.session_id,
            action_flow_id=completed.flow_id,
            error_processor=completed.error_processor,
            result_processor_kind=validated.result_processor_kind,
            error_processor_kind=completed.error_processor_kind,
            trigger_data_reader=self.trigger_data_reader,
            process_registry_probe=self.process_registry_probe,
        )
        try:
            bridge_validated = validate_bridge_delivery_success(bridge_input)
        except BridgeDeliveryContractViolationError as exc:
            self.error_dispatcher.dispatch_violation(
                completed=completed,
                violation=exc,
                flow_token_id=flow_token_id,
            )
            return DispatchOutcome.CONTRACT_VIOLATION_DISPATCHED
        self.bridge_delivery_submitter.dispatch_success(
            completed=completed,
            validated=bridge_validated,
            flow_token_id=flow_token_id,
        )
        return DispatchOutcome.BRIDGE_DELIVERY_DISPATCHED


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_common(
    completed: CompletedAction,
    result: Mapping[str, object],
    plugin_returned_actions: Sequence[Mapping[str, object]],
) -> ValidatedResultProcessingContext:
    """Wrap :func:`validate_common_success` with the AQP-level snapshot."""
    return validate_common_success(
        CommonSuccessInput(
            action_id=completed.action_id,
            action_process_key=completed.process_key,
            completed_parameters=completed.parameters,
            result_data=result,
            plugin_returned_actions=plugin_returned_actions,
            error_processor=completed.error_processor,
            result_processor_kind=completed.result_processor_kind,
            error_processor_kind=completed.error_processor_kind,
        ),
    )


def _build_deterministic_input(
    completed: CompletedAction,
    validated: ValidatedResultProcessingContext,
    resolved: DeterministicResolvedContext,
) -> DeterministicContinuationInput:
    return DeterministicContinuationInput(
        action_id=completed.action_id,
        completed_process_key=completed.process_key,
        completed_parameters=completed.parameters,
        result_data=validated.result_data,
        result_processor_kind=validated.result_processor_kind,
        session_id=completed.session_id,
        flow_id=completed.flow_id,
        context_id=completed.context_id,
        work_product_run_id=resolved.work_product_run_id,
        wbs_id=resolved.wbs_id,
        active_plan=resolved.active_plan,
        focused_wbs=resolved.focused_wbs,
        required_args_by_process=resolved.required_args_by_process,
        owned_arg_slots_by_process=resolved.owned_arg_slots_by_process,
        allowed_result_field_sources=resolved.allowed_result_field_sources,
    )
