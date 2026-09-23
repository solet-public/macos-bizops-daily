"""AQP-side adapters that satisfy the result-processing Protocols.

This module is the integration boundary between
:mod:`ananta.core.result_processing` and
:mod:`ananta.core.actions.action_queue_poller`.  Each adapter
implements one collaborator Protocol and delegates to the existing AQP
behavior so the coordinator pattern can land without rewriting the
established INFERENCE result-processing path.

The factory :func:`build_result_processing_coordinator` wires the four
collaborators into a :class:`SuccessfulResultCoordinator` instance.

Assignment 4 will replace
:class:`_ContractViolationErrorDispatcherAdapter` with a real
process-level error-handler dispatcher.  Until then, contract
violations are routed through the existing
``_route_failed_edge_to_inference`` path used for execution failures.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from ananta.core.plans.advancement import maybe_advance_plan
from ananta.core.plans.parser import parse as parse_plan
from ananta.core.plans.types import ParsedPlan
from ananta.core.plans.windowing import ACTIVE_WBS_HEADER_RE
from ananta.core.prompts.context import ACTIVE_PLAN_MARKER
from ananta.core.result_processing.bridge_delivery import (
    BridgeDeliveryDispatcher,
)
from ananta.core.result_processing.contracts import (
    BridgeDeliveryContractViolationError,
    BridgeDeliveryFailureInput,
    FlowTriggerDataReader,
    ProcessRegistryProbe,
    ResultContractViolationError,
    ValidatedBridgeDeliveryFailureContext,
    ValidatedResultProcessingContext,
    validate_bridge_delivery_failure,
)
from ananta.core.result_processing.coordinator import (
    CompletedAction,
    DeterministicContextResolver,
    DeterministicResolvedContext,
    SuccessfulResultCoordinator,
)
from ananta.core.result_processing.deterministic_continuation import (
    ActionSubmissionService,
    DeterministicContinuationProcessor,
    PlanAdvancer,
)
from ananta.core.result_processing.error_dispatch import (
    BridgeDeliveryFailureContextBuilder,
    ProcessErrorInferenceSubmitter,
    ResultProcessingErrorDispatcher,
    TokenBlocker,
    ViolationRecorder,
)
from ananta.core.state.flow_runtime_graph import TokenState

if TYPE_CHECKING:
    from ananta.core.actions.action_queue_poller import ActionQueuePoller

logger = logging.getLogger(__name__)

_ACTIVE_WORK_PRODUCT_RUN_RE = re.compile(
    r"^ACTIVE_WORK_PRODUCT_RUN:\s*(\S+)", re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Local Protocols (narrow views of AQP state used by adapters)
# ---------------------------------------------------------------------------


class _MemoryServiceFocusedProvider(Protocol):
    """Session-keyed ``get_focused`` accessor used by the resolver (JOS-02)."""

    def get_focused(self, *, session_id: str) -> dict[str, Any]: ...


class _ThinkingServiceAdvancer(Protocol):
    """``advance_current_plan_step`` accessor used by the advancer adapter."""

    def advance_current_plan_step(
        self, *, session_id: str,
    ) -> dict[str, object] | None: ...


# ---------------------------------------------------------------------------
# Inference dispatcher
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _InferenceDispatcherAdapter:
    """Delegate INFERENCE dispatch back to AQP's existing template path."""

    aqp: ActionQueuePoller

    def dispatch_inference(
        self,
        *,
        completed: CompletedAction,
        validated: ValidatedResultProcessingContext,
        flow_token_id: str | None,
    ) -> None:
        del flow_token_id  # ``result_processor_context`` is held by the caller
        effective_result_processor = self.aqp._apply_result_processor_target_override(
            _coerce_optional_str(completed.result_processor),
            completed.result_processor_target,
        )
        self.aqp._process_result_processor_template(
            completed.action_id,
            dict(validated.result_data),
            completed.process_key,
            completed.notes,
            effective_result_processor,
            completed.session_id,
            completed.flow_id,
            completed.context_id,
            dict(completed.parameters),
        )


