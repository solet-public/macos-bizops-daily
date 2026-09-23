"""InferenceService - Service wrapper for LLM inference operations.

This service provides a stable interface for LLM inference, allowing the underlying
inference provider plugin to be swapped without breaking consumer code.

Bootstrap Mode: NOT SUPPORTED (inference not needed during system startup)
Plugin Mode: Wraps default_inference_plugin (or configured alternative via env)
"""

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from ananta.constants import DEFAULT_INFERENCE_PLUGIN as DEFAULT_INFERENCE_PLUGIN
from ananta.core.domain.types import ActionResult
from ananta.core.plugins.plugin_manager import PluginManager
from ananta.error_handling import FrameworkError
from ananta.interfaces.bootstrappable_service_interface import BootstrappableServiceInterface
from ananta.interfaces.inference_service_interface import (
    InferenceRequest,
    InferenceServiceInterface,
)
from ananta.services.inference_service.assembly_types import (
    PromptAssemblyRequest,
    PromptAssemblyResult,
)
from ananta.services.inference_service.cold_context import (
    AUTONOMIC_ASSEMBLED_CONTEXT_KEY,
    assemble_cold_context,
)
from ananta.services.inference_service.completion_request_queue import (
    CompletionRequestStore,
    read_completion_request,
    require_completion_request_store,
)
from ananta.services.inference_service.completion_routing import (
    route_completion_request,
)
from ananta.services.inference_service.deferred_vertex_queue import (
    DeferredVertexStore,
    deferred_vertices_snapshot,
    forward_with_serve_anchor,
    record_deferred_vertex,
    require_deferred_vertex_store,
)
from ananta.services.inference_service.interfaces.provider import InferenceProvider
from ananta.services.inference_service.vertex_resolver import (
    InferenceProviderResolver,
    VertexResolution,
    VertexRouting,
)

if TYPE_CHECKING:
    from ananta.services.context_management.compaction_types import (
        CompactionRequest,
        WarmingRequest,
    )
    from ananta.services.context_management.config import ContextManagementConfig

logger = logging.getLogger(__name__)

# INF-03: stable token carried by every typed provider-vacancy error and the
# one boot-time vacancy warning. Consumers and smokes pin on this token, not
# on message prose.
VACANT_PROVIDER_TOKEN = "inference_service_vacant"

# Process-lifetime latch so the loud vacancy declaration reads ONCE per boot:
# two live sites construct InferenceService (startup_sequence
# _create_service_wrappers + ServiceManager) and would otherwise double-fire.
_vacancy_warning_emitted = False


def _warn_provider_vacant_once() -> None:
    """Emit the single loud boot-time vacancy declaration (INF-03)."""
    global _vacancy_warning_emitted
    if _vacancy_warning_emitted:
        logger.debug(
            "inference_service VACANT (repeat construction — boot warning "
            "already emitted)"
        )
        return
    _vacancy_warning_emitted = True
    logger.warning(
        "inference_service is VACANT: no provider bound in "
        "service_bindings.json and ANANTA_INFERENCE_PROVIDER unset. "
        "Vertex turns DEFER via sys:autonomic / the durable queue; "
        "completion and provider operations raise '%s' until a "
        "provider is bound.",
        VACANT_PROVIDER_TOKEN,
    )


def _qualify_provider(
    *,
    provider_name: str,
    plugin: InferenceProvider,
) -> ActionResult:
    """Run one private, bounded structured request against a ready provider."""
    schema: dict[str, Any] = {
        "type": "object",
        "required": ["qualified"],
        "properties": {"qualified": {"type": "boolean"}},
        "additionalProperties": False,
    }
    request = InferenceRequest(
        "Return exactly the JSON object required by the response schema.",
        temperature=0.0,
        max_tokens=16,
        response_schema=schema,
        use_structured_output=True,
        hide_from_context=True,
    )
    result = plugin.generate_completion(request)
    result_data = result.get("data", {})
    raw_result = result_data.get("result", {})
    completion = raw_result.get("completion") if isinstance(raw_result, dict) else None
    structured_result_valid = False
    if isinstance(completion, str):
        try:
            parsed = json.loads(completion)
        except json.JSONDecodeError:
            parsed = None
        structured_result_valid = parsed == {"qualified": True}
    return {
        "action_status": "completed",
        "actions": [],
        "data": {
            "provider": provider_name,
            "model": plugin.get_configured_model_name(),
            "completed": result.get("action_status") == "completed"
            and result.get("error") is None,
            "structured_result_valid": structured_result_valid,
        },
        "error": None,
    }


