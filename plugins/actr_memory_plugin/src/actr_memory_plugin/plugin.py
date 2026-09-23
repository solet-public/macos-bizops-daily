"""ACT-R Memory Plugin - Biologically-inspired memory with decay and consolidation.

This plugin wraps the memory_service interface, providing:
- Plugin actions for remember, recall, forget, memorize, learn, etc.
- Scheduled operations for periodic maintenance
- CLI access via entry points
"""

import datetime
import logging
from datetime import UTC
from typing import Any, ClassVar, cast

from ananta.core.actions.action_metadata import (
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.config.config_manager import get_config
from ananta.core.config.config_provider import ConfigProvider
from ananta.core.domain.types import ActionResult
from ananta.core.plugins.decorators import service_lifecycle
from ananta.core.plugins.plugin_base import ServicePlugin
from ananta.core.plugins.plugin_contracts import ActionStatus
from ananta.error_handling import FrameworkError
from ananta.interfaces.memory_service_interface import MemoryServiceInterface
from ananta.logging_setup import configure_plugin_logging
from ananta.types.schema_types import SchemaDefinition

from .backend import ACTRMemoryBackend
from .constants import (
    CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
    CONSOLIDATION_CRON,
    MEMORIZATION_QUEUE_CRON,
    PLUGIN_NAME,
    STRENGTH_RECOMPUTE_CRON,
    ErrorCode,
)
from .plugin_config import get_plugin_config_schema
from .plugin_helpers import acting_session_id as _acting_session_id
from .plugin_helpers import build_response

# Knowledge base content tag — excluded from personal memory recall by default.
# Knowledge base articles are accessed exclusively via knowledge_service::search.
_KNOWLEDGE_OFFICIAL_TAG: str = "knowledge:official"

# System-owned flow_id / session_id constants for the three scheduler-fired
# crons in `setup_schedules`. Each cron action_def dispatches a cron-only
# EDGE_SINK sibling on the memory_service interface
# (`service_interface::memory_service::*_cron`) declared with
# `processor_policy_category=ProcessorPolicyCategory.EDGE_SINK` +
# `is_discoverable=False` at
# `ananta/src/ananta/services/memory_service/interfaces/public.py`. The
# EDGE_SINK category causes `action_queue_poller._dispatch_*` to short-
# circuit at the EDGE_SINK_SKIP branch — terminal action, no result-
# processor dispatch, no inference scaffold fires, no `<<get_flow_input>>`
# macro lookup happens, no `core__flows` pre-seed is required. The
# discoverable EDGE-category siblings (process_memorization_queue /
# consolidate / recompute_strengths) stay model-callable for direct
# invocation. See the canonical contract at
# `knowledge_bases/ananta_platform/21_scheduling_service/
# 01_template_flow_record_lifecycle.md` and the §5.3-REDIRECT inventory at
# `workbench/2026-06-17_cron_action_contract_inventory.md`. The constants
# below are retained because `action_factory._enforce_flow_id` refuses
# action defs without a flow_id even on EDGE_SINK paths; per-consumer
# distinct identifiers preserve audit-trail distinguishability.
_ACTR_MEMORIZATION_FLOW_ID = "flow-actr-memorization-queue"
_ACTR_MEMORIZATION_SESSION_ID = "sess-actr-memorization-queue"
_ACTR_STRENGTH_FLOW_ID = "flow-actr-strength-recompute"
_ACTR_STRENGTH_SESSION_ID = "sess-actr-strength-recompute"
_ACTR_CONSOLIDATION_FLOW_ID = "flow-actr-consolidation"
_ACTR_CONSOLIDATION_SESSION_ID = "sess-actr-consolidation"


class ACTRMemoryPlugin(ServicePlugin, MemoryServiceInterface):
    """Memory system with ACT-R decay and consolidation.

    This plugin provides a biologically-inspired memory system that:
    - Decays over time (unused memories fade)
    - Strengthens with use (retrieved memories become stronger)
    - Consolidates automatically (old episodic → semantic summaries)
    - Supports spaced repetition memorization

    Implements MemoryServiceInterface - accessible only via service_interface::
    namespace, not plugin:: namespace.
    """

    service_interfaces: ClassVar[tuple[type, ...]] = (MemoryServiceInterface,)
    supported_interface_versions: ClassVar[dict[type, str]] = {
        MemoryServiceInterface: MemoryServiceInterface.INTERFACE_VERSION
    }

    def __init__(self) -> None:
        super().__init__()
        self.name = PLUGIN_NAME
        self.logger: logging.Logger = logging.getLogger(self.name)

        # Service dependencies (requested via orchestrator.get_service() in prepare_for_readiness)
        # Properly typed for mypy - code checks for None before access
        self._backend: ACTRMemoryBackend | None = None
        self._scheduling_service: Any = None
        self._state_service: Any = None

        # Lifecycle state
        self._services_started = False
        self._schedules_configured = False
        self.config_provider: ConfigProvider | None = None
        # Merged operator config (yaml defaults + profile overrides), captured in
        # initialize() and bound to config_provider in prepare_for_readiness().
        self._operator_config: dict[str, object] = {}

    def initialize(self, config: dict[str, object]) -> None:
        """Re-capture the merged operator config, defensively.

        CORRECTED 2026-08-06 (root cause, config-binding-order incident:
        workbench/2026-08-05_canonical_memory_and_ledger_verification_findings.md-
        adjacent finding, root-caused in the platform's own startup registry).
        The old docstring here claimed "initialize runs before
        prepare_for_readiness" — the OPPOSITE of the platform's actual order:
        ``_start_service_plugins`` (which calls every plugin's
        ``prepare_for_readiness``) runs BEFORE ``_initialize_plugin_configs``
        (which calls this method) — see
        ``ananta/src/ananta/core/orchestration/startup_sequence.py``'s
        ``STARTUP_SEQUENCE`` dependency graph. Binding on this method's
        capture therefore bound ``{}`` on every boot; ``export_allowed_roots``
        and the cron-override keys never took effect.
        ``prepare_for_readiness`` now pulls the merged config directly
        (``get_config().get_plugin_config(...)``, the same pattern
        ``postgres_state_management_plugin`` already uses) rather than
        depending on this method having already run. This method is kept as
        a defensive re-bind — re-applies the config and, if
        ``config_provider`` already exists, rebuilds it.  When a live backend
        already exists, it also rebinds its export containment policy; this is
        the hot-reload consumer of ``export_allowed_roots``.  In case the
        platform's config genuinely changes between the two steps, or a
        future startup-order change reintroduces reliance on this path.
        Never the primary source of truth anymore.
        """
        self._operator_config = config or {}
        if self.config_provider is not None:
            self.config_provider = ConfigProvider(self.name, self._operator_config)
        if self._backend is not None:
            self._backend._export_allowed_roots = self._resolve_export_allowed_roots()

    def get_default_config(self) -> dict[str, Any]:
        """Return default configuration."""
        return {
            "enable_scheduled_operations": True,
            "memorization_queue_cron": MEMORIZATION_QUEUE_CRON,
            "strength_recompute_cron": STRENGTH_RECOMPUTE_CRON,
            "consolidation_cron": CONSOLIDATION_CRON,
        }

    def get_config_schema(self) -> dict[str, object]:
        """Return JSON Schema for plugin configuration."""
        return get_plugin_config_schema()

    def get_schema_definitions(self) -> list[SchemaDefinition]:
        """Return schema definitions for the ACT-R memory tables.

        Called during startup by the schema initialization system.
        Tables: actr_memory_plugin__memory, actr_memory_plugin__memorization
        """
        from ananta.services.memory_service.schema import get_memory_schema

        return [get_memory_schema()]

    # NOTE: set_memory_service(), set_scheduling_service() REMOVED (2025-12-06)
    # Plugin now requests services in prepare_for_readiness() via orchestrator.get_service()
    # See: ananta_build/2025-12-06_service_binding_architecture.md

    def prepare_for_readiness(self) -> None:
        """Initialize plugin. Fail-fast if dependencies unavailable.

        Uses Service Registry pattern: plugin REQUESTS services from orchestrator.
        See: ananta_build/2025-12-06_service_binding_architecture.md
        """
        if not self.orchestrator_ref:
            raise RuntimeError(f"{self.name}: orchestrator_ref not injected")

        APP_HOME = getattr(self.orchestrator_ref, "APP_HOME", None)
        if not APP_HOME:
            raise RuntimeError(
                f"{self.name}: Application directory not configured - plugin cannot initialize"
            )

        # Initialize configuration and logging. Pull the merged operator config
        # DIRECTLY here (get_config().get_plugin_config) rather than depending
        # on initialize() having already run — the platform's real startup
        # order runs prepare_for_readiness (via start_service_plugins) BEFORE
        # initialize_plugin_configs (which calls initialize()), so waiting on
        # self._operator_config here always bound `{}` (2026-08-06 finding);
        # export_allowed_roots and the cron overrides never took effect on any
        # boot. Matches the postgres_state_management_plugin precedent.
        self._operator_config = get_config().get_plugin_config(
            self.name, default_config=self.get_default_config(),
        )
        self.config_provider = ConfigProvider(self.name, self._operator_config)
        self.logger = configure_plugin_logging(APP_HOME, self.name, self.config_provider)
        self.logger.debug(f"Initializing {self.name}")

        # Get required services for MemoryService instantiation
        state_service = self.orchestrator_ref.get_service("state_service")
        if not state_service:
            raise RuntimeError(
                f"{self.name}: state_service not available - check service_bindings.json"
            )
        # Retain the state_service handle on the plugin so `setup_schedules` can
        # pre-seed `core__sessions` + `core__flows` rows for system-owned cron
        # flow_ids per the TEMPLATE_FLOW lookup contract.
        self._state_service = state_service

        vector_service = self.orchestrator_ref.get_service("vector_service")
        embedding_service = self.orchestrator_ref.get_service("embedding_service")
        inference_service = self.orchestrator_ref.get_service("inference_service")

        # Instantiate the ACTRMemoryBackend directly
        self._backend = ACTRMemoryBackend(
            state_service=state_service,
            vector_service=vector_service,
            embedding_service=embedding_service,
            inference_service=inference_service,
            export_allowed_roots=self._resolve_export_allowed_roots(),
        )
        self.logger.debug("ACTRMemoryBackend instantiated with required dependencies")

        # JOS-02 F3 boot invariant: a pre-JOS-02 focus row (no session_id) is
        # invisible to session-filtered reads yet blocks re-pinning its memory
        # platform-wide — fail readiness LOUD with the migration pointer.
        self._backend.assert_no_unscoped_focus_rows()

        # Request scheduling_service (optional - for scheduled maintenance tasks)
        self._scheduling_service = self.orchestrator_ref.get_service("scheduling_service")
        if self._scheduling_service is None:
            self.logger.debug("scheduling_service not available - scheduled operations disabled")
        else:
            self.logger.debug("scheduling_service acquired from orchestrator")

        self.set_ready()

    def _resolve_export_allowed_roots(self) -> list[str]:
        """Read + validate export_allowed_roots from operator config.

        An absent key is the refuse-all default (empty list). A present but
        malformed value is a loud config fault, never a silent admit-all or
        refuse-all — mirrors the connector export_allowed_roots validation.
        """
        raw = self._operator_config.get(CONFIG_KEY_EXPORT_ALLOWED_ROOTS)
        if raw is None:
            return []
        if not isinstance(raw, list) or not all(isinstance(entry, str) for entry in raw):
            raise RuntimeError(
                f"{self.name}: {CONFIG_KEY_EXPORT_ALLOWED_ROOTS} must be a list of "
                "directory path strings"
            )
        return list(raw)

    def _get_backend(self) -> ACTRMemoryBackend:
        """Get backend. Raises FrameworkError if not available (fail-fast)."""
        if self._backend is None:
            raise FrameworkError(
                message="Memory backend not initialized",
                error_code="memory.backend_unavailable",
            )
        return self._backend

    def _build_response(
        self,
        status: str,
        data: dict[str, Any],
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return build_response(status, data, error)

    @service_lifecycle(operation="start")
    async def start_services(self) -> ActionResult:
        """Start the memory plugin services."""
        if self._services_started:
            return cast(
                ActionResult,
                self._build_response(
                    ActionStatus.COMPLETED.value,
                    {"message": "Service already running"},
                ),
            )

        self.logger.debug(f"Starting {self.name} service")

        try:
            self._services_started = True

            return cast(
                ActionResult,
                self._build_response(
                    ActionStatus.COMPLETED.value,
                    {
                        "message": "Service started successfully",
                        "started_at": datetime.datetime.now(UTC).isoformat(),
                    },
                ),
            )

        except Exception as e:
            self.logger.critical(f"Failed to start service: {e}", exc_info=True)
            return cast(
                ActionResult,
                self._build_response(
                    ActionStatus.ERROR.value,
                    {},
                    {
                        "type": "PluginError",
                        "code": f"{PLUGIN_NAME}.service_start_failed",
                        "message": str(e),
                    },
                ),
            )

    @service_lifecycle(operation="stop")
    async def stop_services(self) -> ActionResult:
        """Stop the memory plugin services."""
        if not self._services_started:
            return cast(
                ActionResult,
                self._build_response(
                    ActionStatus.COMPLETED.value,
                    {"message": "Service already stopped"},
                ),
            )

        self.logger.debug(f"Stopping {self.name} service")
        self._services_started = False

        return cast(
            ActionResult,
            self._build_response(
                ActionStatus.COMPLETED.value,
                {"message": "Service stopped successfully"},
            ),
        )

    # ─────────────────────────────────────────────────────────────────────────
    # CORE MEMORY ACTIONS
    # ─────────────────────────────────────────────────────────────────────────

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/remember_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="remember_action",
                parameters={
            "content": ParameterMetadata(
                description="The content to remember",
                required=True,
                type=ParameterType.STRING,
            ),
            "tags": ParameterMetadata(
                description="Optional tags for organization",
                required=False,
                type=ParameterType.LIST,
            ),
            "source_file": ParameterMetadata(
                description="Optional source file path",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="Memory creation result with memory_id",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Memory creation result with memory_id",
            properties={
                "memory_id": ParameterMetadata(
                    type=ParameterType.STRING, description="ID of the created memory"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Success message"
                ),
            },
        ),
                summary="Store a new piece of knowledge in long-term memory",
        requires_result_processor=True,
    )
    def remember_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Store a new episodic memory."""
        content = params.get("content", "")
        tags = params.get("tags", [])
        source_file = params.get("source_file")
        session_id = state.get("session_id")

        service = self._get_backend()
        result = service.remember(
            content=content,
            tags=tags,
            source_file=source_file,
            session_id=session_id,
        )

        if "error" in result:
            return self._build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": ErrorCode.OPERATION_FAILED,
                    "message": result["error"],
                    "plugin_name": PLUGIN_NAME,
                },
            )

        return self._build_response(ActionStatus.COMPLETED.value, result)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/recall_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="recall_action",
                parameters={
            "query": ParameterMetadata(
                description="Search query",
                required=True,
                type=ParameterType.STRING,
            ),
            "top_k": ParameterMetadata(
                description="Number of results to return (default 5)",
                required=False,
                type=ParameterType.INTEGER,
            ),
            "memory_type": ParameterMetadata(
                description="Filter by memory type: 'all', 'episodic', 'semantic_l1', 'semantic_l2'",
                required=False,
                type=ParameterType.STRING,
            ),
            "include_archived": ParameterMetadata(
                description="Include archived memories in search",
                required=False,
                type=ParameterType.BOOLEAN,
            ),
            "tags": ParameterMetadata(
                description="Filter by tags - only return memories that have ALL specified tags (e.g., ['tool_use', 'tool_success'])",
                required=False,
                type=ParameterType.LIST,
            ),
            "exclude_ids": ParameterMetadata(
                description="List of memory IDs to exclude from results (useful for avoiding duplicates)",
                required=False,
                type=ParameterType.LIST,
            ),
        },
        output_type="object",
        output_description="Search results with memories ranked by combined similarity and strength",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Search results with matching memories",
            properties={
                "memories": ParameterMetadata(
                    type=ParameterType.LIST, description="List of matching memories"
                ),
                "count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of memories returned"
                ),
            },
        ),
                summary="Search for relevant memories using semantic similarity and activation strength",
        requires_result_processor=True,
    )
    def recall_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Search memories with strength-weighted ranking."""
        query = params.get("query", "")
        top_k = params.get("top_k", 5)
        memory_type = params.get("memory_type", "all")
        include_archived = params.get("include_archived", False)
        tags = params.get("tags")
        exclude_ids = params.get("exclude_ids")

        # When no explicit tags filter is provided, exclude knowledge base content.
        # Knowledge base articles are accessed via knowledge_service::search only.
        exclude_tags: list[str] | None = None
        if tags is None:
            exclude_tags = [_KNOWLEDGE_OFFICIAL_TAG]

        service = self._get_backend()
        result = service.recall(
            query=query,
            top_k=top_k,
            memory_type=memory_type,
            include_archived=include_archived,
            tags=tags,
            exclude_ids=exclude_ids,
            exclude_tags=exclude_tags,
        )

        if "error" in result:
            return self._build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": ErrorCode.OPERATION_FAILED,
                    "message": result["error"],
                    "plugin_name": PLUGIN_NAME,
                },
            )

        return self._build_response(ActionStatus.COMPLETED.value, result)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/forget_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="forget_action",
                parameters={
            "memory_id": ParameterMetadata(
                description="ID of the memory to archive",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="Archive result",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Archive result",
            properties={
                "memory_id": ParameterMetadata(
                    type=ParameterType.STRING, description="ID of the archived memory"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Success message"
                ),
            },
        ),
                summary="Archive a memory so it no longer appears in searches",
        requires_result_processor=True,
    )
    def forget_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Archive a memory."""
        memory_id = params.get("memory_id", "")

        service = self._get_backend()
        result = service.forget(memory_id=memory_id)

        if "error" in result:
            return self._build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": ErrorCode.OPERATION_FAILED,
                    "message": result["error"],
                    "plugin_name": PLUGIN_NAME,
                },
            )

        return self._build_response(ActionStatus.COMPLETED.value, result)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/reinforce_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="reinforce_action",
                parameters={
            "memory_id": ParameterMetadata(
                description="ID of the memory to reinforce",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="Reinforcement result with updated strength",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Reinforcement result",
            properties={
                "memory_id": ParameterMetadata(
                    type=ParameterType.STRING, description="ID of the reinforced memory"
                ),
                "new_strength": ParameterMetadata(
                    type=ParameterType.FLOAT, description="Updated ACT-R activation strength"
                ),
                "retrieval_count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Total number of retrievals"
                ),
            },
        ),
                summary="Strengthen a memory by adding a retrieval timestamp",
        requires_result_processor=True,
    )
    def reinforce_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Reinforce a memory by adding a retrieval timestamp."""
        memory_id = params.get("memory_id", "")

        service = self._get_backend()
        result = service.reinforce(memory_id=memory_id)

        if "error" in result:
            return self._build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": ErrorCode.OPERATION_FAILED,
                    "message": result["error"],
                    "plugin_name": PLUGIN_NAME,
                },
            )

        return self._build_response(ActionStatus.COMPLETED.value, result)

    # ─────────────────────────────────────────────────────────────────────────
    # FOCUS BUFFER ACTIONS
    # ─────────────────────────────────────────────────────────────────────────

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/focus_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="focus_action",
        parameters={
            "memory_id": ParameterMetadata(
                description="ID of the memory to pin to focus buffer",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="Focus result with memory_id and focused count",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Focus buffer result",
            properties={
                "memory_id": ParameterMetadata(
                    type=ParameterType.STRING, description="ID of the focused memory"
                ),
                "focused_count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Current number of focused memories"
                ),
            },
        ),
        summary="Pin a memory to the focus buffer for guaranteed context inclusion",
        requires_result_processor=True,
    )
    def focus_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Pin a memory to the acting session's focus buffer."""
        memory_id = params.get("memory_id", "")

        service = self._get_backend()
        result = service.focus(
            memory_id=memory_id,
            session_id=_acting_session_id(params, state),
        )

        if "error" in result:
            return self._build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": ErrorCode.OPERATION_FAILED,
                    "message": result["error"],
                    "plugin_name": PLUGIN_NAME,
                },
            )

        return self._build_response(ActionStatus.COMPLETED.value, result)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/unfocus_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="unfocus_action",
        parameters={
            "memory_id": ParameterMetadata(
                description="ID of the memory to unpin from focus buffer",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="Unfocus result",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Unfocus result",
            properties={
                "memory_id": ParameterMetadata(
                    type=ParameterType.STRING, description="ID of the unfocused memory"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
        summary="Remove a memory from the focus buffer"
    )
    def unfocus_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Remove a memory from the acting session's focus buffer."""
        memory_id = params.get("memory_id", "")

        service = self._get_backend()
        result = service.unfocus(
            memory_id=memory_id,
            session_id=_acting_session_id(params, state),
        )

        if "error" in result:
            return self._build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": ErrorCode.OPERATION_FAILED,
                    "message": result["error"],
                    "plugin_name": PLUGIN_NAME,
                },
            )

        return self._build_response(ActionStatus.COMPLETED.value, result)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/get_focused_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="get_focused_action",
        parameters={},
        output_type="list",
        output_description="List of focused memories with full content",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.LIST,
            description="List of focused memories",
            properties={
                "memory_id": ParameterMetadata(
                    type=ParameterType.STRING, description="ID of the focused memory"
                ),
                "content": ParameterMetadata(
                    type=ParameterType.STRING, description="Full memory content"
                ),
            },
        ),
        summary="List memories currently pinned in the focus buffer"
    )
    def get_focused_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the acting session's focused memories with full content."""
        service = self._get_backend()
        memories = service.get_focused(
            session_id=_acting_session_id(params, state),
        )

        return self._build_response(
            ActionStatus.COMPLETED.value,
            {"memories": memories, "count": len(memories)},
        )

    # ─────────────────────────────────────────────────────────────────────────
    # MEMORIZATION ACTIONS
    # ─────────────────────────────────────────────────────────────────────────

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/memorize_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="memorize_action",
                parameters={
            "memory_id": ParameterMetadata(
                description="ID of existing memory to memorize",
                required=False,
                type=ParameterType.STRING,
            ),
            "content": ParameterMetadata(
                description="Content to remember and memorize (creates new memory)",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="Memorization result with next review time",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Memorization queue result",
            properties={
                "memory_id": ParameterMetadata(
                    type=ParameterType.STRING, description="ID of the memory being memorized"
                ),
                "next_review_at": ParameterMetadata(
                    type=ParameterType.STRING, description="ISO timestamp of next scheduled review"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Success message"
                ),
            },
        ),
                summary="Start spaced repetition learning for critical knowledge",
        requires_result_processor=True,
    )
    def memorize_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Add to memorization queue."""
        memory_id = params.get("memory_id")
        content = params.get("content")

        service = self._get_backend()
        result = service.memorize(memory_id=memory_id, content=content)

        if "error" in result:
            return self._build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": ErrorCode.OPERATION_FAILED,
                    "message": result["error"],
                    "plugin_name": PLUGIN_NAME,
                },
            )

        return self._build_response(ActionStatus.COMPLETED.value, result)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/stop_memorizing_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="stop_memorizing_action",
                parameters={
            "memory_id": ParameterMetadata(
                description="ID of memory to stop memorizing",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="Stop memorization result",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Stop memorization result",
            properties={
                "memory_id": ParameterMetadata(
                    type=ParameterType.STRING, description="ID of the memory removed from queue"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Success message"
                ),
            },
        ),
                summary="Stop spaced repetition learning for a memory"
    )
    def stop_memorizing_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Remove from memorization queue."""
        memory_id = params.get("memory_id", "")

        service = self._get_backend()
        result = service.stop_memorizing(memory_id=memory_id)

        if "error" in result:
            return self._build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": ErrorCode.OPERATION_FAILED,
                    "message": result["error"],
                    "plugin_name": PLUGIN_NAME,
                },
            )

        return self._build_response(ActionStatus.COMPLETED.value, result)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/list_memorizing_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="list_memorizing_action",
                parameters={
            "include_completed": ParameterMetadata(
                description="Include completed memorizations",
                required=False,
                type=ParameterType.BOOLEAN,
            ),
        },
        output_type="object",
        output_description="List of memories being memorized",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="List of memorization entries",
            properties={
                "items": ParameterMetadata(
                    type=ParameterType.LIST, description="List of memorization entries"
                ),
                "count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of items in queue"
                ),
            },
        ),
                summary="Show all memories currently in spaced repetition learning",
        requires_result_processor=True,
    )
    def list_memorizing_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """List memorization queue."""
        include_completed = params.get("include_completed", False)

        service = self._get_backend()
        result = service.list_memorizing(include_completed=include_completed)

        if "error" in result:
            return self._build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": ErrorCode.OPERATION_FAILED,
                    "message": result["error"],
                    "plugin_name": PLUGIN_NAME,
                },
            )

        return self._build_response(ActionStatus.COMPLETED.value, result)

    # ─────────────────────────────────────────────────────────────────────────
    # LEARNING / INGESTION ACTIONS
    # ─────────────────────────────────────────────────────────────────────────

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/learn_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="learn_action",
                parameters={
            "path": ParameterMetadata(
                description="File or directory path to ingest",
                required=True,
                type=ParameterType.STRING,
            ),
            "pattern": ParameterMetadata(
                description="Glob pattern for files (default: *.md)",
                required=False,
                type=ParameterType.STRING,
            ),
            "recursive": ParameterMetadata(
                description="Search directories recursively",
                required=False,
                type=ParameterType.BOOLEAN,
            ),
            "memorize": ParameterMetadata(
                description="Also add to memorization queue",
                required=False,
                type=ParameterType.BOOLEAN,
            ),
            "tags": ParameterMetadata(
                description="Tags to apply to ingested memories",
                required=False,
                type=ParameterType.LIST,
            ),
        },
        output_type="object",
        output_description="Ingestion result with memory count",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Ingestion result with memory count",
            properties={
                "path": ParameterMetadata(
                    type=ParameterType.STRING, description="Path that was ingested"
                ),
                "memories_created": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of memories created"
                ),
                "files_skipped": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of files skipped"
                ),
                "memorized": ParameterMetadata(
                    type=ParameterType.BOOLEAN, description="Whether added to memorization queue"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Summary message"
                ),
            },
        ),
                summary="Ingest knowledge from documents into long-term memory",
        requires_result_processor=True,
    )
    def learn_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Ingest knowledge from files."""
        path = params.get("path", "")
        pattern = params.get("pattern", "*.md")
        recursive = params.get("recursive", True)
        memorize = params.get("memorize", False)
        tags = params.get("tags", [])

        service = self._get_backend()
        result = service.learn(
            path=path,
            pattern=pattern,
            recursive=recursive,
            memorize=memorize,
            tags=tags,
        )

        if "error" in result:
            return self._build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": ErrorCode.OPERATION_FAILED,
                    "message": result["error"],
                    "plugin_name": PLUGIN_NAME,
                },
            )

        return self._build_response(ActionStatus.COMPLETED.value, result)

    # ─────────────────────────────────────────────────────────────────────────
    # LIFECYCLE ACTIONS
    # ─────────────────────────────────────────────────────────────────────────

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/consolidate_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="consolidate_action",
                parameters={
            "dry_run": ParameterMetadata(
                description="Preview what would be consolidated without making changes",
                required=False,
                type=ParameterType.BOOLEAN,
            ),
        },
        output_type="object",
        output_description="Consolidation result with actions taken",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Consolidation result with actions taken",
            properties={
                "candidates_found": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of weak memories found"
                ),
                "clusters_formed": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of clusters for summarization"
                ),
                "consolidations": ParameterMetadata(
                    type=ParameterType.LIST, description="List of consolidation actions taken"
                ),
                "dry_run": ParameterMetadata(
                    type=ParameterType.BOOLEAN, description="Whether this was a dry run"
                ),
            },
        ),
                summary="Run memory consolidation to summarize old, weak memories",
        requires_result_processor=True,
    )
    def consolidate_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Run memory consolidation."""
        dry_run = params.get("dry_run", False)

        service = self._get_backend()
        result = service.consolidate(dry_run=dry_run)

        if "error" in result:
            return self._build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": ErrorCode.OPERATION_FAILED,
                    "message": result["error"],
                    "plugin_name": PLUGIN_NAME,
                },
            )

        return self._build_response(ActionStatus.COMPLETED.value, result)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/recompute_strengths_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="recompute_strengths_action",
                parameters={},
        output_type="object",
        output_description="Recomputation result",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Recomputation result",
            properties={
                "updated_count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of memories updated"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Success message"
                ),
            },
        ),
                summary="Force recalculation of all memory strength values"
    )
    def recompute_strengths_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Recompute all memory strengths."""
        service = self._get_backend()
        result = service.recompute_strengths()

        if "error" in result:
            return self._build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": ErrorCode.OPERATION_FAILED,
                    "message": result["error"],
                    "plugin_name": PLUGIN_NAME,
                },
            )

        return self._build_response(ActionStatus.COMPLETED.value, result)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/process_memorization_queue_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="process_memorization_queue_action",
                parameters={},
        output_type="object",
        output_description="Processing result with counts",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Queue processing result",
            properties={
                "processed_count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of reviews processed"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Success message"
                ),
            },
        ),
                summary="Run spaced repetition review processing"
    )
    def process_memorization_queue_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Process memorization queue."""
        del params, state  # Unused - no parameters for this action
        service = self._get_backend()
        result = service.process_memorization_queue()

        if "error" in result:
            return self._build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": ErrorCode.OPERATION_FAILED,
                    "message": result["error"],
                    "plugin_name": PLUGIN_NAME,
                },
            )

        return self._build_response(ActionStatus.COMPLETED.value, result)

    # ─────────────────────────────────────────────────────────────────────────
    # INTROSPECTION ACTIONS
    # ─────────────────────────────────────────────────────────────────────────

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/memory_stats_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="memory_stats_action",
                parameters={},
        output_type="object",
        output_description="Memory statistics",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Memory system statistics",
            properties={
                "total": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Total number of memories"
                ),
                "by_type": ParameterMetadata(
                    type=ParameterType.OBJECT, description="Count by memory type"
                ),
                "by_status": ParameterMetadata(
                    type=ParameterType.OBJECT, description="Count by status"
                ),
                "strength_distribution": ParameterMetadata(
                    type=ParameterType.OBJECT, description="Distribution by strength"
                ),
            },
        ),
                summary="Get statistics about the memory system",
        requires_result_processor=True,
    )
    def memory_stats_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Get memory statistics - action handler."""
        del params, state  # Unused - no parameters for this action
        service = self._get_backend()
        result = service.memory_stats()

        return self._build_response(ActionStatus.COMPLETED.value, result)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/list_memories_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="list_memories_action",
                parameters={
            "memory_type": ParameterMetadata(
                description="Filter by type: 'episodic', 'semantic_l1', 'semantic_l2'",
                required=False,
                type=ParameterType.STRING,
            ),
            "status": ParameterMetadata(
                description="Filter by status: 'active', 'archived'",
                required=False,
                type=ParameterType.STRING,
            ),
            "tag": ParameterMetadata(
                description="Filter by tag",
                required=False,
                type=ParameterType.STRING,
            ),
            "sort_by": ParameterMetadata(
                description="Sort by: 'strength', 'created_at', 'retrieval_count'",
                required=False,
                type=ParameterType.STRING,
            ),
            "limit": ParameterMetadata(
                description="Maximum results to return",
                required=False,
                type=ParameterType.INTEGER,
            ),
        },
        output_type="object",
        output_description="List of memories",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="List of memories",
            properties={
                "memories": ParameterMetadata(
                    type=ParameterType.LIST, description="List of memory objects"
                ),
                "total": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Total matching memories"
                ),
                "showing": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number shown"
                ),
            },
        ),
                summary="Browse memories with filtering and sorting",
        requires_result_processor=True,
    )
    def list_memories_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """List memories."""
        del state  # Unused - state not needed for listing
        service = self._get_backend()
        result = service.list_memories(
            memory_type=params.get("memory_type"),
            status=params.get("status", "active"),
            tag=params.get("tag"),
            sort_by=params.get("sort_by", "strength"),
            limit=params.get("limit", 20),
        )

        if "error" in result:
            return self._build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": ErrorCode.OPERATION_FAILED,
                    "message": result["error"],
                    "plugin_name": PLUGIN_NAME,
                },
            )

        return self._build_response(ActionStatus.COMPLETED.value, result)

    # ─────────────────────────────────────────────────────────────────────────
    # IMPORT/EXPORT ACTIONS
    # ─────────────────────────────────────────────────────────────────────────

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/export_memories_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="export_memories_action",
                parameters={
            "file_path": ParameterMetadata(
                description="Output file path (auto-generated if not specified)",
                required=False,
                type=ParameterType.STRING,
            ),
            "include_archived": ParameterMetadata(
                description="Include archived memories",
                required=False,
                type=ParameterType.BOOLEAN,
            ),
        },
        output_type="object",
        output_description="Export result with file path",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Export result",
            properties={
                "file_path": ParameterMetadata(
                    type=ParameterType.STRING, description="Path to exported file"
                ),
                "memory_count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of memories exported"
                ),
            },
        ),
                summary="Export all memories to a JSON backup file",
        requires_result_processor=True,
    )
    def export_memories_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Export memories to file."""
        del state  # Unused - state not needed for export
        file_path = params.get("file_path")
        include_archived = params.get("include_archived", False)

        service = self._get_backend()
        result = service.export_memories(
            file_path=file_path,
            include_archived=include_archived,
        )

        if "error" in result:
            return self._build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": ErrorCode.OPERATION_FAILED,
                    "message": result["error"],
                    "plugin_name": PLUGIN_NAME,
                },
            )

        return self._build_response(ActionStatus.COMPLETED.value, result)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/import_memories_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="import_memories_action",
                parameters={
            "file_path": ParameterMetadata(
                description="Input file path",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="Import result with counts",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Import result",
            properties={
                "imported_count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of memories imported"
                ),
                "skipped_count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of memories skipped"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Success message"
                ),
            },
        ),
                summary="Import memories from a JSON backup file"
    )
    def import_memories_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Import memories from file."""
        del state  # Unused - state not needed for import
        file_path = params.get("file_path", "")

        service = self._get_backend()
        result = service.import_memories(file_path=file_path)

        if "error" in result:
            return self._build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": ErrorCode.OPERATION_FAILED,
                    "message": result["error"],
                    "plugin_name": PLUGIN_NAME,
                },
            )

        return self._build_response(ActionStatus.COMPLETED.value, result)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/purge_memories_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="purge_memories_action",
                parameters={
            "confirm": ParameterMetadata(
                description="Must be true to proceed with purge",
                required=True,
                type=ParameterType.BOOLEAN,
            ),
        },
        output_type="object",
        output_description="Purge result with deletion counts",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Purge result with deletion counts",
            properties={
                "deleted_memories": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of memories deleted"
                ),
                "deleted_memorizations": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of memorizations deleted"
                ),
                "deleted_vectors": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of vectors deleted"
                ),
                "purged": ParameterMetadata(
                    type=ParameterType.BOOLEAN, description="Whether purge was executed"
                ),
            },
        ),
                summary="Permanently delete all memories from the system",
        requires_result_processor=True,
    )
    def purge_memories_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Purge all memories - action handler."""
        del state  # Unused - state not needed for purge
        confirm = params.get("confirm", False)

        service = self._get_backend()
        result = service.purge_memories(confirm=confirm)

        if "error" in result:
            return self._build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": ErrorCode.OPERATION_FAILED,
                    "message": result["error"],
                    "plugin_name": PLUGIN_NAME,
                },
            )

        return self._build_response(ActionStatus.COMPLETED.value, result)

    # ─────────────────────────────────────────────────────────────────────────
    # SCHEDULING SETUP
    # ─────────────────────────────────────────────────────────────────────────

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/setup_schedules.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="setup_schedules",
                parameters={},
        output_type="object",
        output_description="Schedule setup result",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Schedule setup result",
            properties={
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Success message"
                ),
                "schedules": ParameterMetadata(
                    type=ParameterType.LIST, description="List of configured schedules"
                ),
            },
        ),
                summary="Configure scheduled memory maintenance operations"
    )
    def setup_schedules(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Set up scheduled maintenance operations."""
        del params  # Unused - schedules configured internally
        # The caller-injected `state` carries the caller's flow_id/session_id;
        # the three crons below override it with hardcoded system-owned
        # identifiers so the cron-fired actions do not couple to the caller's
        # session. `action_factory._enforce_flow_id` refuses absent flow_ids
        # even on EDGE_SINK paths, so distinct per-cron identifiers preserve
        # audit-trail distinguishability.
        del state
        if self._schedules_configured:
            return self._build_response(
                ActionStatus.COMPLETED.value,
                {"message": "Schedules already configured"},
            )

        if not self._scheduling_service:
            return self._build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": ErrorCode.SCHEDULING_SERVICE_NOT_AVAILABLE,
                    "message": "scheduling_service not available",
                    "plugin_name": PLUGIN_NAME,
                },
            )

        created_schedules = []

        try:
            config = self.config_provider.config if self.config_provider else {}

            if config.get("enable_scheduled_operations", True):
                # The three crons below dispatch the cron-only EDGE_SINK
                # siblings (`service_interface::memory_service::*_cron`)
                # declared `is_discoverable=False` at
                # `ananta/src/ananta/services/memory_service/interfaces/public.py`.
                # Each cron-sibling is a thin Shape-A pass-through that calls
                # the same backend method as the discoverable EDGE-category
                # verb (process_memorization_queue / consolidate /
                # recompute_strengths). The EDGE_SINK category causes
                # `action_queue_poller._dispatch_*` to short-circuit at the
                # EDGE_SINK_SKIP branch — terminal action, no result-processor
                # dispatch, no inference scaffold, no `<<get_flow_input>>`
                # macro lookup, no `core__flows` pre-seed required. Per the
                # canonical contract at
                # `knowledge_bases/ananta_platform/21_scheduling_service/
                # 01_template_flow_record_lifecycle.md` and the
                # §5.3-REDIRECT inventory at
                # `workbench/2026-06-17_cron_action_contract_inventory.md`.
                # The model-callable EDGE-category verbs stay
                # `is_discoverable=True` for direct model invocation; the
                # cron-only siblings exist alongside them and are
                # registry-invisible to the model (not surfaced via
                # `process_search`).

                # Memorization queue processing (every 6 hours).
                memorization_result = self._scheduling_service.create_cron_schedule(
                    params={
                        "cron_expression": config.get(
                            "memorization_queue_cron", MEMORIZATION_QUEUE_CRON
                        ),
                        "label": "ACT-R Memorization Queue Processing",
                        "tags": [PLUGIN_NAME, "memorization"],
                        "actions": [
                            {
                                "process_key": "service_interface::memory_service::process_memorization_queue_cron",
                                "arguments": {},
                            }
                        ],
                    },
                    state={
                        "flow_id": _ACTR_MEMORIZATION_FLOW_ID,
                        "session_id": _ACTR_MEMORIZATION_SESSION_ID,
                    },
                )
                if memorization_result.get("action_status") == ActionStatus.COMPLETED.value:
                    created_schedules.append("memorization_queue")

                # Strength recomputation (daily).
                strength_result = self._scheduling_service.create_cron_schedule(
                    params={
                        "cron_expression": config.get(
                            "strength_recompute_cron", STRENGTH_RECOMPUTE_CRON
                        ),
                        "label": "ACT-R Strength Recomputation",
                        "tags": [PLUGIN_NAME, "strength"],
                        "actions": [
                            {
                                "process_key": "service_interface::memory_service::recompute_strengths_cron",
                                "arguments": {},
                            }
                        ],
                    },
                    state={
                        "flow_id": _ACTR_STRENGTH_FLOW_ID,
                        "session_id": _ACTR_STRENGTH_SESSION_ID,
                    },
                )
                if strength_result.get("action_status") == ActionStatus.COMPLETED.value:
                    created_schedules.append("strength_recompute")

                # Consolidation (weekly). Semantic equivalence verified: the
                # wrapper calls `self._backend.consolidate(dry_run=dry_run)`,
                # the same code path as the discoverable
                # `service_interface::memory_service::consolidate` verb (both
                # rely on the backend's `EPISODIC_CONSOLIDATION_THRESHOLD=-1.5`
                # + `MIN_AGE_FOR_CONSOLIDATION_DAYS=7` defaults).
                consolidation_result = self._scheduling_service.create_cron_schedule(
                    params={
                        "cron_expression": config.get("consolidation_cron", CONSOLIDATION_CRON),
                        "label": "ACT-R Memory Consolidation",
                        "tags": [PLUGIN_NAME, "consolidation"],
                        "actions": [
                            {
                                "process_key": "service_interface::memory_service::consolidate_cron",
                                "arguments": {"dry_run": False},
                            }
                        ],
                    },
                    state={
                        "flow_id": _ACTR_CONSOLIDATION_FLOW_ID,
                        "session_id": _ACTR_CONSOLIDATION_SESSION_ID,
                    },
                )
                if consolidation_result.get("action_status") == ActionStatus.COMPLETED.value:
                    created_schedules.append("consolidation")

            self._schedules_configured = True

            return self._build_response(
                ActionStatus.COMPLETED.value,
                {
                    "message": f"Created {len(created_schedules)} schedules",
                    "schedules": created_schedules,
                },
            )

        except Exception as e:
            self.logger.error(f"Failed to set up schedules: {e}", exc_info=True)
            return self._build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": ErrorCode.OPERATION_FAILED,
                    "message": str(e),
                    "plugin_name": PLUGIN_NAME,
                },
            )

    # ─────────────────────────────────────────────────────────────────────────
    # INTERFACE METHODS (MemoryServiceInterface implementation)
    # These are called via service_interface:: namespace, not plugin::
    # ─────────────────────────────────────────────────────────────────────────

    # ABC implementation - these override the abstract methods from MemoryServiceInterface
    # Note: The @platform_process decorated methods are named *_action() to avoid conflicts

    def remember(
        self,
        content: str,
        tags: list[str] | None = None,
        source_file: str | None = None,
        session_id: str | None = None,
        embed: bool = True,
    ) -> dict[str, Any]:
        """Store a new memory - interface method.

        Args:
            embed: Whether to generate a vector embedding.  Set to
                ``False`` for focus-only working-context mirrors.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        result = self._backend.remember(
            content=content, tags=tags or [], source_file=source_file,
            session_id=session_id, embed=embed,
        )
        return result

    def recall(
        self,
        query: str,
        top_k: int = 5,
        memory_type: str = "all",
        include_archived: bool = False,
        tags: list[str] | None = None,
        exclude_ids: list[str] | None = None,
        score_by_similarity: bool = False,
        exclude_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Retrieve memories by semantic similarity - interface method.

        Matches MemoryServiceAPI.recall() signature.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        result = self._backend.recall(
            query=query,
            top_k=top_k,
            memory_type=memory_type,
            include_archived=include_archived,
            tags=tags,
            exclude_ids=exclude_ids,
            score_by_similarity=score_by_similarity,
            exclude_tags=exclude_tags,
        )
        return result

    def forget(self, memory_id: str) -> dict[str, Any]:
        """Archive a memory - interface method.

        Matches MemoryServiceAPI.forget() signature.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        result = self._backend.forget(memory_id=memory_id)
        return result

    def reinforce(self, memory_id: str) -> dict[str, Any]:
        """Reinforce a memory by adding a retrieval timestamp - interface method.

        Matches MemoryServiceAPI.reinforce() signature.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        result = self._backend.reinforce(memory_id=memory_id)
        return result

    def memorize(
        self,
        memory_id: str | None = None,
        content: str | None = None,
    ) -> dict[str, Any]:
        """Add a memory to the memorization queue - interface method.

        Matches MemoryServiceAPI.memorize() signature.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        result = self._backend.memorize(memory_id=memory_id, content=content)
        return result

    def list_memories(
        self,
        memory_type: str | None = None,
        status: str = "active",
        tag: str | None = None,
        sort_by: str = "strength",
        limit: int = 20,
    ) -> dict[str, Any]:
        """List memories with filters - interface method.

        Matches MemoryServiceAPI.list_memories() signature.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        result = self._backend.list_memories(
            memory_type=memory_type, status=status, tag=tag, sort_by=sort_by, limit=limit
        )
        return result

    def consolidate(
        self,
        strength_threshold: float = -1.5,
        min_age_days: int = 7,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Summarize weak episodic memories into semantic memories - interface method.

        Matches MemoryServiceAPI.consolidate() signature.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        result = self._backend.consolidate(
            strength_threshold=strength_threshold, min_age_days=min_age_days, dry_run=dry_run
        )
        return result

    def consolidate_cron(self, dry_run: bool = False) -> dict[str, Any]:
        """Cron-only EDGE_SINK wrapper around consolidate - interface method.

        Thin Shape-A pass-through invoked by the actr_memory_plugin
        consolidation cron via `service_interface::memory_service::consolidate_cron`
        (declared `is_discoverable=False` + `EDGE_SINK` in
        `ananta/src/ananta/services/memory_service/interfaces/public.py`).
        Calls the same backend method as the discoverable sibling;
        `action_queue_poller` terminates at EDGE_SINK_SKIP so no inference
        scaffold fires.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        return self._backend.consolidate(dry_run=dry_run)

    def export_memories(
        self,
        file_path: str | None = None,
        include_archived: bool = False,
        include_embeddings: bool = False,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Export memories to JSON file - interface method.

        Matches MemoryServiceAPI.export_memories() signature.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        result = self._backend.export_memories(
            file_path=file_path,
            include_archived=include_archived,
            include_embeddings=include_embeddings,
            tags=tags,
        )
        return result

    def memory_stats(self) -> dict[str, Any]:
        """Get memory system statistics - interface method.

        Matches MemoryServiceInterface.memory_stats() signature.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        return self._backend.memory_stats()

    def purge_memories(self, confirm: bool = False) -> dict[str, Any]:
        """Permanently delete all memories - interface method.

        Matches MemoryServiceInterface.purge_memories() signature.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        return self._backend.purge_memories(confirm=confirm)

    def get_recent_memory(
        self,
        session_id: str | None = None,
        max_events: int = 20,
        max_age_hours: int | None = None,
        namespace_filter: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve recent memory formatted for LLM context - interface method.

        Memories are global - session_id is an optional filter.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        result = self._backend.get_recent_memory(
            session_id=session_id,
            max_events=max_events,
            max_age_hours=max_age_hours,
            namespace_filter=namespace_filter,
        )
        return result

    def get_session_event_stats(self, session_id: str) -> dict[str, Any]:
        """Get conversation event statistics for a session (NOT long-term memories).

        Matches MemoryServiceAPI.get_session_event_stats() signature.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        result = self._backend.get_session_event_stats(session_id=session_id)
        return result

    def learn(
        self,
        path: str,
        pattern: str = "*.md",
        recursive: bool = True,
        memorize: bool = False,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Ingest knowledge from files, optionally memorizing it all - interface method.

        Matches MemoryServiceAPI.learn() signature.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        result = self._backend.learn(
            path=path, pattern=pattern, recursive=recursive, memorize=memorize, tags=tags
        )
        return result

    def ingest_session(
        self,
        transcript: str,
        session_id: str | None = None,
        chunk_by_turns: bool = True,
    ) -> dict[str, Any]:
        """Ingest a conversation transcript as episodic memories - interface method.

        Matches MemoryServiceAPI.ingest_session() signature.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        result = self._backend.ingest_session(
            transcript=transcript, session_id=session_id, chunk_by_turns=chunk_by_turns
        )
        return result

    def memorize_by_tag(self, tag: str) -> dict[str, Any]:
        """Add all memories with a specific tag to memorization queue - interface method.

        Matches MemoryServiceAPI.memorize_by_tag() signature.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        result = self._backend.memorize_by_tag(tag=tag)
        return result

    def stop_memorizing(self, memory_id: str) -> dict[str, Any]:
        """Remove a memory from memorization queue - interface method.

        Matches MemoryServiceAPI.stop_memorizing() signature.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        result = self._backend.stop_memorizing(memory_id=memory_id)
        return result

    def list_memorizing(self, include_completed: bool = False) -> dict[str, Any]:
        """List all memories being memorized - interface method.

        Matches MemoryServiceAPI.list_memorizing() signature.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        result = self._backend.list_memorizing(include_completed=include_completed)
        return result

    def process_memorization_queue(self) -> dict[str, Any]:
        """Process all due memorization reviews - interface method.

        Matches MemoryServiceAPI.process_memorization_queue() signature.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        result = self._backend.process_memorization_queue()
        return result

    def process_memorization_queue_cron(self) -> dict[str, Any]:
        """Cron-only EDGE_SINK wrapper around process_memorization_queue - interface method.

        Thin Shape-A pass-through invoked by the actr_memory_plugin
        memorization-queue cron via
        `service_interface::memory_service::process_memorization_queue_cron`
        (declared `is_discoverable=False` + `EDGE_SINK` in
        `ananta/src/ananta/services/memory_service/interfaces/public.py`).
        Calls the same backend method as the discoverable sibling;
        `action_queue_poller` terminates at EDGE_SINK_SKIP so no inference
        scaffold fires.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        return self._backend.process_memorization_queue()

    def recompute_strengths(self) -> dict[str, Any]:
        """Recalculate activation strength for all active memories - interface method.

        Matches MemoryServiceAPI.recompute_strengths() signature.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        result = self._backend.recompute_strengths()
        return result

    def recompute_strengths_cron(self) -> dict[str, Any]:
        """Cron-only EDGE_SINK wrapper around recompute_strengths - interface method.

        Thin Shape-A pass-through invoked by the actr_memory_plugin
        strength-recomputation cron via
        `service_interface::memory_service::recompute_strengths_cron`
        (declared `is_discoverable=False` + `EDGE_SINK` in
        `ananta/src/ananta/services/memory_service/interfaces/public.py`).
        Calls the same backend method as the discoverable sibling;
        `action_queue_poller` terminates at EDGE_SINK_SKIP so no inference
        scaffold fires.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        return self._backend.recompute_strengths()

    def import_memories(
        self,
        file_path: str,
        regenerate_embeddings: bool = True,
    ) -> dict[str, Any]:
        """Import memories from JSON file - interface method.

        Matches MemoryServiceAPI.import_memories() signature.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        result = self._backend.import_memories(
            file_path=file_path, regenerate_embeddings=regenerate_embeddings
        )
        return result

    def cleanup_orphaned_vectors(
        self, dry_run: bool = False, confirm: bool = False
    ) -> dict[str, Any]:
        """Rebuild the memory-vector namespace - interface method.

        Matches MemoryServiceAPI.cleanup_orphaned_vectors() signature.
        """
        if not self._backend:
            raise FrameworkError(
                message="Memory service backend not initialized",
                error_code="memory.backend_unavailable",
            )
        result = self._backend.cleanup_orphaned_vectors(dry_run=dry_run, confirm=confirm)
        return result

    def store_interaction(
        self,
        session_id: str,
        source_namespace: str,
        event_type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """Store a short-term interaction event - interface method."""
        backend = self._get_backend()
        return backend.store_interaction(
            session_id=session_id,
            source_namespace=source_namespace,
            event_type=event_type,
            content=content,
            metadata=metadata,
            timestamp=timestamp,
        )

    def get_recent_memory_structured(
        self,
        session_id: str | None = None,
        max_events: int = 20,
        max_age_hours: int | None = None,
        namespace_filter: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve recent memory as structured events - interface method.

        Envelope shape {"events": [...], "count": N} — the service-interface
        dispatch contract requires a dict return.
        """
        backend = self._get_backend()
        events = backend.get_recent_memory_structured(
            session_id=session_id,
            max_events=max_events,
            max_age_hours=max_age_hours,
            namespace_filter=namespace_filter,
        )
        return {"events": events, "count": len(events)}

    def delete_memories_by_tag(self, tag: str) -> dict[str, Any]:
        """Delete all memories with a specific tag - interface method."""
        backend = self._get_backend()
        return backend.delete_memories_by_tag(tag=tag)

    def delete_memories_by_ids(self, ids: list[str]) -> dict[str, Any]:
        """Hard-delete specific memories by id, with embeddings - interface method."""
        backend = self._get_backend()
        return backend.delete_memories_by_ids(ids=ids)

    def get_memories_by_tag(
        self,
        tag: str,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        """Get all memories with a specific tag - interface method."""
        backend = self._get_backend()
        return backend.get_memories_by_tag(tag=tag, include_archived=include_archived)

    def upsert_memory_by_tag(
        self,
        content: str,
        tag: str,
        tags: list[str] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Replace-by-tag: store new content, then delete previous memories - interface method."""
        backend = self._get_backend()
        return backend.upsert_memory_by_tag(
            content=content, tag=tag, tags=tags, session_id=session_id
        )

    def reindex_orphaned_vectors(self) -> dict[str, Any]:
        """Attempt to relink orphaned vectors to memories - interface method."""
        backend = self._get_backend()
        return backend.reindex_orphaned_vectors()

    def audit_pinned_notes(
        self,
        include_completed: bool = False,
        strength_threshold: float | None = None,
    ) -> dict[str, Any]:
        """Audit pinned notes for review - interface method."""
        backend = self._get_backend()
        return backend.audit_pinned_notes(
            include_completed=include_completed,
            strength_threshold=strength_threshold,
        )

    def review_blocked_intents(
        self,
        min_age_days: int = 7,
        strength_threshold: float = -1.0,
    ) -> dict[str, Any]:
        """Review blocked intents for potential unblocking - interface method."""
        backend = self._get_backend()
        return backend.review_blocked_intents(
            min_age_days=min_age_days,
            strength_threshold=strength_threshold,
        )

    def focus(self, memory_id: str, *, session_id: str) -> dict[str, Any]:
        """Pin a memory to the acting session's focus buffer - interface method."""
        backend = self._get_backend()
        return backend.focus(memory_id=memory_id, session_id=session_id)

    def unfocus(self, memory_id: str, *, session_id: str) -> dict[str, Any]:
        """Remove a memory from the acting session's focus buffer - interface method."""
        backend = self._get_backend()
        return backend.unfocus(memory_id=memory_id, session_id=session_id)

    def unfocus_all_for_session(self, *, session_id: str) -> dict[str, Any]:
        """Release every pin held by one session - interface method (JOS-02 R1)."""
        backend = self._get_backend()
        return backend.unfocus_all_for_session(session_id=session_id)

    def get_focused(self, *, session_id: str) -> dict[str, Any]:
        """Return the acting session's focused memories - interface method.

        Envelope shape {"memories": [...], "count": N} — the service-interface
        dispatch contract requires a dict return. Focus is session-scoped
        (JOS-02); the per-session capacity valve is the backend's MAX_FOCUSED.
        """
        backend = self._get_backend()
        memories = backend.get_focused(session_id=session_id)
        return {"memories": memories, "count": len(memories)}

    def store_compaction_summary(
        self,
        context_id: str,
        summary: str,
        compacted_event_count: int,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Store a compaction summary from context management - interface method."""
        backend = self._get_backend()
        return backend.store_compaction_summary(
            context_id=context_id,
            summary=summary,
            compacted_event_count=compacted_event_count,
            session_id=session_id,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # ASYNC WRAPPERS - Explicit signatures (no *args/**kwargs per v58)
    # ─────────────────────────────────────────────────────────────────────────

    async def remember_async(
        self,
        content: str,
        tags: list[str] | None = None,
        source_file: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return self.remember(content=content, tags=tags, source_file=source_file, session_id=session_id)

    async def recall_async(
        self,
        query: str,
        top_k: int = 5,
        memory_type: str = "all",
        include_archived: bool = False,
        tags: list[str] | None = None,
        exclude_ids: list[str] | None = None,
        score_by_similarity: bool = False,
    ) -> dict[str, Any]:
        return self.recall(
            query=query, top_k=top_k, memory_type=memory_type,
            include_archived=include_archived, tags=tags, exclude_ids=exclude_ids,
            score_by_similarity=score_by_similarity,
        )

    async def forget_async(self, memory_id: str) -> dict[str, Any]:
        return self.forget(memory_id=memory_id)

    async def reinforce_async(self, memory_id: str) -> dict[str, Any]:
        return self.reinforce(memory_id=memory_id)

    async def memorize_async(
        self,
        memory_id: str | None = None,
        content: str | None = None,
    ) -> dict[str, Any]:
        return self.memorize(memory_id=memory_id, content=content)

    async def stop_memorizing_async(self, memory_id: str) -> dict[str, Any]:
        return self.stop_memorizing(memory_id=memory_id)

    async def list_memorizing_async(self, include_completed: bool = False) -> dict[str, Any]:
        return self.list_memorizing(include_completed=include_completed)

    async def learn_async(
        self,
        path: str,
        pattern: str = "*.md",
        recursive: bool = True,
        memorize: bool = False,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.learn(path=path, pattern=pattern, recursive=recursive, memorize=memorize, tags=tags)

    async def consolidate_async(
        self,
        strength_threshold: float = -1.5,
        min_age_days: int = 7,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.consolidate(
            strength_threshold=strength_threshold, min_age_days=min_age_days, dry_run=dry_run
        )

    async def recompute_strengths_async(self) -> dict[str, Any]:
        return self.recompute_strengths()

    async def process_memorization_queue_async(self) -> dict[str, Any]:
        return self.process_memorization_queue()

    async def list_memories_async(
        self,
        memory_type: str | None = None,
        status: str = "active",
        tag: str | None = None,
        sort_by: str = "strength",
        limit: int = 20,
    ) -> dict[str, Any]:
        return self.list_memories(
            memory_type=memory_type, status=status, tag=tag, sort_by=sort_by, limit=limit
        )

    async def export_memories_async(
        self,
        file_path: str | None = None,
        include_archived: bool = False,
        include_embeddings: bool = False,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.export_memories(
            file_path=file_path,
            include_archived=include_archived,
            include_embeddings=include_embeddings,
            tags=tags,
        )

    async def import_memories_async(
        self,
        file_path: str,
        regenerate_embeddings: bool = True,
    ) -> dict[str, Any]:
        return self.import_memories(file_path=file_path, regenerate_embeddings=regenerate_embeddings)

    async def cleanup_orphaned_vectors_async(
        self, dry_run: bool = False, confirm: bool = False
    ) -> dict[str, Any]:
        return self.cleanup_orphaned_vectors(dry_run=dry_run, confirm=confirm)

    async def get_recent_memory_async(
        self,
        session_id: str | None = None,
        max_events: int = 20,
        max_age_hours: int | None = None,
        namespace_filter: str | None = None,
    ) -> dict[str, Any]:
        return self.get_recent_memory(
            session_id=session_id, max_events=max_events,
            max_age_hours=max_age_hours, namespace_filter=namespace_filter
        )

    async def get_session_event_stats_async(self, session_id: str) -> dict[str, Any]:
        return self.get_session_event_stats(session_id=session_id)

    async def ingest_session_async(
        self,
        transcript: str,
        session_id: str | None = None,
        chunk_by_turns: bool = True,
    ) -> dict[str, Any]:
        return self.ingest_session(transcript=transcript, session_id=session_id, chunk_by_turns=chunk_by_turns)

    async def memorize_by_tag_async(self, tag: str) -> dict[str, Any]:
        return self.memorize_by_tag(tag=tag)

    async def memory_stats_async(self) -> dict[str, Any]:
        return self.memory_stats()

    async def setup_schedules_async(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        return self.setup_schedules(params=params, state=state)

    async def store_interaction_async(
        self,
        session_id: str,
        source_namespace: str,
        event_type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        return self.store_interaction(
            session_id=session_id,
            source_namespace=source_namespace,
            event_type=event_type,
            content=content,
            metadata=metadata,
            timestamp=timestamp,
        )

    async def get_recent_memory_structured_async(
        self,
        session_id: str | None = None,
        max_events: int = 20,
        max_age_hours: int | None = None,
        namespace_filter: str | None = None,
    ) -> dict[str, Any]:
        return self.get_recent_memory_structured(
            session_id=session_id,
            max_events=max_events,
            max_age_hours=max_age_hours,
            namespace_filter=namespace_filter,
        )

    async def delete_memories_by_tag_async(self, tag: str) -> dict[str, Any]:
        return self.delete_memories_by_tag(tag=tag)

    async def delete_memories_by_ids_async(self, ids: list[str]) -> dict[str, Any]:
        return self.delete_memories_by_ids(ids=ids)

    async def get_memories_by_tag_async(
        self,
        tag: str,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        return self.get_memories_by_tag(tag=tag, include_archived=include_archived)

    async def upsert_memory_by_tag_async(
        self,
        content: str,
        tag: str,
        tags: list[str] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return self.upsert_memory_by_tag(
            content=content, tag=tag, tags=tags, session_id=session_id
        )

    async def reindex_orphaned_vectors_async(self) -> dict[str, Any]:
        return self.reindex_orphaned_vectors()

    async def audit_pinned_notes_async(
        self,
        include_completed: bool = False,
        strength_threshold: float | None = None,
    ) -> dict[str, Any]:
        return self.audit_pinned_notes(
            include_completed=include_completed,
            strength_threshold=strength_threshold,
        )

    async def review_blocked_intents_async(
        self,
        min_age_days: int = 7,
        strength_threshold: float = -1.0,
    ) -> dict[str, Any]:
        return self.review_blocked_intents(
            min_age_days=min_age_days,
            strength_threshold=strength_threshold,
        )

    async def store_compaction_summary_async(
        self,
        context_id: str,
        summary: str,
        compacted_event_count: int,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return self.store_compaction_summary(
            context_id=context_id,
            summary=summary,
            compacted_event_count=compacted_event_count,
            session_id=session_id,
        )