def _coerce_optional_str(value: object) -> str | None:
    """Coerce the ``result_processor`` snapshot back into the AQP's expected str."""
    if value is None or isinstance(value, str):
        return value
    return str(value)


# ---------------------------------------------------------------------------
# Deterministic-context resolver
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _DeterministicContextResolverAdapter(DeterministicContextResolver):
    """Build a :class:`DeterministicResolvedContext` from focused memory.

    The resolver is intentionally minimal: it parses the focused active
    plan and reads header markers (``ACTIVE_WBS:``,
    ``ACTIVE_WORK_PRODUCT_RUN:``).  The ``required_args_by_process`` /
    ``owned_arg_slots_by_process`` / ``allowed_result_field_sources``
    maps default to empty for now; once a step actually declares
    ``deterministic_continuation`` in production, the resolver gets
    extended to query the process registry and work-product policies.
    """

    memory_provider: _MemoryServiceFocusedProvider

    def resolve(
        self,
        *,
        completed: CompletedAction,
    ) -> DeterministicResolvedContext:
        # Focus is session-scoped (JOS-02): resolve THE COMPLETED ACTION'S
        # session's plan, never a global buffer. A session-less action has
        # no plan by definition (V-5 ruling: treat-as-no-plan, skip quietly).
        plan_text = self._extract_active_plan_text(completed.session_id or "")
        if plan_text is None:
            return _empty_deterministic_context()
        active_plan = parse_plan(plan_text)
        return DeterministicResolvedContext(
            active_plan=active_plan,
            focused_wbs=None,
            wbs_id=_extract_header(plan_text, ACTIVE_WBS_HEADER_RE),
            work_product_run_id=_extract_header(plan_text, _ACTIVE_WORK_PRODUCT_RUN_RE),
            required_args_by_process={},
            owned_arg_slots_by_process={},
            allowed_result_field_sources={},
        )

    def _extract_active_plan_text(self, session_id: str) -> str | None:
        if not session_id:
            return None
        focused = self.memory_provider.get_focused(session_id=session_id)
        for item in focused["memories"]:
            content = item.get("content", "")
            if isinstance(content, str) and ACTIVE_PLAN_MARKER in content:
                return content
        return None


def _empty_deterministic_context() -> DeterministicResolvedContext:
    return DeterministicResolvedContext(
        active_plan=ParsedPlan(header_lines=(), steps=()),
        focused_wbs=None,
        wbs_id=None,
        work_product_run_id=None,
        required_args_by_process={},
        owned_arg_slots_by_process={},
        allowed_result_field_sources={},
    )


def _extract_header(text: str, regex: re.Pattern[str]) -> str | None:
    """Apply a compiled regex and return its first capture group or ``None``."""
    match = regex.search(text)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Deterministic submission + plan advancement
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _AQPActionSubmissionService(ActionSubmissionService):
    """Submit the validated next action via AQP's action factory."""

    aqp: ActionQueuePoller

    def submit_action(
        self,
        *,
        action_definition: Mapping[str, object],
        parent_action_id: str,
    ) -> str:
        # Inject ``parent_id`` so the recorder stores it as
        # ``core__action_events_id`` (parent provenance).  Bridge delivery
        # and deterministic continuation both rely on this link to trace
        # the originating action from a submitted child.
        # ``submit_action_definition`` mutates the dict — pass a defensive copy.
        merged = dict(action_definition)
        merged["parent_id"] = parent_action_id
        return self.aqp.action_factory.submit_action_definition(merged)