class _InferenceQualificationMixin:
    """Keeps the public provider probe outside the orchestration wrapper."""

    def qualify(self, params: dict[str, Any], state: dict[str, Any]) -> ActionResult:
        del params, state
        service = cast("InferenceService", self)
        plugin = service._ensure_provider_ready()
        return _qualify_provider(
            provider_name=service._inference_plugin_name or type(plugin).__name__,
            plugin=plugin,
        )


class InferenceService(_InferenceQualificationMixin, BootstrappableServiceInterface, InferenceServiceInterface):
    """Service wrapper for inference plugin providers.

    Provides stable interface for LLM inference operations, enabling provider
    swapping (local models, API services, etc.) without breaking consumer code.

    This is a "simple wrapper" - no bootstrap mode, no complex business logic,
    just provider abstraction for swappability.

    Inherits from InferenceServiceInterface to satisfy type contracts when
    passed to services expecting that interface (e.g., ContextManagementService).
    """

    def __init__(
        self,
        plugin_manager: PluginManager | None = None,
        inference_plugin_name: str | None = None,
        app_home: str = "",
        state_service: object | None = None,
        orchestrator: object | None = None,
    ):
        """Initialize InferenceService.

        Args:
            plugin_manager: Plugin manager instance (REQUIRED)
            inference_plugin_name: Override plugin name (default: from constants)
            app_home: Application home directory
            state_service: State service instance for plugin initialization
            orchestrator: Event orchestrator for platform service resolution

        Raises:
            FrameworkError: If plugin_manager is None
        """
        if plugin_manager is None:
            raise FrameworkError(
                "InferenceService requires plugin_manager. "
                "Bootstrap mode not supported for inference operations."
            )

        if inference_plugin_name is None:
            # Try environment variable set by launch script
            import os

            inference_plugin_name = os.environ.get("ANANTA_INFERENCE_PROVIDER")

        if inference_plugin_name is None:
            # INF-03: declared-VACANT provider is a first-class bootable
            # state (operator-ruled 2026-07-03 — the mock_inference_plugin
            # hack is retired). Vertex turns route via the resolver below
            # (session provider / sys:autonomic / durable DEFER — all
            # provider-independent); every provider-touching operation
            # raises the typed vacancy error at _validate_inference_plugin;
            # get_context_management_config serves the vacant-state constant.
            _warn_provider_vacant_once()

        self._inference_plugin_name = inference_plugin_name
        self._inference_plugin: InferenceProvider | None = None
        self._pipeline_factory: Any = None  # Platform-owned PromptPipelineFactory
        self.app_home = app_home
        self._state_service = state_service
        self._orchestrator_ref = orchestrator
        self._flows_with_input_stored: set[str] = set()
        # INF-01 telemetry: organism turns that fell to the LOCAL default
        # model through the sys:autonomic STRUCTURAL fault edge (messaging
        # plugin unreachable / slot lookup raised). Post-flip, a mere
        # vacancy DEFERs instead — so this counts only the §D.3 safe-floor
        # degradations, each logged loud at the call site.
        self._autonomic_fault_degrade_turns: int = 0

        # Phase 5 (Seam B): per-flow inference vertex resolver. Routes
        # MCP-originating flows to their bound session provider (or DEFERs
        # when the bound session is absent) BEFORE the default local model
        # runs. Deferrals are recorded in the durable NO-LOSS queue
        # (``core__inference_deferred_vertex``, INF-01 §D.9) through
        # ``_state_service`` — no in-memory register, so N flows deferred
        # against one absent role all survive (and survive a restart) for
        # the sub-slice-2 vacancy-fill drain.
        self._vertex_resolver = InferenceProviderResolver(
            plugin_manager=plugin_manager,
            state_service=state_service,
        )

        # Initialize via BootstrappableServiceInterface pattern
        super().__init__(plugin_manager)

        # Override plugin_manager type annotation for mypy
        self.plugin_manager: PluginManager = plugin_manager

    def _init_bootstrap(self) -> None:
        """Bootstrap mode not supported for inference service.

        Raises:
            FrameworkError: Always (bootstrap mode not supported)
        """
        raise FrameworkError(
            "InferenceService does not support bootstrap mode. "
            "Inference operations require plugin provider."
        )

    def _init_plugin(self) -> None:
        """Initialize plugin mode - validation deferred until first use."""
        logger.debug(f"InferenceService initializing with plugin: {self._inference_plugin_name}")

    def _validate_inference_plugin(self) -> InferenceProvider:
        """Validate that inference plugin exists and implements InferenceProvider.

        Returns:
            The inference plugin typed as InferenceProvider

        Raises:
            FrameworkError: If the provider is declared vacant (INF-03), or the
                plugin is not found or doesn't implement InferenceProvider
        """
        if self._inference_plugin is None:
            if self._inference_plugin_name is None:
                raise FrameworkError(
                    f"{VACANT_PROVIDER_TOKEN}: inference_service is declared "
                    "VACANT — no provider plugin is bound. Completion and "
                    "provider operations are unavailable; bind a provider in "
                    "service_bindings.json (or set ANANTA_INFERENCE_PROVIDER). "
                    "Vertex turns continue to route via sys:autonomic and the "
                    "durable deferred queue (INF-03)."
                )
            plugin = self.plugin_manager.get_plugin(self._inference_plugin_name)

            if not isinstance(plugin, InferenceProvider):
                raise FrameworkError(
                    f"Inference plugin '{self._inference_plugin_name}' does not implement InferenceProvider. "
                    f"Plugin type: {type(plugin)}"
                )

            # isinstance check narrows type, no cast needed
            self._inference_plugin = plugin

            # CRITICAL: Notify plugin it's an active interface provider
            setter = getattr(plugin, "set_as_active_provider", None)
            if callable(setter):
                setter("InferenceProvider")
                logger.debug(
                    f"Notified {self._inference_plugin_name} that it's active InferenceProvider"
                )

        return self._inference_plugin

    def get_inference_provider(self) -> InferenceProvider | None:
        """Get the underlying inference provider plugin.

        Returns the validated inference plugin typed as InferenceProvider.
        Returns None if plugin is not yet initialized.

        This is the public API for accessing the provider - do not access
        _inference_plugin directly.
        """
        return self._inference_plugin

    def _ensure_provider_ready(self) -> InferenceProvider:
        """Ensure plugin exists, implements InferenceProvider, and is ready.

        Does NOT construct the pipeline factory — use ``_ensure_ready``
        for operations that need prompt assembly.
        """
        plugin = self._validate_inference_plugin()
        if not plugin.is_ready():
            error = plugin.readiness_error or "Unknown readiness error"
            raise FrameworkError(
                f"Inference plugin '{self._inference_plugin_name}' not ready: {error}"
            )
        return plugin

    def _ensure_ready(self) -> InferenceProvider:
        """Ensure plugin is ready AND pipeline factory is constructed.

        Use for operations that need prompt assembly.  For simple
        provider operations (health checks, model info), use
        ``_ensure_provider_ready`` instead.
        """
        plugin = self._ensure_provider_ready()
        if self._pipeline_factory is None:
            self._initialize_pipeline_factory()
        return plugin

    def _initialize_pipeline_factory(self) -> None:
        """Create the PromptPipelineFactory from platform services.

        Constructs ``PipelineDependencies`` from orchestrator-resolved
        services.  No plugin involvement — all adapters wrap platform
        services directly.
        """
        from ananta.core.prompts.pipeline_construction import (
            build_pipeline_dependencies,
        )
        from ananta.core.prompts.pipeline_factory import PromptPipelineFactory

        if self._orchestrator_ref is None:
            raise FrameworkError("orchestrator_ref required for pipeline factory")

        context_config = self.get_context_management_config()
        deps = build_pipeline_dependencies(
            app_home=self.app_home,
            context_config=context_config,
            orchestrator=self._orchestrator_ref,
            state_service=self._state_service,
        )
        self._pipeline_factory = PromptPipelineFactory(deps)
        logger.info("InferenceService: PromptPipelineFactory initialized")

    def process_error(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Error-context inference: handle action errors and provide recovery.

        Phase 5: short-circuit ABOVE ``_execute_transaction`` — if the flow
        is bound to a session vertex, route there (or DEFER when it is
        absent); only unbound flows run the default local model.
        """
        routed = self._route_vertex(is_error=True, state=state, params=params)
        if routed is not None:
            return routed
        self._ensure_ready()
        return self._execute_transaction(params, state)

    def process_results(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Result-processing inference: format results and determine next steps.

        Phase 5: short-circuit ABOVE ``_execute_transaction`` — see
        :meth:`process_error`.
        """
        routed = self._route_vertex(is_error=False, state=state, params=params)
        if routed is not None:
            return routed
        self._ensure_ready()
        return self._execute_transaction(params, state)

    def _route_vertex(
        self,
        *,
        is_error: bool,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> ActionResult | None:
        """Resolve the flow's vertex and route it, or ``None`` for the default.

        ``None`` signals the caller to run the default-provider path
        (``_ensure_ready`` + ``_execute_transaction``) unchanged.

        Order: a per-flow vertex binding (PROVIDER/DEFER) wins; otherwise the
        DEFAULT verdict tries the ``sys:autonomic`` fault-edge holder (INF-01)
        before falling to the local default.
        """
        resolution = self._vertex_resolver.resolve(state)
        if resolution.routing is VertexRouting.PROVIDER:
            return self._dispatch_to_provider(
                resolution=resolution, is_error=is_error, params=params, state=state,
            )
        if resolution.routing is VertexRouting.DEFER:
            flow_id = state.get("flow_id")
            return self._record_deferred_vertex(
                is_error=is_error,
                resolution=resolution,
                flow_id=flow_id if isinstance(flow_id, str) else None,
            )
        # DEFAULT verdict — no per-flow vertex binding. INF-01 fault-edge: try
        # the sys:autonomic frontier holder before the local default model.
        # resolve_autonomic self-guards the plugin-unreachable edge (→ DEFAULT).
        #
        # ★ SUB-SLICE-2 POLICY (the FLIP, Day-ruled): vacant / gone holder →
        # DEFER into the durable NO-LOSS queue, NEVER the local default. The
        # auto-assignment lifecycle (Trigger-1 vacancy-fill/crash-heal on
        # register, Trigger-2 grace-delayed succession at end, manual-set)
        # keeps the slot normally filled, and the first-claim drain re-drives
        # what a vacancy window accumulated. LOCAL remains ONLY for the two
        # structural fault edges resolve_autonomic itself guards: the
        # messaging plugin unreachable, or the slot lookup RAISING — both are
        # "cannot even confirm the slot" states where deferring would
        # black-hole the organism's own turn (§D.3 safe floor). The
        # flip-assertion smoke (autonomic_flip_smoke.py) FAILS the build if
        # vacancy ever falls LOCAL again.
        autonomic = self._vertex_resolver.resolve_autonomic()
        if autonomic.routing is VertexRouting.PROVIDER:
            # §D.4/B.2 cold-context: the autonomic holder is NOT the flow's
            # originator, so the forward carries the organism's ASSEMBLED
            # context (best-effort; raw flow refs remain the floor).
            assembled = self._assemble_cold_context(params=params, state=state)
            forward_state = (
                {**state, AUTONOMIC_ASSEMBLED_CONTEXT_KEY: assembled}
                if assembled is not None
                else state
            )
            return self._dispatch_to_provider(
                resolution=autonomic, is_error=is_error, params=params,
                state=forward_state,
            )
        if autonomic.routing is VertexRouting.DEFER:
            flow_id = state.get("flow_id")
            return self._record_deferred_vertex(
                is_error=is_error,
                resolution=autonomic,
                flow_id=flow_id if isinstance(flow_id, str) else None,
            )
        # Structural fault edge → local default model (loud, counted).
        self._autonomic_fault_degrade_turns += 1
        logger.warning(
            "sys:autonomic fault-degrade turn #%d: slot unconfirmable "
            "(plugin unreachable / lookup fault) — organism turn runs the "
            "LOCAL default model (§D.3 safe floor)",
            self._autonomic_fault_degrade_turns,
        )
        return None

    def _assemble_cold_context(
        self,
        *,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Best-effort §D.4 cold-context assembly (seam kept for the smokes)."""
        return assemble_cold_context(
            self._ensure_pipeline_factory, params=params, state=state,
            orchestrator=self._orchestrator_ref,
        )

    def _ensure_pipeline_factory(self) -> Any:
        """The platform-owned pipeline factory, built on first use; fail loud."""
        if self._pipeline_factory is None:
            self._initialize_pipeline_factory()
        factory = self._pipeline_factory
        if factory is None:
            raise FrameworkError("pipeline factory unavailable")
        return factory

    def _dispatch_to_provider(
        self,
        *,
        resolution: VertexResolution,
        is_error: bool,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Route a PROVIDER-verdict resolution to its session vertex (tagged-vertex
        path + ``sys:autonomic`` fault-edge). Delegates to
        ``forward_with_serve_anchor``: fails LOUD on the N6 no-provider invariant,
        and mints the INF-06 durable serve-anchor BEFORE the forward (durability-
        FIRST §8-bis Q1a) so no forward is ever anchorless.
        """
        return forward_with_serve_anchor(
            self._deferred_vertex_store(),
            resolution=resolution,
            is_error=is_error,
            params=params,
            state=state,
            now_iso=datetime.now(UTC).isoformat(),
            degrade=self._record_deferred_vertex,
        )

    def _record_deferred_vertex(
        self,
        *,
        is_error: bool,
        resolution: VertexResolution,
        flow_id: str | None,
    ) -> ActionResult:
        """Durable NO-LOSS deferral (§D.9) — see ``deferred_vertex_queue``."""
        return record_deferred_vertex(
            self._deferred_vertex_store(),
            is_error=is_error,
            resolution=resolution,
            flow_id=flow_id,
        )

    def _deferred_vertex_store(self) -> DeferredVertexStore:
        """The durable-queue state surface; fails LOUD if absent/incompatible."""
        return require_deferred_vertex_store(self._state_service)

    def get_deferred_vertices(self) -> dict[str, dict[str, object]]:
        """Durable-queue snapshot keyed by ``flow_id`` (drain / SUB-05 hook)."""
        return deferred_vertices_snapshot(self._deferred_vertex_store())

    # ------------------------------------------------------------------
    # INF-02 — autonomic-routed completion requests (async durable queue)
    # ------------------------------------------------------------------

    def submit_completion_request(
        self,
        *,
        purpose: str,
        messages: list[dict[str, str]],
        resume_process_key: str,
        correlation: dict[str, str],
    ) -> dict[str, object]:
        """Route a completion request per the INF-02 session-PRIMARY precedence.

        A WRAPPER-level surface (like the vertex resolver — NOT on the
        provider interface, whose rule is one-provider/no-routing).
        Precedence + verdict contract: ``completion_routing``.
        """
        return route_completion_request(
            resolution=self._vertex_resolver.resolve_autonomic(),
            store=self._completion_request_store(),
            provider_bound=self._inference_plugin_name is not None,
            purpose=purpose,
            messages=messages,
            resume_process_key=resume_process_key,
            correlation=correlation,
        )

    def get_completion_request(self, request_id: str) -> dict[str, object] | None:
        """Read one completion-request row (the resume verb's fetch path)."""
        return read_completion_request(
            self._completion_request_store(), request_id=request_id)

    def _completion_request_store(self) -> CompletionRequestStore:
        """The durable-queue state surface; fails LOUD if absent/incompatible."""
        return require_completion_request_store(self._state_service)

    def _execute_transaction(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Inference transaction entry point.

        Delegates to ``inference_transaction.execute`` which owns the
        full orchestration flow.
        """
        from ananta.services.inference_service.inference_transaction import (
            execute as execute_transaction,
        )

        plugin = self._inference_plugin
        if plugin is None or self._inference_plugin_name is None:
            raise FrameworkError("Inference plugin not initialized")

        return execute_transaction(
            plugin,
            self._inference_plugin_name,
            self._pipeline_factory,
            self._orchestrator_ref,
            self._state_service,  # type: ignore[arg-type]  # runtime is StateService
            self._flows_with_input_stored,
            params,
            state,
        )

    def propose_name(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Naming inference: propose a human-friendly name for a file or artifact.

        Used when user request is vague and a descriptive name must be derived.

        Args:
            params: Parameters dict containing intent_text, artifact_type, input_filename
            state: State dict for plugin execution

        Returns:
            ActionResult with proposed display_name, extension, confidence, and flags

        Raises:
            FrameworkError: If plugin not available or request fails
        """
        plugin = self._ensure_provider_ready()

        return plugin.propose_name(params, state)


    def assemble_prompt(
        self,
        request: PromptAssemblyRequest,
    ) -> PromptAssemblyResult:
        """Assemble a prompt via the platform-owned pipeline factory.

        Uses the ``PromptPipelineFactory`` constructed from the plugin's
        ``PipelineDependencies`` during first readiness check.

        Raises:
            FrameworkError: If plugin not available, not ready, or
                provider-only (no pipeline factory).
        """
        self._ensure_ready()

        from ananta.core.prompts.profiles import (
            INFERENCE_PROFILE,
            TEXT_COMPLETION_PROFILE,
            THINKING_ARTIFACT_PROFILE,
            THINKING_PROFILE,
        )
        from ananta.services.inference_service.assembly import (
            assemble_prompt as _assemble,
        )

        profiles = {
            "inference": INFERENCE_PROFILE,
            "thinking": THINKING_PROFILE,
            "thinking_artifact": THINKING_ARTIFACT_PROFILE,
            "text_completion": TEXT_COMPLETION_PROFILE,
        }
        profile = profiles.get(request.profile_name)
        if profile is None:
            raise FrameworkError(
                f"Unknown assembly profile: {request.profile_name!r}"
            )

        return _assemble(request, profile, self._pipeline_factory)

    def validate_availability(self) -> ActionResult:
        """Check if inference service is available and responsive.

        Returns:
            ActionResult indicating service health

        Raises:
            FrameworkError: If plugin not available
        """
        plugin = self._ensure_provider_ready()
        return plugin.validate_availability()

    def get_model_info(self, model: str | None = None) -> ActionResult:
        """Get information about available model(s).

        Args:
            model: Optional specific model name (returns all if not specified)

        Returns:
            ActionResult with model information in data.result field

        Raises:
            FrameworkError: If plugin not available
        """
        # TODO: Implement model filtering when model parameter is provided to return specific model info
        _ = model  # Acknowledge parameter is part of public API
        plugin = self._ensure_provider_ready()
        return plugin.get_model_info()

    def is_ready(self) -> bool:
        """Check if the inference service is ready for use.

        Returns True if the underlying plugin is validated and ready.
        This method does not raise on unready state (use get_readiness_error for details).
        """
        if self._inference_plugin is None:
            # Plugin not yet validated - try to validate
            try:
                self._validate_inference_plugin()
            except FrameworkError:
                return False

        if self._inference_plugin is None:
            return False

        return self._inference_plugin.is_ready()

    def get_readiness_error(self) -> str | None:
        """Get the error message if not ready, None if ready.

        Returns detailed error message explaining why the service is not ready,
        or None if the service is ready for use.
        """
        if self._inference_plugin is None:
            try:
                self._validate_inference_plugin()
            except FrameworkError as e:
                return str(e)

        if self._inference_plugin is None:
            return f"Inference plugin '{self._inference_plugin_name}' not found"

        return self._inference_plugin.readiness_error

    def generate_completion(self, request: InferenceRequest) -> ActionResult:
        """Generate completion from inference model (low-level provider method).

        Args:
            request: Structured inference request with messages and config.

        Returns:
            ActionResult with completion response.

        Raises:
            FrameworkError: If plugin not available or request fails.
        """
        plugin = self._ensure_provider_ready()
        return plugin.generate_completion(request)

    def get_configured_model_name(self) -> str:
        """Get the configured model name for this inference provider.

        Returns:
            The model name configured in the plugin (e.g., 'meta-llama-3.1-8b-instruct')

        Raises:
            FrameworkError: If plugin not available or no model configured
        """
        plugin = self._ensure_provider_ready()
        return plugin.get_configured_model_name()

    def generate_compaction_summary(
        self, request: "CompactionRequest"
    ) -> str:
        """Generate compaction summary. Delegates to inference plugin.

        Args:
            request: The compaction request with messages and config.

        Returns:
            Generated summary text.

        Raises:
            FrameworkError: If plugin doesn't implement ContextManagementContract.
        """
        from ananta.interfaces.context_management_contract import ContextManagementContract

        plugin = self._ensure_provider_ready()

        if not isinstance(plugin, ContextManagementContract):
            raise FrameworkError(
                "Inference plugin does not implement ContextManagementContract"
            )

        return plugin.generate_compaction_summary(request)

    def warm_cache(self, request: "WarmingRequest") -> bool:
        """Warm KV cache. Delegates to inference plugin.

        Args:
            request: The warming request with messages and config.

        Returns:
            True if warming succeeded.

        Raises:
            FrameworkError: If plugin doesn't implement ContextManagementContract.
        """
        from ananta.interfaces.context_management_contract import ContextManagementContract

        plugin = self._ensure_provider_ready()

        if not isinstance(plugin, ContextManagementContract):
            raise FrameworkError(
                "Inference plugin does not implement ContextManagementContract"
            )

        return plugin.warm_cache(request)

    def get_context_management_config(self) -> "ContextManagementConfig":
        """Get context management config from inference plugin.

        Note: This method does NOT require the plugin to be fully ready.
        Config is loaded during plugin initialization and accessible before
        provider verification completes.

        Returns:
            ContextManagementConfig from the inference plugin.

        Raises:
            FrameworkError: If plugin not found or doesn't implement ContextManagementContract.
        """
        from ananta.interfaces.context_management_contract import ContextManagementContract

        # INF-03 (Reviewer-A B1): under a declared-vacant provider there is no
        # plugin to supply this config, but the consumers are provider-
        # independent (boot wires DiscoveryService + ContextService from it;
        # cold_context assembles briefings per organism turn). Return the ONE
        # defined vacant-state constant instead of raising — this method is
        # pre-readiness CONFIG, not inference execution; every real inference
        # operation still raises typed via _validate_inference_plugin.
        if self._inference_plugin_name is None:
            from ananta.services.context_management.config import (
                VACANT_PROVIDER_CONTEXT_CONFIG,
            )

            logger.debug(
                "get_context_management_config: provider VACANT — serving "
                "VACANT_PROVIDER_CONTEXT_CONFIG (INF-03)"
            )
            return VACANT_PROVIDER_CONTEXT_CONFIG

        # Use _validate_inference_plugin instead of _ensure_ready - config is
        # available during initialization, before readiness check passes
        plugin = self._validate_inference_plugin()

        if not isinstance(plugin, ContextManagementContract):
            raise FrameworkError(
                "Inference plugin does not implement ContextManagementContract"
            )

        return plugin.get_context_management_config()

    def _capture_bootstrap_state(self) -> dict[str, object]:
        """No bootstrap state to capture (bootstrap mode not supported)."""
        return {}

    def _restore_bootstrap_data(self, data: dict[str, object]) -> None:
        """No bootstrap data to restore (bootstrap mode not supported)."""
        pass
