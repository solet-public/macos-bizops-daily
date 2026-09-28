import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ananta.core.contexts.normalization import normalize_flow_id, normalize_session_id
from ananta.core.domain.error_codes import ErrorCode
from ananta.core.domain.types import ActionResult, ErrorDetail
from ananta.core.plans.advancement import (
    has_focused_plan as _has_focused_plan_check,
)
from ananta.core.plans.advancement import (
    maybe_advance_plan,
)
from ananta.core.plans.contracts.action_normalization import (
    inject_job_context,
    inject_observation_into_create_extended_plan,
)
from ananta.core.plans.types import (
    ParsedPlan,
    ParsedPlanStep,
)
from ananta.core.plugins.plugin_base import PluginBase
from ananta.core.plugins.plugin_contracts import ActionStatus
from ananta.core.prompts.context import ACTIVE_PLAN_MARKER, PromptContext
from ananta.core.prompts.decode.action_extraction import (
    has_explicit_actions_key,
    parse_llm_response_for_actions,
    validate_actions_found,
)
from ananta.core.prompts.decode.action_schema import (  # re-imported for callers
    _CANONICAL_ARG_SCHEMA,
    _FUNCTION_ARG_PROPERTIES,
    _MAX_PLUGIN_ARG_PROPERTIES_IN_OUTPUT_SCHEMA,
    _PREWARM_MIN_ITEMS,
    _action_schema,
    _narrow_arg_schema,
    _parse_process_keys,
    _step_narrowed_schema,
    extract_invocation_arg_properties,
)
from ananta.core.prompts.stages import (
    ContextStage,
    FormatStage,
)
from ananta.error_handling import AnantaError, PluginError
from ananta.interfaces import (
    InferenceRequest,
    InferenceServiceUnavailableError,
)
from ananta.interfaces.context_management_contract import ContextManagementContract
from ananta.interfaces.inference_errors import InferenceError
from ananta.interfaces.inference_service_interface import InferenceServiceInterface
from ananta.interfaces.state_aware_plugin import StateAwarePlugin
from ananta.logging_setup import configure_plugin_logging
from ananta.services.context_management.compaction_types import (
    CompactionRequest,
    WarmingRequest,
)
from ananta.services.context_management.config import ContextManagementConfig
from ananta.services.context_management.types import (
    ContextIdSource,
    ContextMode,
)
from ananta.services.inference_service.interfaces import (
    InferenceProvider,
    InferenceServiceAPI,
)
from ananta.services.inference_service.transaction import (
    build_placeholder_context,
    create_inference_error_response,
    create_success_response,
    extract_action_parameters,
    extract_job_id_from_context,
    log_request_to_state,
    log_response_to_state,
    normalize_action_definitions,
    require_user_prompt,
    validate_planning_extension_content,
)
from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    SchemaDefinition,
    TableSchema,
)

from .providers.lm_studio_provider import LMStudioProvider
from .validation import validate_action_parameters

# ── Plan Openings Catalog Constants ──
# Knowledge base containing the openings catalog (plan template guidance)
_GUIDANCE_KB_NAME = "prospection_and_goal_directed_planning"
_GUIDANCE_EXAMPLES_SUBDIR = "examples"
_LM_STUDIO_RETRY_INITIAL_SECONDS = 2.0
_LM_STUDIO_RETRY_MAX_SECONDS = 60.0
# Keep this aligned with the established Qwen completion budget.  The pre-warm
# prompt normally emits a very small schema-valid response, but a reasoning
# capable model can spend its output budget before producing that response.
_GRAMMAR_PREWARM_MAX_TOKENS = 2048
_GRAMMAR_PREWARM_PROMPT = (
    "Return the shortest valid response permitted by the response schema. "
    "Use empty strings, empty arrays, and empty objects wherever the schema permits them."
)


if TYPE_CHECKING:
    from ananta.core.plans.work_products import WorkProductRegister
    from ananta.core.services.prompt_context_builder import PromptContextBuilder
    from ananta.services.context_management.content_storage import (
        FileContextContentStorage,
    )
    from ananta.services.context_management.service import ContextManagementService
    from ananta.services.inference_service.interfaces.provider import InferenceDefaults
    from ananta.services.memory_service import MemoryService


