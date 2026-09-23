"""
ActionQueuePoller - Simple, reliable background service for processing queued actions.

This replaces the complex trigger-based approach with a simple polling mechanism
that checks for queued actions every 1-2 seconds and processes them reliably.

Phase 1 Enhancement: ExecutionContext lifecycle management.
"""

import ast
import asyncio
import contextlib
import json
import logging
import os
import re
from collections import OrderedDict
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

from ananta.constants import (
    CONTEXT_KEY_DATE,
    CONTEXT_KEY_FLOW_ID,
    CONTEXT_KEY_PROCESS_KEY,
    CONTEXT_KEY_SESSION_ID,
    CONTEXT_KEY_TIME,
    CONTEXT_KEY_TIMESTAMP,
    CONTEXT_KEY_TIMEZONE,
    CONTEXT_KEY_TIMEZONE_OFFSET,
    NOTES_MAX_LENGTH,
    TEMPLATE_VAR_ACTION_ARGUMENTS,
    TEMPLATE_VAR_ACTION_ID,
    TEMPLATE_VAR_AVAILABLE_ATTACHMENTS,
    TEMPLATE_VAR_CANONICAL_SCHEMA,
    TEMPLATE_VAR_ERROR,
    TEMPLATE_VAR_ERROR_DETAILS,
    TEMPLATE_VAR_ERROR_MESSAGE,
    TEMPLATE_VAR_FAILED_ACTION,
    TEMPLATE_VAR_FAILED_PROCESS_KEY,
    TEMPLATE_VAR_FLOW_ID,
    TEMPLATE_VAR_NOTES,
    TEMPLATE_VAR_PROCESS_KEY,
    TEMPLATE_VAR_RESULT,
    TEMPLATE_VAR_SESSION_ID,
)
from ananta.core.actions.action_path_liveness import ACTION_PATH_LIVENESS
from ananta.core.actions.orphan_reaper import reap_orphaned_processing_actions
from ananta.core.actions.payload_bounds import (
    OversizedActionPayloadError,
    check_claimed_parameters_size,
)
from ananta.core.contexts.normalization import normalize_flow_id, normalize_session_id
from ananta.core.domain.enums import JobStatus
from ananta.core.domain.types import ActionResult
from ananta.core.plans import parse as parse_plan
from ananta.core.plans.windowing import (
    ACTIVE_WBS_HEADER_RE,
    ACTIVE_WORK_PRODUCT_RUN_RE,
)
from ananta.core.plans.work_product_policies import (
    get_all_owned_output_slots,
    get_audio_midi_policy,
)
from ananta.core.plans.work_product_runtime import record_successful_action_products
from ananta.core.plans.work_product_store import WorkProductStoreAdapter
from ananta.core.plans.work_products import WorkProductRegister
from ananta.core.plugins.plugin_contracts import ActionStatus
from ananta.core.prompts.context import ACTIVE_PLAN_MARKER
from ananta.core.prompts.stages.context import SKIP_SEMANTIC_RECALL_KEY
from ananta.core.result_processing.coordinator import (
    CompletedAction,
    DispatchOutcome,
    SuccessfulResultCoordinator,
)
from ananta.core.result_processing.enums import (
    ErrorProcessorKind,
    ResultProcessorKind,
)
from ananta.core.result_processing.error_dispatch import (
    ResultProcessingErrorDispatcher,
)
from ananta.core.state.execution_token_context import result_processor_context
from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph, TokenState
from ananta.core.tracking.attachment_extractor import (
    AttachmentExtractionError,
    AttachmentExtractor,
)
from ananta.interfaces.attachment_schema import AttachmentFields, MetadataFields
from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.services.blob_storage_service import BlobStorageService
from ananta.utils.naming import NamingError, normalize_name

if TYPE_CHECKING:
    from ananta.core.contexts.action_contexts import TemplateFunctionContext
    from ananta.core.orchestration.execution_context import ExecutionContextManager
    from ananta.services.context_management.service import ContextManagementService
    from ananta.services.discovery_service import DiscoveryService

logger = logging.getLogger(__name__)

WBS_STEP_REF_RE = re.compile(r"WBS Step (\d+)")

# Non-terminal complement of the ``core__job`` status domain. That domain is
# closed by a CHECK constraint to ``JobStatus``'s members; a "pending" job is
# one NOT in a terminal state {completed, cancelled}, i.e. {queued, processing,
# error}. The legacy raw query filtered ``status NOT IN ('completed', 'failed',
# 'cancelled')`` — its 'failed' term was vestigial ('failed' is not in the job
# domain, which uses 'error'), so this complement is behavior-identical.
_PENDING_JOB_STATES: frozenset[JobStatus] = frozenset(
    {JobStatus.QUEUED, JobStatus.PROCESSING, JobStatus.ERROR}
)

# Over-read window for the dispatch fetch. Set to the ``query_ordered`` hard cap
# (``_MAX_ORDERED_LIMIT`` in ``ananta.services.state_service.ordered_query`` = 100):
# the read pulls the oldest-by-sequence queued rows, then ``excluded_versions``
# filtering happens in Python before the ``max_actions_per_poll`` take. It MUST
# equal that cap so ``len(rows) == _DISPATCH_READ_CAP`` faithfully means "the read
# was bounded, more queued rows exist" — the condition the dispatch tripwire keys on.
# The tripwire also assumes ``max_actions_per_poll <= _DISPATCH_READ_CAP`` (true:
# the poll size is 10, hardcoded in ``action_coordinator``); were that to invert,
# a full read could never fill a batch and the tripwire would false-fire each poll.
_DISPATCH_READ_CAP = 100

# How often the D8 orphan reap runs, in poll cycles. At the default 1 s poll
# interval this is roughly every five minutes — far more often than the
# one-hour orphan threshold needs, and rare enough that the extra query is
# invisible against dispatch traffic.
_ORPHAN_REAP_EVERY_N_CYCLES = 300

# REL-03 swap-window guard. The two VERTEX result/error-PROCESSING process
# keys — the ONLY action class that produced the mid-cutover
# ``Empty source_namespace`` traceback burst (each of a flow's already-queued
# process_error/process_results siblings re-hits ``_resolve_io_process_key``
# and re-terminates an already-failed flow). Scoping the terminated-flow drop
# to exactly these keys lets terminal EDGE_SINK deliveries, bridge
# deliver_error escape-valve actions, and cleanup pass through untouched.
_INFERENCE_PROCESS_ERROR_KEY: Final[str] = (
    "service_interface::inference_service::process_error"
)
_INFERENCE_PROCESS_RESULTS_KEY: Final[str] = (
    "service_interface::inference_service::process_results"
)
_RESULT_ERROR_PROCESSING_KEYS: Final[frozenset[str]] = frozenset(
    {_INFERENCE_PROCESS_ERROR_KEY, _INFERENCE_PROCESS_RESULTS_KEY}
)
# Bounded FIFO tombstone of terminated flow_ids. flow_ids are never reused, so
# a stale entry can only ever match a genuinely-terminated flow — the cap
# bounds memory without a TTL. Mirrors the deferred-vertex LRU cap style.
_TERMINATED_FLOW_TOMBSTONE_CAP: Final[int] = 2048

# The three spellings a completed read envelope arrives in (enum, str(enum),
# enum value) — the state interface is not consistent about which it returns.
# Shared by ``_query_records`` and the double-execution detector so the two
# cannot drift: a detector that mis-reads the envelope status would go blind
# exactly when the read seam changed.
_COMPLETED_READ_STATUSES: Final[tuple[object, ...]] = (
    ActionStatus.COMPLETED,
    str(ActionStatus.COMPLETED),
    ActionStatus.COMPLETED.value,
)


@dataclass
class QueuedAction:
    """Represents a core__action_events record ready for processing"""

    id: str
    process_key: str
    parameters: str  # JSON string from core__action_events table
    notes: str
    created_at: str
    session_id: str | None = None
    flow_id: str | None = None
    context_id: str | None = None  # Platform context ID for OUTPUT event correlation
    result_processor: str | None = None  # JSON string for result processor template
    result_processor_target: str | None = None  # Override target VERTEX process key
    template_namespace: str | None = None  # Namespace for template variable resolution
    compiled_version: str | None = None  # Compiler version (e.g., "1.0") if action was compiled
    validation_timestamp: str | None = None  # ISO timestamp of compilation validation
    flow_token_id: str | None = None  # FRG token ID for completion tracking
    job_result_ref: str | None = None  # Async job ID for post_message attachment routing


class ActionProcessorProtocol(Protocol):
    def process_action(
        self, process_key: str, parameters: dict[str, object]
    ) -> dict[str, object]: ...
    def execute_action(self, action: QueuedAction) -> dict[str, object]: ...


class ActionFactoryProtocol(Protocol):
    def create_action(self, action_data: dict[str, object]) -> dict[str, object]: ...
    def submit_result_with_template(
        self, results_data: dict[str, object], template_data: dict[str, object]
    ) -> str: ...
    def submit_action_definition(
        self, action_def: dict[str, object], context: dict[str, object] | None = None
    ) -> str: ...
    def get_process_customizations(self, process_key: str) -> dict[str, object] | None: ...


class EventBusProtocol(Protocol):
    def publish(self, event: object) -> bool: ...


class TemplateFunctionRegistryProtocol(Protocol):
    """Protocol for TemplateFunctionRegistry used to resolve <<<:...>>> template functions."""

    def execute_function(self, func_call: str, context: "TemplateFunctionContext") -> str:
        """Execute a single template function call with typed context."""
        ...

    def resolve_in_data_structure(self, data: object, context: "TemplateFunctionContext") -> object:
        """Recursively resolve <<<:...>>> template function patterns with typed context."""
        ...


class MemoryServiceProtocol(Protocol):
    """Protocol for MemoryService used to record tool uses."""

    def remember(
        self,
        content: str,
        tags: list[str] | None = None,
        source_file: str | None = None,
        session_id: str | None = None,
        embed: bool = True,
    ) -> dict[str, object]:
        """Store a memory."""
        ...

    def get_focused(self, *, session_id: str) -> dict[str, Any]:
        """Return the acting session's focus envelope: {"memories": [...], "count": N}."""
        ...


class IOInterfaceRegistryProtocol(Protocol):
    """Protocol for IOInterfaceRegistry used for IO plugin detection."""

    def get_namespaces(self) -> list[str]:
        """Get all registered IO interface plugin namespaces."""
        ...

    def is_registered(self, namespace: str) -> bool:
        """Check if namespace is a registered IO interface plugin."""
        ...


class IOInterfaceServiceProtocol(Protocol):
    """Protocol for IOInterfaceService used for artifact delivery helpers."""

    registry: IOInterfaceRegistryProtocol

    def _prepare_attachments(
        self,
        attachments: list[dict[str, object]] | None,
        job_result_ref: str | None,
    ) -> list[dict[str, object]]:
        """Prepare and normalize attachments from explicit list or job reference."""
        ...


def _typed_error_detail(value: object) -> Mapping[str, object] | None:
    """Extract a typed ``ErrorDetail`` mapping from a failure, or ``None``.

    The failure paths into ``_mark_action_failed`` carry one of three things:
    an :class:`AnantaError` subclass (which already renders itself via
    ``to_dict()``), a plugin's ``ErrorDetail`` dict lifted off a failed
    ActionResult, or a bare string / arbitrary exception with no typing at all.
    Only the first two can contribute a code; the third legitimately yields
    ``None`` and keeps the generic constant.

    Returning ``None`` rather than a synthesized stub is deliberate: a fabricated
    code would be indistinguishable from a real one downstream, which is the
    failure this whole change exists to end.
    """
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        rendered = to_dict()
        if isinstance(rendered, Mapping) and rendered.get("code"):
            return rendered
        return None
    if isinstance(value, Mapping) and value.get("code"):
        return value
    return None