@dataclass(frozen=True, slots=True)
class _AQPPlanAdvancer(PlanAdvancer):
    """Trigger the platform's standard plan-marker advancement.

    Wraps :func:`ananta.core.plans.advancement.maybe_advance_plan` with
    the action-name conveyance the deterministic path needs: the plan is
    advancing because a *non-error*, *non-await-user* tool action
    completed deterministically.

    The plan-lifecycle service is resolved LAZILY per advance (the poller
    exists before plugin bindings do), and an unresolvable service FAILS
    LOUD — a deterministic chain whose marker cannot advance is guaranteed
    to violate ``completed_key_not_declared_by_current_step`` on its next
    hop, so a silent no-op here just moves the failure somewhere mute
    (proven live: Track-A first production run died exactly this way while
    the advancer no-opped behind a ``getattr(..., None)``).
    """

    memory_provider: _MemoryServiceFocusedProvider
    plan_lifecycle_resolver: Callable[[], object | None]

    def advance(self, *, session_id: str) -> None:
        thinking_service = self.plan_lifecycle_resolver()
        if thinking_service is None:
            raise RuntimeError(
                "deterministic-continuation advancement: plan-lifecycle "
                "service is not resolvable — the chain's next hop would "
                "fail its step contract; refusing to continue silently"
            )
        maybe_advance_plan(
            action_name="deterministic_continuation",
            is_continuation=True,
            memory_provider=self.memory_provider,
            thinking_service=cast(_ThinkingServiceAdvancer, thinking_service),
            session_id=session_id,
        )


# ---------------------------------------------------------------------------
# Error-dispatch adapters (Assignment 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _AQPViolationRecorder(ViolationRecorder):
    """Persist a violation row via ``state_service.write_state``."""

    aqp: ActionQueuePoller

    def record(self, *, row: Mapping[str, object]) -> str:
        result = self.aqp.state_service.write_state(
            namespace="core",
            data={
                "table": "result_processing_violations",
                "record": dict(row),
            },
        )
        return _extract_violation_id(result)


def _extract_violation_id(result: object) -> str:
    """Return the StateService-assigned ``generated_id`` for a fresh row.

    The platform owns row-id assignment for tables (``id`` is a
    protected standard field).  This helper mirrors
    :meth:`ActionEventRecorder._extract_generated_id` — it digs into
    ``data.result.generated_id`` first, then ``data.generated_id``, and
    raises if neither is present.
    """
    if isinstance(result, Mapping):
        data = result.get("data", {})
        if isinstance(data, Mapping):
            inner = data.get("result")
            if isinstance(inner, Mapping):
                generated = inner.get("generated_id")
                if isinstance(generated, str) and generated:
                    return generated
            generated = data.get("generated_id")
            if isinstance(generated, str) and generated:
                return generated
    raise RuntimeError(
        "result_processing_glue: StateService did not return a "
        "generated_id for the violation row",
    )


@dataclass(frozen=True, slots=True)
class _AQPProcessErrorInferenceSubmitter(ProcessErrorInferenceSubmitter):
    """Delegate process-error submission to AQP's existing inference path."""

    aqp: ActionQueuePoller

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
    ) -> bool:
        return self.aqp._route_failed_edge_to_inference(
            error_message=error_message,
            process_key=process_key,
            failed_arguments=dict(failed_arguments) if failed_arguments else None,
            notes=notes,
            session_id=session_id,
            flow_id=flow_id,
            context_id=context_id,
            canonical_schema=dict(canonical_schema) if canonical_schema else None,
        )


@dataclass(frozen=True, slots=True)
class _AQPTokenBlocker(TokenBlocker):
    """Mark the parent flow token as failed so continuation does not fire."""

    aqp: ActionQueuePoller

    def block_token(self, *, flow_token_id: str | None) -> None:
        if not flow_token_id:
            return
        self.aqp._flow_runtime_graph.update_token_state(
            flow_token_id, TokenState.FAILED,
        )