class Plugin(
    PluginBase,
    StateAwarePlugin,
    InferenceServiceInterface,
    InferenceServiceAPI,
    InferenceProvider,
    ContextManagementContract,
):
    """Default Inference Plugin - LM Studio Only.

    Implements:
    - InferenceServiceInterface: Backwards compatibility (deprecated)
    - InferenceServiceAPI: Public, decorated methods
    - InferenceProvider: Internal, non-decorated lifecycle methods

    Single provider (LM Studio), no fallback, fail-fast on errors.
    """

    def __init__(self) -> None:
        super().__init__()
        self.name = "default_inference_plugin"
        self.logger: logging.Logger = logging.getLogger(self.name)
        self.provider: LMStudioProvider | None = None
        self._current_action_name = "unknown"
        self.action_factory = None  # Will be injected by ActionProcessor
        self.state_service = None  # Will be injected by framework via set_state_service()

        # Conversational context components (initialized in prepare_for_readiness)
        self._memory_service: MemoryService | None = None
        self._context_builder: PromptContextBuilder | None = None

        # Platform-managed context components (initialized in set_memory_service)
        self._context_management_service: ContextManagementService | None = None
        self._content_storage: FileContextContentStorage | None = None

        # Pipeline factory (initialized in set_memory_service)
        self._format_stage: FormatStage | None = None
        self._cached_system_prompt: str | None = None

        # Thinking service (resolved lazily for plan advancement)
        self._thinking_service: Any = None
        self._thinking_service_resolved: bool = False

        # Track flows that have stored INPUT events (bounded to prevent memory growth)
        # Used for deduplication: multiple process_results calls share same flow_input
        self._flows_with_input_stored: set[str] = set()

        # Synthesized delivery bindings (set during guidance/WBS binding,
        # consumed by bound argument enforcement, reset per inference request)
        self._synthesized_delivery_attachment: str | None = None
        self._synthesized_delivery_session_id: str | None = None

        # LM Studio is provisioned separately from the platform and can come
        # up after the platform. Keep its retry worker local to this plugin; it is a
        # daemon because this non-LifecycleManaged plugin has no stop hook.
        self._availability_retry_thread: threading.Thread | None = None
        self._availability_retry_lock = threading.Lock()
        self._prewarm_retry_thread: threading.Thread | None = None
        self._prewarm_retry_lock = threading.Lock()

    def get_readiness_error(self) -> str | None:
        return self.readiness_error

    def _require_ready_for_inference(self) -> None:
        """Reject every direct provider path while LM Studio is still unavailable."""
        if self.is_ready():
            return
        error_message = self.get_readiness_error() or "LM Studio is not ready"
        raise InferenceServiceUnavailableError(
            error_message,
            details={"plugin": self.name, "readiness_error": error_message},
        )

    def prepare_for_readiness(self) -> None:
        """Initialize plugin and LM Studio provider.

        Fail fast for invalid platform wiring and configuration. A configured
        but not-yet-running LM Studio is boot-optional: the plugin enters an
        explicit unready state and retries in the background.
        """
        # Get APP_HOME from orchestrator (injected before prepare_for_readiness)
        if not self.orchestrator_ref:
            raise RuntimeError(f"{self.name}: orchestrator_ref not injected - cannot initialize")

        self._app_home = getattr(self.orchestrator_ref, "APP_HOME", None)
        if not self._app_home:
            raise RuntimeError(
                f"{self.name}: Application directory not configured - plugin cannot initialize"
            )

        # Initialize config provider from config file - REQUIRED (no fallback)
        config_manager = getattr(self.orchestrator_ref, "config_manager", None)
        if config_manager is None:
            raise RuntimeError(
                f"{self.name}: config_manager not available - plugin requires configuration"
            )

        config = config_manager.get_plugin_config_provider(self.name)
        if not config:
            raise RuntimeError(
                f"{self.name}: Plugin configuration not found - "
                f"ensure config file exists at profile/config/plugins/{self.name}.json"
            )

        self.config_provider = config
        self.logger = configure_plugin_logging(self._app_home, self.name, self.config_provider)
        self.logger.debug(f"Initializing {self.name} plugin")

        # Initialize LM Studio provider from config - REQUIRED fields (no defaults)
        self._validate_required_provider_config()
        assert self.config_provider is not None  # Validated above

        lm_studio_url = str(self.config_provider.get("base_url"))
        lm_studio_model = str(self.config_provider.get("model"))
        timeout = self.config_provider.get_int("timeout_seconds")
        self._default_temperature = self.config_provider.get_float("temperature")
        self._default_max_tokens = self.config_provider.get_int("max_tokens")

        self.logger.debug(
            f"Config loaded: base_url={lm_studio_url}, model={lm_studio_model}, "
            f"timeout={timeout}, temperature={self._default_temperature}, "
            f"max_tokens={self._default_max_tokens}"
        )

        self.provider = LMStudioProvider(lm_studio_url, lm_studio_model, timeout)
        self.logger.debug(
            f"LM Studio provider initialized: {lm_studio_url}, model={lm_studio_model}"
        )

        # The inference server is deliberately not contacted before router
        # registration.  Cold guests load both models asynchronously and an
        # early probe/pre-warm competes with the embeddings model that setup
        # needs to qualify first.
        self.set_ready()

    def start_post_registration_work(self) -> None:
        """Begin optional inference validation only after router activation."""
        assert self.logger is not None
        result = self.validate_availability()
        if result.get("action_status") == ActionStatus.ERROR.value:
            assert self.config_provider is not None
            endpoint = str(self.config_provider.get("base_url"))
            error_message = f"LM Studio not available at {endpoint} after router registration"
            self.set_error(error_message)
            self.logger.warning("%s — waiting", error_message)
            self._start_availability_retry()
            return
        self.set_ready()
        self._start_prewarm_retry()

    def _start_availability_retry(self) -> None:
        """Start one daemon worker that waits for LM Studio without restart storms."""
        with self._availability_retry_lock:
            if (
                self._availability_retry_thread is not None
                and self._availability_retry_thread.is_alive()
            ):
                return
            retry_thread = threading.Thread(
                target=self._retry_availability_until_ready,
                name=f"{self.name}-availability-retry",
                daemon=True,
            )
            self._availability_retry_thread = retry_thread
            retry_thread.start()

    def _start_prewarm_retry(self) -> None:
        """Compile optional grammars off the startup path, retrying failures."""
        with self._prewarm_retry_lock:
            if self._prewarm_retry_thread is not None and self._prewarm_retry_thread.is_alive():
                return
            retry_thread = threading.Thread(
                target=self._retry_prewarm_until_complete,
                name=f"{self.name}-grammar-prewarm",
                daemon=True,
            )
            self._prewarm_retry_thread = retry_thread
            retry_thread.start()

    def _retry_prewarm_until_complete(self) -> None:
        """Retry grammar compilation without withdrawing startup readiness."""
        retry_seconds = _LM_STUDIO_RETRY_INITIAL_SECONDS
        while True:
            try:
                self._prewarm_canonical_grammars()
            except InferenceError as exc:
                self.logger.warning("LM Studio pre-warm failed: %s — retrying", exc)
                time.sleep(retry_seconds)
                retry_seconds = min(retry_seconds * 2, _LM_STUDIO_RETRY_MAX_SECONDS)
            else:
                return

    def _retry_availability_until_ready(self) -> None:
        """Retry availability indefinitely with a bounded request rate."""
        retry_seconds = _LM_STUDIO_RETRY_INITIAL_SECONDS
        while True:
            time.sleep(retry_seconds)
            result = self.validate_availability()
            if result.get("action_status") == ActionStatus.COMPLETED.value:
                try:
                    self._prewarm_canonical_grammars()
                except InferenceError as exc:
                    error_message = f"LM Studio pre-warm failed after becoming available: {exc}"
                    self.set_error(error_message)
                    self.logger.warning("%s — retrying", error_message)
                else:
                    self.set_ready()
                    self.logger.info(
                        "LM Studio is serving the configured model; inference plugin is ready"
                    )
                    return
            retry_seconds = min(retry_seconds * 2, _LM_STUDIO_RETRY_MAX_SECONDS)

    # Common step-narrowed schema shapes to pre-warm alongside canonical schemas.
    # Each entry is a list of process keys that produces a distinct grammar.
    _PREWARM_NARROWED_SHAPES: tuple[list[str], ...] = (
        ["service_interface::knowledge_service::search"],
        ["plugin::agent_messaging_plugin::post_message"],
        [
            "service_interface::knowledge_service::search",
            "service_interface::thinking_service::upsert_plan",
        ],
        [
            "service_interface::thinking_service::upsert_plan",
            "service_interface::memory_service::recall",
            "plugin::agent_messaging_plugin::post_message",
        ],
    )

    def _prewarm_canonical_grammars(self) -> None:
        """Send throwaway requests to compile schema grammars.

        LM Studio compiles JSON-schema grammars on first encounter.  Each
        unique schema shape produces a distinct grammar (~60-170 s each).
        Pre-warming the common variants at startup shifts that cost out of
        the user's first real interactions.

        Warms both canonical (opening turn) and narrowed (step-execution)
        schema shapes.
        """
        assert self.provider is not None
        assert self.logger is not None

        # Canonical schemas (opening turn)
        for min_items in _PREWARM_MIN_ITEMS:
            schema = _action_schema(min_items=min_items)
            self._prewarm_one_schema(f"canonical minItems={min_items}", schema)

        # Narrowed schemas (step execution)
        for keys in self._PREWARM_NARROWED_SHAPES:
            schema = _step_narrowed_schema(keys)
            label = " + ".join(k.split("::")[-1] for k in keys)
            self._prewarm_one_schema(f"narrowed [{label}]", schema)

    def _prewarm_one_schema(self, label: str, schema: dict[str, object]) -> None:
        """Send a single throwaway request to compile a grammar."""
        assert self.provider is not None
        assert self.logger is not None

        self.logger.info("PRE-WARM: compiling grammar for %s …", label)
        start = time.time()

        request = InferenceRequest(
            [{"role": "user", "content": _GRAMMAR_PREWARM_PROMPT}],
            temperature=0.0,
            max_tokens=_GRAMMAR_PREWARM_MAX_TOKENS,
            response_schema=schema,
            context_metadata={"purpose": "grammar_prewarm"},
        )

        try:
            self.provider.generate_completion(request)
        except InferenceError as exc:
            elapsed = time.time() - start
            self.logger.warning(
                "PRE-WARM: %s failed in %.1fs (%s)",
                label,
                elapsed,
                type(exc).__name__,
            )
            raise

        elapsed = time.time() - start
        self.logger.info("PRE-WARM: %s compiled in %.1fs", label, elapsed)

    def set_action_factory(self, action_factory: Any) -> None:
        """ActionFactory injection method for ActionFactory-centered architecture."""
        self.action_factory = action_factory
        if self.logger:
            self.logger.debug(f"ActionFactory injected into {self.name}")

    def set_state_service(self, state_service: Any) -> None:
        """State service injection — called by framework during plugin wiring."""
        self.state_service = state_service
        if self.logger:
            self.logger.info("State service injected into %s", self.name)

    def set_memory_service(self, memory_service: Any) -> None:
        """Memory service injection method - called after services are created.

        Initializes conversational context components:
        - PromptContextBuilder for memory context assembly
        - ContextManagementService for platform-managed context
        - FileContextContentStorage for context event content
        - PromptPipeline for observable prompt assembly

        Note: Context window management is handled via ContextManagementService.
        Cache warming is handled via warm_cache(WarmingRequest) interface method.

        Args:
            memory_service: The memory service instance from orchestrator.

        Raises:
            RuntimeError: If memory_service is None (fail-fast).
        """
        from ananta.core.services.prompt_context_builder import PromptContextBuilder

        if not memory_service:
            raise RuntimeError(f"{self.name}: memory_service injection failed - service is None")

        self._memory_service = memory_service

        # Initialize PromptContextBuilder with memory service
        # Note: memory_service is validated non-None above at line 202
        self._context_builder = PromptContextBuilder(memory_service)
        self.logger.debug("PromptContextBuilder initialized")

        # Initialize platform-managed context components
        if self.orchestrator_ref:
            ctx_svc = self.orchestrator_ref.get_service("context_management_service")
            # Type narrowing: get_service returns object, but we know the type
            if ctx_svc is not None:
                from ananta.services.context_management.service import ContextManagementService

                if isinstance(ctx_svc, ContextManagementService):
                    self._context_management_service = ctx_svc
            if self._context_management_service:
                self.logger.debug("ContextManagementService retrieved from orchestrator")
                # Use shared content storage from ContextManagementService
                # This ensures all context events (INPUT/OUTPUT) use the same storage location
                self._content_storage = self._context_management_service.content_storage
                self.logger.debug("Using shared content_storage from ContextManagementService")
            else:
                self.logger.debug(
                    "ContextManagementService not available - platform context disabled"
                )

        # Initialize PromptPipeline for observable prompt assembly
        # Uses pass-through mode for TemplateStage since templates are resolved upstream
        if not self._app_home:
            raise RuntimeError(f"{self.name}: APP_HOME not set - cannot initialize pipeline")

        prompts_dir = Path(self._app_home) / "config" / "prompts"
        if not prompts_dir.exists():
            raise RuntimeError(
                f"{self.name}: prompts_dir not found at {prompts_dir} - cannot initialize pipeline"
            )

        # Resolve step guidance articles directory (direct file read for plan step guidance)
        self._guidance_articles_dir = (
            Path(self._app_home).parent
            / "knowledge_bases"
            / _GUIDANCE_KB_NAME
            / _GUIDANCE_EXAMPLES_SUBDIR
        )

        # Get context config for ContextStage (uses config-driven attachment_scan_limit)
        context_config = self.get_context_management_config()

        # Build playbook section reader from thinking service (if available)
        playbook_section_reader = self._build_playbook_section_reader()

        # Keep reference to FormatStage for system prompt access
        self._format_stage = FormatStage(prompts_dir)

        # Pipeline factory for profile-based assembly.
        # The factory holds the dependency references and produces pipelines
        # for any profile (INFERENCE, THINKING, TEXT_COMPLETION).
        from ananta.core.prompts.pipeline_factory import (
            PipelineDependencies,
            PromptPipelineFactory,
        )
        from ananta.core.prompts.vertex_enrichment import (
            enrich_vertex as _platform_enrich_vertex,
        )

        self._pipeline_deps = PipelineDependencies(
            format_stage=self._format_stage,
            context_stage=ContextStage(
                self._context_builder,
                context_management_service=self._context_management_service,
                content_storage=self._content_storage,
                context_config=context_config,
                memory_service=memory_service,
                playbook_section_reader=playbook_section_reader,
            ),
            catalog_data_source=self._build_catalog_data_source(),
            guidance_article_reader=self._build_guidance_article_reader(),
            guidance_process_lookup=self._build_guidance_process_lookup(),
            vertex_enricher=_platform_enrich_vertex,
            process_arg_lookup=self._build_process_arg_lookup(),
            plan_advancer=self._build_plan_advancer(),
        )
        self._pipeline_factory = PromptPipelineFactory(self._pipeline_deps)
        self.logger.debug("PipelineDependencies and factory initialized")

        # Cache base system prompt for generate_text_completion (KV cache sharing)
        # Built-in Processes injected at request time via _get_system_prompt_with_builtins()
        self._cached_system_prompt = self._format_stage._load_global_system()

        self.logger.debug("Conversational context components initialized via injection")

    def _build_playbook_section_reader(self) -> Callable[[str, str], str] | None:
        """Build a callable for reading playbook sections from the thinking service.

        Looks up the thinking service via the orchestrator and returns a closure
        that calls get_playbook_section(playbook_id, section_id) and extracts
        the content string. Returns None if the thinking service is unavailable.
        """
        if not self.orchestrator_ref:
            return None

        from ananta.core.orchestration.service_bindings import ServiceName

        thinking_svc = self.orchestrator_ref.get_service(
            ServiceName.PLAN_LIFECYCLE_SERVICE,
        )
        if not thinking_svc:
            self.logger.debug("Plan lifecycle service not available - playbook hydration disabled")
            return None

        get_section = getattr(thinking_svc, "get_playbook_section", None)
        if not callable(get_section):
            self.logger.debug(
                "Thinking service lacks get_playbook_section - playbook hydration disabled",
            )
            return None

        def reader(playbook_id: str, section_id: str) -> str:
            result = get_section(playbook_id, section_id)
            if not isinstance(result, dict):
                msg = f"get_playbook_section returned non-dict: {type(result)}"
                raise ValueError(msg)
            content = result.get("content", "")
            if not isinstance(content, str) or not content:
                msg = (
                    f"Playbook section empty or missing: "
                    f"playbook_id={playbook_id}, section_id={section_id}"
                )
                raise ValueError(msg)
            return content

        self.logger.debug("Playbook section reader built from thinking service")
        return reader

    # ─────────────────────────────────────────────────────────────────────────
    # PLAN ADVANCEMENT (platform-owned marker bookkeeping)
    # ─────────────────────────────────────────────────────────────────────────

    def _resolve_thinking_service(self) -> Any:
        """Lazily resolve the plan lifecycle service for plan advancement."""
        if self._thinking_service_resolved:
            return self._thinking_service
        self._thinking_service_resolved = True
        if not self.orchestrator_ref:
            return None
        from ananta.core.orchestration.service_bindings import ServiceName

        svc = self.orchestrator_ref.get_service(
            ServiceName.PLAN_LIFECYCLE_SERVICE,
        )
        if svc and hasattr(svc, "advance_current_plan_step"):
            self._thinking_service = svc
        return self._thinking_service

    def _has_focused_plan(self, *, session_id: str) -> bool:
        """Delegate to platform advancement module (session-scoped, JOS-02)."""
        return _has_focused_plan_check(
            self._memory_service, session_id=session_id,
        )

    def _maybe_advance_plan(
        self, *, is_continuation: bool = False, session_id: str,
    ) -> None:
        """Delegate to platform advancement module (session-scoped, JOS-02)."""
        maybe_advance_plan(
            action_name=self._current_action_name,
            is_continuation=is_continuation,
            memory_provider=self._memory_service,
            thinking_service=self._resolve_thinking_service(),
            session_id=session_id,
        )

    def _get_system_prompt_with_builtins(self) -> str | None:
        """Get the baseline system prompt with stale injected sections stripped.

        Returns the cached system prompt with any previously-persisted process
        catalog or plan execution blocks removed. The fresh catalog is injected
        at request time by ``CatalogStage``.

        Returns:
            Clean baseline system prompt, or None if no cached prompt.
        """
        if not self._cached_system_prompt:
            return None

        content = self._cached_system_prompt
        content = self._strip_section(content, self._BUILTIN_PROCESSES_HEADER)
        content = self._strip_section(content, self._CORE_PROCESSES_HEADER)
        content = self._strip_section(content, self._IO_PROCESSES_HEADER)
        content = self._strip_section(content, self._PLAN_EXECUTION_HEADER)
        content = self._strip_section(content, "## Plugin Process Availability")
        return content

    # Sentinel headers for dedupe detection / section stripping
    _BUILTIN_PROCESSES_HEADER = "## Built-in Processes"
    _EXECUTION_PLANS_HEADER = "## Execution Plans"
    _CORE_PROCESSES_HEADER = "## Core Processes"
    _IO_PROCESSES_HEADER = "## IO Processes"
    _PLAN_EXECUTION_HEADER = "## Plan Execution"

    @staticmethod
    def _strip_section(content: str, header: str) -> str:
        """Remove a ``## Header`` section from content if present.

        Sections are delimited by the next ``\\n## `` marker or end-of-string.
        Trailing whitespace from the removal is cleaned up.
        """
        if header not in content:
            return content
        start = content.index(header)
        end = content.find("\n## ", start + len(header))
        if end == -1:
            end = len(content)
        result = content[:start].rstrip() + content[end:]
        return result.strip()

    @staticmethod
    def _replace_or_append_section(content: str, header: str, new_block: str) -> str:
        """Replace an existing ``## Header`` section or append a new one.

        Sections are delimited by the next ``\\n## `` marker or end-of-string.
        """
        if header in content:
            start = content.index(header)
            end = content.find("\n## ", start + len(header))
            if end == -1:
                end = len(content)
            return content[:start] + new_block + content[end:]
        return f"{content}\n\n{new_block}"

    @staticmethod
    def _get_focused_plan_text(ctx: PromptContext) -> str | None:
        """Extract plan text from focused memories."""
        for mem in ctx.focused_memories:
            content = mem.get("content", "")
            if isinstance(content, str) and ACTIVE_PLAN_MARKER in content:
                return content
        return None

    # ── Kept live methods (used by pipeline adapters and decode contract) ──

    def _resolve_wbs_step_number(self, step: ParsedPlanStep) -> int | None:
        """Delegate to platform wbs_bindings module."""
        from ananta.core.prompts.plan_drivers.wbs_bindings import resolve_wbs_step_number

        return resolve_wbs_step_number(step)

    @staticmethod
    def _get_focused_wbs_text(ctx: "PromptContext") -> str:
        """Extract focused WBS document text from context.

        Looks for a focused memory item whose content starts with
        ``# Work Breakdown Structure``.
        """
        if not ctx.focused_memories:
            return ""
        for mem in ctx.focused_memories:
            content = mem.get("content", "")
            if isinstance(content, str) and content.strip().startswith(
                "# Work Breakdown Structure"
            ):
                return content
        return ""

    @staticmethod
    def _get_focused_resolved_intake_text(ctx: "PromptContext") -> str:
        """Extract focused Resolved Intake State document from context.

        Looks for a focused memory item whose content starts with
        ``# Resolved Intake State``.
        """
        if not ctx.focused_memories:
            return ""
        for mem in ctx.focused_memories:
            content = mem.get("content", "")
            if isinstance(content, str) and content.strip().startswith("# Resolved Intake State"):
                return content
        return ""

    def _resolve_guidance_target_step(
        self,
        parsed: ParsedPlan,
    ) -> ParsedPlanStep | None:
        """Choose the step whose guidance should drive this focused turn.

        Normal focused turns use the active ``[>]`` step (falling back to the
        first executable step). When the active step is a real wait boundary
        with no process keys, use the next concrete pending step instead.
        """
        current = parsed.current_step
        if current is None:
            first_num = parsed.first_executable_step_number
            if first_num is not None:
                current = parsed.step_by_number(first_num)
        if current is None:
            return None
        if current.process_keys:
            return current
        for step in parsed.steps:
            if step.number <= current.number:
                continue
            if step.is_completed or step.is_skipped:
                continue
            if step.process_keys:
                return step
            return None
        return current

    @staticmethod
    def _navigate_to_args_schema(
        process_data: dict[str, object],
    ) -> dict[str, Any] | None:
        """Navigate invocation_schema envelope to the inner arguments schema."""
        schema = process_data.get("invocation_schema")
        if not isinstance(schema, dict):
            return None
        outer_props = schema.get("properties")
        if not isinstance(outer_props, dict):
            return None
        args_schema = outer_props.get("arguments")
        return args_schema if isinstance(args_schema, dict) else None

    def _build_process_arg_schema(
        self,
        process_key: str,
    ) -> dict[str, object] | None:
        """Build the arguments schema for one concrete process key."""
        function_name = process_key.rsplit("::", 1)[-1] if "::" in process_key else process_key

        if function_name in _FUNCTION_ARG_PROPERTIES:
            return _narrow_arg_schema([function_name])

        schema = self._narrow_arg_schema_from_registry([process_key])
        if schema == _CANONICAL_ARG_SCHEMA:
            return None
        return schema

    def _narrow_arg_schema_from_registry(
        self,
        model_keys: list[str],
    ) -> dict[str, object]:
        """Build a narrowed arg schema using dynamic registry lookups.

        Falls back to the static maps for known functions and uses
        the process registry for unknown ones, so new service_interface
        processes work without hardcoded entries.
        """
        _, _, function_names = _parse_process_keys(model_keys)
        discovery_svc = self._get_discovery_service()

        def _registry_props(fn: str) -> dict[str, dict[str, object]]:
            for key in model_keys:
                if key.endswith(f"::{fn}"):
                    process_data = discovery_svc.get_process_by_key(key)
                    if not isinstance(process_data, dict):
                        return {}
                    return extract_invocation_arg_properties(
                        process_data,
                        max_properties=_MAX_PLUGIN_ARG_PROPERTIES_IN_OUTPUT_SCHEMA,
                    )
            return {}

        def _registry_required(fn: str) -> set[str]:
            for key in model_keys:
                if key.endswith(f"::{fn}"):
                    process_data = discovery_svc.get_process_by_key(key)
                    if not isinstance(process_data, dict):
                        return set()
                    args_schema = Plugin._navigate_to_args_schema(process_data)
                    if args_schema is None:
                        return set()
                    properties = args_schema.get("properties")
                    if not isinstance(properties, dict):
                        return set()
                    return set(properties.keys())
            return set()

        return _narrow_arg_schema(
            function_names,
            registry_lookup=_registry_props,
            registry_required_lookup=_registry_required,
        )

    def _validate_step_contract(
        self,
        ctx: PromptContext,
        actions: list[dict[str, Any]],
    ) -> None:
        """Validate emitted actions against model-visible keys.

        Delegates contract validation to the platform module, then
        enforces bound arguments and injects work product values.
        """
        visible_keys = ctx.model_visible_process_keys or ctx.current_step_process_keys
        if not visible_keys:
            return

        from ananta.core.plans.contracts.action_contract import (
            validate_step_contract,
        )

        validate_step_contract(actions, visible_keys)

        # After validation + reorder, enforce WBS bound argument values
        if ctx.plan_state is not None:
            from ananta.core.plans.work_product_runtime import (
                enforce_bound_argument_values,
            )

            register = (
                self._load_work_product_register(ctx)
                if self._has_composed_references(ctx)
                else None
            )
            enforce_bound_argument_values(ctx.plan_state, actions, register)

        # Inject platform-computed deterministic filenames for owned slots
        if ctx.plan_state is not None and self._state_service is not None:
            from ananta.core.plans.work_product_runtime import (
                inject_work_product_values,
            )

            inject_work_product_values(
                ctx.plan_state, actions, self._state_service,
                self._build_process_arg_lookup(),
            )

    def _has_composed_references(self, ctx: PromptContext) -> bool:
        """Check whether the current step has any Composed: references."""
        if ctx.plan_state is None:
            return False
        from ananta.core.plans.work_product_runtime import resolve_current_bound_sub_steps

        bound_sub_steps, _ = resolve_current_bound_sub_steps(ctx.plan_state)
        return any(bs.composed_references for bs in bound_sub_steps)

    def _load_work_product_register(
        self,
        ctx: PromptContext,
    ) -> "WorkProductRegister":
        """Load the current WBS work-product register."""
        wbs_id = self._resolve_active_wbs_id(ctx)
        if not wbs_id:
            raise RuntimeError("WORK_PRODUCTS: no active WBS ID in focused plan")
        from ananta.core.plans.work_product_store import WorkProductStoreAdapter
        from ananta.core.plans.work_products import WorkProductRegister

        if not self.state_service:
            raise RuntimeError("WORK_PRODUCTS: No state service — cannot load register")
        store = WorkProductStoreAdapter(self.state_service)
        register_data = store.load_register(wbs_id)
        return (
            WorkProductRegister.deserialize(register_data)
            if register_data
            else WorkProductRegister()
        )

    def _resolve_active_wbs_id(self, ctx: PromptContext) -> str | None:
        """Resolve the active WBS ID from the focused plan."""
        plan_text = self._get_focused_plan_text(ctx)
        if not plan_text:
            return None
        from ananta.core.plans.windowing import ACTIVE_WBS_HEADER_RE

        wbs_match = ACTIVE_WBS_HEADER_RE.search(plan_text)
        return wbs_match.group(1) if wbs_match else None

    @staticmethod
    def _is_discovery_no_matches(ctx: PromptContext) -> bool:
        """Check if the discovery observation returned no matching processes.

        Checks both top-level ``action_result.process_count`` and the
        ``action_result.data.process_count`` wrapper, mirroring the dual-shape
        handling in ``_extract_discovery_processes``.
        """
        if not ctx.raw_observation_dict:
            return False
        action_result = ctx.raw_observation_dict.get("action_result")
        if not isinstance(action_result, dict):
            return False
        # Top-level (DiscoveryResult.to_dict format)
        if action_result.get("process_count") == 0:
            return True
        # Nested under data wrapper (template-wrapped format)
        data = action_result.get("data")
        return isinstance(data, dict) and data.get("process_count") == 0

    @staticmethod
    def _extract_observation_process_key(ctx: PromptContext) -> str | None:
        """Extract the completed process key from the raw observation dict."""
        if not ctx.raw_observation_dict:
            return None
        key = ctx.raw_observation_dict.get("process_key")
        return key if isinstance(key, str) else None

    @staticmethod
    def _extract_discovery_processes(ctx: PromptContext) -> list[object]:
        """Extract the processes list from a discovery observation's action_result.

        DiscoveryResult.to_dict() places ``processes`` at the top level of the
        result dict.  The template may also wrap results under a ``data`` key,
        so both locations are checked.
        """
        if not ctx.raw_observation_dict:
            return []
        action_result = ctx.raw_observation_dict.get("action_result")
        if not isinstance(action_result, dict):
            return []
        # Top-level (DiscoveryResult.to_dict format)
        raw = action_result.get("processes")
        if not isinstance(raw, list):
            # Nested under data wrapper (template-wrapped format)
            data = action_result.get("data")
            if isinstance(data, dict):
                raw = data.get("processes")
        return raw if isinstance(raw, list) else []

    @staticmethod
    def _parse_process_key(process_key: str) -> tuple[str, str, str]:
        """Parse a process key into (provider_type, provider, function_name).

        Process keys follow the format ``provider_type::provider::function_name``.
        Returns ("", "", "") if the key cannot be parsed.
        """
        parts = process_key.split("::")
        if len(parts) >= 3:
            return (parts[0], parts[1], parts[2])
        return ("", "", "")

    @staticmethod
    def _parse_plugin_process_key(pkey: str) -> tuple[str, str] | None:
        """Parse a plugin:: process key into (provider, function_name) or None."""
        if not pkey.startswith("plugin::"):
            return None
        parts = pkey.split("::")
        if len(parts) < 3:
            return None
        return (parts[1], parts[2])

    def _build_process_arg_lookup(self) -> Any:
        """Build a ProcessArgLookup adapter for the pipeline factory.

        The adapter wraps discovery service lookups behind the
        ``ProcessArgLookup`` protocol so the decode contract stage
        can resolve argument properties without a plugin reference.
        """
        discovery_svc = self._get_discovery_service()

        class _Adapter:
            def get_arg_properties(
                self,
                process_key: str,
            ) -> dict[str, dict[str, object]]:
                process_data = discovery_svc.get_process_by_key(process_key)
                if not isinstance(process_data, dict):
                    return {}
                # Return the full property dict — output_schema build
                # sites apply their own merged-total cap, while runtime
                # callers (bound-arg type checks, validation) need every
                # key to avoid the silent-coerce hole on truncated args.
                return extract_invocation_arg_properties(process_data)

            def get_required_properties(
                self,
                process_key: str,
            ) -> set[str]:
                process_data = discovery_svc.get_process_by_key(process_key)
                if not isinstance(process_data, dict):
                    return set()
                args_schema = Plugin._navigate_to_args_schema(process_data)
                if args_schema is None:
                    return set()
                required_raw = args_schema.get("required")
                if not isinstance(required_raw, list):
                    return set()
                return {str(r) for r in required_raw}

            def get_declared_properties(
                self,
                process_key: str,
            ) -> set[str]:
                process_data = discovery_svc.get_process_by_key(process_key)
                if not isinstance(process_data, dict):
                    return set()
                args_schema = Plugin._navigate_to_args_schema(process_data)
                if args_schema is None:
                    return set()
                properties = args_schema.get("properties")
                if not isinstance(properties, dict):
                    return set()
                return set(properties.keys())

        return _Adapter()

    def _build_plan_advancer(self) -> Any:
        """Build a PlanAdvancer adapter for the pipeline factory.

        The adapter wraps the thinking service's
        ``advance_current_plan_step`` behind the ``PlanAdvancer``
        protocol.  Raises if the thinking service is unavailable —
        missing infrastructure must not silently disable advancement.
        """
        plugin = self

        class _Adapter:
            def advance_current_plan_step(
                self, *, session_id: str,
            ) -> dict[str, Any] | None:
                thinking_svc = plugin._resolve_thinking_service()
                if thinking_svc is None:
                    raise RuntimeError(
                        "PlanAdvancer: thinking service unavailable — cannot advance plan step"
                    )
                result: dict[str, Any] | None = thinking_svc.advance_current_plan_step(
                    session_id=session_id,
                )
                return result

        return _Adapter()

    # ── Guidance adapters (Slice 2) ──

    def _build_guidance_article_reader(self) -> Any:
        """Build a GuidanceArticleReader adapter for the pipeline factory."""
        articles_dir = self._guidance_articles_dir

        class _Adapter:
            def read_article(self, article_name: str) -> str | None:
                path = articles_dir / article_name
                if not path.is_file():
                    return None
                return path.read_text(encoding="utf-8").rstrip("\n")

        return _Adapter()

    def _build_guidance_process_lookup(self) -> Any:
        """Build a ProcessDataLookup adapter using platform implementation."""
        from ananta.core.prompts.plan_drivers.guidance_drivers import (
            DiscoveryProcessDataLookup,
        )

        return DiscoveryProcessDataLookup(
            discovery_service=self._get_discovery_service(),
            state_service=self._state_service,
        )

    # ── Catalog adapter (Slice 8) ──

    def _build_catalog_data_source(self) -> Any:
        """Build a CatalogDataSource adapter for the pipeline factory."""
        plugin = self

        class _Adapter:
            def get_system_prompt_processes(self) -> list[dict[str, object]]:
                svc = plugin._get_discovery_service()
                result: list[dict[str, object]] = svc.get_system_prompt_processes()
                return result

            def get_all_io_processes(self) -> list[dict[str, object]]:
                svc = plugin._get_discovery_service()
                all_procs: dict[str, dict[str, object]] = svc.get_all_processes()
                io: list[dict[str, object]] = []
                for key, data in all_procs.items():
                    parts = key.split("::")
                    if len(parts) >= 3 and parts[0] == "plugin" and parts[2] == "post_message":
                        io.append(
                            {
                                "process_key": key,
                                "description": data.get("description", ""),
                                "invocation_schema": data.get("invocation_schema", {}),
                            }
                        )
                io.sort(key=lambda p: str(p.get("process_key", "")))
                return io

            def get_process_by_key(self, process_key: str) -> dict[str, object] | None:
                svc = plugin._get_discovery_service()
                result = svc.get_process_by_key(process_key)
                return result if isinstance(result, dict) else None

        return _Adapter()

    def _get_discovery_service(self) -> Any:
        """Get the discovery service from the orchestrator. Fail-fast."""
        if not self.orchestrator_ref:
            raise RuntimeError("Cannot get discovery service: orchestrator not available")
        svc = self.orchestrator_ref.get_service("discovery_service")
        if svc is None:
            raise RuntimeError("Cannot get discovery service: not available")
        return svc

    def _get_io_namespace(self, state: dict[str, Any] | None) -> str | None:
        """Get the IO plugin namespace from state.

        Checks for a pre-resolved ``io_process_key`` in state first (set by
        pipeline stage adapters), falling back to the flow-based resolution.
        """
        if not state:
            return None
        # Pre-resolved key from pipeline stage adapters (avoids flow lookup)
        io_key = state.get("io_process_key")
        if not io_key:
            io_key = self._resolve_active_io_process_key(state)
        if not io_key or "::" not in io_key:
            return None
        # plugin::agent_messaging_plugin::post_message → agent_messaging_plugin
        parts = io_key.split("::")
        return parts[1] if len(parts) >= 3 else None

    def get_schema_definitions(self) -> list[SchemaDefinition]:
        sessions_table = TableSchema(
            table_name="inference_sessions",
            columns={
                "session_id": ColumnDefinition(type=ColumnType.TEXT, unique=True),
                "model_name": ColumnDefinition(type=ColumnType.TEXT),
                "provider_name": ColumnDefinition(type=ColumnType.TEXT),
                "configuration": ColumnDefinition(type=ColumnType.TEXT),
                "status": ColumnDefinition(type=ColumnType.TEXT),
            },
            id_prefix="iss",
        )

        usage_metrics_table = TableSchema(
            table_name="usage_metrics",
            columns={
                "metric_id": ColumnDefinition(type=ColumnType.TEXT, unique=True),
                "session_id": ColumnDefinition(type=ColumnType.TEXT),
                "provider_name": ColumnDefinition(type=ColumnType.TEXT),
                "model_name": ColumnDefinition(type=ColumnType.TEXT),
                "tokens_used": ColumnDefinition(type=ColumnType.INTEGER),
                "request_count": ColumnDefinition(type=ColumnType.INTEGER),
                "timestamp": ColumnDefinition(type=ColumnType.DATETIME),
            },
            id_prefix="usm",
        )

        return [
            SchemaDefinition(
                namespace="default_inference_plugin",
                tables={
                    "inference_sessions": sessions_table,
                    "usage_metrics": usage_metrics_table,
                },
            )
        ]

    def get_context_management_config(self) -> ContextManagementConfig:
        """Get context management configuration for this plugin.

        Reads configuration from config provider. ALL keys are required.
        Plugin MUST fail to load if any field missing.

        Raises:
            PluginError: If config provider is not initialized or required keys are missing
        """
        config = self._get_validated_config_provider()
        self._validate_required_context_config_keys(config)

        # Parse enums
        context_mode = self._parse_context_mode(config)
        context_id_source = self._parse_context_id_source(config)
        context_id_address_key = self._validate_address_key(config, context_id_source)

        # Validate numeric constraints - all count fields must be positive
        positive_int_keys = [
            "context.chars_per_token",
            "context.warm_max_tokens",
            "context.max_char_count",
            "context.soft_max_char_count",
            "context.target_char_count",
            "context.precache_char_count",
            "context.min_summary_tokens",
            "context.discovery_intent_max_tokens",
            "context.attachment_scan_limit",
            "context.model_context_tokens",
        ]
        for key in positive_int_keys:
            self._validate_positive_integer_config(config, key)

        # Validate threshold hierarchy
        self._validate_threshold_hierarchy(config)

        # Validate similarity threshold range
        self._validate_similarity_threshold(config)

        return ContextManagementConfig(
            context_mode=context_mode,
            context_id_source=context_id_source,
            context_id_address_key=context_id_address_key,
            supports_compaction=config.get_bool("context.supports_compaction"),
            supports_clear=config.get_bool("context.supports_clear"),
            auto_compact=config.get_bool("context.auto_compact"),
            warming_enabled=config.get_bool("context.warming_enabled"),
            max_char_count=config.get_int("context.max_char_count"),
            soft_max_char_count=config.get_int("context.soft_max_char_count"),
            target_char_count=config.get_int("context.target_char_count"),
            precache_char_count=config.get_int("context.precache_char_count"),
            warm_max_tokens=config.get_int("context.warm_max_tokens"),
            warm_temperature=config.get_float("context.warm_temperature"),
            summary_temperature=config.get_float("context.summary_temperature"),
            chars_per_token=config.get_int("context.chars_per_token"),
            min_summary_tokens=config.get_int("context.min_summary_tokens"),
            discovery_intent_max_tokens=config.get_int("context.discovery_intent_max_tokens"),
            discovery_intent_temperature=config.get_float("context.discovery_intent_temperature"),
            discovery_min_similarity_threshold=config.get_float(
                "context.discovery_min_similarity_threshold"
            ),
            attachment_scan_limit=config.get_int("context.attachment_scan_limit"),
            model_context_tokens=config.get_int("context.model_context_tokens"),
        )

    def _get_validated_config_provider(self) -> Any:
        """Get and validate config provider exists."""
        config = getattr(self, "config_provider", None)
        if config is None:
            raise PluginError(
                message="Config provider not initialized",
                error_code=ErrorCode.PLUGIN_CONFIG_ERROR,
                plugin_name=self.name,
            )
        return config

    def _validate_required_provider_config(self) -> None:
        """Validate all required provider config keys are present (no defaults)."""
        if not self.config_provider:
            raise RuntimeError(f"{self.name}: Config provider not initialized")

        required_keys = [
            "base_url",
            "model",
            "timeout_seconds",
            "temperature",
            "max_tokens",
        ]
        missing_keys = [key for key in required_keys if self.config_provider.get(key) is None]
        if missing_keys:
            raise RuntimeError(
                f"{self.name}: Missing required config keys: {', '.join(missing_keys)}. "
                f"These must be set in profile/config/plugins/{self.name}.json"
            )

    def _validate_required_context_config_keys(self, config: Any) -> None:
        """Validate all required context config keys are present."""
        required_keys = [
            "context.mode",
            "context.id_source",
            "context.supports_compaction",
            "context.supports_clear",
            "context.auto_compact",
            "context.warming_enabled",
            "context.max_char_count",
            "context.soft_max_char_count",
            "context.target_char_count",
            "context.precache_char_count",
            "context.warm_max_tokens",
            "context.warm_temperature",
            "context.summary_temperature",
            "context.chars_per_token",
            "context.min_summary_tokens",
            "context.discovery_intent_max_tokens",
            "context.discovery_intent_temperature",
            "context.discovery_min_similarity_threshold",
            "context.attachment_scan_limit",
            "context.model_context_tokens",
        ]
        missing_keys = [key for key in required_keys if config.get(key) is None]
        if missing_keys:
            raise PluginError(
                message=f"Missing required context config keys: {', '.join(missing_keys)}",
                error_code=ErrorCode.PLUGIN_CONFIG_ERROR,
                plugin_name=self.name,
            )

    def _parse_context_mode(self, config: Any) -> ContextMode:
        """Parse and validate context mode enum."""
        mode_str = str(config.get("context.mode"))
        try:
            return ContextMode(mode_str)
        except ValueError as e:
            valid_modes = ", ".join(f"'{m.value}'" for m in ContextMode)
            raise PluginError(
                message=f"Invalid context.mode '{mode_str}': must be {valid_modes}",
                error_code=ErrorCode.PLUGIN_CONFIG_ERROR,
                plugin_name=self.name,
            ) from e

    def _parse_context_id_source(self, config: Any) -> ContextIdSource:
        """Parse and validate context ID source enum."""
        source_str = str(config.get("context.id_source"))
        try:
            return ContextIdSource(source_str)
        except ValueError as e:
            valid_sources = ", ".join(f"'{s.value}'" for s in ContextIdSource)
            raise PluginError(
                message=f"Invalid context.id_source '{source_str}': must be {valid_sources}",
                error_code=ErrorCode.PLUGIN_CONFIG_ERROR,
                plugin_name=self.name,
            ) from e

    def _validate_address_key(self, config: Any, source: ContextIdSource) -> str | None:
        """Validate address_key is present when required."""
        address_key_value = config.get("context.id_address_key")
        context_id_address_key = str(address_key_value) if address_key_value else None
        if source == ContextIdSource.ADDRESS_BOOK and not context_id_address_key:
            raise PluginError(
                message="context.id_address_key required when context.id_source is 'address_book'",
                error_code=ErrorCode.PLUGIN_CONFIG_ERROR,
                plugin_name=self.name,
            )
        return context_id_address_key

    def _validate_positive_integer_config(self, config: Any, key: str) -> None:
        """Validate a config key is a positive integer."""
        value = config.get_int(key)
        if value <= 0:
            raise PluginError(
                message=f"{key} must be > 0, got {value}",
                error_code=ErrorCode.PLUGIN_CONFIG_ERROR,
                plugin_name=self.name,
            )

    def _validate_threshold_hierarchy(self, config: Any) -> None:
        """Validate threshold hierarchy constraints (char-based only)."""
        max_char = config.get_int("context.max_char_count")
        soft_max_char = config.get_int("context.soft_max_char_count")
        target_char = config.get_int("context.target_char_count")

        if soft_max_char >= max_char:
            raise PluginError(
                message=f"soft_max_char_count ({soft_max_char}) must be < "
                f"max_char_count ({max_char})",
                error_code=ErrorCode.PLUGIN_CONFIG_ERROR,
                plugin_name=self.name,
            )
        if target_char >= soft_max_char:
            raise PluginError(
                message=f"target_char_count ({target_char}) must be < "
                f"soft_max_char_count ({soft_max_char})",
                error_code=ErrorCode.PLUGIN_CONFIG_ERROR,
                plugin_name=self.name,
            )

    def _validate_similarity_threshold(self, config: Any) -> None:
        """Validate similarity threshold is in valid range (0, 1].

        Similarity threshold must be > 0 (otherwise discovery always returns no_matches)
        and <= 1 (maximum possible cosine similarity).
        """
        threshold = config.get_float("context.discovery_min_similarity_threshold")
        if threshold <= 0:
            raise PluginError(
                message=f"discovery_min_similarity_threshold ({threshold}) must be > 0",
                error_code=ErrorCode.PLUGIN_CONFIG_ERROR,
                plugin_name=self.name,
            )
        if threshold > 1:
            raise PluginError(
                message=f"discovery_min_similarity_threshold ({threshold}) must be <= 1",
                error_code=ErrorCode.PLUGIN_CONFIG_ERROR,
                plugin_name=self.name,
            )

    def get_parameter_validators(
        self,
    ) -> dict[str, Callable[[dict[str, Any]], tuple[bool, str | None]]]:
        return {
            "query_model": self._validate_query_parameters,
        }

    def get_custom_validators(self) -> list[Any]:
        from ananta.core.plugins.plugin_validation import (
            PluginValidationHook,
            ValidationPhase,
        )

        return [
            PluginValidationHook(
                plugin_name=self.name,
                action_name="query_model",
                validator_function=self._validate_resolved_data,
                validation_phase=ValidationPhase.POST_PARAMETER,
                priority=90,
                description="Validate that all template variables have been resolved",
            ),
        ]

    def _validate_model_parameter(self, arguments: dict[str, Any]) -> tuple[bool, str | None]:
        """Validate model parameter structure and provider availability.

        Enforces strict model name validation - model.name must match the configured model.
        NO backward compatibility for arbitrary model names.
        """
        if "model" not in arguments:
            return False, "Missing required 'model' parameter"

        model = arguments["model"]
        if not isinstance(model, dict):
            return False, "Model parameter must be a dictionary"

        if "name" not in model:
            return False, "Model dictionary must contain 'name' key"

        # STRICT: model.name must match configured model (no backward-compat for arbitrary names)
        requested_name = model["name"]
        config = getattr(self, "config_provider", None)
        if config:
            configured_model = config.get("model")
            if configured_model and requested_name != configured_model:
                return False, (
                    f"Model name mismatch: requested '{requested_name}' "
                    f"but plugin is configured for '{configured_model}'"
                )

        return True, None

    def _validate_prompt_parameter(self, arguments: dict[str, Any]) -> tuple[bool, str | None]:
        """Validate prompt parameter structure and content."""
        if "prompt" not in arguments:
            return False, "Missing required 'prompt' parameter"

        prompt = arguments["prompt"]
        if isinstance(prompt, dict):
            if "user" not in prompt and "system" not in prompt and "messages" not in prompt:
                return (
                    False,
                    "Prompt dictionary must contain 'user', 'system', or 'messages' key",
                )
        elif not isinstance(prompt, str):
            return False, "Prompt must be a string or dictionary"
        elif len(prompt.strip()) == 0:
            return False, "Prompt string cannot be empty"

        return True, None

    def _validate_temperature_parameter(self, model: dict[str, Any]) -> tuple[bool, str | None]:
        """Validate optional temperature parameter."""
        if "temperature" in model:
            temp = model["temperature"]
            if not isinstance(temp, int | float) or temp < 0 or temp > 2:
                return False, "Temperature must be a number between 0 and 2"
        return True, None

    def _validate_max_tokens_parameter(self, model: dict[str, Any]) -> tuple[bool, str | None]:
        """Validate optional max_tokens parameter."""
        if "max_tokens" in model:
            tokens = model["max_tokens"]
            if not isinstance(tokens, int) or tokens < 1:
                return False, "Max tokens must be a positive integer"
        return True, None

    def _validate_query_parameters(self, arguments: dict[str, Any]) -> tuple[bool, str | None]:
        """Validate query parameters using focused helper methods."""
        # Validate model parameter
        is_valid, error = self._validate_model_parameter(arguments)
        if not is_valid:
            return False, error

        # Validate prompt parameter
        is_valid, error = self._validate_prompt_parameter(arguments)
        if not is_valid:
            return False, error

        model = arguments["model"]

        # Validate optional temperature parameter
        is_valid, error = self._validate_temperature_parameter(model)
        if not is_valid:
            return False, error

        # Validate optional max_tokens parameter
        is_valid, error = self._validate_max_tokens_parameter(model)
        if not is_valid:
            return False, error

        return True, None

    def _validate_resolved_data(self, arguments: dict[str, Any]) -> tuple[bool, str | None]:
        import json
        import re

        try:
            # Convert arguments to JSON string to search for any remaining template patterns
            args_str = json.dumps(arguments)

            # Check for any remaining __PATTERN__ that should have been resolved
            template_pattern = re.compile(r"__([^_]+(?:_[^_]+)*)__")
            matches = template_pattern.findall(args_str)

            if matches:
                unresolved_vars = ", ".join(f"__{var}__" for var in matches)
                return (
                    False,
                    f"PLUGIN_VALIDATION_FAILED: Found unresolved template "
                    f"variables: {unresolved_vars}. Framework should resolve "
                    f"all templates before plugins receive data.",
                )

            return True, None

        except Exception as e:
            return False, f"Error validating resolved data: {e!s}"

    def _validate_provider_initialized(self) -> None:
        """Ensure provider is initialized, raise PluginError if not."""
        if not self.provider:
            raise PluginError(
                message="LM Studio provider not initialized",
                error_code="inference.provider_not_initialized",
                details={"plugin": self.name},
            )

    # ── Event persistence (Slice 5 — thin wrappers to platform modules) ──

    def _store_post_inference_events(
        self,
        context_id: str,
        state: dict[str, Any],
        resolved_action_params: dict[str, Any],
        completion_text: str,
        prompt_ctx: "PromptContext",
    ) -> None:
        """Delegate to platform event_persistence module."""
        from ananta.services.inference_service.event_persistence import (
            store_post_inference_events,
        )

        context_service, content_storage = self._require_context_services()
        config = self.get_context_management_config()
        store_post_inference_events(
            context_id,
            state,
            resolved_action_params,
            completion_text,
            prompt_ctx,
            self._flows_with_input_stored,
            event_writer=context_service.events,
            content_storage=content_storage,
            provider_name=self.name,
            sessions=context_service.sessions,
            compaction=context_service,
            compaction_config=config,
        )

    def _load_context_messages(self, context_id: str) -> list[dict[str, str]]:
        """Delegate to platform context_loader module."""
        from ananta.services.inference_service.context_loader import (
            load_context_messages,
            require_context_services,
        )

        context_service, content_storage = require_context_services(
            self._context_management_service,
            self._content_storage,
        )
        return load_context_messages(context_id, context_service, content_storage)

    def _resolve_context_id(
        self,
        action_params: dict[str, Any],
        state: dict[str, Any],
        context_id_source: ContextIdSource,
        address_key: str | None = None,
    ) -> str:
        """Delegate to platform context_id module."""
        from ananta.services.inference_service.context_id import resolve_context_id

        if not self._context_management_service:
            raise RuntimeError("context_id resolution requires context_management_service")
        return resolve_context_id(
            action_params,
            state,
            context_id_source.value,
            provider_name=self.name,
            registry=self._context_management_service.registry,
            address_key=address_key,
        )

    def _require_context_services(
        self,
    ) -> tuple[Any, Any]:
        """Validate and return required context services."""
        from ananta.services.inference_service.context_loader import require_context_services

        return require_context_services(
            self._context_management_service,
            self._content_storage,
        )

    _ACTION_MAX_TOKENS = 16384

    def get_inference_defaults(self) -> "InferenceDefaults":
        """Return provider-configured inference defaults."""
        from ananta.services.inference_service.interfaces.provider import InferenceDefaults

        assert self.config_provider is not None
        action_temp = self.config_provider.get_float(
            "context.discovery_intent_temperature",
        )
        return InferenceDefaults(
            temperature=self._default_temperature,
            max_tokens=self._default_max_tokens,
            action_vertex_temperature=action_temp,
            action_vertex_max_tokens=self._ACTION_MAX_TOKENS,
        )

    def _prepare_inference_request(
        self,
        action_params: dict[str, Any],
        action_name: str,
        state: dict[str, Any],
        context_config: ContextManagementConfig,
    ) -> tuple[
        InferenceRequest, dict[str, Any], str | None, str | None, dict[str, Any], PromptContext
    ]:
        """Prepare and validate inference request via assembly contract.

        Routes through assemble_prompt() with INFERENCE_PROFILE for
        profile-driven pipeline execution and spec-aware serialization.

        Args:
            action_params: Action parameters from request
            action_name: Name of the action being executed
            state: Current state dict
            context_config: Context management configuration

        Returns:
            Tuple of (request, model_info, resolved_user_input,
            context_id, resolved_action_params, prompt_ctx)
        """
        # Validate and extract model parameters
        model_info, _ = validate_action_parameters(action_params)

        # Normalize IDs to prevent empty string propagation
        normalized_flow_id = normalize_flow_id(state.get("flow_id"))
        normalized_session_id = normalize_session_id(state.get("session_id"))

        # Resolve context_id based on configured mode and source
        # In delegated mode, don't pass context_id to pipeline (model manages its own context)
        context_id: str | None = None
        if context_config.context_mode == ContextMode.PLATFORM:
            context_id = self._resolve_context_id(
                action_params,
                state,
                context_config.context_id_source,
                context_config.context_id_address_key,
            )

        # Pre-resolve IO namespace for pipeline stages (PlanStateStage, etc.)
        io_namespace = self._get_io_namespace(state)

        # Route through the assembly contract — profile-driven pipeline execution
        # with spec-aware serialization (role resolution, adjacent merge, system
        # consolidation).
        from ananta.core.prompts.profiles import INFERENCE_PROFILE
        from ananta.services.inference_service.assembly import (
            assemble_prompt as _assemble,
        )
        from ananta.services.inference_service.assembly_types import (
            PromptAssemblyRequest,
        )

        assembly_request = PromptAssemblyRequest(
            profile_name="inference",
            flow_id=normalized_flow_id or "",
            action_name=action_name,
            session_id=normalized_session_id or "",
            raw_action_params=action_params,
            context_id=context_id,
            io_namespace=io_namespace,
        )
        assembly_result = _assemble(
            assembly_request,
            INFERENCE_PROFILE,
            self._pipeline_factory,
        )
        if assembly_result.prompt_context is None:
            raise PluginError(
                message="Assembly returned no prompt context",
                error_code="inference.assembly_no_context",
                plugin_name=self.name,
            )
        ctx: PromptContext = assembly_result.prompt_context

        from ananta.services.inference_service.transaction import resolve_inference_params

        temperature, max_tokens = resolve_inference_params(
            self.get_inference_defaults(),
            ctx.output_schema,
            model_info,
            ctx.api_payload,
        )

        # Source of truth: ctx.user_prompt is the canonical user prompt from pipeline
        resolved_user_input = require_user_prompt(ctx)

        # Build context metadata
        context_metadata: dict[str, Any] = {
            "session_id": state.get("session_id"),
            "flow_id": state.get("flow_id"),
            "action_name": action_name,
            "_pipeline_context_injected": True,  # Context handled by ContextStage
        }

        # Create InferenceRequest from assembly output — uses spec-aware
        # serialized messages instead of raw ctx.messages
        request = InferenceRequest(
            prompt=list(assembly_result.messages),
            temperature=temperature,
            max_tokens=max_tokens,
            context_metadata=context_metadata,
            response_schema=assembly_result.output_schema,
            use_structured_output=True,
        )

        return request, model_info, resolved_user_input, context_id, ctx.resolved_action_params, ctx

    def _store_assistant_response(
        self,
        session_id: str,
        completion_text: str,
    ) -> None:
        """Delegate to platform event_persistence module."""
        from ananta.services.inference_service.event_persistence import store_assistant_response

        if not self._memory_service:
            raise RuntimeError("Memory service not available for storing assistant response")
        store_assistant_response(
            session_id,
            completion_text,
            memory_service=self._memory_service,
            provider_name=self.name,
        )

    def _store_system_messages_if_first_request(self) -> None:
        """NO-OP: System messages are NOT stored as context events."""
        from ananta.services.inference_service.event_persistence import (
            store_system_messages_if_first_request,
        )

        store_system_messages_if_first_request()

    def _execute_inference_and_extract_completion(
        self, request: InferenceRequest
    ) -> tuple[dict[str, Any], str]:
        """Execute inference and extract completion text from result."""
        # Delegate to provider
        result = self.generate_completion(request)

        # Check for errors - error is ErrorDetail | None per ActionResult type
        error_value = result.get("error")
        if error_value:
            # ErrorDetail is a TypedDict with 'message' key
            error_msg = error_value.get("message", "Inference failed")
            raise PluginError(
                message=error_msg,
                error_code="inference.generation_failed",
                details=result.get("data", {}),
            )

        # Extract completion text - data is dict[str, object] per ActionResult type
        result_data = result.get("data", {})
        completion_result_raw = result_data.get("result", {})
        completion_result = completion_result_raw if isinstance(completion_result_raw, dict) else {}
        completion_text_raw = completion_result.get("completion", "")
        completion_text = str(completion_text_raw) if completion_text_raw else ""

        return result_data, completion_text

    def _resolve_active_io_process_key(self, state: dict[str, Any]) -> str | None:
        """Resolve the active IO plugin's post_message process key from flow trigger_data.

        Delegates to the shared PluginBase._resolve_io_process_key implementation.

        Returns:
            Process key like ``plugin::<namespace>::post_message``, or None on failure.
        """
        try:
            return self._resolve_io_process_key(state)
        except RuntimeError:
            return None

    def _process_inference_request(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Core inference implementation used by all public methods.

        Handles plugin-level concerns (state, config, prompt assembly) and delegates
        actual inference to the strongly-typed generate_completion method.
        """
        action_name, action_params = extract_action_parameters(params)
        self._current_action_name = action_name
        self.logger.debug(f"{self.name} executing action: {action_name}")

        # Reset per-request delivery bindings
        self._synthesized_delivery_attachment = None
        self._synthesized_delivery_session_id = None

        # Platform-owned plan advancement: mark previous step [X], activate next [>].
        # Focus is session-scoped (JOS-02): the VERTEX dispatch stamps the
        # action's OWN session into ``state``; a session-less vertex is
        # treated as plan-less (V-5 skip+log ruling).
        acting_session = str(state.get("session_id") or "")
        has_observation = "observation" in action_params.get("prompt", {})
        self._maybe_advance_plan(
            is_continuation=has_observation, session_id=acting_session,
        )

        # Guard: plan just completed and this is a result-processing vertex.
        if action_name == "process_results" and not self._has_focused_plan(
            session_id=acting_session,
        ):
            self.logger.info(
                "FLOW_COMPLETE: No focused plan for session %s after advancement "
                "on a process_results vertex — flow is done, skipping inference",
                acting_session or "<none>",
            )
            return ActionResult(
                action_status="completed",
                data={"status": "flow_complete"},
                actions=[],
            )

        try:
            return self._infer_and_process(params, state, action_name, action_params)
        except (PluginError, AnantaError, Exception) as e:
            io_process_key = self._resolve_active_io_process_key(state)
            return create_inference_error_response(
                e, action_name, state, io_process_key,
            )

    def _infer_and_process(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
        action_name: str,
        action_params: dict[str, Any],
    ) -> ActionResult:
        """Execute inference, store events, and parse the response into actions."""
        context_config = self.get_context_management_config()
        is_platform_mode = context_config.context_mode == ContextMode.PLATFORM

        if is_platform_mode and not self._context_management_service:
            raise PluginError(
                message="context_mode=platform requires context_management_service to be injected",
                error_code=ErrorCode.PLUGIN_CONFIG_ERROR,
                plugin_name=self.name,
            )

        self._validate_provider_initialized()

        (
            request,
            model_info,
            resolved_user_input,
            context_id,
            resolved_action_params,
            prompt_ctx,
        ) = self._prepare_inference_request(action_params, action_name, state, context_config)

        self.logger.debug("Context injected via PromptPipeline")

        from ananta.services.inference_service.transaction import validate_model_config

        validate_model_config(model_info, self.get_configured_model_name())

        log_request_to_state(
            state,
            action_name,
            model_info,
            request_messages=request.messages,
            request_temperature=request.temperature,
            request_max_tokens=request.max_tokens,
            resolved_user_input=resolved_user_input,
            state_service=self.state_service,
        )

        if is_platform_mode and context_id:
            self._store_system_messages_if_first_request()

        result_data, completion_text = self._execute_inference_and_extract_completion(request)

        if is_platform_mode and context_id:
            self._store_post_inference_events(
                context_id, state, resolved_action_params, completion_text, prompt_ctx,
            )

        log_response_to_state(
            state, action_name, model_info, completion_text, result_data,
            state_service=self.state_service,
        )

        return self._parse_and_normalize_actions(
            params, state, action_params,
            model_info, resolved_user_input, context_id,
            prompt_ctx, completion_text, result_data,
        )

    def _parse_and_normalize_actions(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
        action_params: dict[str, Any],
        model_info: dict[str, Any],
        resolved_user_input: str | None,
        context_id: str | None,
        prompt_ctx: PromptContext,
        completion_text: str,
        result_data: dict[str, Any],
    ) -> ActionResult:
        """Parse LLM response text into executable actions and build the success response."""
        actions_to_execute = parse_llm_response_for_actions(completion_text)
        if not actions_to_execute and not has_explicit_actions_key(completion_text):
            error_msg = validate_actions_found(actions_to_execute, completion_text)
            if error_msg:
                raise PluginError(
                    message=error_msg,
                    error_code="inference.invalid_llm_response_structure",
                    plugin_name=self.name,
                )

        inject_observation_into_create_extended_plan(
            actions_to_execute,
            getattr(prompt_ctx, "tool_observation", None),
        )

        placeholder_context = build_placeholder_context(
            state, model_info, resolved_user_input,
        )
        actions_to_execute = normalize_action_definitions(
            actions_to_execute,
            state.get("session_id"),
            state.get("flow_id"),
            context=placeholder_context,
            context_id=context_id,
        )

        self._validate_step_contract(prompt_ctx, actions_to_execute)

        validate_planning_extension_content(prompt_ctx, actions_to_execute)

        job_id_context = extract_job_id_from_context(
            state, action_params, raw_params=params,
        )
        inject_job_context(actions_to_execute, job_id_context)

        result_dict = {"timestamp": result_data.get("timestamp")}
        return create_success_response(completion_text, actions_to_execute, result_dict)

    def process_error(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Error-context inference: handle action errors and provide recovery.

        Args:
            params: Parameters dict containing prompt and optional model config
            state: State dict for plugin execution

        Returns:
            ActionResult with error analysis and recovery suggestions
        """
        return self._process_inference_request(params, state)

    def process_results(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Result-processing inference: format results and determine next steps.

        Uses result-focused discovery to provide output-relevant process information.

        Args:
            params: Parameters dict containing prompt and optional model config
            state: State dict for plugin execution

        Returns:
            ActionResult with formatted output and next-step actions
        """
        return self._process_inference_request(params, state)

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
        """
        return self._process_inference_request(params, state)

    async def execute_async(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        return self._process_inference_request(params, state)

    # ========================================
    # InferenceServiceInterface Implementation
    # ========================================

    def generate_completion(self, request: InferenceRequest) -> ActionResult:
        """Generate completion via LM Studio - INTERNAL HELPER METHOD.

        This method is called by public interface methods
        (process_error, process_results, propose_name).
        It should NOT be exposed as a plugin action.

        Delegates to provider.
        """
        self._require_ready_for_inference()

        if not self.provider:
            raise InferenceServiceUnavailableError(
                "LM Studio provider not initialized. Call prepare_for_readiness first.",
                details={"plugin": self.name},
            )

        return self.provider.generate_completion(request)

    def validate_availability(self) -> ActionResult:
        """Check LM Studio availability - INTERNAL HELPER METHOD.

        Delegates to provider.
        """
        if not self.provider:
            error_detail: ErrorDetail = {
                "type": "InferenceError",
                "code": "inference.provider_not_initialized",
                "message": "Provider not initialized",
                "details": {},
                "severity": "ERROR",
                "timestamp": datetime.now(UTC).isoformat(),
            }
            return {
                "action_status": ActionStatus.ERROR.value,
                "data": {"available": False, "provider": "lm_studio"},
                "error": error_detail,
                "timestamp": datetime.now(UTC).isoformat(),
            }

        return self.provider.validate_availability()

    def get_model_info(self) -> ActionResult:
        """Get LM Studio model information - INTERNAL HELPER METHOD.

        Delegates to provider.
        """
        if not self.provider:
            error_detail: ErrorDetail = {
                "type": "InferenceError",
                "code": "inference.provider_not_initialized",
                "message": "Provider not initialized",
                "details": {},
                "severity": "ERROR",
                "timestamp": datetime.now(UTC).isoformat(),
            }
            return {
                "action_status": ActionStatus.ERROR.value,
                "data": {},
                "error": error_detail,
                "timestamp": datetime.now(UTC).isoformat(),
            }

        return self.provider.get_model_info()

    def get_configured_model_name(self) -> str:
        """Get the configured model name for this inference provider.

        Returns:
            The model name configured in the plugin (e.g., 'meta-llama-3.1-8b-instruct')

        Raises:
            PluginError: If no model is configured
        """
        if not self.provider:
            raise PluginError(
                message="Cannot get model name: provider not initialized",
                error_code=ErrorCode.PLUGIN_CONFIG_ERROR,
                plugin_name=self.name,
            )
        model_name = self.provider.model
        if not model_name:
            raise PluginError(
                message="No model configured in provider",
                error_code=ErrorCode.PLUGIN_CONFIG_ERROR,
                plugin_name=self.name,
            )
        return model_name

    def _build_text_completion_messages(
        self,
        prompt: str,
        context_user_message: str | None,
        load_context: bool,
    ) -> list[dict[str, str]]:
        """Build messages list for text completion.

        Structure: [system, ...conversation_history..., context_user_message, prompt]

        Args:
            prompt: The prompt text to complete.
            context_user_message: Optional user message to prepend before the prompt.
            load_context: Whether to load conversation history from context.

        Returns:
            List of message dicts with role and content keys.
        """
        messages: list[dict[str, str]] = []

        # Add system prompt with built-in processes injected at request time
        system_prompt = self._get_system_prompt_with_builtins()
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # Load conversation history from context (same as main inference path)
        if load_context:
            self._append_context_history_for_text_completion(messages)

        # Add the current query as a user message (if provided)
        if context_user_message:
            messages.append({"role": "user", "content": context_user_message})
            preview = context_user_message[:60].replace(chr(10), " ")
            self.logger.debug(f"TEXT_COMPLETION: query: {preview}...")

        # Add the prompt as the final user message
        messages.append({"role": "user", "content": prompt})
        self.logger.info(f"TEXT_COMPLETION: Total messages: {len(messages)}")

        return messages

    def _append_context_history_for_text_completion(
        self,
        messages: list[dict[str, str]],
    ) -> None:
        """Append conversation history from context for text completion.

        Args:
            messages: The messages list to append to (mutated in place).
        """
        context_config = self.get_context_management_config()
        is_platform = context_config.context_mode == ContextMode.PLATFORM
        if not (is_platform and self._context_management_service):
            return

        # Resolve context_id using plugin's configured method
        context_id = self._resolve_context_id(
            action_params={},
            state={},
            context_id_source=context_config.context_id_source,
            address_key=context_config.context_id_address_key,
        )
        # Load conversation history (INPUT/OUTPUT events only)
        conversation_messages = self._load_context_messages(context_id)
        messages.extend(conversation_messages)
        msg_count = len(conversation_messages)
        self.logger.info(f"TEXT_COMPLETION: Loaded {msg_count} messages from context {context_id}")
        for i, msg in enumerate(conversation_messages):
            role = msg.get("role", "?")
            content_preview = msg.get("content", "")[:60].replace("\n", " ")
            self.logger.debug(f"TEXT_COMPLETION: [{i}] {role}: {content_preview}...")

    def _extract_completion_text(self, result: ActionResult) -> str:
        """Extract completion text from inference result.

        Args:
            result: The inference result from the provider.

        Returns:
            The extracted completion text.

        Raises:
            PluginError: If completion failed or response format is invalid.
        """
        if result.get("action_status") != ActionStatus.COMPLETED.value:
            error_detail = result.get("error")
            error_msg = (
                error_detail.get("message", "Unknown error") if error_detail else "Unknown error"
            )
            raise PluginError(
                message=f"Completion failed: {error_msg}",
                error_code=ErrorCode.PLUGIN_EXECUTION_ERROR,
                plugin_name=self.name,
            )

        # Extract text content from response
        # LM Studio returns data.result.completion (plain text)
        data = result.get("data") or {}
        raw_result = data.get("result", {})
        if not isinstance(raw_result, dict):
            raise PluginError(
                message=f"Invalid response format: {type(raw_result)}",
                error_code=ErrorCode.PLUGIN_EXECUTION_ERROR,
                plugin_name=self.name,
            )

        # LM Studio format: data.result.completion
        completion = raw_result.get("completion", "")
        if not completion:
            raise PluginError(
                message="No completion text in response",
                error_code=ErrorCode.PLUGIN_EXECUTION_ERROR,
                plugin_name=self.name,
            )
        return str(completion)

    def generate_text_completion(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float,
        context_user_message: str | None = None,
        load_context: bool = True,
    ) -> str:
        """Generate text completion with optional conversation context.

        By default, loads conversation history from context before executing inference.
        Does not store INPUT/OUTPUT events (hidden from context).

        Args:
            prompt: The prompt text to complete.
            max_tokens: Maximum tokens to generate.
            temperature: Sampling temperature.
            context_user_message: Optional user message to prepend before the prompt.
                Used for intent classification where the user's query should appear
                as a separate message before the classification prompt.
            load_context: Whether to load conversation history from context.
                Default True for operations like intent classification.
                Set False for operations like compaction that process context directly.

        Returns:
            The generated completion text.

        Raises:
            PluginError: If completion fails or context loading fails.
        """
        self._require_ready_for_inference()

        if not self.provider:
            raise PluginError(
                message="Cannot generate completion: provider not initialized",
                error_code=ErrorCode.PLUGIN_CONFIG_ERROR,
                plugin_name=self.name,
            )

        raw_messages = self._build_text_completion_messages(
            prompt, context_user_message, load_context
        )

        # Route through assembly contract for consistent serialization
        from ananta.core.prompts.profiles import TEXT_COMPLETION_PROFILE
        from ananta.services.inference_service.assembly import (
            assemble_prompt as _assemble,
        )
        from ananta.services.inference_service.assembly_types import PromptAssemblyRequest

        assembly_request = PromptAssemblyRequest(
            profile_name="text_completion",
            flow_id="",
            action_name="text_completion",
            session_id="",
            pre_built_messages=tuple(raw_messages),
        )
        assembly_result = _assemble(
            assembly_request, TEXT_COMPLETION_PROFILE, self._pipeline_factory,
        )

        req = InferenceRequest(
            prompt=list(assembly_result.messages),
            max_tokens=max_tokens,
            temperature=temperature,
            use_structured_output=False,
            hide_from_context=True,
        )
        result = self.provider.generate_completion(req)

        return self._extract_completion_text(result)

    def generate_compaction_summary(self, request: CompactionRequest) -> str:
        """Generate summary from messages. Required if supports_compaction=True.

        Args:
            request: The compaction request with messages and config.

        Returns:
            Generated summary text.

        Raises:
            PluginError: If summary generation fails.
        """
        # Build prompt for summary generation
        messages_text = "\n".join(
            f"[{msg.get('role', 'unknown')}]: {msg.get('content', '')}"
            for msg in request.messages_to_summarize
        )

        existing_summary_text = ""
        if request.existing_summary:
            existing_summary_text = (
                f"\nExisting summary to incorporate:\n{request.existing_summary}\n"
            )

        prompt = f"""Generate a concise summary of the following conversation.
{existing_summary_text}
Conversation:
{messages_text}

The summary should:
- Capture key points and decisions
- Be {request.summary_budget_chars} characters or less
- Focus on information needed for future context

Summary:"""

        return self.generate_text_completion(
            prompt=prompt,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            load_context=False,  # Compaction processes context directly, don't load it again
        )

    def warm_cache(self, request: WarmingRequest) -> bool:
        """Warm KV cache with context. Required if warming_enabled=True.

        Cache warming populates the LLM's KV cache with context, but does NOT
        require a response. The goal is to have the model process the context
        so subsequent requests benefit from cached attention matrices.

        Args:
            request: The warming request with messages and config.

        Returns:
            True if warming succeeded.

        Raises:
            PluginError: If the provider call fails (network, timeout, etc).
        """
        self._require_ready_for_inference()

        if not self.provider:
            raise PluginError(
                message="Cannot warm cache: provider not initialized",
                error_code=ErrorCode.PLUGIN_CONFIG_ERROR,
                plugin_name=self.name,
            )

        # Build prompt from messages for cache warming
        messages_text = "\n".join(
            f"[{msg.get('role', 'unknown')}]: {msg.get('content', '')}" for msg in request.messages
        )

        prompt = f"""[Context Restoration]
The following is the conversation history being restored to cache:
{messages_text}

Acknowledge receipt with a brief confirmation."""

        # Send to provider to warm KV cache
        # Empty completion is acceptable - the goal is cache population, not response text
        req = InferenceRequest(
            prompt=prompt,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            use_structured_output=False,
        )
        result = self.provider.generate_completion(req)

        # Check for actual errors (network, timeout), but NOT empty completion
        if result.get("action_status") != ActionStatus.COMPLETED.value:
            error_detail = result.get("error")
            error_msg = (
                error_detail.get("message", "Unknown error") if error_detail else "Unknown error"
            )
            raise PluginError(
                message=f"Cache warming failed: {error_msg}",
                error_code=ErrorCode.PLUGIN_EXECUTION_ERROR,
                plugin_name=self.name,
            )

        # Empty completion is acceptable for warming - KV cache was populated
        return True

    @property
    def service_interfaces(self) -> tuple[type, ...]:
        """Declare that this plugin satisfies InferenceProvider."""
        return (InferenceProvider,)

    @property
    def supported_interface_versions(self) -> dict[type, str]:
        return {InferenceProvider: "1.0.0"}

    def get_config_schema(self) -> dict[str, object]:
        """Declare configuration schema for the inference plugin.

        Returns JSON Schema for setup flow to generate UI/prompts.
        """
        return {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "title": "Default Inference Plugin",
            "description": (
                "Configuration for LLM inference service (LM Studio, Ollama, or OpenAI-compatible)"
            ),
            "type": "object",
            "required": ["base_url", "model"],
            "properties": {
                "base_url": {
                    "type": "string",
                    "format": "uri",
                    "title": "API Base URL",
                    "description": "Base URL for the inference API endpoint",
                    "default": "http://localhost:1234/v1",
                    "examples": [
                        "http://localhost:1234/v1",
                        "http://host.docker.internal:1234/v1",
                        "https://api.openai.com/v1",
                        "http://localhost:11434/v1",
                    ],
                    "x-group": "connection",
                    "x-order": 1,
                },
                "model": {
                    "type": "string",
                    "title": "Model Name",
                    "description": "Name of the LLM model to use for inference",
                    "default": "meta-llama-3.1-8b-instruct",
                    "examples": [
                        "meta-llama-3.1-8b-instruct",
                        "llama-3.1-8b-instruct",
                        "gpt-4o",
                        "gpt-4o-mini",
                        "claude-3-5-sonnet-20241022",
                    ],
                    "x-group": "connection",
                    "x-order": 2,
                },
                "api_key": {
                    "type": "string",
                    "title": "API Key",
                    "description": (
                        "API key for authentication (required for "
                        "OpenAI/Anthropic, optional for local providers)"
                    ),
                    "x-secret": True,
                    "x-group": "security",
                    "x-order": 1,
                },
                "timeout_seconds": {
                    "type": "integer",
                    "title": "Timeout",
                    "description": (
                        "Request timeout in seconds (inference can take longer than embeddings)"
                    ),
                    "default": 120,
                    "minimum": 10,
                    "maximum": 600,
                    "x-group": "advanced",
                    "x-order": 1,
                },
            },
            "x-test-endpoint": "/v1/chat/completions",
            "x-test-method": "validate_availability",
        }


# REMOVED: Module-level execute aliases per no-back-compat policy.
# Access the plugin through the service wrapper (InferenceService) instead.