class ActionQueuePoller:
    """
    Background service that polls for queued actions and processes them.

    Much simpler and more reliable than database triggers.
    """

    def __init__(
        self,
        state_service: StateServiceProtocol,
        action_processor: ActionProcessorProtocol,
        flow_runtime_graph: FlowRuntimeGraph,
        action_factory: ActionFactoryProtocol,
        execution_context_manager: "ExecutionContextManager | None" = None,
        template_registry: TemplateFunctionRegistryProtocol | None = None,
        memory_service: MemoryServiceProtocol | None = None,
        blob_storage_service: BlobStorageService | None = None,
        context_management_service: "ContextManagementService | None" = None,
        discovery_service: "DiscoveryService | None" = None,
        io_interface_service: IOInterfaceServiceProtocol | None = None,
        app_home: str = "",
        poll_interval: float = 1.0,
        max_actions_per_poll: int = 10,
        inference_model_name: str | None = None,
    ) -> None:
        # FAIL-FAST: blob_storage_service is required for attachment extraction
        if blob_storage_service is None:
            raise RuntimeError(
                "BlobStorageService is required for ActionQueuePoller. "
                "Attachment extraction cannot work without it."
            )

        self.state_service = state_service
        self.action_processor = action_processor
        self._flow_runtime_graph = flow_runtime_graph  # Required: FRG token completion
        self.action_factory = action_factory  # Required: action submission and templates
        self.execution_context_manager = (
            execution_context_manager  # Phase 1: ExecutionContext lifecycle
        )
        self.template_registry = template_registry  # For resolving <<<:...>>> template functions
        self.memory_service = memory_service  # For recording tool uses to memory
        self._blob_storage_service = blob_storage_service  # For attachment extraction
        self._context_management_service = (
            context_management_service  # For tracking discovered processes
        )
        self._discovery_service = discovery_service  # For getting built-in processes
        self._io_interface_service = (
            io_interface_service  # For IO plugin detection and artifact delivery
        )
        self.app_home = app_home  # For template function context
        self.poll_interval = poll_interval
        self.max_actions_per_poll = max_actions_per_poll
        # Per-container deploy lineage marker used by the self-deployment
        # plugin's complete_deploy targeting (addendum §K). Defaults to
        # 'local' for non-cloud births; cloud task definitions set
        # SOLET_VERSION=v<N> per the per-birth + blue-green deploys.
        self._solet_version = os.environ.get("SOLET_VERSION") or "local"
        self.inference_model_name = inference_model_name  # For error routing model config
        self._max_flow_errors: int = 3  # Safety net for unrecoverable errors only
        self._max_recoverable_retries: int = 3  # Per-process consecutive retry bound
        self._recoverable_error_counts: dict[str, int] = {}  # flow_id:process_key → count
        # REL-03: bounded FIFO tombstone of terminated flow_ids. Populated at
        # the single ``_terminate_flow`` choke point; read only on the
        # result/error-processing dispatch path to drop doomed siblings of an
        # already-terminated flow before they re-hit ``_resolve_io_process_key``
        # (the blue-green swap-window burst). In-memory only — NO DB read on
        # the universal dequeue path.
        self._terminated_flow_ids: OrderedDict[str, None] = OrderedDict()
        self.running = False
        self.poller_task: asyncio.Task[None] | None = None
        # L3 blue-green Slice D: optional color-active gate. When set, the
        # poll loop skips claim/dispatch on each tick where the callable
        # returns False. Default-None means always-active (backward-compat
        # for legacy / no-router deployments). The orchestrator binds this
        # to `lambda: self.is_active_color` in _delegate_action_attributes.
        self._is_active_color_getter: Callable[[], bool] | None = None
        # Deterministic-continuation plan advancement: resolves the
        # plan-lifecycle service LAZILY (this poller is built before plugin
        # bindings exist; the orchestrator wires the resolver in
        # _delegate_action_attributes alongside the color getter). The
        # deterministic path FAILS LOUD when unresolvable — a chain without
        # marker advancement is guaranteed to violate the step contract on
        # its second hop (proven live, Track-A first production run).
        self._plan_lifecycle_resolver: Callable[[], object | None] | None = None

        # IO post_message detection: build frozenset from registry at init time
        if io_interface_service is not None:
            io_namespaces = io_interface_service.registry.get_namespaces()
            self._io_post_message_keys: frozenset[str] = frozenset(
                f"plugin::{ns}::post_message" for ns in io_namespaces
            )
        else:
            self._io_post_message_keys = frozenset()

        # Attachment extraction support
        self._attachment_extractor = AttachmentExtractor(blob_storage_service)
        self._pending_attachments: dict[str, dict[str, dict[str, object]]] = {}
        self._async_process_cache: dict[str, bool] = {}

        # Register cleanup callback with FRG
        self._flow_runtime_graph.register_completion_callback(self._cleanup_pending_attachments)

        # Result-processing coordinator: lazy-built on first dispatch so the
        # AQP can still be constructed when ``memory_service`` is wired up
        # after init (e.g. during plugin lifecycle setup).
        self._result_processing_coordinator: SuccessfulResultCoordinator | None = None
        # Shared process-level error-handler dispatcher (Assignment 4); reused
        # by both contract violations and execution failures.
        self._result_processing_error_dispatcher: ResultProcessingErrorDispatcher | None = None

        # Metrics
        self.total_actions_processed = 0
        # Rows seen ``queued`` on the most recent fetch — corroborating
        # context published alongside the D5 stall verdict, which is driven
        # by poll age alone (GAU-10). See
        # ananta.core.actions.action_path_liveness.
        self._last_observed_queue_depth = 0
        self.total_poll_cycles = 0
        # Double executions of a SINGLE action row, detected on the success
        # path by ``_report_double_execution``. Process-local like the counters
        # above (reset on restart) — the durable half of the signal is the
        # WARNING log line, which carries the action_id and both timestamps.
        self.total_double_executions_detected = 0
        self.last_poll_time: datetime | None = None

    def _cleanup_pending_attachments(self, flow_id: str) -> None:
        """Clean up pending attachments when a flow completes."""
        if flow_id in self._pending_attachments:
            del self._pending_attachments[flow_id]
            logger.debug("Cleaned up pending attachments for flow %s", flow_id)

    def _has_blob_fields(self, customizations: dict[str, object] | None) -> bool:
        """Check if customizations has blob_fields or blob_fields_list."""
        if not customizations:
            return False
        return bool(customizations.get("blob_fields") or customizations.get("blob_fields_list"))

    def _extract_and_store_attachments(
        self,
        process_key: str,
        result: dict[str, object],
        flow_id: str | None,
    ) -> list[dict[str, object]]:
        """Extract attachments from action result and store in pending buffer.

        After extraction, injects available_attachments into result["data"]
        so the LLM knows which attachment names are available for selection.

        Args:
            process_key: The process key for looking up customizations
            result: The action result containing data with blob_id fields
            flow_id: The flow ID for keying pending attachments

        Raises:
            AttachmentExtractionError: If extraction fails
        """
        if not flow_id:
            return []

        customizations = self.action_factory.get_process_customizations(process_key)
        has_blobs = self._has_blob_fields(customizations)
        if not has_blobs:
            return []

        # Extract attachments using blob_fields mapping
        attachments = self._attachment_extractor.extract(result, customizations)  # type: ignore[arg-type]
        if not attachments:
            # Even with no attachments, inject empty list so schema can be constrained
            # This prevents LLM from hallucinating attachment names
            self._inject_available_attachments_if_possible(result, [])
            return []

        # Store attachments keyed by external_id
        if flow_id not in self._pending_attachments:
            self._pending_attachments[flow_id] = {}

        external_ids: list[str] = []
        for att in attachments:
            metadata = att.get(AttachmentFields.ADDITIONAL_METADATA)
            if not isinstance(metadata, dict):
                raise AttachmentExtractionError(
                    "Attachment missing additional_metadata after extraction"
                )
            external_id = metadata.get(MetadataFields.EXTERNAL_ID)
            if not isinstance(external_id, str):
                raise AttachmentExtractionError("Attachment missing external_id after extraction")
            self._pending_attachments[flow_id][external_id] = att
            external_ids.append(external_id)

        # Inject available_attachments into result["data"]
        self._inject_available_attachments(result, external_ids)

        logger.debug(
            "Extracted %d attachments for flow %s: %s", len(attachments), flow_id, external_ids
        )
        return attachments

    def _inject_available_attachments(
        self, result: dict[str, object], external_ids: list[str]
    ) -> None:
        """Inject available_attachments into result['data'].

        Raises:
            AttachmentExtractionError: If result['data'] is not a dict
        """
        data = result.get("data")
        if not isinstance(data, dict):
            raise AttachmentExtractionError(
                f"Cannot inject available_attachments: result['data'] is not a dict, "
                f"got {type(data).__name__}. This violates the plugin result contract."
            )
        data[AttachmentFields.AVAILABLE_ATTACHMENTS] = external_ids

    def _inject_available_attachments_if_possible(
        self, result: dict[str, object], external_ids: list[str]
    ) -> None:
        """Inject available_attachments into result['data'] if possible.

        Unlike _inject_available_attachments, this silently skips if result['data']
        is not a dict. Used when injecting empty list for schema constraint.
        """
        data = result.get("data")
        if isinstance(data, dict):
            data[AttachmentFields.AVAILABLE_ATTACHMENTS] = external_ids

    def _record_work_products_after_success(
        self,
        process_key: str,
        action_parameters: dict[str, object] | None,
        attachments: list[dict[str, object]],
        session_id: str | None,
    ) -> None:
        """Record WBS work products after a producing action succeeds."""
        if action_parameters is None:
            return

        argument_slots = frozenset(action_parameters)
        policies = get_audio_midi_policy()
        if not get_all_owned_output_slots(policies) & argument_slots:
            return

        context = self._resolve_work_product_context(session_id or "")
        if context is None:
            return

        wbs_id, wbs_step_number, run_id = context
        store = WorkProductStoreAdapter(
            self.state_service, work_product_run_id=run_id,
        )
        register_data = store.load_register(wbs_id)
        register = (
            WorkProductRegister.deserialize(register_data)
            if register_data
            else WorkProductRegister()
        )
        recorded = record_successful_action_products(
            register=register,
            wbs_run_id=wbs_id,
            step_number=wbs_step_number,
            process_key=process_key,
            arguments=action_parameters,
            attachments=attachments,
            process_argument_slots=argument_slots,
        )
        if recorded:
            store.save_register(wbs_id, register.serialize())

    def _resolve_work_product_context(
        self,
        session_id: str,
    ) -> tuple[str, int, str | None] | None:
        """Resolve active WBS identity and plan step number from focused memory.

        Keyed by the completed action's session (JOS-02 — focus is
        session-scoped; a session-less action has no plan).

        Returns ``(wbs_id, plan_step_number, work_product_run_id)``.
        ``work_product_run_id`` is the shared register key for joseki
        fragments (``None`` when not set).
        """
        if self.memory_service is None or not session_id:
            return None

        plan_text = self._extract_focused_plan_text(
            self.memory_service.get_focused(session_id=session_id)["memories"],
        )
        if plan_text is None:
            return None

        wbs_match = ACTIVE_WBS_HEADER_RE.search(plan_text)
        if not wbs_match:
            return None

        current = parse_plan(plan_text).current_step
        if current is None:
            return None

        step_match = WBS_STEP_REF_RE.search(current.full_text())
        if not step_match:
            return None

        run_match = ACTIVE_WORK_PRODUCT_RUN_RE.search(plan_text)
        run_id = run_match.group(1) if run_match else None

        return wbs_match.group(1), current.number, run_id

    @staticmethod
    def _extract_focused_plan_text(
        focused_memories: list[dict[str, object]],
    ) -> str | None:
        """Extract active plan text from focused memories."""
        for memory in focused_memories:
            content = memory.get("content")
            if isinstance(content, str) and ACTIVE_PLAN_MARKER in content:
                return content
        return None

    # =========================================================================
    # Attachment Resolution (post_message execution)
    # =========================================================================

    def _is_io_post_message_action(self, action_def: dict[str, object]) -> bool:
        """Check if action is an IO interface plugin post_message action.

        Uses registry-derived frozenset for exact matching. Does NOT match
        non-IO plugins that happen to have a method named post_message.
        """
        process_key = action_def.get("process_key")
        if isinstance(process_key, str):
            return process_key in self._io_post_message_keys
        # Check nested process object (internal/programmatic actions)
        process = action_def.get("process")
        if isinstance(process, dict):
            fn = process.get("function_name")
            provider = process.get("provider")
            return (
                fn == "post_message"
                and isinstance(provider, str)
                and self._io_interface_service is not None
                and self._io_interface_service.registry.is_registered(provider)
            )
        return False

    def _is_io_post_message_queued(self, action: QueuedAction) -> bool:
        """Check if a QueuedAction is an IO interface post_message action."""
        return action.process_key in self._io_post_message_keys

    def _resolve_attachments_for_execution(self, action: QueuedAction) -> None:
        """Resolve LLM-provided attachment name strings to full attachment objects.

        Called AFTER schema validation passes, BEFORE action execution.
        Modifies action.parameters in-place to replace name strings with
        resolved blob objects that the IO interface can deliver.

        This must happen at execution time (not submission time) because:
        - Validation runs on stored parameters and expects raw name strings
        - Resolution transforms strings → blob dicts, which would fail validation
        """
        if not self._is_io_post_message_queued(action):
            return

        if not action.flow_id:
            return

        try:
            parameters = json.loads(action.parameters) if action.parameters else {}
        except json.JSONDecodeError:
            return  # Let execution handle parse errors

        raw_attachments = parameters.get(AttachmentFields.ATTACHMENTS)

        # Nothing to resolve: missing, empty, or already-resolved (plugin-sourced)
        if not raw_attachments or not isinstance(raw_attachments, list):
            return
        if isinstance(raw_attachments[0], dict):
            return

        # Resolve name strings to full attachment objects
        validated_names = self._validate_attachment_names(raw_attachments)
        if validated_names is None:
            parameters[AttachmentFields.ATTACHMENTS] = []
        else:
            resolved = self._resolve_refs_to_attachments(validated_names, action.flow_id)
            parameters[AttachmentFields.ATTACHMENTS] = resolved

        # Update action parameters with resolved attachments
        action.parameters = json.dumps(parameters)

    # Internal ID prefixes that should never be used as attachment names
    _INTERNAL_ID_PREFIXES = ("flow-", "sess-", "ctx-", "ae-", "act-")

    def _normalize_attachment_name(self, name: str) -> str | None:
        """Normalize a single attachment name to external_id format.

        Args:
            name: Raw attachment name from LLM

        Returns:
            Normalized external_id, or None if name should be skipped

        Raises:
            ValueError: If name is an internal ID or invalid
        """
        # Strip leading dots - LLM sometimes uses jq-like path syntax (.field)
        normalized = name.lstrip(".")
        if not normalized:
            return None

        # Reject internal ID prefixes - MUST happen BEFORE normalize_name
        # Otherwise "flow-123" becomes "flow123" and bypasses rejection
        if normalized.startswith(self._INTERNAL_ID_PREFIXES):
            raise ValueError(
                f"Invalid attachment name: '{normalized}' is an internal ID, not an attachment. "
                f"Only use names from available_attachments."
            )

        # Normalize to external_id rules (lowercase, underscores, strip extension)
        base_name, _ = os.path.splitext(normalized)
        if base_name:
            try:
                normalized = normalize_name(base_name)
            except NamingError as e:
                raise ValueError(f"Invalid attachment name '{name}': {e}") from e

        return normalized

    def _validate_attachment_names(self, names: object) -> list[str] | None:
        """Validate attachment names and return list of strings or None."""
        if names is None:
            return None
        if not isinstance(names, list):
            raise ValueError(f"attachments must be a list, got {type(names).__name__}")
        if not names:
            return None

        validated: list[str] = []
        for name in names:
            if not isinstance(name, str):
                raise ValueError(f"attachments items must be strings, got {type(name).__name__}")
            normalized = self._normalize_attachment_name(name)
            if normalized:
                validated.append(normalized)

        return validated or None

    def _resolve_refs_to_attachments(
        self, refs: list[str], flow_id: str
    ) -> list[dict[str, object]]:
        """Resolve refs to attachment objects from pending buffer.

        Fails fast on unknown refs - the schema constraint should prevent this,
        so any failure here indicates a bug that needs to be fixed.
        """
        pending = self._pending_attachments.get(flow_id, {})
        attachments: list[dict[str, object]] = []
        for ref in refs:
            if ref not in pending:
                available = list(pending.keys()) if pending else "none"
                raise ValueError(f"Unknown attachment ref '{ref}'. Available: {available}")
            attachments.append(pending[ref])
        return attachments

    def _resolve_job_result_ref(self, action: QueuedAction) -> None:
        """Resolve job_result_ref to attachments for IO post_message actions.

        When a post_message action has job_result_ref but no attachments,
        builds attachments from the async job payload using IOInterfaceService
        helpers. Checks target plugin capabilities — if plugin lacks file upload,
        formats a text fallback instead.

        Reads job_result_ref from the explicit QueuedAction field (populated from
        the job_result_ref column on core__action_events).

        Modifies action.parameters in-place.
        """
        if self._io_interface_service is None:
            return

        if not action.job_result_ref:
            return

        try:
            parameters = json.loads(action.parameters) if action.parameters else {}
        except json.JSONDecodeError:
            return

        # Only resolve if attachments are empty/absent (model didn't provide them)
        existing_attachments = parameters.get(AttachmentFields.ATTACHMENTS)
        if existing_attachments:
            return

        try:
            resolved_attachments = self._io_interface_service._prepare_attachments(
                None, action.job_result_ref
            )
            if resolved_attachments:
                parameters[AttachmentFields.ATTACHMENTS] = resolved_attachments
                action.parameters = json.dumps(parameters)
                logger.debug(
                    f"Resolved {len(resolved_attachments)} attachments from "
                    f"job_result_ref for {action.process_key}"
                )
        except Exception as e:
            logger.warning(f"Failed to resolve job_result_ref for {action.process_key}: {e}")

    def _inject_session_id_if_missing(self, action: QueuedAction) -> None:
        """Inject session_id into IO post_message arguments from flow context.

        The model sometimes omits session_id from post_message arguments.
        The flow's session_id (from trigger_data) is the authoritative source,
        so we inject it when the model didn't provide it.

        Modifies action.parameters in-place.
        """
        if not action.session_id:
            return

        try:
            parameters = json.loads(action.parameters) if action.parameters else {}
        except json.JSONDecodeError:
            return

        if parameters.get("session_id"):
            return

        parameters["session_id"] = action.session_id
        action.parameters = json.dumps(parameters)
        logger.info(
            "Injected session_id from flow context into %s arguments",
            action.process_key,
        )

    def set_is_active_color_getter(self, getter: Callable[[], bool]) -> None:
        """Wire the orchestrator's color-active flag into the poll loop (L3 Slice D).

        When set, each poll-loop iteration consults the getter; False skips
        the SKIP LOCKED claim and dispatch path while keeping the loop alive
        so resume is instant. Distinct from ``self.running``: ``running=False``
        means stop forever; the getter returning False means temporarily
        quiesce while the active color serves.
        """
        self._is_active_color_getter = getter

    def set_plan_lifecycle_resolver(
        self, resolver: Callable[[], object | None],
    ) -> None:
        """Wire lazy plan-lifecycle resolution for deterministic advancement.

        The poller is constructed before plugin bindings exist, so the
        deterministic-continuation path cannot capture the plan-lifecycle
        service at build time. The orchestrator binds this resolver in
        ``_delegate_action_attributes`` (the same site as the color getter);
        ``resolve_plan_lifecycle`` consults it at advance time.
        """
        self._plan_lifecycle_resolver = resolver

    def resolve_plan_lifecycle(self) -> object | None:
        """The plan-lifecycle service, resolved lazily; None when unwired."""
        if self._plan_lifecycle_resolver is None:
            return None
        return self._plan_lifecycle_resolver()

    async def start(self) -> None:
        """Start the background polling loop"""
        if self.running:
            logger.error("ActionQueuePoller already running")
            return
        self.running = True
        self.poller_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Stop the polling loop gracefully"""
        if not self.running:
            return
        self.running = False

        if self.poller_task:
            self.poller_task.cancel()
            try:
                await self.poller_task
            except asyncio.CancelledError:
                pass

    async def _poll_loop(self) -> None:
        """Main polling loop - runs continuously until stopped.

        L3 blue-green Slice D: when the orchestrator's color-active getter
        is bound and returns False, the tick skips ``_poll_once`` so no
        actions get claimed on this color. The loop keeps cycling so the
        deployment plugin can flip back to active without restart.
        """
        while self.running:
            try:
                if self._is_active_color_getter is None or self._is_active_color_getter():
                    await self._poll_once()
                    self.total_poll_cycles += 1
                    self.last_poll_time = datetime.now(UTC)
                    self._maybe_reap_orphans()

            except Exception as e:
                logger.error(f"Error in polling cycle: {e}", exc_info=True)
                # Continue polling despite errors

            # Wait for next poll cycle
            await asyncio.sleep(self.poll_interval)

    async def _poll_once(self) -> None:
        """Execute one polling cycle"""
        # Query for queued actions
        queued_actions = await self._get_queued_actions()

        if not queued_actions:
            # D5: stamp the heartbeat on an EMPTY cycle too. If only productive
            # cycles refreshed it, a quiet platform would be indistinguishable
            # from a frozen one and the liveness signal would reproduce the
            # exact ambiguity it exists to remove.
            ACTION_PATH_LIVENESS.record_poll_cycle(queue_depth=0, dispatched=0)
            return

        # Claim and execute ONE row at a time.  Claiming a fetched batch up
        # front used to prevent a concurrent poller from seeing its rows, but
        # it also meant that a stall in the first handler stranded every later
        # row in ``processing`` without ever entering its handler.  The claim
        # below is a conditional update (id + status=queued), so it preserves
        # the concurrent-poller exclusion without claiming work we have not
        # actually begun.
        dispatched = 0
        for action in queued_actions:
            if not self._mark_action_processing(action.id):
                # A competing poller or a cancellation transitioned the row
                # after our bounded queue read.  This poller did not acquire
                # it, so it must never execute it.
                continue
            try:
                await self._process_action(action)
                self.total_actions_processed += 1
                dispatched += 1

            except Exception as e:
                logger.error(f"Failed to process action {action.id}: {e}", exc_info=True)
                # Mark action as failed, carrying the exception's typing when it
                # has any — str(e) alone discards an AnantaError's error_code.
                self._mark_action_failed(
                    action.id, str(e), error_detail=_typed_error_detail(e),
                )

        # Stamped only after the drain loop returns. That placement is the
        # signal: a handler that holds the GIL (the 2026-08-15 failure mode)
        # never lets this line run, so the age grows and the stall becomes
        # visible from the still-serving HTTP surface.
        ACTION_PATH_LIVENESS.record_poll_cycle(
            queue_depth=self._last_observed_queue_depth,
            dispatched=dispatched,
        )

    def _maybe_reap_orphans(self) -> None:
        """Run the D8 orphan reap occasionally, never on every cycle.

        Gated on a cycle count rather than run inline because the reap issues
        its own query and the poll loop is serial — a per-cycle reap would add
        a database round trip to the platform's hottest loop for work that only
        matters on the timescale of hours.

        Failures are swallowed with a log: an orphan reap is maintenance, and
        it must never be able to take down the dispatch loop that carries every
        session's work. This is the one place where continuing past an error is
        clearly right.
        """
        if self.total_poll_cycles % _ORPHAN_REAP_EVERY_N_CYCLES != 0:
            return
        try:
            reap_orphaned_processing_actions(self.state_service)
        except Exception as exc:  # noqa: BLE001 — maintenance must not stop dispatch
            logger.error("Orphan reap pass failed: %s", exc, exc_info=True)

    def _requires_main_thread(self, process_key: str) -> bool:
        """
        Determine if an action requires main thread execution.

        Console actions (VSCode, terminal) need main thread for signal handling.
        Inference actions can run in thread pool to prevent blocking.
        """
        main_thread_patterns = ["start_console", "console_plugin", "vscode", "terminal"]

        process_key_lower = process_key.lower()
        return any(pattern in process_key_lower for pattern in main_thread_patterns)

    def _handles_own_completion(self, process_key: str) -> bool:
        """
        Determine if an action handles its own result_processor completion.

        These plugins return ActionStatus.COMPLETED immediately but spawn background
        threads that call ActionFactory.submit_result_with_template() when work completes.
        Skipping result_processor here prevents double-processing race conditions.
        """
        self_completing_patterns = ["claude_code_plugin", "inference_request"]

        process_key_lower = process_key.lower()
        return any(pattern in process_key_lower for pattern in self_completing_patterns)

    def _is_async_process(self, process_key: str) -> bool:
        """Check if a process is marked async (completion handled by AsyncJobManager).

        Async plugin processes frequently return an immediate enqueue response (job_id)
        but do not yet have user-deliverable artifacts. Those artifacts are delivered via
        a later completion handler action.
        """
        cached = self._async_process_cache.get(process_key)
        if cached is not None:
            return cached

        is_async = self._read_async_flag_from_registry(process_key)
        self._async_process_cache[process_key] = is_async
        return is_async

    def _read_async_flag_from_registry(self, process_key: str) -> bool:
        """Read is_async from action_blueprint.metadata in the process registry."""
        blueprint = self._parse_json_field(self._fetch_action_blueprint(process_key))
        meta = blueprint.get("metadata")
        return bool(meta.get("is_async")) if isinstance(meta, dict) else False

    def _fetch_action_blueprint(self, process_key: str) -> object:
        """Fetch the action_blueprint column for a process_key.

        ``process_key`` is the registry's unique identifier, so the first
        matching record is the single row the legacy ``LIMIT 1`` selected.
        """
        result = self.state_service.query_state(
            "core",
            {"table": "process_registry", "filters": {"process_key": process_key}},
        )
        record = self._first_record(result)
        return record.get("action_blueprint") if record else None

    @staticmethod
    def _parse_json_field(raw: object) -> dict[str, object]:
        """Parse a raw value that may be a JSON string or already a dict."""
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw:
            with contextlib.suppress(json.JSONDecodeError):
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
        return {}

    async def _get_queued_actions(self) -> list[QueuedAction]:
        """Fetch queued actions via the state interface (bounded over-read).

        The legacy dispatch SQL LEFT-JOINed ``core__sessions`` for the template
        namespace and applied a JSONB ``excluded_versions`` containment filter,
        neither expressible in the state grammar. Restructured into a
        single-namespace ``query_ordered`` read (``status='queued'`` ordered by
        ``sequence`` then ``id``, capped at ``_DISPATCH_READ_CAP``), a Python
        ``excluded_versions`` filter, a take of ``max_actions_per_poll``, and one
        batch namespace-enrichment read. Exact in practice: only the single
        self-deployment ``complete_deploy`` row ever carries ``excluded_versions``
        and it is enqueued at the BACK by ``sequence``, so the oldest-N window is
        never materially thinned (Architect ground-truth); ``_warn_if_dispatch_starved``
        is the fail-loud tripwire if that ever stops holding. ``include_deleted=True``
        matches the legacy query, which carried no ``is_deleted`` predicate. The
        ``id`` tie-break is new (the raw query ordered by ``sequence`` alone) and
        makes equal-``sequence`` ordering deterministic.
        """
        try:
            result = self.state_service.query_ordered(
                "core",
                {
                    "table": "action_events",
                    "filters": {"status": ActionStatus.QUEUED.value},
                    "order_by": [["sequence", "asc"], ["id", "asc"]],
                    "limit": _DISPATCH_READ_CAP,
                    "include_deleted": True,
                },
            )
            rows = self._query_records(result)
            # Queue depth for the D5 liveness signal. Free — the poller has to
            # read these rows to do its own work, so publishing the count costs
            # nothing and needs no extra query on the health path.
            self._last_observed_queue_depth = len(rows)
            claimable = [
                row
                for row in rows
                if not self._version_excluded(row.get("excluded_versions"))
            ]
            taken = claimable[: self.max_actions_per_poll]
            self._warn_if_dispatch_starved(len(rows), len(taken))

            namespaces = self._fetch_session_namespaces(taken)
            actions: list[QueuedAction] = []
            for row in taken:
                session_id = row.get("core__sessions_id")
                namespace = (
                    namespaces.get(session_id) if isinstance(session_id, str) else None
                )
                try:
                    actions.append(self._build_queued_action(row, namespace))
                except OversizedActionPayloadError as exc:
                    # Fail ONLY the oversized row and keep the rest of the
                    # batch. D12 in INCIDENT.md: the poller claims in batches,
                    # and when one oversized action wedged its batch the five
                    # innocent small actions beside it were stranded in
                    # ``processing`` with no retry path. Letting this propagate
                    # would reproduce that — worse, the caller's except-all
                    # below would return an EMPTY batch and stall dispatch
                    # entirely for as long as the row sits at the queue head.
                    logger.error(
                        "OVERSIZED_ACTION_PAYLOAD: failing action %s (%s) — %s",
                        exc.action_id,
                        row.get("process_key"),
                        exc,
                    )
                    self._mark_action_failed(exc.action_id, str(exc))
            return actions

        except Exception as e:
            logger.error(f"Error querying queued actions: {e}", exc_info=True)
            return []

    def _version_excluded(self, raw: object) -> bool:
        """True if THIS poller's ``SOLET_VERSION`` is in the action's
        ``excluded_versions`` list — the self-deployment ``complete_deploy``
        targeting filter (only that one producer ever sets it). The JSONB column
        reads back as a Python list (or, defensively, a JSON string); ``NULL`` /
        empty / malformed means not excluded, i.e. claim the row.
        """
        if raw is None:
            return False
        versions: object = raw
        if isinstance(versions, str):
            try:
                versions = json.loads(versions)
            except json.JSONDecodeError:
                return False
        if not isinstance(versions, list):
            return False
        return self._solet_version in versions

    def _warn_if_dispatch_starved(self, rows_read: int, dispatched: int) -> None:
        """Fail-loud tripwire (Q2 ground-truth guard). The over-read hit the cap
        — so MORE queued rows exist beyond the window — yet ``excluded_versions``
        filtering left fewer than a full batch. Under the load-bearing invariant
        (only ~1 ``complete_deploy`` row is ever excluded) this is impossible; if
        it fires, ``excluded_versions`` has gone high-cardinality and the queued
        backlog may stall behind the filter.
        """
        if rows_read == _DISPATCH_READ_CAP and dispatched < self.max_actions_per_poll:
            logger.warning(
                "DISPATCH-TRIPWIRE: read hit the %d-row cap but only %d of %d "
                "slots filled after excluded_versions filtering — excluded_versions "
                "may have gone high-cardinality (expected ~1 complete_deploy row); "
                "queued backlog may stall.",
                _DISPATCH_READ_CAP,
                dispatched,
                self.max_actions_per_poll,
            )

    def _fetch_session_namespaces(
        self, rows: list[dict[str, object]]
    ) -> dict[str, str | None]:
        """Batch-resolve ``core__sessions_id`` → ``namespace`` for the dispatch
        rows (replaces the legacy ``LEFT JOIN core__sessions``). One ``=ANY``
        read; a row whose session has no match (the LEFT-JOIN-NULL case) gets no
        map entry, so its ``template_namespace`` resolves to ``None``.

        A FAILED sessions read (non-completed envelope) RAISES — the caller's
        try/except then drops the whole batch to ``[]`` and retries next poll.
        The legacy single-query LEFT JOIN was all-or-nothing on a DB error;
        splitting the read into two must preserve that, otherwise a transient
        sessions-read blip would silently dispatch the batch with EVERY namespace
        nulled (distinct from the legitimate per-row LEFT-JOIN-NULL above).
        """
        session_ids: list[str] = []
        for row in rows:
            session_id = row.get("core__sessions_id")
            if isinstance(session_id, str):
                session_ids.append(session_id)
        if not session_ids:
            return {}
        result = self.state_service.query_state(
            "core",
            {"table": "sessions", "filters": {"id": session_ids}},
        )
        if result.get("action_status") not in (
            ActionStatus.COMPLETED,
            str(ActionStatus.COMPLETED),
            ActionStatus.COMPLETED.value,
        ):
            raise RuntimeError(
                "dispatch namespace enrichment: sessions read returned "
                f"{result.get('action_status')!r}"
            )
        namespaces: dict[str, str | None] = {}
        for record in self._query_records(result):
            session_id = record.get("id")
            if isinstance(session_id, str):
                namespace = record.get("namespace")
                namespaces[session_id] = namespace if isinstance(namespace, str) else None
        return namespaces

    @staticmethod
    def _truncate_notes(raw_note: object) -> str:
        """Convert and truncate notes field."""
        notes_text = str(raw_note) if raw_note is not None else ""
        return notes_text[:NOTES_MAX_LENGTH] if notes_text else ""

    @staticmethod
    def _opt_field(row: dict[str, object], key: str) -> str | None:
        """Optional dispatch field: the value if truthy, else ``None`` — the
        legacy ``_get_optional_field`` falsy→None semantics (distinct from
        ``_opt_str``, which keeps an empty string)."""
        value = row.get(key)
        return cast("str | None", value) if value else None

    def _build_queued_action(
        self, row: dict[str, object], template_namespace: str | None
    ) -> QueuedAction:
        """Marshal one dispatch row + its enriched namespace into a QueuedAction.

        Fields are read by column name (the rows are ``SELECT *`` dicts).
        ``created_at`` is populated from ``sequence`` — preserving the legacy
        positional mapping (record index 4 was ``a.sequence``) — and
        ``template_namespace`` is the batch-resolved session namespace (the
        former ``LEFT JOIN`` column), supplied by the caller.
        """
        parameters = row.get("parameters")
        raw_parameters = cast("str", parameters) if parameters else "{}"
        # Bound the payload BEFORE anything parses it. ``parameters`` is a
        # ColumnType.TEXT column, so psycopg hands back a plain ``str`` with no
        # driver-side parse — ``len()`` here is O(1) and is the last point on
        # the dispatch path that is still upstream of every ``json.loads`` of
        # this string (_resolve_attachments, _prepare_action_parameters, and
        # the executor all parse it downstream of this call). A guard placed at
        # any of those sites would run after the two-hour GIL-held parse that
        # caused the 2026-08-15 outage.
        check_claimed_parameters_size(
            raw_parameters,
            action_id=cast("str", row.get("id")),
            process_key=cast("str", row.get("process_key")),
        )
        return QueuedAction(
            id=cast("str", row.get("id")),
            process_key=cast("str", row.get("process_key")),
            parameters=raw_parameters,
            notes=self._truncate_notes(row.get("notes")),
            created_at=str(row.get("sequence")),
            session_id=cast("str | None", row.get("core__sessions_id")),
            flow_id=cast("str | None", row.get("core__flows_id")),
            context_id=cast("str | None", row.get("context_id")),
            result_processor=cast("str | None", row.get("result_processor")),
            result_processor_target=self._opt_field(row, "result_processor_target"),
            template_namespace=template_namespace,
            compiled_version=self._opt_field(row, "compiled_version"),
            validation_timestamp=self._opt_field(row, "validation_timestamp"),
            flow_token_id=self._opt_field(row, "flow_token_id"),
            job_result_ref=self._opt_field(row, "job_result_ref"),
        )

    # Platform routing fields that the model or inference plugin may include
    # in action arguments but are not part of the process's functional interface.
    # The executor (_filter_and_inject_arguments) already drops these for processes
    # that don't declare them — validation should match that behavior.
    _PLATFORM_ROUTING_FIELDS = frozenset({"session_id", "flow_id"})

    def _get_canonical_arguments_schema(self, process_key: str) -> dict[str, object] | None:
        """Look up the canonical arguments sub-schema for a process from discovery service.

        Returns the arguments schema from the invocation_schema, or None if unavailable.
        """
        if not self._discovery_service:
            return None

        process_data = self._discovery_service.get_process_by_key(process_key)
        if not process_data:
            return None

        invocation_schema = process_data.get("invocation_schema")
        if not isinstance(invocation_schema, dict):
            return None

        properties = invocation_schema.get("properties")
        if not isinstance(properties, dict):
            return None

        arguments_schema = properties.get("arguments")
        if not isinstance(arguments_schema, dict):
            return None

        return arguments_schema

    def _prepare_action_parameters(
        self,
        action: QueuedAction,
        arguments_schema: dict[str, object],
    ) -> dict[str, object] | None:
        """Parse, repair, strip, and clamp action parameters against schema.

        Returns None when the raw parameters cannot be parsed (caller should
        skip validation and let ActionProcessor handle the parse error).
        """
        try:
            parameters: dict[str, object] = (
                json.loads(action.parameters) if action.parameters else {}
            )
        except json.JSONDecodeError:
            return None

        parameters = self._repair_argument_aliases(action.process_key, parameters)
        parameters = self._inject_layer_policy(
            action.process_key, parameters, action.session_id or "",
        )
        schema_properties = arguments_schema.get("properties", {})
        if (
            isinstance(schema_properties, dict)
            and arguments_schema.get("additionalProperties") is False
        ):
            parameters = {k: v for k, v in parameters.items() if k in schema_properties}
        if isinstance(schema_properties, dict):
            parameters = self._clamp_schema_properties(
                action.process_key, parameters, schema_properties,
            )
        return parameters

    def _validate_action_arguments(
        self,
        action: QueuedAction,
    ) -> tuple[bool, str | None, dict[str, object] | None]:
        """Validate action arguments against canonical invocation_schema from process registry.

        Returns:
            Tuple of (is_valid, error_message, canonical_arguments_schema).
            is_valid is True if arguments pass validation or no schema is available.
        """
        if action.process_key.startswith("service_interface::inference_service::"):
            return (True, None, None)

        arguments_schema = self._get_canonical_arguments_schema(action.process_key)
        if not arguments_schema:
            return (True, None, None)

        parameters = self._prepare_action_parameters(action, arguments_schema)
        if parameters is None:
            return (True, None, None)

        from ananta.platform.json_schema_validator import JSONSchemaValidator

        result = JSONSchemaValidator().validate_against_schema(parameters, arguments_schema)
        if result.valid:
            action.parameters = json.dumps(parameters)
            return (True, None, None)

        error_msg = self._build_validation_error_message(
            action.process_key,
            result.errors,
            arguments_schema,
        )
        return (False, error_msg, arguments_schema)

    _LAYER_POLICY_TARGET_SUFFIX = "knowledge_service::search"
    _LAYER_POLICY_KEYS: tuple[str, ...] = (
        "knowledge_layers",
        "min_knowledge_layer",
        "max_knowledge_layer",
        "include_unlayered",
    )

    def _current_step_layer_policy(self, session_id: str) -> object | None:
        """The acting session's current-step layer policy, or None (JOS-02)."""
        if self.memory_service is None or not session_id:
            return None
        plan_text = self._extract_focused_plan_text(
            self.memory_service.get_focused(session_id=session_id)["memories"],
        )
        if plan_text is None:
            return None
        try:
            current = parse_plan(plan_text).current_step
        except Exception:  # noqa: BLE001 — never block dispatch on parse failure
            return None
        return None if current is None else current.layer_policy

    def _inject_layer_policy(
        self,
        process_key: str,
        parameters: dict[str, object],
        session_id: str,
    ) -> dict[str, object]:
        """Inject the active plan step's ``LAYER_POLICY`` into search args.

        Reads the ACTING session's focused plan (JOS-02), locates the current
        step, and copies any layer-policy fields that the model didn't
        already set into the search action's arguments. Model-supplied values
        win; the platform only fills gaps.

        Silent no-op for non-search actions and when no focused plan or
        no active layer policy exists.
        """
        if not process_key.endswith(self._LAYER_POLICY_TARGET_SUFFIX):
            return parameters
        layer_policy = self._current_step_layer_policy(session_id)
        if layer_policy is None:
            return parameters

        injected: dict[str, object] = {}
        for k, v in layer_policy.as_arguments().items():  # type: ignore[attr-defined]
            if k in parameters:
                continue
            parameters[k] = v
            injected[k] = v
        if injected:
            logger.info(
                "LAYER_POLICY: injected %s into %s",
                injected,
                process_key,
            )
        return parameters

    # Narrow, process-specific alias map.  Each entry maps
    # (process_key_suffix) → {wrong_arg: correct_arg}.
    _ARGUMENT_ALIAS_MAP: dict[str, dict[str, str]] = {
        "knowledge_service::search": {"content": "query"},
    }

    def _repair_argument_aliases(
        self,
        process_key: str,
        parameters: dict[str, object],
    ) -> dict[str, object]:
        """Apply deterministic argument-name aliases for known model confusions.

        Returns parameters (possibly mutated) with corrected keys.
        Only fires when the canonical name is absent and the alias is present as a str.
        """
        for suffix, aliases in self._ARGUMENT_ALIAS_MAP.items():
            if not process_key.endswith(suffix):
                continue
            for wrong, correct in aliases.items():
                if (
                    correct not in parameters
                    and wrong in parameters
                    and isinstance(parameters[wrong], str)
                ):
                    parameters[correct] = parameters.pop(wrong)
                    logger.info(
                        "ALIAS REPAIR: %s — renamed argument '%s' → '%s'",
                        process_key,
                        wrong,
                        correct,
                    )
        return parameters

    def _clamp_schema_properties(
        self,
        process_key: str,
        parameters: dict[str, object],
        schema_properties: dict[str, object],
    ) -> dict[str, object]:
        """Clamp numeric parameter values to their schema min/max bounds."""
        result = dict(parameters)
        for key, prop_schema in schema_properties.items():
            if not isinstance(prop_schema, dict) or key not in result:
                continue
            val = result[key]
            if not isinstance(val, (int, float)):
                continue
            mn = prop_schema.get("minimum")
            mx = prop_schema.get("maximum")
            if isinstance(mn, (int, float)) and val < mn:
                logger.warning(
                    "VALIDATION_CLAMP: %s.%s clamped %r → %r (minimum)",
                    process_key, key, val, mn,
                )
                result[key] = type(val)(mn)
            elif isinstance(mx, (int, float)) and val > mx:
                logger.warning(
                    "VALIDATION_CLAMP: %s.%s clamped %r → %r (maximum)",
                    process_key, key, val, mx,
                )
                result[key] = type(val)(mx)
        return result

    def _build_validation_error_message(
        self,
        process_key: str,
        errors: list[str],
        schema: dict[str, object],
    ) -> str:
        error_lines = [
            f"Argument validation failed for process '{process_key}':",
            "",
        ]
        for error in errors:
            error_lines.append(f"  - {error}")
        error_lines.append("")
        error_lines.append("Canonical argument schema:")
        error_lines.append(json.dumps(schema, default=str))
        error_lines.append("")
        error_lines.append("Retry the action with arguments that conform to this schema.")
        return "\n".join(error_lines)

    async def _process_action(self, action: QueuedAction) -> None:
        """
        Process a single action using fire-and-forget pattern.

        Actions are marked as 'completed' immediately after successful dispatch.
        Long-running operations (like inference) should create new actions for their responses.

        Phase 1: ExecutionContext lifecycle management integrated.
        Phase 2: FRG token lifecycle management.
        """
        # Note: Actions are already marked as 'processing' in _poll_once before this method is called

        # REL-03 swap-window guard: drop a result/error-processing sibling of an
        # already-terminated flow WITHOUT executing it. During a blue-green
        # cutover a failed flow's already-queued process_error/process_results
        # siblings would each re-hit ``_resolve_io_process_key`` (empty
        # source_namespace), emit a full traceback, and re-terminate the dead
        # flow — the observed burst. The first terminate tombstones the flow;
        # every later sibling short-circuits here. Checked before
        # ``_prepare_action_for_execution`` so no FRG token / ExecutionContext
        # work happens for a doomed action.
        if self._is_terminated_flow_sibling(action):
            self._drop_terminated_flow_sibling(action)
            return

        if not self._prepare_action_for_execution(action):
            return

        try:
            self._resolve_io_context(action)

            # Execute the action via ActionProcessor (dispatch only, don't wait for completion)
            # SMART FIX: Use thread pool for blocking actions, main thread for console actions
            if self._requires_main_thread(action.process_key):
                result = self.action_processor.execute_action(action)
            else:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, self.action_processor.execute_action, action
                )

            # Actions are marked as 'completed' immediately after successful dispatch
            # The actual work (like inference) happens asynchronously and creates new actions for responses
            is_success = bool(result.get("success", False))
            if is_success:
                self._mark_action_completed(action.id, result, action.flow_token_id)
            else:
                # Only mark as failed if there was an immediate error during dispatch
                error_value = result.get("error", "Unknown error")
                error_msg = str(error_value) if error_value is not None else "Unknown error"
                # ``error_value`` is frequently a full plugin ErrorDetail dict;
                # str() above keeps the readable message, and the typed copy
                # rides alongside it instead of being thrown away.
                self._mark_action_failed(
                    action.id,
                    error_msg,
                    action.flow_token_id,
                    error_detail=_typed_error_detail(error_value),
                )
                logger.error(f"Action {action.id} failed to dispatch: {error_msg}")

            # Tool use recording disabled - context already contains tool execution info
            # and storing parameters risks overfitting on past usage patterns.
            # See: conversation with user about tool_use memory value
            # self._record_tool_use(action, result, is_success)

        except Exception as e:
            # Immediate dispatch failure
            logger.error(f"Failed to dispatch action {action.id}: {e}", exc_info=True)
            self._mark_action_failed(
                action.id,
                str(e),
                action.flow_token_id,
                error_detail=_typed_error_detail(e),
            )

    def _prepare_action_for_execution(self, action: QueuedAction) -> bool:
        """Returns True if the action is ready for execution, False if it was rejected."""
        # FRG: Update token state to DISPATCHED before execution
        if action.flow_token_id:
            self._flow_runtime_graph.update_token_state(action.flow_token_id, TokenState.DISPATCHED)

        # PHASE 1: Create ExecutionContext for flow if it doesn't exist
        if self.execution_context_manager and action.flow_id:
            if not self.execution_context_manager.has_context(action.flow_id):
                self.execution_context_manager.create_context(action.flow_id)

        # Pre-execution validation: check arguments against canonical invocation_schema
        is_valid, validation_error, canonical_schema = self._validate_action_arguments(action)
        if not is_valid:
            logger.info(
                f"VALIDATION FAILED: {action.process_key} (id={action.id}) - routing to error processor"
            )
            self._mark_action_failed(
                action.id,
                validation_error or "Argument validation failed",
                action.flow_token_id,
                canonical_schema=canonical_schema,
            )
            return False

        return True

    def _resolve_io_context(self, action: QueuedAction) -> None:
        # Post-validation: resolve attachment name strings to full objects for post_message
        # Must happen AFTER validation (which checks raw arguments) and BEFORE execution
        self._resolve_attachments_for_execution(action)

        # Resolve job_result_ref for IO post_message actions (reads from explicit field)
        if self._is_io_post_message_queued(action):
            self._resolve_job_result_ref(action)
            self._inject_session_id_if_missing(action)

    def _mark_action_processing(self, action_id: str) -> bool:
        """Atomically claim one queued action, returning whether this poller won.

        The ``id`` plus ``status='queued'`` predicate is the state-interface
        compare-and-set: only one concurrent poller can transition a row to
        ``processing``.  A zero-row result is an expected lost claim (another
        poller, cancellation, or deletion won the transition), not a reason to
        execute a stale fetched row.  Any malformed or failed state-service
        result is raised rather than silently treated as a lost claim.
        """
        result = self.state_service.update_state(
            namespace="core",
            query={
                "table": "action_events",
                "filters": {
                    "id": action_id,
                    "status": ActionStatus.QUEUED.value,
                },
            },
            updates={"status": ActionStatus.PROCESSING.value},
        )
        if result.get("action_status") not in _COMPLETED_READ_STATUSES:
            raise RuntimeError(
                f"Failed to claim queued action {action_id}: "
                f"state-service status={result.get('action_status')!r}"
            )
        data = result.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"Failed to claim queued action {action_id}: missing data envelope")
        inner = data.get("result")
        if not isinstance(inner, dict):
            raise RuntimeError(
                f"Failed to claim queued action {action_id}: missing result envelope"
            )
        updated = inner.get("updated")
        if isinstance(updated, bool) or not isinstance(updated, int):
            raise RuntimeError(
                f"Failed to claim queued action {action_id}: invalid updated count {updated!r}"
            )
        if updated > 1:
            raise RuntimeError(
                f"Claim of action {action_id} updated {updated} rows (expected at most one)"
            )
        return updated == 1

    def _update_action_status_to_completed(self, action_id: str) -> None:
        """Complete the action AND clear any ``error_message`` a prior attempt left.

        This writer used to set ``status`` alone, while its failure counterpart
        ``_update_action_status_to_failed`` sets ``status`` *and*
        ``error_message``. That asymmetry is the whole of adopter issue #9:
        ``error_message`` is one mutable column on the ``action_events`` row,
        but ``result`` is the LATEST of many ``action_results`` rows for the
        same action. When one action row executes twice — attempt 1 fails and
        stamps the column, attempt 2 succeeds and appends a result row — the
        envelope reader (``PlatformSurface.process_result``) pairs attempt 1's
        error with attempt 2's success. Both halves are real; they are from
        different executions.

        Clearing the column fixes the envelope. Clearing it SILENTLY would be
        worse than the bug: the stale column is currently the only surviving
        evidence that an action executed twice at all, so a bare clear would
        close the symptom and delete the instrument. Hence the split below —
        clear the field, and treat having had something to clear as a detected
        double execution worth announcing.

        The status write stays faithful (``WHERE id =``) and zero-affected
        stays tolerated: a status write that misses a deleted/cancelled row
        must never raise inside the drain loop.
        """
        stale_error = self._read_stale_error_message(action_id)
        self.state_service.update_state(
            namespace="core",
            query={"table": "action_events", "filters": {"id": action_id}},
            updates={
                "status": ActionStatus.COMPLETED.value,
                # Explicit NULL, not an omission: the column is the merge
                # surface, so leaving it alone is what produced #9.
                "error_message": None,
            },
        )
        if stale_error is not None:
            self._report_double_execution(action_id, stale_error)

    def _read_stale_error_message(self, action_id: str) -> str | None:
        """The action row's current ``error_message``, or ``None`` if it has none.

        Read immediately BEFORE the success write, because the write destroys
        it. A non-empty value here cannot have come from this execution — the
        dispatch branch is a strict ``if is_success: mark_completed else:
        mark_failed``, so one execution never travels both paths. It therefore
        means an EARLIER attempt at this same action row already failed.

        A read that does not come back is a blind spot for the detector, not a
        reason to fail the completion: it is announced (below) rather than
        swallowed, so a detector that has gone deaf is visible as such instead
        of looking like a quiet, race-free platform.
        """
        read = self.state_service.query_state(
            "core",
            {"table": "action_events", "filters": {"id": action_id}},
        )
        if read.get("action_status") not in _COMPLETED_READ_STATUSES:
            logger.warning(
                "DOUBLE-EXECUTION DETECTOR BLIND: could not read action_events "
                "for %s before clearing error_message (read envelope status=%r); "
                "a stale error on this row would be cleared unannounced.",
                action_id,
                read.get("action_status"),
            )
            return None
        record = self._first_record(read)
        if record is None:
            # Routine: the row was deleted or cancelled between claim and
            # completion. Zero-affected is already tolerated by the write.
            return None
        value = self._opt_str(record, "error_message")
        if value is None or not value.strip():
            return None
        return value

    def _report_double_execution(self, action_id: str, stale_error: str) -> None:
        """Announce that one action row was executed more than once.

        Called only when the success path found a non-empty ``error_message``
        to clear. That is evidence, not housekeeping — the two attempts leave
        no per-process provenance in the rows, so this log line is currently
        the only place the race is observable at all. It carries the action_id
        and both timestamps so the gap between the attempts can be measured
        without re-deriving it from the tables.

        Deliberately WARNING, not DEBUG: the poller logs its successes at
        DEBUG, and a signal that only appears when debug logging happens to be
        on is not an instrument.
        """
        self.total_double_executions_detected += 1
        previous_at, previous_rows = self._previous_result_row_summary(action_id)
        logger.warning(
            "DOUBLE-EXECUTION DETECTED: action %s completed, but an earlier "
            "attempt at the SAME action row had already failed and stamped "
            "error_message. Clearing the stale error. "
            "previous_attempt_result_at=%s this_completion_at=%s "
            "previous_result_rows=%d detections_this_process=%d "
            "cleared_error_message=%r",
            action_id,
            previous_at if previous_at is not None else "unknown",
            datetime.now(UTC).isoformat(),
            previous_rows,
            self.total_double_executions_detected,
            stale_error,
        )

    def _previous_result_row_summary(self, action_id: str) -> tuple[str | None, int]:
        """``(created_at of the newest result row so far, how many exist)``.

        Read on the detection path only. ``_mark_action_completed`` stores THIS
        execution's result row after the status write, so every row visible
        here belongs to an earlier execution — the newest of them is when the
        attempt that stamped ``error_message`` recorded its outcome.

        Returns ``(None, 0)`` when the earlier attempt recorded no result row
        at all. That combination is itself informative: it is the signature of
        the adjacent defect where a second execution flips an already-failed
        action to ``completed`` and takes the early return before storing
        anything. Reporting it honestly beats inventing a timestamp.
        """
        read = self.state_service.query_state(
            "core",
            {"table": "action_results", "filters": {"core__action_events_id": action_id}},
        )
        records = self._query_records(read)
        if not records:
            return None, 0
        newest = max(records, key=lambda r: str(r.get("created_at") or ""))
        created_at = newest.get("created_at")
        # Stringify rather than type-guard: providers serialise this column as
        # an ISO string, but a datetime here must still reach the log line —
        # dropping it would report "unknown" for a timestamp we actually have.
        return (None if created_at is None else str(created_at)), len(records)

    def _retrieve_action_details(
        self,
        action_id: str,
    ) -> (
        tuple[
            str,
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
            dict[str, object] | None,
            str | None,
            str | None,
            str | None,
        ]
        | None
    ):
        """Retrieve action details for result processing.

        Returns:
            (process_key, notes, result_processor, result_processor_target,
             session_id, flow_id, context_id, parameters,
             result_processor_kind, error_processor, error_processor_kind)
            or None. process_key is guaranteed to be a non-empty string if tuple is returned.
            result_processor_kind and error_processor_kind are the
            persisted step-level kinds (one of the enum values) or None
            for actions that did not originate from a plan-derived
            execution.

        Raises:
            ValueError: If action has invalid process_key
        """
        action_result = self.state_service.query_state(
            "core",
            {"table": "action_events", "filters": {"id": action_id}},
        )

        record = self._first_record(action_result)
        if not record:
            return None

        process_key = self._validate_process_key(action_id, record.get("process_key"))
        parameters = self._parse_action_parameters(record.get("parameters"))
        return (
            process_key,
            self._opt_str(record, "notes"),
            self._opt_str(record, "result_processor"),
            self._opt_str(record, "result_processor_target"),
            self._opt_str(record, "core__sessions_id"),
            self._opt_str(record, "core__flows_id"),
            self._opt_str(record, "context_id"),
            parameters,
            self._opt_str(record, "result_processor_kind"),
            self._opt_str(record, "error_processor"),
            self._opt_str(record, "error_processor_kind"),
        )

    def _parse_action_parameters(self, raw: object) -> dict[str, object] | None:
        """Parse action parameters from DB record value.

        Parameters are stored as JSON string in the database.
        Returns parsed dict or None if missing/invalid.
        """
        if raw is None:
            return None
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else None
            except (json.JSONDecodeError, ValueError):
                return None
        return None

    def _store_action_result(
        self, action_id: str, result: dict[str, object], process_key: str
    ) -> None:
        """Store the result data in core__action_results table.

        Raises:
            RuntimeError: If storing the result fails
        """
        import json

        result_record = {
            "core__action_events_id": action_id,
            "result_data": json.dumps(result),
            "result_source": process_key,
        }

        store_result = self.state_service.write_state(
            namespace="core", data={"table": "action_results", "record": result_record}
        )

        if store_result.get("action_status") != "completed":
            raise RuntimeError(f"Failed to store action result for {action_id}: {store_result}")

    def _is_sql_query_result(self, result_data: object) -> bool:
        """Check if result data contains SQL query results."""
        return (
            isinstance(result_data, dict)
            and "data" in result_data
            and "records" in result_data["data"]
        )

    def _extract_sql_records(self, result: dict[str, object]) -> dict[str, object] | list[object]:
        """Extract SQL query records from nested result structure."""
        data = result.get("data")
        if not isinstance(data, dict):
            return result

        if not self._is_sql_query_result(data):
            return result

        # SQL query result structure: result["data"]["data"]["records"]
        nested_data = data.get("data")
        if not isinstance(nested_data, dict):
            return result

        records = nested_data.get("records")
        if not isinstance(records, list):
            return result

        return self._format_sql_records(records)

    def _format_sql_records(self, records: list[object]) -> list[object]:
        """Format SQL records for user-friendly display."""
        # Convert list of tuples to readable format for process_keys
        if records and len(records) > 0 and isinstance(records[0], list):
            # Extract first column (typically process_key)
            formatted_records: list[object] = []
            for record in records:
                if isinstance(record, list) and len(record) > 0:
                    formatted_records.append(record[0])
            return formatted_records
        return records

    def _extract_success_based_data(self, result: dict[str, object]) -> dict[str, object]:
        """Extract data based on success indicator."""
        if "success" in result and result.get("success"):
            # Use the data field if available, otherwise use success indicator
            data = result.get("data", result)
            if isinstance(data, dict):
                return data
            return result
        return result

    def _extract_result_data_for_template(
        self, result: dict[str, object]
    ) -> dict[str, object] | list[object]:
        """Extract useful data from result for template processing."""
        if "data" not in result:
            return result

        # Try SQL result extraction first
        sql_data = self._extract_sql_records(result)
        if sql_data is not result:
            return sql_data

        # Fall back to success-based extraction
        return self._extract_success_based_data(result)

    def _should_skip_result_processor(self, process_key: str | None) -> bool:
        """Check if action should skip result processor.

        Skips for:
        - self-completing processes that submit their own completion actions
        - async processes whose user-visible completion is handled by AsyncJobManager
        """
        if not process_key:
            return False

        if self._handles_own_completion(process_key):
            return True
        if self._is_async_process(process_key):
            return True
        return False

    def _validate_result_processor_requirements(self, result_processor: str | None) -> bool:
        """Validate that result processor and required dependencies are available."""
        if not (result_processor and self.action_factory):
            return False
        return True

    @staticmethod
    def _apply_result_processor_target_override(
        result_processor: str | None,
        result_processor_target: str | None,
    ) -> str | None:
        """Replace the process_key in a result_processor template with the override target.

        When an action carries ``result_processor_target``, the default
        target VERTEX in the template is replaced so results route to
        the override instead (e.g., ``process_planning_results`` instead
        of the default ``process_results``).

        Returns the modified JSON string, or the original if no override.
        """
        if not result_processor_target or not result_processor:
            return result_processor

        parsed = json.loads(result_processor)
        if isinstance(parsed, dict):
            parsed["process_key"] = result_processor_target
            return json.dumps(parsed)
        return result_processor

    def _inject_context_field_into_template(
        self, template_data: dict[str, object], field_name: str, field_value: str | None
    ) -> None:
        """Inject action context into template metadata, never process arguments."""
        if not field_value or field_name in template_data:
            return

        template_data[field_name] = field_value

    def _inject_skip_semantic_recall_into_template(self, template_data: dict[str, object]) -> None:
        """Inject skip_semantic_recall=True into template metadata.

        Result processor actions should skip semantic recall because:
        1. They present action results, not respond to new user queries
        2. Semantic recall uses the original user input, which can pull stale memories
        3. Memory pollution causes the LLM to respond with unrelated context

        See: knowledge_base/2026-02-05_claude_memory_system_refactor_v2.md
        """
        template_data[SKIP_SEMANTIC_RECALL_KEY] = True

    def _prepare_template_data_and_context(
        self,
        result_processor: str | None,
        session_id: str | None,
        flow_id: str | None,
        context_id: str | None,
    ) -> dict[str, object]:
        """Parse and prepare template data with inherited context."""
        import json

        if result_processor is None:
            raise ValueError("result_processor cannot be None")

        parsed = json.loads(result_processor)
        if not isinstance(parsed, dict):
            raise ValueError(f"result_processor must be a JSON object, got {type(parsed)}")

        template_data: dict[str, object] = parsed

        self._inject_context_field_into_template(template_data, CONTEXT_KEY_SESSION_ID, session_id)
        self._inject_context_field_into_template(template_data, CONTEXT_KEY_FLOW_ID, flow_id)
        self._inject_context_field_into_template(template_data, "context_id", context_id)

        # Skip semantic recall for result processor actions.
        # Result processors present action results to users - they don't need semantic memory
        # which can pollute context with stale/unrelated memories from previous conversations.
        # See: knowledge_base/2026-02-05_claude_memory_system_refactor_v2.md
        self._inject_skip_semantic_recall_into_template(template_data)

        return template_data

    def _build_results_data(
        self,
        action_id: str,
        result: dict[str, object],
        session_id: str | None,
        flow_id: str | None,
        context_id: str | None,
        process_key: str | None,
        notes: str | None,
        action_parameters: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Build results dictionary for template substitution."""
        result_data = self._extract_result_data_for_template(result)

        results_data: dict[str, object] = {
            TEMPLATE_VAR_RESULT: result_data,
            TEMPLATE_VAR_ACTION_ID: action_id,
            TEMPLATE_VAR_SESSION_ID: session_id,
            TEMPLATE_VAR_FLOW_ID: flow_id,
            TEMPLATE_VAR_PROCESS_KEY: process_key,
            TEMPLATE_VAR_ACTION_ARGUMENTS: action_parameters or {},
        }
        # Extract available_attachments for inline template resolution in user instructions.
        # Observation pipeline moved action_result to assistant message, so the attachment
        # list must be resolved directly in the user message via <<AVAILABLE_ATTACHMENTS>>.
        data = result.get("data")
        if isinstance(data, dict):
            available = data.get("available_attachments")
            if isinstance(available, list):
                results_data[TEMPLATE_VAR_AVAILABLE_ATTACHMENTS] = available

        # context_id for ActionFactory._inject_session_context to propagate to child actions
        if context_id:
            results_data["context_id"] = context_id
        if notes:
            results_data[TEMPLATE_VAR_NOTES] = notes

        if isinstance(result_data, dict):
            self._merge_result_variables(results_data, result_data)
        else:
            data_field = result.get("data")
            if isinstance(data_field, dict):
                self._merge_result_variables(results_data, data_field)

        return results_data

    def _prepare_template_copy(
        self,
        template: dict[str, object],
        session_id: str | None,
        flow_id: str | None,
        context_id: str | None,
        notes: str | None,
    ) -> dict[str, object]:
        """Prepare a template copy with session context injected."""
        template_copy = deepcopy(template)
        if session_id and CONTEXT_KEY_SESSION_ID not in template_copy:
            template_copy[CONTEXT_KEY_SESSION_ID] = session_id
        if flow_id and CONTEXT_KEY_FLOW_ID not in template_copy:
            template_copy[CONTEXT_KEY_FLOW_ID] = flow_id
        if context_id and "context_id" not in template_copy:
            template_copy["context_id"] = context_id
        if notes and "notes" not in template_copy:
            template_copy["notes"] = notes[:NOTES_MAX_LENGTH]
        return template_copy

    def _extract_templates_to_submit(
        self,
        template_data: dict[str, object],
        session_id: str | None,
        flow_id: str | None,
        context_id: str | None,
        notes: str | None,
    ) -> list[dict[str, object]]:
        """Extract list of templates to submit from template_data."""
        templates: list[dict[str, object]] = []

        actions = template_data.get("actions")
        if isinstance(actions, list):
            for idx, action_template in enumerate(actions):
                if not isinstance(action_template, dict):
                    logger.error(
                        f"RESULT-PROCESSOR: Skipping invalid action template at index {idx}"
                    )
                    continue
                templates.append(
                    self._prepare_template_copy(
                        action_template, session_id, flow_id, context_id, notes
                    )
                )
        else:
            templates.append(
                self._prepare_template_copy(template_data, session_id, flow_id, context_id, notes)
            )

        return templates

    def _execute_action_factory_template_processing(
        self,
        action_id: str,
        result: dict[str, object],
        template_data: dict[str, object],
        session_id: str | None,
        flow_id: str | None,
        context_id: str | None,
        notes: str | None,
        process_key: str | None = None,
        action_parameters: dict[str, object] | None = None,
    ) -> bool:
        """Execute template processing through ActionFactory and handle results.

        Raises:
            RuntimeError: If template submission fails
        """
        results_data = self._build_results_data(
            action_id,
            result,
            session_id,
            flow_id,
            context_id,
            process_key,
            notes,
            action_parameters,
        )
        templates = self._extract_templates_to_submit(
            template_data, session_id, flow_id, context_id, notes
        )

        # NOTE: oneOf schema injection REMOVED - it caused LM Studio hangs
        # The model reads process schemas from SYSTEM message (built-in) and
        # USER message (discovery results). The response_format should validate
        # structure only, not enumerate all possible processes.
        # See: knowledge_base/2026-02-02_inference_and_discord_troubleshooting.md

        for action_template in templates:
            template_result = self.action_factory.submit_result_with_template(
                results_data, action_template
            )
            if not template_result:
                raise RuntimeError(f"Template submission failed for action {action_id}")

        return True

    # ── Dead oneOf injection helpers removed ──────────────────────────
    # The following block (lines ~1551-1987 in prior versions) contained
    # _inject_process_keys_into_templates and ~15 helper methods that
    # built oneOf-based action schemas.  These caused LM Studio grammar
    # compilation hangs and were replaced by enum-based step-narrowed
    # schemas in DecodeContractStage.  The helpers were never called
    # after that replacement but remained as dead code.
    #
    # Deleted 2026-04-17 per CRITICAL_GRAMMAR_RULES.md R13/R14.
    # ────────────────────────────────────────────────────────────────────

    # (dead oneOf helpers deleted — see comment above)

    def _merge_result_variables(
        self,
        results_data: dict[str, object],
        payload: dict[str, object],
        visited: set[int] | None = None,
    ) -> None:
        """Recursively merge payload dict into results_data with lowercase and uppercase aliases."""
        if visited is None:
            visited = set()

        payload_id = id(payload)
        if payload_id in visited:
            return
        visited.add(payload_id)

        for key, value in payload.items():
            if key not in results_data:
                results_data[key] = value

            upper_key = key.upper()
            if upper_key not in results_data:
                results_data[upper_key] = value

            if isinstance(value, dict):
                self._merge_result_variables(results_data, value, visited)

    def _process_result_processor_template(
        self,
        action_id: str,
        result: dict[str, object],
        process_key: str | None,
        notes: str | None,
        result_processor: str | None,
        session_id: str | None,
        flow_id: str | None,
        context_id: str | None,
        action_parameters: dict[str, object] | None = None,
    ) -> None:
        """Process result_processor template if present (DATABASE-FIRST)."""
        if self._should_skip_result_processor(process_key):
            return

        # NOTE: no_matches discovery results now flow through to inference like other results.
        # The LLM can interpret context and retry discovery with better keywords.
        # See: knowledge_base/.archive/2026-01-16_claude_full_mar_prompting.md for analysis.

        if not self._validate_result_processor_requirements(result_processor):
            return

        template_data = self._prepare_template_data_and_context(
            result_processor, session_id, flow_id, context_id
        )
        self._execute_action_factory_template_processing(
            action_id,
            result,
            template_data,
            session_id,
            flow_id,
            context_id,
            notes,
            process_key,
            action_parameters,
        )

    def _submit_returned_actions(
        self,
        result: dict[str, object],
        notes: str | None,
        session_id: str | None,
        flow_id: str | None,
        context_id: str | None,
    ) -> bool:
        """Submit actions returned by a plugin to the action queue (Pattern 6a support)."""
        actions_list = result.get("actions")

        if not isinstance(actions_list, list) or not actions_list:
            return False

        # Fail fast: flow_id is required for action submission
        if not flow_id:
            raise ValueError("Cannot submit returned actions without flow_id")

        compilation_context = self._build_timestamp_context(session_id, flow_id)

        for i, action_def in enumerate(actions_list):
            success = self._submit_single_returned_action(
                action_def,
                i,
                len(actions_list),
                notes,
                session_id,
                flow_id,
                context_id,
                compilation_context,
            )
            if not success:
                return False
        return True

    def _submit_single_returned_action(
        self,
        action_def: object,
        index: int,
        total: int,
        notes: str | None,
        session_id: str | None,
        flow_id: str,
        context_id: str | None,
        compilation_context: dict[str, object],
    ) -> bool:
        """Submit a single returned action to the queue.

        Returns:
            True if action was submitted successfully, False if it failed
            and was routed to process_error.
        """
        if not isinstance(action_def, dict):
            raise ValueError(
                f"Invalid action definition at index {index}: expected dict, got {type(action_def)}"
            )

        # Fail-fast: post_message requires context_id in platform mode
        # Without context_id, OUTPUT events cannot be written to conversation history.
        # Check both the inherited parent context_id AND the action_def's own context_id
        # (the inference plugin sets context_id directly on generated actions).
        effective_context_id = context_id or action_def.get("context_id")
        if (
            self._context_management_service is not None
            and self._is_io_post_message_action(action_def)
            and effective_context_id is None
        ):
            raise ValueError(
                f"post_message action at index {index} requires context_id in platform mode. "
                "Without context_id, ASSISTANT messages cannot be added to conversation history."
            )

        self._inject_context_fields(action_def, session_id, flow_id, context_id, notes)
        self._expand_json_arguments(action_def)

        try:
            self.action_factory.submit_action_definition(action_def, context=compilation_context)
            return True
        except Exception as e:
            process_key = str(action_def.get("process_key", "unknown"))
            failed_args = action_def.get("arguments")
            canonical_schema = self._get_canonical_arguments_schema(process_key)
            logger.error(
                f"Invalid LLM-returned action {index + 1}/{total}: {e}. Routing to process_error."
            )
            self._route_failed_edge_to_inference(
                error_message=f"Action submission failed: {e}",
                process_key=process_key,
                failed_arguments=failed_args if isinstance(failed_args, dict) else None,
                notes=notes,
                session_id=session_id,
                flow_id=flow_id,
                context_id=context_id,
                canonical_schema=canonical_schema,
            )
            return False

    def _inject_context_fields(
        self,
        action_def: dict[str, object],
        session_id: str | None,
        flow_id: str,
        context_id: str | None,
        notes: str | None,
    ) -> None:
        """Inject session_id, flow_id, context_id, and notes into action definition.

        Args:
            action_def: Action definition to inject fields into
            session_id: Optional session ID
            flow_id: Required flow ID (all actions require flow context)
            context_id: Platform context ID for OUTPUT event correlation
            notes: Optional notes to attach
        """
        if session_id and CONTEXT_KEY_SESSION_ID not in action_def:
            action_def[CONTEXT_KEY_SESSION_ID] = session_id
        # NOTE: session_id is NOT injected into arguments — the model reads it from
        # the user message metadata trailer and passes it explicitly when needed
        # (e.g., post_message). Non-IO actions (search, discovery) don't need it.
        # flow_id: inject the parent flow ONLY when absent — an explicit stamp
        # is intentional routing (the joseki run driver stamps its own run
        # flow; the unconditional overwrite silently re-homed the whole chain
        # onto the caller's flow, starving the run-flow-scoped reconciler —
        # Track-A first production run, 2026-07-05). Model-generated actions
        # can never carry flow_id (output schema: additionalProperties false),
        # so the parent-flow default still applies to every inference action.
        if CONTEXT_KEY_FLOW_ID not in action_def:
            action_def[CONTEXT_KEY_FLOW_ID] = flow_id
        # context_id for platform OUTPUT events - inherit from parent action
        if context_id and "context_id" not in action_def:
            action_def["context_id"] = context_id

        # Inject parent notes only if action doesn't already have its own notes.
        if notes and TEMPLATE_VAR_NOTES not in action_def:
            action_def[TEMPLATE_VAR_NOTES] = notes[:NOTES_MAX_LENGTH]

    def _build_timestamp_context(
        self, session_id: str | None, flow_id: str | None
    ) -> dict[str, object]:
        """Build compilation context with timestamp variables."""
        import time
        from datetime import UTC, datetime

        now_utc = datetime.now(UTC)
        now_local = datetime.now()

        if time.daylight:
            tz_offset = time.altzone
            tz_name = time.tzname[1]
        else:
            tz_offset = time.timezone
            tz_name = time.tzname[0]

        tz_hours = -tz_offset // 3600
        tz_sign = "+" if tz_hours >= 0 else "-"
        tz_str = f"UTC{tz_sign}{abs(tz_hours)}"

        # Normalize IDs - only include if valid (never empty strings)
        normalized_session_id = normalize_session_id(session_id)
        normalized_flow_id = normalize_flow_id(flow_id)

        context: dict[str, object] = {
            CONTEXT_KEY_TIMESTAMP: now_utc.isoformat(),
            CONTEXT_KEY_DATE: now_local.strftime("%Y-%m-%d"),
            CONTEXT_KEY_TIME: now_local.strftime(f"%H:%M:%S {tz_name}"),
            CONTEXT_KEY_TIMEZONE: tz_name,
            CONTEXT_KEY_TIMEZONE_OFFSET: tz_str,
        }

        # Only include IDs if they're valid - prevents empty string propagation
        # Use canonical lowercase keys (CONTEXT_KEY_*) for consistent resolution
        if normalized_session_id:
            context[CONTEXT_KEY_SESSION_ID] = normalized_session_id
        if normalized_flow_id:
            context[CONTEXT_KEY_FLOW_ID] = normalized_flow_id

        return context

    def _expand_json_arguments(self, action_def: dict[str, object]) -> None:
        """Merge JSON-encoded argument payloads into the argument dict."""
        arguments = action_def.get("arguments")
        if not isinstance(arguments, dict) or not arguments:
            return

        query_value = arguments.get("query")

        # Handle dict (already parsed) or string payloads
        parsed_payload: dict[str, object] | None = None
        if isinstance(query_value, dict):
            parsed_payload = query_value
        elif isinstance(query_value, str):
            parsed_payload = self._parse_query_json(query_value)

        if not parsed_payload:
            return

        for key, value in parsed_payload.items():
            if key not in arguments or arguments[key] is None:
                arguments[key] = value

    def _parse_query_json(self, payload: str) -> dict[str, object] | None:
        """Parse string payload as JSON dict if it looks like structured arguments."""
        candidate = payload.strip()
        if not candidate or not candidate.startswith("{") or "}" not in candidate:
            return None

        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(candidate)
            except (ValueError, SyntaxError):
                return None

        return parsed if isinstance(parsed, dict) else None

    def _should_skip_error_routing(
        self,
        process_key: str | None,
        error_message: str = "",
    ) -> bool:
        """Check if error routing should be skipped for this process.

        Inference service errors are generally terminal (model unavailable,
        truncation) and cannot be recovered by retrying through
        ``process_error``.  The exception is **step contract violations**,
        which indicate the model emitted valid JSON but picked the wrong
        process — a recoverable mistake that ``process_error`` can correct
        by re-prompting with the violation details.
        """
        if process_key and "inference_service" in process_key:
            recoverable_patterns = (
                "Step contract violation",
                "Planning-extension rewrite rejected",
                "LLM response parsing failed",
            )
            if any(p in error_message for p in recoverable_patterns):
                return False  # Recoverable — let error routing retry
            return True

        startup_keywords = ["start_console", "start_jsonrpc", "start_interface"]
        if process_key and any(x in process_key for x in startup_keywords):
            return True

        return False

    def _is_recoverable_error(
        self,
        process_key: str | None,
        error_message: str = "",
    ) -> bool:
        """Check if an error is recoverable via process_error retry.

        Any error that reaches error processing inference is recoverable
        by definition — ``_should_skip_error_routing`` already filtered
        out terminal errors (inference service failures, startup errors).
        Recoverable errors bypass the error budget so the model can
        self-correct with the failure visible in its conversation context.

        The error budget only catches infrastructure failures that slip
        through without matching any known recoverable pattern.
        """
        # Argument validation failures — the error message includes the
        # canonical schema so the model can retry with corrected arguments.
        if "Argument validation failed" in error_message:
            return True

        # Step contract violations — model picked wrong process, can retry.
        if "Step contract violation" in error_message:
            return True

        # Planning-extension rejections — model can adjust plan text.
        if "Planning-extension rewrite rejected" in error_message:
            return True

        # Plugin execution failures — runtime errors from audio processing,
        # synthesis, etc. that the model can recover from by adjusting
        # arguments or choosing a different approach.
        if process_key and "plugin::" in process_key:
            return True

        # Service interface errors (except inference_service, which is
        # handled as terminal by _should_skip_error_routing).
        if process_key and "service_interface::" in process_key:
            return True

        return False

    def _fetch_process_error_template(self) -> dict[str, object] | None:
        """Fetch process_error template from registry."""
        import json

        result = self.state_service.query_state(
            "core",
            {
                "table": "process_registry",
                "filters": {
                    "process_key": _INFERENCE_PROCESS_ERROR_KEY
                },
            },
        )

        record = self._first_record(result)
        if record is None:
            logger.error("ERROR-ROUTING: No process_error template found")
            return None

        template_json = record.get("action_definition_template")

        if not template_json:
            logger.error("ERROR-ROUTING: action_definition_template is empty")
            return None

        template = json.loads(template_json) if isinstance(template_json, str) else template_json
        return template if isinstance(template, dict) else None

    def _prepare_error_template(
        self,
        template: dict[str, object],
        session_id: str | None,
        flow_id: str | None,
        context_id: str | None,
        process_key: str,
    ) -> dict[str, object]:
        """Prepare error template with session context and model config.

        Template function resolution (<<<:...>>> patterns) is handled by ActionFactory
        via NewTemplateEngine, ensuring a single template resolution path.

        IMPORTANT: We use 'failed_process_key' instead of 'process_key' to avoid
        triggering _merge_template_with_base which would incorrectly merge the
        failed action's template with the error handler template.

        FAIL-FAST: Requires inference_model_name to be set - no fallback to 'default'.
        """
        template_copy = deepcopy(template)

        # Skip semantic recall — error recovery needs the error + plan,
        # not memories recalled against the original user query.
        template_copy[SKIP_SEMANTIC_RECALL_KEY] = True

        # Inject session context for propagation
        if session_id:
            template_copy[CONTEXT_KEY_SESSION_ID] = session_id
        if flow_id:
            template_copy[CONTEXT_KEY_FLOW_ID] = flow_id
        if context_id:
            template_copy["context_id"] = context_id

        # Inject failed_process_key for context (NOT process_key to avoid template merge)
        template_copy["failed_process_key"] = process_key

        # Inject model config - required for inference (FAIL-FAST: no defaults)
        if not self.inference_model_name:
            raise ValueError(
                "ActionQueuePoller requires inference_model_name for error routing. "
                "Cannot route errors without explicit model configuration."
            )

        arguments = template_copy.get("arguments", {})
        if isinstance(arguments, dict):
            params = arguments.get("params")
            if not isinstance(params, dict):
                raise ValueError("process_error template must declare an object arguments.params")
            # Only specify model name - inference plugin provides temperature/max_tokens from config
            params["model"] = {
                "name": self.inference_model_name,
            }
            template_copy["arguments"] = arguments

        return template_copy

    def _route_failed_edge_to_inference(
        self,
        error_message: str,
        process_key: str,
        failed_arguments: dict[str, object] | None,
        notes: str | None,
        session_id: str | None,
        flow_id: str | None,
        context_id: str | None,
        canonical_schema: dict[str, object] | None = None,
    ) -> bool:
        """Route failed edge/tool action to process_error inference for recovery.

        Raises:
            ValueError: If flow_id is missing
            RuntimeError: If template fetch or submission fails
        """

        # Fail fast: flow_id is required for template resolution
        if not flow_id:
            raise ValueError("ERROR-ROUTING: flow_id is required for template context")

        # Terminal errors: skip error routing entirely
        if self._should_skip_error_routing(process_key, error_message):
            logger.error(
                "ERROR-ROUTING: Skipping error routing for %s — terminating flow. Error: %s",
                process_key,
                error_message,
            )
            self._terminate_flow(flow_id)
            return False

        # Flows with no vertex binding (no IO origin AND no inference-vertex
        # binding) have no recipient for an inference-formatted error message.
        # Terminate the flow rather than burn an LLM call to format an
        # undeliverable error — process_error would itself crater at the
        # same source_namespace lookup, just one inference round-trip
        # later. See ``_flow_has_no_vertex_binding`` for the contract.
        if self._flow_has_no_vertex_binding(flow_id):
            logger.warning(
                "ERROR-ROUTING: Flow %s has no IO origin — terminating flow without "
                "process_error (programmatic submission failed: %s). Error: %s",
                flow_id,
                process_key,
                error_message,
            )
            self._terminate_flow(flow_id)
            return False

        # Recoverable errors bypass the flow-level circuit breaker but
        # are bounded per-process to prevent infinite retry loops.
        is_recoverable = self._is_recoverable_error(process_key, error_message)
        if is_recoverable:
            retry_key = f"{flow_id}:{process_key}"
            count = self._recoverable_error_counts.get(retry_key, 0) + 1
            self._recoverable_error_counts[retry_key] = count
            if count > self._max_recoverable_retries:
                logger.error(
                    "ERROR-ROUTING: Process %s exceeded max recoverable retries "
                    "(%d/%d) in flow %s — terminating flow. Latest: %s",
                    process_key,
                    count,
                    self._max_recoverable_retries,
                    flow_id,
                    error_message,
                )
                self._terminate_flow(flow_id)
                return False
        else:
            error_count = self._get_flow_error_count(flow_id)
            if error_count >= self._max_flow_errors:
                logger.error(
                    "ERROR-ROUTING: Flow %s exceeded max errors (%d/%d) — terminating flow. Latest: %s",
                    flow_id,
                    error_count,
                    self._max_flow_errors,
                    error_message,
                )
                self._terminate_flow(flow_id)
                return False

        template = self._fetch_process_error_template()
        if not template:
            raise RuntimeError("ERROR-ROUTING: Failed to fetch process_error template")

        # process_key validated above
        error_data = self._build_error_routing_data(
            error_message,
            process_key,
            failed_arguments,
            canonical_schema,
            session_id,
            flow_id,
            context_id,
            notes,
        )

        prepared_template = self._prepare_error_template(
            template, session_id, flow_id, context_id, process_key
        )

        # Submit through ActionFactory - template functions resolved via NewTemplateEngine
        submitted_id = self.action_factory.submit_result_with_template(
            error_data, prepared_template
        )
        if submitted_id:
            return True

        raise RuntimeError("ERROR-ROUTING: Failed to submit process_error action")

    def _build_error_routing_data(
        self,
        error_message: str,
        process_key: str,
        failed_arguments: dict[str, object] | None,
        canonical_schema: dict[str, object] | None,
        session_id: str | None,
        flow_id: str | None,
        context_id: str | None,
        notes: str | None,
    ) -> dict[str, object]:
        error_data: dict[str, object] = {
            TEMPLATE_VAR_ERROR: error_message,
            TEMPLATE_VAR_ACTION_ARGUMENTS: failed_arguments or {},
            TEMPLATE_VAR_FAILED_ACTION: {
                CONTEXT_KEY_PROCESS_KEY: process_key,
                "arguments": failed_arguments or {},
            },
            TEMPLATE_VAR_FAILED_PROCESS_KEY: process_key,
            TEMPLATE_VAR_CANONICAL_SCHEMA: canonical_schema or {},
            TEMPLATE_VAR_SESSION_ID: session_id,
            TEMPLATE_VAR_FLOW_ID: flow_id,
        }
        if context_id:
            error_data["context_id"] = context_id
        if notes:
            error_data[TEMPLATE_VAR_NOTES] = notes
        return error_data

    def _terminate_flow(self, flow_id: str) -> None:
        """Mark a flow as failed in the database.

        Called when the error circuit breaker fires or error routing is
        skipped.  The flow status is the canonical record — REST clients
        can poll it to learn that the flow ended in an error.

        Also tombstones the flow (REL-03) so any of its already-queued
        result/error-processing siblings are dropped before execution rather
        than each re-hitting ``_resolve_io_process_key`` and re-terminating the
        dead flow (the blue-green swap-window burst). This is the single
        terminate choke point for all four error-routing branches, so the
        tombstone covers every path that fails a flow here.
        """
        self.state_service.update_state(
            namespace="core",
            query={"table": "flows", "filters": {"id": flow_id}},
            updates={"status": "failed"},
        )
        self._remember_terminated_flow(flow_id)

    def _remember_terminated_flow(self, flow_id: str) -> None:
        """Record ``flow_id`` in the bounded-FIFO terminated-flow tombstone."""
        self._terminated_flow_ids.pop(flow_id, None)
        self._terminated_flow_ids[flow_id] = None
        while len(self._terminated_flow_ids) > _TERMINATED_FLOW_TOMBSTONE_CAP:
            self._terminated_flow_ids.popitem(last=False)

    def _is_terminated_flow_sibling(self, action: QueuedAction) -> bool:
        """True when ``action`` is a result/error-processing action
        (``process_error`` / ``process_results``) whose flow has already been
        terminated.

        Scoped to exactly the two VERTEX inference keys in
        ``_RESULT_ERROR_PROCESSING_KEYS`` so terminal EDGE_SINK deliveries,
        bridge deliver_error escape-valve actions, and cleanup for a failed
        flow all pass through untouched. Pure in-memory membership — the
        process_key frozenset test short-circuits for the vast majority of
        actions, and only these two keys pay the tombstone lookup, so the
        universal dequeue path takes on no DB read.
        """
        return (
            action.process_key in _RESULT_ERROR_PROCESSING_KEYS
            and action.flow_id is not None
            and action.flow_id in self._terminated_flow_ids
        )

    def _drop_terminated_flow_sibling(self, action: QueuedAction) -> None:
        """Mark a doomed result/error-processing sibling failed WITHOUT
        executing it or routing it back through error handling.

        Executing it would re-hit the same empty-source_namespace resolution,
        emit another full traceback, and re-terminate the already-failed flow
        (the swap-window burst this guard exists to collapse). ``FAILED`` is
        terminal, so the row is never re-polled. The FRG token is deliberately
        left alone: the flow already carries the canonical ``status=failed``
        record, continuation for a dead flow is moot, and skipping the token
        write avoids a missing-row raise inside the serial poll loop.
        """
        reason = (
            f"SWAP-GUARD: flow {action.flow_id} already terminated — dropping "
            f"queued {action.process_key} sibling (id={action.id}) without "
            "execution (REL-03 swap-window burst guard)"
        )
        logger.info(reason)
        self._update_action_status_to_failed(action.id, reason)

    def _extract_trigger_data_from_flow_row(
        self, flow_id: str,
    ) -> dict[str, object] | None:
        """Read the ``flows`` row for ``flow_id`` and return its parsed
        ``trigger_data`` dict, or ``None`` if the row is missing / malformed /
        carries no trigger payload.

        The defensive shape-unwinding lives here so callers stay simple.
        """
        result = self.state_service.read_state(
            namespace="core",
            query={"table": "flows", "filters": {"id": flow_id}},
        )
        data = result.get("data")
        if not isinstance(data, dict):
            return None
        records = data.get("records")
        if not isinstance(records, list) or not records:
            return None
        first = records[0]
        if not isinstance(first, dict):
            return None
        raw = first.get("trigger_data")
        if raw is None:
            return None
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None
        return None

    def _flow_has_no_vertex_binding(self, flow_id: str) -> bool:
        """True when the flow has no vertex binding for inference error
        delivery — meaning no IO source AND (per the v1 vertex routing
        design) no inference-vertex session binding either.

        Programmatic action submissions (boot-time starting actions,
        scheduled cron polls, system-internal flows) carry no inbound user
        message, so they have no IO plugin to post errors back to. Routing
        their failures through inference-driven ``process_error`` would
        burn an LLM call to format an explanation that has no recipient —
        the resulting ``process_error`` action then fails inside
        ``_resolve_io_process_key`` with "Empty source_namespace" anyway,
        terminating the flow after wasting the inference round-trip.

        Semantic (per
        ``workbench/2026-06-13_coding_agent_inference_interface_design_v3.md``
        §8 Step 5, CR6): now that MCP-initiated flows carry an
        ``inference_vertex_session_id`` / ``inference_vertex_role`` tag in
        ``trigger_data`` (added by ``_build_process_call_trigger_data`` in
        agent_messaging_plugin) AND the Phase-5 vertex resolver consumes
        them, this predicate returns ``True`` only when the flow has NO IO
        source AND NO vertex binding. A flow bound to a coding-agent session
        vertex routes ``process_error`` through that session's
        ``SessionInferenceProvider`` (or DEFERs when the session is absent)
        rather than the default plugin, so even without an IO origin the
        error has a real recipient — do not skip it.

        Reads the flow's ``trigger_data`` JSON column via
        ``_extract_trigger_data_from_flow_row`` to avoid threading an
        additional service dependency through the poller — same pattern
        as ``_terminate_flow`` above. Returns ``True`` if the flow row is
        missing too (treat as "no binding" rather than crashing the
        error-routing path).
        """
        trigger_data = self._extract_trigger_data_from_flow_row(flow_id)
        if trigger_data is None:
            return True
        source_namespace = trigger_data.get("source_namespace")
        has_io_source = isinstance(source_namespace, str) and bool(source_namespace)
        vertex_instance = trigger_data.get("inference_vertex_session_id")
        vertex_role = trigger_data.get("inference_vertex_role")
        has_vertex_binding = (
            (isinstance(vertex_instance, str) and bool(vertex_instance))
            or (isinstance(vertex_role, str) and bool(vertex_role))
        )
        return not has_io_source and not has_vertex_binding

    def _get_flow_error_count(self, flow_id: str) -> int:
        """Count failed actions in this flow — a scalar aggregate, no rows.

        THE READ IS GONE, not bounded (2026-08-15 PDT / 2026-08-16 UTC). This
        used to fetch every failed action row and return ``len(records)``, and
        the number it feeds is a CIRCUIT BREAKER: the caller terminates the flow
        once the count reaches ``_max_flow_errors`` (3).

        That made the old shape worse than the sibling defect in
        ``FlowRuntimeGraph.get_pending_token_count``, which at least failed
        loudly. Here ``_query_records`` graceful-degrades a refused read to
        ``[]`` — deliberately, so the poll loop keeps draining — so once the
        default row bound dropped to 100 this returned **0 errors for a flow with
        thousands**, ``0 >= 3`` was False, and **the breaker silently stopped
        firing.** Measured on the live trace: ``flow-ledger-periodic-poll`` had
        **2,902** failed actions, 29x the cap, against a threshold of 3.

        It is also the third site in one night whose bound fails exactly when it
        is needed: this one is only ever consulted on the failure path, so the
        read that was too big to complete was one taken *because the flow was
        already going wrong*.

        ``count`` runs the aggregate inside the owner plugin and ships a scalar,
        so it is outside the row cap entirely and cannot regress at any table
        size. Filters are unchanged, and ``count`` applies no automatic
        ``is_deleted`` exclusion — neither did the ``query_state`` it replaces.
        """
        result = self.state_service.count(
            "core",
            {
                "table": "action_events",
                "filters": {"flow_id_trace": flow_id, "status": ActionStatus.FAILED.value},
            },
        )
        value = self._query_scalar(result)
        if value is None:
            # FAIL OPEN, LOUDLY. Returning 0 here means "do not terminate", which
            # is the safe direction for a TRANSIENT read miss — a blip must not
            # kill a healthy flow, and the poll loop must keep draining. But the
            # silence is what let this defect run: an unreadable breaker input is
            # now an ERROR in the log naming the flow, so "the breaker is not
            # firing" can be distinguished from "there is nothing to fire on".
            logger.error(
                "ERROR-ROUTING: could not read the failed-action count for flow "
                "%s — the max-flow-errors breaker CANNOT FIRE this pass and the "
                "flow continues. Envelope: %r",
                flow_id,
                result,
            )
            return 0
        return value

    def _query_scalar(self, count_result: ActionResult) -> int | None:
        """Return the integer from a ``count`` envelope, or ``None`` if unusable.

        The scalar is NESTED at ``data.result.value``; reading ``data.value``
        would yield ``None`` on a perfectly healthy response. ``None`` is
        returned rather than 0 so the caller must decide what an unknown count
        means — 0 and "unknown" are the same value here and opposite facts.
        """
        if count_result.get("action_status") not in _COMPLETED_READ_STATUSES:
            return None
        data = count_result.get("data")
        inner = data.get("result") if isinstance(data, dict) else None
        value = inner.get("value") if isinstance(inner, dict) else None
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        return value

    def _record_tool_use(
        self,
        action: QueuedAction,
        result: dict[str, object],
        is_success: bool,
    ) -> None:
        """Record tool execution to memory for future reference.

        Called after action completion to build tool use history.
        This enables memory-centric tool selection: the system can query
        past tool uses to inform future tool selections.

        Args:
            action: The executed action
            result: The execution result
            is_success: Whether the action succeeded
        """
        if not self.memory_service:
            return

        try:
            # Import at runtime to avoid circular imports
            from ananta.services.memory_service.domain_classifier import classify_domain
            from ananta.services.memory_service.tool_use_types import (
                ToolResultStatus,
                create_tool_use_record,
            )

            # Determine result status
            if is_success:
                status = ToolResultStatus.SUCCESS
            else:
                status = ToolResultStatus.FAILURE

            # Parse arguments from JSON parameters
            try:
                arguments = json.loads(action.parameters) if action.parameters else {}
            except (json.JSONDecodeError, TypeError):
                arguments = {"raw": str(action.parameters)[:200]}

            # Get result data
            result_data = result.get("data", result.get("error", ""))

            # Classify domain from process_key
            domain = classify_domain(action.process_key)

            # Create record with proper truncation
            arguments_as_object: dict[str, object] = dict(arguments)
            record = create_tool_use_record(
                process_key=action.process_key,
                arguments=arguments_as_object,
                result_status=status,
                result_data=result_data,
                domain=domain,
                session_id=action.session_id,
                flow_id=action.flow_id,
            )

            # Store to memory
            self.memory_service.remember(
                content=record.to_memory_content(),
                tags=record.get_tags(),
                session_id=action.session_id,
            )

        except Exception as e:
            # Non-fatal: log and continue
            logger.error(f"Failed to record tool use for {action.process_key}: {e}")

    def _mark_action_completed(
        self,
        action_id: str,
        result: dict[str, object],
        flow_token_id: str | None = None,
    ) -> None:
        """
        Mark action as successfully completed with pure database-first result storage and template processing.

        ARCHITECTURAL SIMPLIFICATION: This method handles all result processing directly without events.
        REFACTORED: Extracted helper methods to reduce complexity from E(31) to manageable level.
        Phase 2: FRG token completion integrated.
        """

        # Step 1: Update action status to completed
        self._update_action_status_to_completed(action_id)

        # Step 2: Get action details for result processing
        action_details = self._retrieve_action_details(action_id)
        if not action_details:
            return

        (
            process_key,
            notes,
            result_processor,
            result_processor_target,
            session_id,
            flow_id,
            context_id,
            action_parameters,
            result_processor_kind,
            error_processor,
            error_processor_kind,
        ) = action_details

        # Log action completion at INFO level for visibility
        logger.debug(f"ACTION COMPLETED: {process_key} (id={action_id})")

        # Step 3: Store the result data in core__action_results table
        self._store_action_result(action_id, result, process_key)

        # Step 3.5: Extract attachments if blob_fields configured
        # This populates _pending_attachments and injects available_attachments
        attachments = self._extract_and_store_attachments(process_key, result, flow_id)

        # Step 3.6: Record WBS work products only after successful execution
        self._record_work_products_after_success(
            process_key,
            action_parameters,
            attachments,
            session_id,
        )

        # Step 4: Submit actions returned by the plugin (Pattern 6a support)
        # If any returned action fails validation, it is routed to process_error.
        # In that case, skip Step 5 — the error flow handles the response.
        actions_submitted = self._submit_returned_actions(
            result, notes, session_id, flow_id, context_id
        )

        dispatch_outcome = self._maybe_dispatch_result_processing(
            action_id=action_id,
            process_key=process_key,
            result=result,
            notes=notes,
            result_processor=result_processor,
            result_processor_target=result_processor_target,
            error_processor=error_processor,
            result_processor_kind=result_processor_kind,
            error_processor_kind=error_processor_kind,
            session_id=session_id,
            flow_id=flow_id,
            context_id=context_id,
            action_parameters=action_parameters,
            flow_token_id=flow_token_id,
            actions_submitted=actions_submitted,
        )

        # Step 6: FRG token completion
        # Generic gate: if the result carries blocks_continuation=True,
        # mark the token as failed to prevent automatic plan advancement.
        # Contract violations also block — the result row stays
        # completed, but the parent flow must not auto-advance until
        # the process-level error handler resolves the violation.
        blocked = (
            bool(result.get("blocks_continuation", False))
            or dispatch_outcome is DispatchOutcome.CONTRACT_VIOLATION_DISPATCHED
        )
        if blocked:
            logger.info(
                f"BLOCKED: {process_key} (id={action_id}) — "
                f"result blocks_continuation={result.get('blocks_continuation', False)}, "
                f"dispatch_outcome={dispatch_outcome}"
            )

        # Only complete token if no pending jobs are linked to it
        if flow_token_id:
            if self._token_has_pending_jobs(flow_token_id):
                # Token has pending jobs - transition to WAITING_JOB
                self._flow_runtime_graph.update_token_state(flow_token_id, TokenState.WAITING_JOB)
            else:
                # No pending jobs - complete the token
                # Default to "completed" if service didn't explicitly set action_status
                result_summary = {"action_status": result.get("action_status", "completed")}
                self._flow_runtime_graph.complete_token(
                    flow_token_id, success=not blocked, result_summary=result_summary
                )

    def _maybe_dispatch_result_processing(
        self,
        *,
        action_id: str,
        process_key: str,
        result: dict[str, object],
        notes: str | None,
        result_processor: str | None,
        result_processor_target: str | None,
        error_processor: str | None,
        result_processor_kind: str | None,
        error_processor_kind: str | None,
        session_id: str | None,
        flow_id: str | None,
        context_id: str | None,
        action_parameters: dict[str, object] | None,
        flow_token_id: str | None,
        actions_submitted: bool,
    ) -> DispatchOutcome | None:
        """Decide whether the completed action needs result-processing dispatch.

        Skips dispatch when:
        * the plugin returned ``actions`` but submission failed (the
          process_error flow is handling routing already); or
        * the action is EDGE_SINK — terminal, carries no routing fields.

        Otherwise delegates to :meth:`_dispatch_result_processing`.
        """
        if actions_submitted is False and result.get("actions"):
            logger.info(
                f"Skipping result_processor for {process_key}: "
                "returned action submission failed, routed to process_error"
            )
            return None
        # EDGE_SINK actions (e.g. ``deliver_result``, ``deliver_error``,
        # plugin ``start_interface``) carry no result-side routing — they
        # are terminal on success.  The coordinator's common-success
        # validator demands a kind, so route terminal actions around the
        # success dispatch entirely.  ``error_processor`` may still be
        # attached for failure handling; that path runs from
        # ``_mark_action_failed`` and is not gated here.
        if result_processor_kind is None and result_processor is None:
            logger.debug(
                f"EDGE_SINK_SKIP: {process_key} (id={action_id}) — terminal action, "
                "no dispatch"
            )
            return None
        logger.debug(
            f"FRG_CONTEXT_SET: Setting parent context to flow_token_id={flow_token_id}"
        )
        with result_processor_context(flow_token_id):
            return self._dispatch_result_processing(
                action_id=action_id,
                process_key=process_key,
                result=result,
                notes=notes,
                result_processor=result_processor,
                result_processor_target=result_processor_target,
                error_processor=error_processor,
                result_processor_kind=result_processor_kind,
                error_processor_kind=error_processor_kind,
                session_id=session_id,
                flow_id=flow_id,
                context_id=context_id,
                action_parameters=action_parameters,
                flow_token_id=flow_token_id,
            )

    def _dispatch_result_processing(
        self,
        *,
        action_id: str,
        process_key: str,
        result: dict[str, object],
        notes: str | None,
        result_processor: str | None,
        result_processor_target: str | None,
        error_processor: str | None,
        result_processor_kind: str | None,
        error_processor_kind: str | None,
        session_id: str | None,
        flow_id: str | None,
        context_id: str | None,
        action_parameters: dict[str, object] | None,
        flow_token_id: str | None,
    ) -> DispatchOutcome:
        """Dispatch a successful tool result through the coordinator.

        Owns:

        * coordinator lazy-construction (memory_service may be wired
          after :meth:`__init__`);
        * :class:`CompletedAction` snapshot assembly;
        * single delegation call to
          :meth:`SuccessfulResultCoordinator.handle_successful_result`.

        The coordinator decides whether the success path needs inference
        (existing ``process_results`` template), deterministic
        continuation, or bridge delivery.  Returns the
        :class:`DispatchOutcome` so the caller can gate parent-token
        completion (contract violations block continuation).
        """
        coordinator = self._get_result_processing_coordinator()
        completed = CompletedAction(
            action_id=action_id,
            process_key=process_key,
            parameters=action_parameters or {},
            notes=notes,
            result_processor=result_processor,
            error_processor=error_processor,
            result_processor_kind=(
                ResultProcessorKind(result_processor_kind)
                if result_processor_kind else None
            ),
            error_processor_kind=(
                ErrorProcessorKind(error_processor_kind)
                if error_processor_kind else None
            ),
            result_processor_target=result_processor_target,
            session_id=session_id,
            flow_id=flow_id,
            context_id=context_id,
        )
        return coordinator.handle_successful_result(
            completed=completed,
            result=result,
            # Pattern 6a actions have already been submitted by Step 4;
            # the coordinator's common-success validator must see an
            # empty list so the deterministic invariant is preserved
            # without breaking the inference path.
            plugin_returned_actions=(),
            flow_token_id=flow_token_id,
        )

    def _get_result_processing_coordinator(self) -> SuccessfulResultCoordinator:
        """Lazy-build the result-processing coordinator on first dispatch."""
        if self._result_processing_coordinator is None:
            from ananta.core.actions.result_processing_glue import (
                build_result_processing_coordinator,
            )
            self._result_processing_coordinator = (
                build_result_processing_coordinator(self)
            )
        return self._result_processing_coordinator

    def _get_error_dispatcher(self) -> ResultProcessingErrorDispatcher:
        """Lazy-build the shared error dispatcher.

        Used by execution-failure routing so it shares a structurally
        identical process-level error-handler submission path with the
        contract-violation route.
        """
        if self._result_processing_error_dispatcher is None:
            from ananta.core.actions.result_processing_glue import (
                build_error_dispatcher,
            )
            self._result_processing_error_dispatcher = build_error_dispatcher(self)
        return self._result_processing_error_dispatcher

    def _update_action_status_to_failed(self, action_id: str, error_message: str) -> None:
        """Update action status to failed in database (tolerate zero-affected)."""
        self.state_service.update_state(
            namespace="core",
            query={"table": "action_events", "filters": {"id": action_id}},
            updates={
                "status": ActionStatus.FAILED.value,
                "error_message": error_message,
            },
        )

    def _retrieve_failed_action_details(
        self, action_id: str
    ) -> tuple[
        str, object, str | None, str | None, str | None,
        str | None, str | None, str | None,
    ] | None:
        """Retrieve action details for error processing. Returns None on failure.

        Tuple shape:
            (process_key, parameters_raw, notes, error_processor,
             session_id, flow_id, context_id, error_processor_kind)
        """
        action_result = self.state_service.query_state(
            "core",
            {"table": "action_events", "filters": {"id": action_id}},
        )

        record = self._first_record(action_result)
        if not record:
            return None

        process_key = self._validate_process_key(action_id, record.get("process_key"))
        return (
            process_key,
            record.get("parameters"),  # parameters_raw (object)
            self._opt_str(record, "notes"),
            self._opt_str(record, "error_processor"),
            self._opt_str(record, "core__sessions_id"),
            self._opt_str(record, "core__flows_id"),
            self._opt_str(record, "context_id"),
            self._opt_str(record, "error_processor_kind"),
        )

    def _query_records(self, query_result: ActionResult) -> list[dict[str, object]]:
        """Return the dict records from a ``query_state`` result, or ``[]``.

        Graceful-degrade by design: a non-completed or malformed read envelope
        yields no rows rather than raising. The poll loop must keep draining —
        a transient read miss is not fatal — which matches the fire-and-forget
        semantics of the legacy ``execute_sql`` helpers this replaced. The state
        interface returns records as a list of dicts (``SELECT *``).
        """
        if query_result.get("action_status") not in _COMPLETED_READ_STATUSES:
            return []

        data = query_result.get("data", {})
        records = data.get("records", [])
        if not isinstance(records, list):
            return []
        return [record for record in records if isinstance(record, dict)]

    def _first_record(self, query_result: ActionResult) -> dict[str, object] | None:
        """First dict record from a ``query_state`` result, or ``None``."""
        records = self._query_records(query_result)
        return records[0] if records else None

    def _validate_process_key(self, action_id: str, value: object) -> str:
        """Validate and return process_key. Raises if invalid."""
        if not isinstance(value, str) or not value:
            raise ValueError(f"Action {action_id} has invalid process_key: {value!r}")
        return value

    @staticmethod
    def _opt_str(record: dict[str, object], key: str) -> str | None:
        """Optional string column from a dict record (``None`` if absent/non-str)."""
        value = record.get(key)
        return value if isinstance(value, str) else None

    def _parse_failed_arguments(self, parameters_raw: object) -> dict[str, object] | None:
        """Parse parameters from failed action."""
        if not parameters_raw:
            return None
        try:
            if isinstance(parameters_raw, str):
                parsed = json.loads(parameters_raw)
                return parsed if isinstance(parsed, dict) else None
            if isinstance(parameters_raw, dict):
                return parameters_raw
        except Exception:
            pass
        return None

    def _mark_action_failed(
        self,
        action_id: str,
        error_message: str,
        flow_token_id: str | None = None,
        canonical_schema: dict[str, object] | None = None,
        error_detail: Mapping[str, object] | None = None,
    ) -> None:
        """Mark action as failed with error message and invoke error_processor if present.

        ``error_detail`` carries the caller's TYPED failure information — a
        plugin's ``ErrorDetail`` mapping (``{type, code, message, details,
        severity, timestamp}``) or an :class:`AnantaError`'s ``to_dict()``.
        Without it this method could only ever store a constant
        ``code="action_failed"``, because ``error_message`` is a string and the
        typing had already been destroyed at the call site. Every
        ``/process/call`` consumer platform-wide reads that code, so the
        constant made failure classes indistinguishable — against the
        fast-fail / no-silent-fallback rules.

        Omitting it is legitimate and keeps the historical constant: some
        failures genuinely have no typing to carry. The parameter ADDS typing
        where typing exists; it never invents it.
        """
        self._update_action_status_to_failed(action_id, error_message)

        details = self._retrieve_failed_action_details(action_id)
        if not details:
            return

        (
            process_key,
            parameters_raw,
            notes,
            error_processor,
            session_id,
            flow_id,
            context_id,
            error_processor_kind,
        ) = details

        # Log action failure at INFO level for visibility
        logger.info(f"ACTION FAILED: {process_key} (id={action_id}) - {error_message}")
        failed_arguments = self._parse_failed_arguments(parameters_raw)

        # Store error result for console display. The typed detail wins when the
        # caller had one; the generic code remains the honest answer when it did
        # not. ``message`` is always the operator-facing string this method was
        # called with, so typing never costs readability.
        stored_error: dict[str, object] = {"message": error_message, "code": "action_failed"}
        if error_detail is not None:
            stored_error = {**dict(error_detail), "message": error_message}
            stored_error.setdefault("code", "action_failed")
        error_result: dict[str, object] = {
            "action_status": "failed",
            "error": stored_error,
            "data": None,
        }
        self._store_action_result(action_id, error_result, process_key)

        # Process error_processor template if present
        self._process_error_processor_template(
            action_id,
            error_message,
            process_key,
            notes,
            error_processor,
            session_id,
            flow_id,
            context_id,
            canonical_schema=canonical_schema,
            failed_arguments=failed_arguments,
        )

        # Route to inference for recovery if no error_processor attached.
        # Funneled through the shared error dispatcher so execution
        # failures and contract violations submit structurally identical
        # process-level error-handler actions (handoff 2026-05-03
        # Section 11).  The dispatcher branches on
        # ``error_processor_kind`` to route bridge-delivery actions to
        # the bridge instead of inference (handoff 2026-05-10 Section 10).
        if not error_processor:
            completed = CompletedAction(
                action_id=action_id,
                process_key=process_key,
                parameters=failed_arguments or {},
                notes=notes,
                result_processor=None,
                error_processor=error_processor,
                result_processor_kind=None,
                error_processor_kind=(
                    ErrorProcessorKind(error_processor_kind)
                    if error_processor_kind else None
                ),
                result_processor_target=None,
                session_id=session_id,
                flow_id=flow_id,
                context_id=context_id,
            )
            self._get_error_dispatcher().dispatch_execution_failure(
                error_message=error_message,
                process_key=process_key,
                failed_arguments=failed_arguments,
                notes=notes,
                session_id=session_id,
                flow_id=flow_id,
                context_id=context_id,
                canonical_schema=canonical_schema,
                completed=completed,
                flow_token_id=flow_token_id,
            )

        # FRG token failure handling
        if flow_token_id:
            self._flow_runtime_graph.complete_token(
                flow_token_id, success=False, result_summary={"error": error_message}
            )

    def _inject_ids_into_template(
        self,
        template: dict[str, object],
        session_id: str | None,
        flow_id: str | None,
        context_id: str | None,
    ) -> None:
        """Inject session_id, flow_id, and context_id into template metadata."""
        if session_id and CONTEXT_KEY_SESSION_ID not in template:
            template[CONTEXT_KEY_SESSION_ID] = session_id

        if flow_id and CONTEXT_KEY_FLOW_ID not in template:
            template[CONTEXT_KEY_FLOW_ID] = flow_id

        if context_id and "context_id" not in template:
            template["context_id"] = context_id

    def _prepare_error_recovery_templates(
        self,
        template_data: dict[str, object],
        session_id: str | None,
        flow_id: str | None,
        context_id: str | None,
        notes: str | None,
    ) -> list[dict[str, object]]:
        """Prepare error recovery templates with IDs and notes injected."""
        templates: list[dict[str, object]] = []
        actions_list = template_data.get("actions")

        if isinstance(actions_list, list):
            for idx, action_template in enumerate(actions_list):
                if not isinstance(action_template, dict):
                    logger.error(
                        f"ERROR-PROCESSOR: Skipping invalid action template at index {idx}"
                    )
                    continue
                template_copy = deepcopy(action_template)
                self._inject_ids_into_template(template_copy, session_id, flow_id, context_id)
                if notes and "notes" not in template_copy:
                    template_copy["notes"] = f"Error recovery: {notes[:200]}"[:NOTES_MAX_LENGTH]
                templates.append(template_copy)
        else:
            template_copy = deepcopy(template_data)
            self._inject_ids_into_template(template_copy, session_id, flow_id, context_id)
            if notes and "notes" not in template_copy:
                template_copy["notes"] = f"Error recovery: {notes[:200]}"[:NOTES_MAX_LENGTH]
            templates.append(template_copy)

        return templates

    def _process_error_processor_template(
        self,
        action_id: str,
        error_message: str,
        process_key: str,
        notes: str | None,
        error_processor: str | None,
        session_id: str | None,
        flow_id: str | None,
        context_id: str | None,
        canonical_schema: dict[str, object] | None = None,
        failed_arguments: dict[str, object] | None = None,
    ) -> None:
        """Process error_processor template if present.

        Raises:
            json.JSONDecodeError: If error_processor is not valid JSON
            RuntimeError: If template submission fails
        """
        if not error_processor:
            return

        # Parse error_processor as JSON template
        parsed = json.loads(error_processor)
        if not isinstance(parsed, dict):
            raise ValueError(f"error_processor must be a JSON object, got {type(parsed).__name__}")

        template_data: dict[str, object] = parsed
        self._inject_ids_into_template(template_data, session_id, flow_id, context_id)

        # Prepare error data for template substitution
        error_data: dict[str, object] = {
            TEMPLATE_VAR_ERROR: error_message,
            TEMPLATE_VAR_ERROR_MESSAGE: error_message,
            TEMPLATE_VAR_ERROR_DETAILS: error_message,
            TEMPLATE_VAR_ACTION_ID: action_id,
            TEMPLATE_VAR_PROCESS_KEY: process_key,
            TEMPLATE_VAR_ACTION_ARGUMENTS: failed_arguments or {},
            TEMPLATE_VAR_FAILED_ACTION: {
                CONTEXT_KEY_PROCESS_KEY: process_key,
                "arguments": failed_arguments or {},
            },
            TEMPLATE_VAR_FAILED_PROCESS_KEY: process_key,
            TEMPLATE_VAR_CANONICAL_SCHEMA: canonical_schema or {},
            TEMPLATE_VAR_SESSION_ID: session_id,
            TEMPLATE_VAR_FLOW_ID: flow_id,
        }
        if context_id:
            error_data["context_id"] = context_id
        if notes:
            error_data[TEMPLATE_VAR_NOTES] = notes

        # Prepare and submit error recovery templates
        templates = self._prepare_error_recovery_templates(
            template_data, session_id, flow_id, context_id, notes
        )
        for action_template in templates:
            if not self.action_factory.submit_result_with_template(error_data, action_template):
                raise RuntimeError(
                    f"Failed to submit error recovery template for action {action_id}"
                )

    def _token_has_pending_jobs(self, token_id: str) -> bool:
        """Check if token has pending jobs linked to it.

        Returns True if there are jobs with this token that are not in a
        terminal state. Used to decide whether to complete the token or
        transition it to WAITING_JOB. The non-terminal status set is the
        closed-domain complement ``_PENDING_JOB_STATES`` (see its definition).
        """
        result = self.state_service.query_state(
            "core",
            {
                "table": "job",
                "filters": {
                    "flow_token_id": token_id,
                    "status": [status.value for status in _PENDING_JOB_STATES],
                },
            },
        )
        return len(self._query_records(result)) > 0

    def get_metrics(self) -> dict[str, object]:
        """Get polling metrics for monitoring"""
        return {
            "running": self.running,
            "total_actions_processed": self.total_actions_processed,
            "total_poll_cycles": self.total_poll_cycles,
            # Non-zero means one action row executed more than once while this
            # process was up (adopter issue #9's upstream cause). Every increment
            # has a matching DOUBLE-EXECUTION DETECTED warning carrying the
            # action_id and both timestamps.
            "total_double_executions_detected": self.total_double_executions_detected,
            "last_poll_time": self.last_poll_time.isoformat() if self.last_poll_time else None,
            "poll_interval": self.poll_interval,
            "max_actions_per_poll": self.max_actions_per_poll,
        }