@dataclass(frozen=True, slots=True)
class _AQPFlowTriggerDataReader(FlowTriggerDataReader):
    """Read ``flow.trigger_data`` for a flow via ``state_service``."""

    aqp: ActionQueuePoller

    def read_trigger_data(self, flow_id: str) -> Mapping[str, object] | None:
        result = self.aqp.state_service.read_state(
            namespace="core",
            query={"table": "flows", "filters": {"id": flow_id}},
        )
        # ``read_state`` returns the canonical ActionResult shape:
        # ``{"action_status": ..., "data": {"records": [<row>, ...]}}``.
        # Walk down to the first record, then pull ``trigger_data`` (which
        # may be JSON-encoded text for blob-shaped columns).
        data = result.get("data")
        if not isinstance(data, Mapping):
            return None
        records = data.get("records")
        if not isinstance(records, list) or not records:
            return None
        row = records[0]
        if not isinstance(row, Mapping):
            return None
        trigger_data = row.get("trigger_data")
        if isinstance(trigger_data, str):
            try:
                decoded = json.loads(trigger_data)
            except json.JSONDecodeError:
                return None
            return decoded if isinstance(decoded, Mapping) else None
        return trigger_data if isinstance(trigger_data, Mapping) else None


@dataclass(frozen=True, slots=True)
class _AQPProcessRegistryProbe(ProcessRegistryProbe):
    """Check whether a process_key is registered with the platform."""

    aqp: ActionQueuePoller

    def is_process_registered(self, process_key: str) -> bool:
        registry = getattr(self.aqp.action_factory, "process_registry", None)
        if not isinstance(registry, Mapping):
            return False
        processes = registry.get("processes")
        if not isinstance(processes, Mapping):
            return False
        return process_key in processes


@dataclass(frozen=True, slots=True)
class _AQPBridgeDeliveryFailureContextBuilder(
    BridgeDeliveryFailureContextBuilder,
):
    """Build a :class:`ValidatedBridgeDeliveryFailureContext`.

    Reuses :func:`validate_bridge_delivery_failure` so the failure
    payload travels through the same trigger_data + process-registry
    checks as success-side bridge delivery.  Returns ``None`` if the
    bridge contract is violated — the caller (error dispatcher) then
    falls back to the inference escape valve.
    """

    trigger_data_reader: FlowTriggerDataReader
    process_registry_probe: ProcessRegistryProbe

    def build(
        self,
        *,
        completed: CompletedAction,
        error_message: str,
        failed_arguments: Mapping[str, object] | None,
        canonical_schema: Mapping[str, object] | None,
    ) -> ValidatedBridgeDeliveryFailureContext | None:
        error_payload: dict[str, object] = {
            "error_message": error_message,
            "process_key": completed.process_key,
            "action_id": completed.action_id,
        }
        if failed_arguments:
            error_payload["failed_arguments"] = dict(failed_arguments)
        if canonical_schema:
            error_payload["canonical_schema"] = dict(canonical_schema)
        payload = BridgeDeliveryFailureInput(
            action_id=completed.action_id,
            action_process_key=completed.process_key,
            completed_parameters=completed.parameters,
            error_payload=error_payload,
            action_session_id=completed.session_id,
            action_flow_id=completed.flow_id,
            trigger_data_reader=self.trigger_data_reader,
            process_registry_probe=self.process_registry_probe,
        )
        try:
            return validate_bridge_delivery_failure(payload)
        except BridgeDeliveryContractViolationError:
            return None


@dataclass(frozen=True, slots=True)
class _CoordinatorErrorDispatcherAdapter:
    """Adapt :class:`ResultProcessingErrorDispatcher` to the coordinator's Protocol."""

    dispatcher: ResultProcessingErrorDispatcher

    def dispatch_violation(
        self,
        *,
        completed: CompletedAction,
        violation: ResultContractViolationError | BridgeDeliveryContractViolationError,
        flow_token_id: str | None,
    ) -> None:
        self.dispatcher.dispatch_contract_violation(
            completed=completed,
            violation=violation,
            flow_token_id=flow_token_id,
        )


# ---------------------------------------------------------------------------
# Coordinator factory
# ---------------------------------------------------------------------------


def build_result_processing_coordinator(
    aqp: ActionQueuePoller,
) -> SuccessfulResultCoordinator:
    """Wire the dispatch collaborators into a coordinator.

    Plan advancement resolves the plan-lifecycle service lazily through
    the poller (wired by the orchestrator post-binding) and fails loud at
    advance time when unresolvable — see :class:`_AQPPlanAdvancer`.
    """
    memory_provider = aqp.memory_service
    if memory_provider is None:
        raise RuntimeError(
            "result_processing_glue: memory_service is required to build "
            "the deterministic-context resolver",
        )
    submission_service = _AQPActionSubmissionService(aqp=aqp)
    advancer = _AQPPlanAdvancer(
        memory_provider=memory_provider,
        plan_lifecycle_resolver=aqp.resolve_plan_lifecycle,
    )
    deterministic_submitter = DeterministicContinuationProcessor(
        submission_service=submission_service,
        plan_advancer=advancer,
    )
    bridge_dispatcher = BridgeDeliveryDispatcher(
        submission_service=submission_service,
    )
    trigger_reader = _AQPFlowTriggerDataReader(aqp=aqp)
    process_probe = _AQPProcessRegistryProbe(aqp=aqp)
    error_dispatcher = _build_error_dispatcher(
        aqp=aqp,
        bridge_dispatcher=bridge_dispatcher,
        trigger_reader=trigger_reader,
        process_probe=process_probe,
    )
    return SuccessfulResultCoordinator(
        inference_dispatcher=_InferenceDispatcherAdapter(aqp=aqp),
        deterministic_context_resolver=_DeterministicContextResolverAdapter(
            memory_provider=memory_provider,
        ),
        deterministic_submitter=deterministic_submitter,
        bridge_delivery_submitter=bridge_dispatcher,
        trigger_data_reader=trigger_reader,
        process_registry_probe=process_probe,
        error_dispatcher=_CoordinatorErrorDispatcherAdapter(
            dispatcher=error_dispatcher,
        ),
    )


def build_error_dispatcher(
    aqp: ActionQueuePoller,
) -> ResultProcessingErrorDispatcher:
    """Build the standalone error dispatcher for use by failure paths.

    The execution-failure path consumes the same dispatcher as
    contract violations so :meth:`ResultProcessingErrorDispatcher.
    dispatch_execution_failure` produces structurally identical
    process-level error-handler submissions.
    """
    submission_service = _AQPActionSubmissionService(aqp=aqp)
    bridge_dispatcher = BridgeDeliveryDispatcher(
        submission_service=submission_service,
    )
    trigger_reader = _AQPFlowTriggerDataReader(aqp=aqp)
    process_probe = _AQPProcessRegistryProbe(aqp=aqp)
    return _build_error_dispatcher(
        aqp=aqp,
        bridge_dispatcher=bridge_dispatcher,
        trigger_reader=trigger_reader,
        process_probe=process_probe,
    )


def _build_error_dispatcher(
    *,
    aqp: ActionQueuePoller,
    bridge_dispatcher: BridgeDeliveryDispatcher,
    trigger_reader: FlowTriggerDataReader,
    process_probe: ProcessRegistryProbe,
) -> ResultProcessingErrorDispatcher:
    """Shared error-dispatcher factory used by both call paths."""
    return ResultProcessingErrorDispatcher(
        violation_recorder=_AQPViolationRecorder(aqp=aqp),
        inference_submitter=_AQPProcessErrorInferenceSubmitter(aqp=aqp),
        token_blocker=_AQPTokenBlocker(aqp=aqp),
        bridge_failure_submitter=bridge_dispatcher,
        bridge_failure_context_builder=_AQPBridgeDeliveryFailureContextBuilder(
            trigger_data_reader=trigger_reader,
            process_registry_probe=process_probe,
        ),
    )
