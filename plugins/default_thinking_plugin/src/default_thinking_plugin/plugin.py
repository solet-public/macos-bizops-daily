"""Default Thinking Plugin — structured planning over platform inference.

Manages per-task contexts, stores task metadata, and routes internal
reasoning through the platform inference service (DEP-01 Phase-2b: the
plugin owns no model path). Satisfies ThinkingProvider contract
structurally.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ananta.core.config.config_provider import ConfigProvider
from ananta.core.domain.enums import ActionStatus
from ananta.core.domain.types import ActionResult
from ananta.core.plans import (
    advance_plan_markers,
    normalize_content,
    normalize_for_new_plan_install,
    parse,
    render_plan_steps,
)
from ananta.core.plans.projection import project_next_work_item
from ananta.core.plans.session_scoped_memory import SessionScopedMemory
from ananta.core.plans.transitions import (
    complete_graft_step,
    ensure_active_marker,
    inject_wbs_headers,
    splice_execution_tail,
)
from ananta.core.plans.windowing import build_plan_window
from ananta.core.plugins.plugin_base import ServicePlugin
from ananta.error_handling import FrameworkError
from ananta.interfaces.inference_service_interface import InferenceRequest
from ananta.interfaces.thinking_provider_interface import ThinkingProvider
from ananta.logging_setup import configure_plugin_logging
from ananta.services.context_management.types import (
    ContextActorType,
    ContextEventType,
)
from ananta.types.schema_types import SchemaDefinition
from default_knowledge_plugin.chunking import strip_article_metadata_preamble

from .artifact_authoring import ArtifactAuthoringService
from .artifact_helpers import (
    extract_fenced_block,
    extract_section,
    validate_no_unresolved_placeholders,
)
from .authored_lifecycle import AuthoredJosekiLifecycle
from .authored_registration import AuthoredJosekiRegistrar
from .authored_validation import (
    parse_joseki_key,
    validate_authored_wbs,
)
from .authored_validation import (
    validate_authored_joseki as validate_authored_joseki_card,
)
from .constants import (
    KB_AUTHORED_JOSEKI,
    KB_PLAN_TEMPLATES,
    KB_THINKING_PLANS,
    KB_THINKING_PLAYBOOKS,
    PLUGIN_NAME,
    SHIPPED_SYSTEM_PROMPT_RELATIVE_PATH,
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    TASK_TYPE_PLAN,
    VALID_STATUSES,
    VALID_TASK_TYPES,
    ErrorCode,
)
from .joseki_run_gateway import JosekiRunGateway
from .joseki_run_store import JosekiRunStore
from .pipeline_resolver import (
    check_json_schema_value,
    collect_layer_config_sources,
    collect_parameter_group_sources,
    validate_pipeline_spec_raw_and_loaded,
)
from .plan_store import (
    PlanStore,
    count_plan_steps,
    generate_plan_summary,
)
from .plan_template_lifecycle import PlanTemplateLifecycle
from .pull_execution_service import PullExecutionService
from .schema import NAMESPACE, get_thinking_schema
from .wbs_authoring import WbsAuthoringService

if TYPE_CHECKING:
    from ananta.services.context_management.content_storage import (
        FileContextContentStorage,
    )
    from ananta.services.context_management.service import ContextManagementService

logger = logging.getLogger(__name__)


# Plan window construction is now in ``ananta.core.plans.windowing``.
_build_plan_window = build_plan_window


# WBS projection now in ``ananta.core.plans.projection``.
_project_next_work_item = project_next_work_item


# Playbook section helpers and artifact helpers now in artifact_helpers.py.
# extract_section and list_section_ids imported at top.


_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


# Request-shaping defaults for internal reasoning completions routed
# through the platform inference service (DEP-01 Phase-2b). These are
# NOT a model path: the bound provider owns model choice and transport
# limits. InferenceRequest requires both fields by interface contract,
# mirroring the session-ledger generate_completion consumer's
# per-purpose constants. Temperature 0.0 restores the retired path's
# exact contract — downstream consumers parse the completion
# structurally (count_plan_steps, _parse_planning_actions) and had
# load-bearing determinism at temp 0.
_THINKING_COMPLETION_TEMPERATURE = 0.0
_THINKING_COMPLETION_MAX_TOKENS = 16384


# Completion-text locations inside a usable envelope, canonical first
# (``inference_transaction._invoke_and_extract``'s ``data.result.completion``)
# then the vendor-tolerant fallbacks the session-ledger consumer accepts.
_COMPLETION_TEXT_PATHS: tuple[tuple[str, ...], ...] = (
    ("result", "completion"),
    ("completion",),
    ("text",),
    ("message", "content"),
)


def _resolve_shipped_system_prompt_path() -> Path:
    """This plugin's own shipped default system prompt (package-relative).

    ``Path(__file__).resolve().parents[2]`` is the plugin root
    (``plugins/default_thinking_plugin/``) -- ``parents[0]`` is this
    module's own package dir, ``parents[1]`` is ``src/``. Ships in every
    seed that includes this plugin, unlike ``profile/config/``, which
    genesis excludes by design.
    """
    return Path(__file__).resolve().parents[2] / SHIPPED_SYSTEM_PROMPT_RELATIVE_PATH


def _envelope_data(result: object) -> dict[str, Any] | None:
    """Unwrap a usable ``generate_completion`` envelope to its data dict.

    Returns ``None`` for any unusable envelope per the ActionResult
    contract — an ``error`` payload or a non-completed status — so the
    caller can fail loud with a typed error. (The current LM Studio
    provider RAISES on failure rather than returning such envelopes;
    those exceptions propagate loud through the seam untouched.)
    """
    if not isinstance(result, dict) or result.get("error"):
        return None
    if result.get("action_status") not in (None, "completed"):
        return None
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    return data if isinstance(data, dict) else None


def _text_at(data: dict[str, Any], path: tuple[str, ...]) -> str:
    """Return non-blank string content at ``path`` inside ``data``, else ``""``."""
    node: object = data
    for key in path:
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
    return node if isinstance(node, str) and node.strip() else ""


def _extract_completion_text(result: object) -> str:
    """Pull completion text out of a ``generate_completion`` envelope."""
    data = _envelope_data(result)
    if data is None:
        return ""
    for path in _COMPLETION_TEXT_PATHS:
        text = _text_at(data, path)
        if text:
            return text
    return ""


def _decode_blob_content(
    raw_content: object, spec_id: str, blob_id: str,
) -> str:
    """Decode blob retrieve_blob ``content`` field into a UTF-8 string.

    The blob storage filesystem provider returns ``content`` as a
    **hex-encoded string** (one ASCII pair per byte). Bootstrap mode
    returns raw bytes. Both shapes need to land as a decoded UTF-8
    string before JSON parsing.
    """
    if isinstance(raw_content, bytes):
        return raw_content.decode("utf-8")
    if isinstance(raw_content, str):
        if raw_content and _HEX_RE.match(raw_content):
            try:
                return bytes.fromhex(raw_content).decode("utf-8")
            except ValueError as exc:
                raise FrameworkError(
                    message=(
                        f"Pipeline Spec {spec_id!r}: blob {blob_id} "
                        f"hex-decoding failed: {exc}"
                    ),
                    error_code=ErrorCode.INTERNAL_ERROR,
                ) from exc
        if raw_content.strip():
            return raw_content
    raise FrameworkError(
        message=(
            f"Pipeline Spec {spec_id!r}: blob {blob_id} returned "
            f"empty or invalid content"
        ),
        error_code=ErrorCode.INTERNAL_ERROR,
    )


def _parse_pipeline_spec_response(
    content: str, spec_id: str,
) -> dict[str, Any]:
    """Parse a Pipeline Spec model response as a JSON object.

    The thinking model is instructed to emit raw JSON — no markdown,
    no code fences, no headers. This parser strips leading/trailing
    whitespace and parses with the strict JSON parser. The model
    sometimes wraps with a stray code fence; we strip a single
    leading/trailing fence as a tolerance, but reject anything else.
    """
    if not content:
        raise FrameworkError(
            message=f"Pipeline Spec {spec_id!r} content is empty",
            error_code=ErrorCode.PARAMETER_ERROR,
        )
    body = content.strip()
    if body.startswith("```"):
        first_newline = body.find("\n")
        if first_newline != -1:
            body = body[first_newline + 1:]
        if body.endswith("```"):
            body = body[: -3].rstrip()
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise FrameworkError(
            message=(
                f"Pipeline Spec {spec_id!r} JSON failed to parse: {exc}"
            ),
            error_code=ErrorCode.PARAMETER_ERROR,
        ) from exc
    if not isinstance(parsed, dict):
        raise FrameworkError(
            message=(
                f"Pipeline Spec {spec_id!r} JSON must be an object, "
                f"got {type(parsed).__name__}"
            ),
            error_code=ErrorCode.PARAMETER_ERROR,
        )
    return parsed


def _derive_wbs_id(manifest_id: str, phase_number: int) -> str:
    """Derive a canonical WBS ID from manifest ID and phase number.

    Strip the ``wmf-`` prefix, prepend ``wbs-``, append ``-phase{N}``.
    Example: ``wmf-neuro-ambient-001`` → ``wbs-neuro-ambient-001-phase1``.
    """
    suffix = manifest_id.removeprefix("wmf-")
    return f"wbs-{suffix}-phase{phase_number}"


# ── Adapter classes for ArtifactAuthoringService protocols ──────────


class _PluginKnowledgeWriter:
    """Adapts the plugin's knowledge base helpers to KnowledgeWriter."""

    def __init__(self, plugin: DefaultThinkingPlugin) -> None:
        self._plugin = plugin

    def write(self, path: str, content: str) -> None:
        self._plugin._write_to_thinking_plans_kb(path, content)  # noqa: SLF001

    def read(self, path: str) -> str:
        return self._plugin._read_from_thinking_plans_kb(path)  # noqa: SLF001


class _RunWbsRegistrarAdapter:
    """Adapts the author-by-value WBS registration to the run gateway's seam.

    Run WBSes are single-work-item joseki fragments: phase metadata is
    constant by construction (phase 1, 'joseki-run').
    """

    def __init__(self, plugin: DefaultThinkingPlugin) -> None:
        self._plugin = plugin

    def register(
        self, *, content: str, wbs_id: str, manifest_id: str, session_id: str,
    ) -> dict[str, Any]:
        return self._plugin.register_authored_work_breakdown_structure(
            content=content,
            wbs_id=wbs_id,
            manifest_id=manifest_id,
            phase_number=1,
            phase_name="joseki-run",
            session_id=session_id,
        )


class _PluginPlanBufferAdapter:
    """The session-scoped plan-focus surface over the memory service (JOS-02).

    ``get_focused``/``unfocus`` are memory-service verbs (the same focus
    provider ``PlanStore`` consumes); install reuses the plugin's own
    ``upsert_plan`` so the ACTIVE_PLAN framing is produced by the
    production path, never re-implemented. ``release_session_focus``
    clears the session's WHOLE buffer (R1 — run sessions are ephemeral,
    every pin in one is run-scoped by construction).
    """

    def __init__(self, plugin: DefaultThinkingPlugin) -> None:
        self._plugin = plugin

    def _focused_plan_item(self, session_id: str) -> dict[str, Any] | None:
        from ananta.core.prompts.context import ACTIVE_PLAN_MARKER

        memory = self._plugin._session_memory(session_id)  # noqa: SLF001
        for item in memory.get_focused()["memories"]:
            content = item.get("content", "")
            if isinstance(content, str) and ACTIVE_PLAN_MARKER in content:
                return item
        return None

    def has_focused_plan(self, *, session_id: str) -> bool:
        return self._focused_plan_item(session_id) is not None

    def upsert_plan(self, content: str, *, session_id: str) -> dict[str, Any]:
        return self._plugin.upsert_plan(content, session_id=session_id)

    def release_session_focus(self, *, session_id: str) -> None:
        memory_service = self._plugin._get_memory_service()  # noqa: SLF001
        memory_service.unfocus_all_for_session(session_id=session_id)


class _PluginStateStore:
    """Adapts the plugin's state service to StateStore."""

    def __init__(self, state_service: Any, namespace: str) -> None:
        self._state_service = state_service
        self._namespace = namespace

    def write_state(
        self,
        namespace: str,
        data: dict[str, object],
    ) -> Any:
        return self._state_service.write_state(namespace=namespace, data=data)

    def read_state(
        self,
        namespace: str,
        query: dict[str, object],
    ) -> dict[str, Any]:
        result = self._state_service.read_state(namespace=namespace, query=query)
        return result if isinstance(result, dict) else {}

    def update_state(
        self,
        namespace: str,
        query: dict[str, object],
        updates: dict[str, object],
    ) -> dict[str, Any]:
        result = self._state_service.update_state(
            namespace=namespace, query=query, updates=updates,
        )
        return result if isinstance(result, dict) else {}

    def generate_id(self, *, prefix: str) -> str:
        return str(self._state_service.generate_id(prefix=prefix))


class _PluginFocusManager:
    """Adapts the plugin's focus helpers to FocusManager.

    Session-bound at construction (JOS-02): every operation acts on the
    binding session's focus buffer, never a global one.
    """

    def __init__(self, plugin: DefaultThinkingPlugin, session_id: str) -> None:
        self._plugin = plugin
        self._memory = plugin._session_memory(session_id)  # noqa: SLF001

    def upsert(self, content: str, *, doc_tag: str, label: str) -> str:
        return self._plugin._upsert_focused_document(  # noqa: SLF001
            content,
            doc_tag=doc_tag,
            label=label,
            memory_service=self._memory,
        )

    def defocus_by_label(self, label: str) -> None:
        self._plugin._defocus_documents_by_label(  # noqa: SLF001
            label, self._memory,
        )

    def get_focused(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self._memory.get_focused()["memories"]
        return result


class _PluginJosekiCardWriter:
    """Adapts the plugin's authored-joseki KB write to JosekiCardWriter."""

    def __init__(self, plugin: DefaultThinkingPlugin) -> None:
        self._plugin = plugin

    def write(self, path: str, content: str) -> None:
        self._plugin._write_to_authored_joseki_kb(path, content)  # noqa: SLF001


class _PluginJosekiCardReader:
    """Adapts the plugin's authored-joseki KB read to JosekiCardReader."""

    def __init__(self, plugin: DefaultThinkingPlugin) -> None:
        self._plugin = plugin

    def read(self, path: str) -> str:
        return self._plugin._read_from_authored_joseki_kb(path)  # noqa: SLF001


class _PluginPlanTemplateCardWriter:
    """Adapts the plugin's plan_templates KB write to PlanTemplateCardWriter."""

    def __init__(self, plugin: DefaultThinkingPlugin) -> None:
        self._plugin = plugin

    def write(self, path: str, content: str) -> None:
        self._plugin._write_to_plan_templates_kb(path, content)  # noqa: SLF001


class _PluginPlanTemplateCardReader:
    """Adapts the plugin's plan_templates KB read to PlanTemplateCardReader."""

    def __init__(self, plugin: DefaultThinkingPlugin) -> None:
        self._plugin = plugin

    def read(self, path: str) -> str:
        return self._plugin._read_from_plan_templates_kb(path)  # noqa: SLF001


class _PluginProcessSchemaLookup:
    """Adapts the plugin's discovery service to ProcessSchemaLookup.

    Retrieves full argument property schemas (including ``minimum``,
    ``maximum``, ``enum`` constraints) for WBS argument validation.
    """

    def __init__(self, plugin: DefaultThinkingPlugin) -> None:
        self._plugin = plugin

    def get_arg_properties(
        self, process_key: str,
    ) -> dict[str, dict[str, object]]:
        try:
            discovery = self._plugin._get_discovery_service()  # noqa: SLF001
        except FrameworkError:
            return {}
        process_data = discovery.get_process_by_key(process_key)
        if not isinstance(process_data, dict):
            return {}
        extracted = DefaultThinkingPlugin._extract_arg_properties(process_data)
        if extracted is None:
            return {}
        arg_props, required_names = extracted
        # Fold the object-level "required" list into each property dict —
        # consumers (``_validate_wbs_arguments``, ``authored_validation``)
        # read per-property ``required``, which JSON schema keeps at the
        # object level. Without this fold the required-argument checks can
        # never fire (they read a key that is structurally absent). New
        # dicts, not mutations: the registry owns the originals.
        return {
            name: {**prop, "required": name in required_names}
            for name, prop in arg_props.items()
            if isinstance(prop, dict)
        }

    def key_exists(self, process_key: str) -> bool:
        """True when the key resolves in the LIVE process registry.

        Deliberately does NOT swallow a missing discovery service: an
        unknown-key verdict from a dead registry would be a false error,
        so unavailability propagates loudly instead (fail fast).
        """
        discovery = self._plugin._get_discovery_service()  # noqa: SLF001
        return discovery.get_process_by_key(process_key) is not None


class DefaultThinkingPlugin(ServicePlugin):
    """Structured reasoning plugin using local LLM.

    Manages per-task contexts, invokes LMStudio backend, stores task metadata.
    Each task gets its own context stream for conversation history.
    """

    def __init__(self) -> None:
        super().__init__()
        self.name = PLUGIN_NAME
        self.logger: logging.Logger = logging.getLogger(self.name)

        # LMStudio connection (initialized in prepare_for_readiness)
        self._system_prompt: str = ""
        self._identity_text: str = ""

        # Services (acquired in prepare_for_readiness or lazily)
        self._state_service: Any = None
        self.config_provider: ConfigProvider | None = None
        self._context_management_service: ContextManagementService | None = None
        self._content_storage: FileContextContentStorage | None = None
        # ArtifactAuthoringService / WbsAuthoringService are built per call,
        # session-bound (JOS-02) — no eager instances.

        # Plan advancement cursor guards — track, PER SESSION (JOS-02), the
        # active plan cursor the model most recently saw/worked on. When a
        # plan install, replace, or graft changes the active frontier before
        # that step has executed, one advancement cycle must be skipped so the
        # new current step is not auto-completed. Thread-safety contract (R4):
        # different sessions touch disjoint keys; same-session actions
        # serialize through the action queue.
        self._plan_cursors: dict[str, str] = {}
        self._graft_skips: dict[str, int] = {}

    # ─────────────────────────────────────────────────────────────────────────
    # PLUGIN METADATA
    # ─────────────────────────────────────────────────────────────────────────

    def get_default_config(self) -> dict[str, Any]:
        """Return default configuration.

        The plugin owns NO model path (DEP-01 Phase-2b): internal
        reasoning routes through the platform inference service, whose
        provider binding is the autonomic lane's concern. Only the
        system-prompt OVERRIDE location remains configurable here -- if
        no file exists at this profile-relative path, the plugin falls
        back to its own shipped default (see
        ``_resolve_shipped_system_prompt_path``), so an operator never
        needs to set this just to get a working prompt.
        """
        return {
            "system_prompt_path": "config/prompts/thinking_system_prompt.md",
        }

    def get_config_schema(self) -> dict[str, object]:
        """Return JSON Schema for plugin configuration."""
        defaults = self.get_default_config()
        return {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "title": "Default Thinking Plugin Configuration",
            "type": "object",
            "required": [],
            "properties": {
                "system_prompt_path": {
                    "type": "string",
                    "title": "System Prompt Path",
                    "description": (
                        "Profile-relative path to an OPERATOR OVERRIDE of the reasoning "
                        "system prompt. Optional -- when no file exists here, the plugin "
                        "uses its own shipped default prompt instead of running empty."
                    ),
                    "default": defaults["system_prompt_path"],
                },
            },
        }

    @property
    def service_interfaces(self) -> tuple[type, ...]:
        """Service interfaces provided by this plugin."""
        return (ThinkingProvider,)

    @property
    def supported_interface_versions(self) -> dict[type, str]:
        return {ThinkingProvider: "1.0.0"}

    def get_schema_definitions(self) -> list[SchemaDefinition]:
        """Return schema definitions for the thinking_task table."""
        return [get_thinking_schema()]

    # ─────────────────────────────────────────────────────────────────────────
    # LIFECYCLE
    # ─────────────────────────────────────────────────────────────────────────

    def get_readiness_error(self) -> str | None:
        return self.readiness_error

    def prepare_for_readiness(self) -> None:
        """Initialize plugin. Fail-fast if dependencies unavailable."""
        if not self.orchestrator_ref:
            raise RuntimeError(f"{self.name}: orchestrator_ref not injected")

        app_home = getattr(self.orchestrator_ref, "APP_HOME", None)
        if not app_home:
            raise RuntimeError(f"{self.name}: APP_HOME not configured")

        # Initialize configuration and logging
        defaults = self.get_default_config()
        config_manager = getattr(self.orchestrator_ref, "config_manager", None)
        if config_manager:
            self.config_provider = config_manager.get_plugin_config_provider(self.name, defaults)
        if not self.config_provider:
            self.config_provider = ConfigProvider(self.name, defaults)

        self.logger = configure_plugin_logging(app_home, self.name, self.config_provider)
        self.logger.debug(f"Initializing {self.name}")

        # Get required state_service
        self._state_service = self.orchestrator_ref.get_service("state_service")
        if not self._state_service:
            raise RuntimeError(f"{self.name}: state_service not available")

        self._system_prompt = self._load_system_prompt(app_home, self.config_provider)

        # Load identity text for plan prompts
        identity_path = Path(app_home) / "config" / "identity.json"
        if identity_path.exists():
            identity_data = json.loads(identity_path.read_text(encoding="utf-8"))
            identity_items = identity_data.get("identity", [])
            if isinstance(identity_items, list):
                self._identity_text = " ".join(str(item) for item in identity_items)
            self.logger.debug(f"Loaded identity: {len(self._identity_text)} chars")

        # Plan store adapter — delegates plan persistence to extracted module.
        self._plan_store = PlanStore(
            get_memory_service=self._get_memory_service,
            get_knowledge_service=self._get_knowledge_service,
            state_service=self._state_service,
            cursor_holder=self,
        )

        # Artifact/WBS authoring services are constructed PER CALL with a
        # session-bound focus manager (JOS-02) — see
        # _artifact_authoring_service / _wbs_authoring_service. The services
        # are stateless dependency holders; per-call construction mirrors the
        # _pull_execution_service precedent.

        # Acquire context_management_service (optional at startup — may be initialized later)
        self._acquire_context_services()

        self.set_ready()

    def _load_system_prompt(self, app_home: str, config_provider: ConfigProvider) -> str:
        """Resolve and load the reasoning system prompt.

        The operator override (profile-relative, set via
        ``system_prompt_path``) wins when present -- that is the intended
        per-solet customization path. Otherwise fall back to this
        plugin's own shipped default, which ships in every seed (unlike
        ``profile/config/``, which genesis excludes) so a fresh solet
        never silently runs with an empty planning prompt.
        """
        prompt_path_str = str(config_provider.get("system_prompt_path"))
        override_path = Path(app_home) / prompt_path_str
        shipped_path = _resolve_shipped_system_prompt_path()
        if override_path.exists():
            prompt = override_path.read_text(encoding="utf-8").rstrip("\n")
            self.logger.debug(f"Loaded operator-override system prompt: {len(prompt)} chars")
            return prompt
        if shipped_path.exists():
            prompt = shipped_path.read_text(encoding="utf-8").rstrip("\n")
            self.logger.debug(f"Loaded shipped default system prompt: {len(prompt)} chars")
            return prompt
        self.logger.warning(
            f"System prompt not found at either {override_path} or the shipped default "
            f"{shipped_path}, using empty"
        )
        return ""

    async def start_services(self) -> ActionResult:
        """Start services. Thinking plugin is stateless — just mark as started."""
        if self._services_started:
            return {"action_status": ActionStatus.COMPLETED.value}
        self._services_started = True
        self.logger.debug(f"{self.name}: services started")
        return {"action_status": ActionStatus.COMPLETED.value}

    async def stop_services(self) -> ActionResult:
        """Stop services. Thinking plugin holds no connections."""
        if not self._services_started:
            return {"action_status": ActionStatus.COMPLETED.value}
        if self.is_active_interface_provider():
            return {"action_status": ActionStatus.ERROR.value}
        self._services_started = False
        self.logger.debug(f"{self.name}: services stopped")
        return {"action_status": ActionStatus.COMPLETED.value}

    def _acquire_context_services(self) -> None:
        """Try to acquire context management services. Non-fatal if unavailable."""
        if self._context_management_service:
            return

        if not self.orchestrator_ref:
            return

        ctx_svc = self.orchestrator_ref.get_service("context_management_service")
        if ctx_svc and hasattr(ctx_svc, "registry"):
            from ananta.services.context_management.service import ContextManagementService

            if isinstance(ctx_svc, ContextManagementService):
                self._context_management_service = ctx_svc
                self._content_storage = ctx_svc.content_storage
                self.logger.debug("Context management services acquired")

    def _require_context_services(
        self,
    ) -> tuple[ContextManagementService, FileContextContentStorage]:
        """Validate and return required context services. Lazy acquisition."""
        if not self._context_management_service:
            self._acquire_context_services()

        if not self._context_management_service:
            raise FrameworkError(
                message="context_management_service not available",
                error_code=ErrorCode.CONTEXT_SERVICES_MISSING,
            )
        if not self._content_storage:
            raise FrameworkError(
                message="content_storage not available",
                error_code=ErrorCode.CONTEXT_SERVICES_MISSING,
            )
        return self._context_management_service, self._content_storage

    # ─────────────────────────────────────────────────────────────────────────
    # PLATFORM INFERENCE (DEP-01 Phase-2b — the plugin owns no model path)
    # ─────────────────────────────────────────────────────────────────────────

    def _generate_thinking_completion(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
    ) -> str:
        """Route an internal reasoning request through the inference service.

        The provider binding is the service's concern: on this low-level
        completion path the request serves on the BOUND provider (interim
        behavior, consistent with the operator's D.1 Option-B scoping of
        the autonomic lane to fault-degrade edges; INF-02 tracks an
        autonomic-routed completion surface). Provider-raised errors
        propagate loud; an envelope with no usable completion raises the
        typed ``inference_unusable``. There is no fallback model path in
        this plugin.
        """
        service = (
            self.orchestrator_ref.get_service("inference_service")
            if self.orchestrator_ref
            else None
        )
        if service is None:
            raise FrameworkError(
                message="inference_service not available for thinking completion",
                error_code=ErrorCode.BACKEND_NOT_AVAILABLE,
            )
        request = InferenceRequest(
            messages,
            temperature=_THINKING_COMPLETION_TEMPERATURE,
            max_tokens=_THINKING_COMPLETION_MAX_TOKENS,
            # Freeform planning/reasoning prose — no action-JSON schema.
            use_structured_output=False,
            context_metadata={"purpose": purpose},
        )
        self.logger.info(
            "THINKING COMPLETION via inference_service: purpose=%s messages=%d",
            purpose,
            len(messages),
        )
        result = service.generate_completion(request)
        completion = _extract_completion_text(result)
        if not completion:
            raise FrameworkError(
                message=(
                    f"inference_service returned no usable completion for "
                    f"{purpose!r} — the provider envelope carried an error, "
                    f"a non-completed status, or empty text"
                ),
                error_code=ErrorCode.INFERENCE_UNUSABLE,
            )
        return completion

    # ─────────────────────────────────────────────────────────────────────────
    # CONTEXT EVENT HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _append_context_event(
        self,
        context_id: str,
        content: str,
        event_type: ContextEventType,
        actor_type: ContextActorType,
    ) -> None:
        """Store an event in the task's context stream."""
        ctx_svc, storage = self._require_context_services()

        content_path, char_count = storage.store_event(context_id, content)
        result = ctx_svc.events.append_event(
            context_id=context_id,
            event_type=event_type.value,
            actor_type=actor_type.value,
            content_path=content_path,
            content_char_count=char_count,
            actor_id=self.name,
        )
        if result.get("action_status") != ActionStatus.COMPLETED.value:
            raise FrameworkError(
                message=f"Failed to append context event: {result.get('error')}",
                error_code=f"{PLUGIN_NAME}.event_append_failed",
            )

    def _load_context_messages(self, context_id: str) -> list[dict[str, str]]:
        """Load conversation history from a task's context as messages."""
        ctx_svc, storage = self._require_context_services()
        messages: list[dict[str, str]] = []

        # Check for snapshot first
        snapshot = ctx_svc.snapshots.get_latest_snapshot(context_id)
        if snapshot:
            summary_path = str(snapshot.get("summary_path", ""))
            if summary_path:
                summary_content = storage.read_text(summary_path)
                messages.append({"role": "system", "content": f"[Prior context summary]\n{summary_content}"})
            end_event_id = str(snapshot.get("end_event_id", ""))
            events = ctx_svc.events.list_events_after_snapshot(context_id, end_event_id)
        else:
            events = ctx_svc.events.list_all_events(context_id)

        conversation_types = {ContextEventType.INPUT.value, ContextEventType.OUTPUT.value}
        for event in events:
            event_type_str = str(event.get("event_type", ""))
            if event_type_str not in conversation_types:
                continue

            content_path = str(event.get("content_path", ""))
            if not content_path:
                continue

            content = storage.read_text(content_path)
            # Map event type string to message role
            if event_type_str == ContextEventType.INPUT.value:
                role = "user"
            elif event_type_str == ContextEventType.OUTPUT.value:
                role = "assistant"
            else:
                role = "user"
            messages.append({"role": role, "content": content})

        return messages

    # ─────────────────────────────────────────────────────────────────────────
    # TASK DB HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _get_task(self, task_id: str) -> dict[str, Any]:
        """Get a task record by ID. Raises on not found."""
        result = self._state_service.read_state(
            namespace=NAMESPACE,
            query={"table": "thinking_task", "filters": {"id": task_id, "is_deleted": 0}},
        )
        rows = result.get("data", {}).get("records", [])
        if not rows:
            raise FrameworkError(
                message=f"Task not found: {task_id}",
                error_code=ErrorCode.TASK_NOT_FOUND,
            )
        return dict(rows[0])

    def _save_task(self, record: dict[str, Any]) -> str:
        """Save a task record. Returns the task ID."""
        if "id" in record and record["id"]:
            result = self._state_service.upsert_state(
                namespace=NAMESPACE,
                data={"table": "thinking_task", "record": record, "conflict_columns": ["id"]},
            )
        else:
            result = self._state_service.write_state(
                namespace=NAMESPACE,
                data={"table": "thinking_task", "record": record},
            )

        if result.get("action_status") != ActionStatus.COMPLETED.value:
            raise FrameworkError(
                message=f"Failed to save task: {result.get('error', 'unknown')}",
                error_code=ErrorCode.OPERATION_FAILED,
            )

        # Extract generated ID
        data = result.get("data", {})
        if isinstance(data, dict):
            result_data = data.get("result", {})
            if isinstance(result_data, dict):
                generated_id = result_data.get("generated_id")
                if isinstance(generated_id, str):
                    return generated_id

        return str(record.get("id", ""))

    # ─────────────────────────────────────────────────────────────────────────
    # THINKING SERVICE INTERFACE IMPLEMENTATION
    # ─────────────────────────────────────────────────────────────────────────

    def create_task(
        self,
        title: str,
        prompt: str,
        task_type: str = "plan",
        messages: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Create a thinking task with its own context.

        Args:
            title: Task title
            prompt: User prompt (also stored as INPUT event)
            task_type: Type of task (plan, analysis, deliberation)
            messages: Optional pre-built message list. When provided, used instead
                of default [system, user] construction. Allows callers to build
                multi-message prompts (e.g., system + identity + assistant + user).
        """
        if not title:
            raise FrameworkError(
                message="title is required",
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        if not prompt:
            raise FrameworkError(
                message="prompt is required",
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        if task_type not in VALID_TASK_TYPES:
            raise FrameworkError(
                message=f"Invalid task_type: {task_type}. Must be one of: {', '.join(sorted(VALID_TASK_TYPES))}",
                error_code=ErrorCode.PARAMETER_ERROR,
            )

        # Create context for this task
        ctx_svc, _ = self._require_context_services()
        context_id = ctx_svc.registry.create_context(
            context_type="task",
            label=f"thinking: {title}",
            metadata={"plugin": self.name, "task_type": task_type},
        )

        # Use pre-built messages if provided, otherwise default construction
        if messages is None:
            messages = []
            if self._system_prompt:
                messages.append({"role": "system", "content": self._system_prompt})
            messages.append({"role": "user", "content": prompt})

        # Store INPUT event
        self._append_context_event(context_id, prompt, ContextEventType.INPUT, ContextActorType.HUMAN)

        # Route reasoning through the platform inference service
        completion = self._generate_thinking_completion(
            messages, purpose="thinking_task_create",
        )

        # Store OUTPUT event
        self._append_context_event(context_id, completion, ContextEventType.OUTPUT, ContextActorType.AGENT)

        # Save task record
        task_id = self._save_task(
            {
                "title": title,
                "task_type": task_type,
                "status": STATUS_ACTIVE,
                "context_id": context_id,
                "latest_response": completion,
            }
        )

        return {
            "task_id": task_id,
            "context_id": context_id,
            "task_type": task_type,
            "response": completion,
        }

    def continue_task(self, task_id: str, prompt: str) -> dict[str, Any]:
        """Continue reasoning on an existing task."""
        if not prompt:
            raise FrameworkError(
                message="prompt is required",
                error_code=ErrorCode.PARAMETER_ERROR,
            )

        task = self._get_task(task_id)
        context_id = str(task["context_id"])

        # Load history from context
        history_messages = self._load_context_messages(context_id)

        # Build messages: system prompt + history + new prompt
        messages: list[dict[str, str]] = []
        if self._system_prompt:
            messages.append({"role": "system", "content": self._system_prompt})
        messages.extend(history_messages)
        messages.append({"role": "user", "content": prompt})

        # Store INPUT event
        self._append_context_event(context_id, prompt, ContextEventType.INPUT, ContextActorType.HUMAN)

        # Route reasoning through the platform inference service
        completion = self._generate_thinking_completion(
            messages, purpose="thinking_task_continue",
        )

        # Store OUTPUT event
        self._append_context_event(context_id, completion, ContextEventType.OUTPUT, ContextActorType.AGENT)

        # Update task record
        task["latest_response"] = completion
        self._save_task(task)

        return {
            "task_id": task_id,
            "response": completion,
        }

    def get_task(self, task_id: str) -> dict[str, Any]:
        """Get task metadata and latest response."""
        task = self._get_task(task_id)
        return {
            "task_id": task.get("id", task_id),
            "title": task.get("title", ""),
            "task_type": task.get("task_type", ""),
            "status": task.get("status", ""),
            "context_id": task.get("context_id", ""),
            "memory_id": task.get("memory_id"),
            "latest_response": task.get("latest_response"),
            "created_at": task.get("created_at", ""),
            "updated_at": task.get("updated_at", ""),
        }

    def list_tasks(
        self,
        task_type: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """List thinking tasks with optional filters."""
        filters: dict[str, Any] = {"is_deleted": 0}
        if task_type:
            if task_type not in VALID_TASK_TYPES:
                raise FrameworkError(
                    message=f"Invalid task_type: {task_type}",
                    error_code=ErrorCode.PARAMETER_ERROR,
                )
            filters["task_type"] = task_type
        if status:
            if status not in VALID_STATUSES:
                raise FrameworkError(
                    message=f"Invalid status: {status}",
                    error_code=ErrorCode.PARAMETER_ERROR,
                )
            filters["status"] = status

        result = self._state_service.read_state(
            namespace=NAMESPACE,
            query={"table": "thinking_task", "filters": filters},
        )
        rows = result.get("data", {}).get("records", [])

        tasks = [
            {
                "task_id": row.get("id", ""),
                "title": row.get("title", ""),
                "task_type": row.get("task_type", ""),
                "status": row.get("status", ""),
                "created_at": row.get("created_at", ""),
            }
            for row in rows
        ]

        return {"tasks": tasks, "count": len(tasks)}

    def archive_task(self, task_id: str) -> dict[str, Any]:
        """Archive a completed or abandoned task."""
        task = self._get_task(task_id)
        task["status"] = STATUS_COMPLETED
        self._save_task(task)

        return {
            "task_id": task_id,
            "message": f"Archived task: {task.get('title', task_id)}",
        }

    # ─────────────────────────────────────────────────────────────────────────
    # MEMORY SERVICE HELPER
    # ─────────────────────────────────────────────────────────────────────────

    def _get_memory_service(self) -> Any:
        """Get memory service from orchestrator. Fails fast."""
        if not self.orchestrator_ref:
            raise FrameworkError(
                message="orchestrator_ref not available",
                error_code=ErrorCode.BACKEND_NOT_AVAILABLE,
            )
        svc = self.orchestrator_ref.get_service("memory_service")
        if not svc:
            raise FrameworkError(
                message="memory_service not available",
                error_code=ErrorCode.BACKEND_NOT_AVAILABLE,
            )
        return svc

    def _session_memory(self, session_id: str) -> SessionScopedMemory:
        """The acting session's view of the focus surface (JOS-02).

        Every focus-buffer read/write in this plugin goes through a bound
        view so writes cannot drift across sessions; construction fails fast
        on an empty session.
        """
        return SessionScopedMemory(
            memory_service=self._get_memory_service(),
            session_id=session_id,
        )

    # ── Per-session plan-cursor guards (JOS-02) ───────────────────────

    def get_presented_plan_cursor(self, session_id: str) -> str:
        """The plan cursor most recently presented to *session_id*."""
        return self._plan_cursors.get(session_id, "")

    def set_presented_plan_cursor(self, session_id: str, cursor: str) -> None:
        """Record the presented plan cursor for *session_id* (PlanStore seam)."""
        if cursor:
            self._plan_cursors[session_id] = cursor
        else:
            self._plan_cursors.pop(session_id, None)

    def _get_discovery_service(self) -> Any:
        """Get discovery service from orchestrator. Fails fast."""
        if not self.orchestrator_ref:
            raise FrameworkError(
                message="orchestrator_ref not available",
                error_code=ErrorCode.BACKEND_NOT_AVAILABLE,
            )
        svc = self.orchestrator_ref.get_service("discovery_service")
        if not svc:
            raise FrameworkError(
                message="discovery_service not available",
                error_code=ErrorCode.BACKEND_NOT_AVAILABLE,
            )
        return svc

    def _get_knowledge_service(self) -> Any | None:
        """Get knowledge service from orchestrator, if available."""
        if not self.orchestrator_ref:
            return None
        return self.orchestrator_ref.get_service("knowledge_service")

    # ─────────────────────────────────────────────────────────────────────────
    # PLAN KNOWLEDGE BASE STORAGE
    # ─────────────────────────────────────────────────────────────────────────
    def _write_plan_to_knowledge_base(self, plan_id: str, plan_content: str) -> str | None:
        """Delegate to PlanStore."""
        return self._plan_store.write_plan_to_knowledge_base(plan_id, plan_content)

    def _read_plan_from_kb(self, kb_path: str) -> str | None:
        """Delegate to PlanStore."""
        return self._plan_store.read_plan_from_kb(kb_path)

    @staticmethod
    def _extract_kb_path(plan_item: dict[str, Any]) -> str | None:
        """Delegate to plan_store module function."""
        from .plan_store import extract_kb_path

        return extract_kb_path(plan_item)

    @staticmethod
    def _set_kb_path_tag(tags: list[str], kb_path: str) -> list[str]:
        """Delegate to plan_store module function."""
        from .plan_store import set_kb_path_tag

        return set_kb_path_tag(tags, kb_path)

    def _write_playbook_to_knowledge_base(
        self,
        playbook_id: str,
        content: str,
    ) -> str | None:
        """Write or update a playbook in the thinking_playbooks knowledge base.

        Creates a date-organized file: ``playbooks/<YYYY-MM-DD>/<playbook_id>.md``.
        On subsequent calls for the same playbook, overwrites via ``edit_file``.

        Returns the knowledge base path on success, ``None`` on failure.
        """
        knowledge_service = self._get_knowledge_service()
        if knowledge_service is None:
            self.logger.warning("Knowledge service unavailable — playbook not written")
            return None

        date_str = datetime.date.today().isoformat()
        path = f"playbooks/{date_str}/{playbook_id}.md"

        try:
            knowledge_service.create_file(
                name=KB_THINKING_PLAYBOOKS,
                path=path,
                content=content,
            )
            self.logger.info("Playbook created in knowledge base: %s/%s", KB_THINKING_PLAYBOOKS, path)
            return path
        except FileNotFoundError:
            self.logger.warning("%s knowledge base not installed — skipping", KB_THINKING_PLAYBOOKS)
            return None
        except FileExistsError:
            return self._update_playbook_in_knowledge_base(knowledge_service, path, content)
        except Exception:
            self.logger.exception("Failed to write playbook to knowledge base: %s", path)
            return None

    def _update_playbook_in_knowledge_base(
        self,
        knowledge_service: Any,
        path: str,
        content: str,
    ) -> str | None:
        """Overwrite an existing playbook file in the knowledge base."""
        try:
            knowledge_service.edit_file(
                name=KB_THINKING_PLAYBOOKS,
                path=path,
                content=content,
            )
            self.logger.info("Playbook updated in knowledge base: %s/%s", KB_THINKING_PLAYBOOKS, path)
            return path
        except Exception:
            self.logger.exception("Failed to update playbook in knowledge base: %s", path)
            return None

    def _read_playbook(self, playbook_id: str) -> str:
        """Read a playbook from the knowledge base by playbook ID.

        Looks up the ``knowledge_base_path`` from the ``thinking_playbook``
        table and reads the file content.

        Raises:
            FrameworkError: If the playbook is not found or not readable.
        """
        record = self._get_playbook_record(playbook_id)
        kb_path = str(record.get("knowledge_base_path", ""))
        if not kb_path:
            raise FrameworkError(
                message=f"Playbook {playbook_id} has no knowledge base path",
                error_code=ErrorCode.PLAYBOOK_NOT_FOUND,
            )

        knowledge_service = self._get_knowledge_service()
        if knowledge_service is None:
            raise FrameworkError(
                message="Knowledge service unavailable",
                error_code=ErrorCode.BACKEND_NOT_AVAILABLE,
            )

        try:
            result = knowledge_service.get_file(name=KB_THINKING_PLAYBOOKS, path=kb_path)
            content: str = result.get("content", "")
            if not content:
                raise FrameworkError(
                    message=f"Playbook {playbook_id} is empty at {kb_path}",
                    error_code=ErrorCode.PLAYBOOK_NOT_FOUND,
                )
            return content
        except FrameworkError:
            raise
        except Exception as exc:
            raise FrameworkError(
                message=f"Failed to read playbook {playbook_id}: {exc}",
                error_code=ErrorCode.OPERATION_FAILED,
            ) from exc

    def _read_playbook_section(self, playbook_id: str, section_id: str) -> str:
        """Read a single section from a playbook by playbook ID and section ID.

        Raises:
            FrameworkError: If the playbook or section is not found.
        """
        content = self._read_playbook(playbook_id)
        try:
            return extract_section(content, section_id)
        except ValueError as exc:
            raise FrameworkError(
                message=f"Playbook {playbook_id}: {exc}",
                error_code=ErrorCode.SECTION_NOT_FOUND,
            ) from exc

    def _get_playbook_record(self, playbook_id: str) -> dict[str, Any]:
        """Look up a playbook row by ID. Fail-fast if not found."""
        result = self._state_service.read_state(
            namespace=NAMESPACE,
            query={
                "table": "thinking_playbook",
                "filters": {"id": playbook_id, "is_deleted": 0},
            },
        )
        records = result.get("data", {}).get("records", [])
        if not records:
            raise FrameworkError(
                message=f"Playbook not found: {playbook_id}",
                error_code=ErrorCode.PLAYBOOK_NOT_FOUND,
            )
        return dict(records[0])

    @staticmethod
    def _format_article_block(article: object, index: int) -> tuple[str, str] | None:
        if not isinstance(article, dict):
            return None
        content = article.get("content")
        if not isinstance(content, str) or not content.strip():
            return None

        source = str(article.get("file_path", "unknown"))
        knowledge_base_name = str(article.get("knowledge_base", "unknown"))
        score = article.get("score")
        raw_tags = article.get("article_tags", [])
        role = article.get("article_role", "reference")

        score_str = f", score: {score:.2f}" if isinstance(score, float) else ""
        # Strip knowledge:tag: prefix for compact display
        display_tags = [t.removeprefix("knowledge:tag:") for t in raw_tags] if isinstance(raw_tags, list) else []
        tags_str = f", tags: {', '.join(display_tags)}" if display_tags else ""

        header = f"ARTICLE {index} (role: {role}, source: {knowledge_base_name}/{source}{score_str}{tags_str}):"
        clipped = content.strip()[:8000]
        return f"{header}\n\n{clipped}", clipped

    def _search_planning_references(self, query: str) -> list[Any] | None:
        """Search knowledge base for planning references. Returns results list or None."""
        knowledge_service = self._get_knowledge_service()
        if not knowledge_service:
            return None

        try:
            result = knowledge_service.search(
                query=query,
                top_k=5,
                tags=["knowledge:tag:planning_reference"],
                min_score=0.60,
            )
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"KB planning reference retrieval failed: {e}")
            return None

        if not isinstance(result, dict):
            return None

        results = result.get("results", [])
        if not isinstance(results, list) or not results:
            return None
        return results

    def _build_knowledge_base_context(self, goal: str, topic: str | None = None) -> str:
        """Fetch planning reference articles from knowledge base for the thinking model.

        Searches for planning patterns, reference plans, and process contracts.
        Formats results as article sections with provenance headers, followed by
        a PROCESS CONTRACTS section extracted from the articles.

        Args:
            goal: The user's full goal (fallback search query).
            topic: Domain topic (preferred search query).

        Returns empty string if knowledge service unavailable or no results.
        """
        results = self._search_planning_references(topic or goal)
        if not results:
            return ""

        intro = "The following reference material was retrieved from the knowledge base."
        article_blocks: list[str] = []
        all_article_content: list[str] = []

        for i, article in enumerate(results, 1):
            formatted = DefaultThinkingPlugin._format_article_block(article, i)
            if formatted is None:
                continue
            block, clipped = formatted
            article_blocks.append(block)
            all_article_content.append(clipped)

        if not article_blocks:
            return ""

        # Join: intro \n\n first_article --- second_article --- PROCESS CONTRACTS
        body_sections = list(article_blocks)

        # Append PROCESS CONTRACTS section extracted from knowledge base articles
        contracts = self._extract_process_contracts(all_article_content)
        if contracts:
            body_sections.append(contracts)

        return intro + "\n\n" + "\n\n---\n\n".join(body_sections)

    @staticmethod
    def _extract_arg_properties(process_data: dict[str, Any]) -> tuple[dict[str, Any], set[str]] | None:
        invocation = process_data.get("invocation_schema", {})
        if not isinstance(invocation, dict):
            return None
        outer_props = invocation.get("properties", {})
        if not isinstance(outer_props, dict):
            return None
        args_schema = outer_props.get("arguments", {})
        if not isinstance(args_schema, dict):
            return None
        arg_props = args_schema.get("properties", {})
        if not isinstance(arg_props, dict):
            return None
        required_raw = args_schema.get("required", [])
        required_names: set[str] = set(required_raw) if isinstance(required_raw, list) else set()
        return arg_props, required_names

    @staticmethod
    def _format_param_line(name: str, prop: object, required_names: set[str]) -> str:
        if not isinstance(prop, dict):
            return name
        param_type = prop.get("type", "string")
        req = " REQUIRED" if name in required_names else ""
        desc = prop.get("description", "")
        brief = f" — {desc}" if desc else ""
        return f"  {name} ({param_type}{req}){brief}"

    def _extract_process_contracts(self, article_contents: list[str]) -> str:
        """Extract process keys from KB articles and build a PROCESS CONTRACTS section.

        Scans article content for fully-qualified process keys
        (provider_type::provider::function_name), looks up each in the discovery
        service to get parameter names, and formats a compact reference.

        Returns empty string if no process keys found or discovery unavailable.
        """
        # Extract all process keys from article content
        combined = "\n".join(article_contents)
        process_key_pattern = re.compile(r"(?:plugin|service_interface)::\w+::\w+")
        found_keys: list[str] = list(dict.fromkeys(process_key_pattern.findall(combined)))

        if not found_keys:
            return ""

        try:
            discovery_service = self._get_discovery_service()
        except FrameworkError:
            return ""

        lines: list[str] = ["PROCESS CONTRACTS:"]

        for process_key in found_keys:
            process_data = discovery_service.get_process_by_key(process_key)
            if not process_data:
                continue

            extracted = DefaultThinkingPlugin._extract_arg_properties(process_data)
            if extracted is None:
                continue
            arg_props, required_names = extracted

            # Exclude platform-internal fields from contracts
            _EXCLUDED_PARAMS = {"state", "session_id", "job_result_ref"}
            params: list[str] = []
            for name, prop in arg_props.items():
                if name in _EXCLUDED_PARAMS:
                    continue
                params.append(DefaultThinkingPlugin._format_param_line(name, prop, required_names))

            if params:
                lines.append(f"\n{process_key}:")
                lines.extend(params)

        return "\n".join(lines) if len(lines) > 1 else ""

    def _build_plan_messages(
        self,
        goal: str,
        topic: str | None = None,
        context: str | None = None,
    ) -> list[dict[str, str]]:
        """Build messages for plan generation.

        Structure:
        - system(rules), system(identity), assistant(kb_context),
          [assistant(prior_observation)], user(goal)

        The knowledge base context provides reference plans, process contracts,
        and workflow patterns as grounding material for the thinking model.

        Args:
            goal: The user's full goal text.
            topic: Domain topic for knowledge base search.
            context: Prior observation from the vertex that preceded create_extended_plan
                (e.g., knowledge base search results). Injected as assistant
                message so the planner sees what the model learned before deciding
                to plan.
        """
        messages: list[dict[str, str]] = []

        # 1. System prompt (rules)
        if self._system_prompt:
            messages.append({"role": "system", "content": self._system_prompt})

        # 2. Identity
        if self._identity_text:
            messages.append({"role": "system", "content": self._identity_text})

        # 3. Knowledge base context (assistant role to avoid cache-breaking rehoisting)
        kb_context = self._build_knowledge_base_context(goal, topic=topic)
        if kb_context:
            messages.append({"role": "assistant", "content": kb_context})

        # 4. Prior observation context from the calling vertex
        if context:
            messages.append({"role": "assistant", "content": context})

        # 5. User goal (just the goal, no registry dump or rules)
        messages.append({"role": "user", "content": goal})

        return messages

    def _is_memory_focused(self, memory_id: str, memory_service: Any) -> bool:
        """Check if a memory is in the focus buffer."""
        focused: list[dict[str, Any]] = memory_service.get_focused()["memories"]
        return any(m.get("memory_id") == memory_id for m in focused)

    def _safe_unfocus(self, memory_id: str, memory_service: Any) -> bool:
        """Delegate to PlanStore."""
        return self._plan_store.safe_unfocus(memory_id, memory_service)

    def _load_full_plan_content(
        self,
        existing_plan: dict[str, Any],
        plan_id: str,
        extract_kb_path: Any,
    ) -> str:
        """Load the full plan text from KB (Tier 1) with focused-memory fallback."""
        kb_path = extract_kb_path(existing_plan)
        if kb_path:
            kb_content = self._plan_store.read_plan_from_kb(kb_path)
            if kb_content is not None:
                return kb_content
            self.logger.error(
                "PLAN_ADVANCE: KB read failed (kb_path=%s, plan_id=%s). Falling back to focused memory content.",
                kb_path, plan_id,
            )
        else:
            self.logger.error(
                "PLAN_ADVANCE: Two-tier invariant broken — focused plan has no kb_path tag (plan_id=%s). Advancing from compact focused window may lose step details.",
                plan_id,
            )
        return str(existing_plan.get("content", ""))

    def advance_current_plan_step(
        self, *, session_id: str,
    ) -> dict[str, Any] | None:
        """Platform-owned plan marker advancement (two-tier).

        Called by the inference plugin at the start of continuation VERTEXes.
        Marks the current ``[>]`` step as ``[X]`` and activates the next
        executable step as ``[>]``.  This is pure bookkeeping — the model
        never calls this directly.

        **Two-tier storage**: reads the FULL plan from the knowledge base
        (Tier 1) for advancement, then writes the advanced full plan back
        to knowledge base and a compact excerpt to focused memory (Tier 2).
        This prevents large plans from bloating the model context.

        A per-session cursor guard (``_plan_cursors``, JOS-02) ensures that a
        newly presented active step skips one advancement cycle. This covers
        freshly created/replaced plans and same-plan grafts that shift the
        active frontier to a new step.
        """
        from .plan_store import extract_kb_path, extract_plan_id, plan_cursor

        memory_service = self._session_memory(session_id)
        existing_plan = self._plan_store.find_focused_plan(memory_service)
        if not existing_plan:
            return None

        plan_id = extract_plan_id(existing_plan)
        full_content = self._load_full_plan_content(existing_plan, plan_id, extract_kb_path)

        parsed_existing = parse(full_content)
        current_cursor = plan_cursor(plan_id, parsed_existing)

        # Graft guard: after a graft, skip the next N advancement calls
        # to prevent the first execution step from being auto-completed
        # before the model has a chance to execute it. Update the cursor
        # tracker so the post-graft cursor change is recorded as
        # acknowledged here — otherwise the cursor-mismatch guard below
        # would fire a redundant second skip on the next call after the
        # model has already acted on the new cursor.
        if self._graft_skips.get(session_id, 0) > 0:
            self._graft_skips[session_id] -= 1
            self.set_presented_plan_cursor(session_id, current_cursor)
            self.logger.info(
                "PLAN_ADVANCE: Skipped — graft guard active (%d remaining)",
                self._graft_skips[session_id],
            )
            return None

        if current_cursor != self.get_presented_plan_cursor(session_id):
            self.set_presented_plan_cursor(session_id, current_cursor)
            self.logger.info(
                "PLAN_ADVANCE: Skipped — new active cursor %s",
                current_cursor or "<none>",
            )
            return None

        advanced_content = advance_plan_markers(full_content)
        if not advanced_content:
            if parsed_existing.is_complete:
                self.logger.info(
                    "PLAN_ADVANCE: Plan %s is complete — all steps done, defocusing",
                    plan_id,
                )
                self._defocus_documents_by_label("active_plan", memory_service)
                return None
            self.logger.info(
                "PLAN_ADVANCE: No advancement possible (plan_id=%s, content_len=%d, current_step=%s)",
                plan_id,
                len(full_content),
                next(
                    (line.strip()[:50] for line in full_content.split("\n") if line.strip().startswith("[>]")),
                    "no [>] found",
                ),
            )
            return None

        advanced_parsed = parse(advanced_content)
        self.set_presented_plan_cursor(
            session_id, plan_cursor(plan_id, advanced_parsed),
        )
        raw_plan_text = render_plan_steps(advanced_parsed)

        self.logger.info(
            "PLAN_ADVANCE: Advancing plan %s",
            plan_id,
        )
        return self._plan_store.upsert_existing_plan(
            raw_plan_text,
            existing_plan,
            memory_service,
        )

    @staticmethod
    def _extract_plan_id(plan_item: dict[str, Any]) -> str:
        """Extract plan_id from a focused plan item's tags."""
        for tag in plan_item.get("tags", []):
            if isinstance(tag, str) and tag.startswith("plan:pln-"):
                return tag[len("plan:") :]
        return ""

    @staticmethod
    def _plan_cursor(plan_id: str, parsed_plan: Any) -> str:
        """Return a stable cursor for the currently presented active step."""
        current_step = parsed_plan.current_step_number
        if current_step is None:
            current_step = parsed_plan.first_executable_step_number
        if current_step is None:
            return plan_id
        return f"{plan_id}:{current_step}"

    def upsert_plan(self, content: str, *, session_id: str) -> dict[str, Any]:
        """Write or replace the acting session's plan content directly.

        No inference — just storage. Replaces ␤ (U+2424) with real newlines
        before storage. Creates a new plan if none exists, or updates the
        existing active plan's memory content in place. Focus is
        session-scoped (JOS-02) — the plan installs into the acting
        session's buffer only.

        The platform derives the current step from the existing focused plan.
        The model does not supply a step number.
        """
        if not content:
            raise FrameworkError(
                message="content is required",
                error_code=ErrorCode.PARAMETER_ERROR,
            )

        plan_text = normalize_content(content)
        memory_service = self._session_memory(session_id)
        existing_plan = self._plan_store.find_focused_plan(memory_service)

        if existing_plan:
            return self._plan_store.upsert_into_existing(
                plan_text,
                existing_plan,
                memory_service,
            )

        return self._plan_store.install_first_plan(plan_text, memory_service)

    def _find_focused_plan(
        self,
        memory_service: Any,
    ) -> dict[str, Any] | None:
        """Delegate to PlanStore."""
        return self._plan_store.find_focused_plan(memory_service)

    def _upsert_into_existing(
        self,
        plan_text: str,
        existing_plan: dict[str, Any],
        memory_service: Any,
    ) -> dict[str, Any]:
        """Delegate to PlanStore."""
        return self._plan_store.upsert_into_existing(plan_text, existing_plan, memory_service)

    def _replace_plan(
        self,
        parsed_submitted: Any,
        existing_plan: dict[str, Any],
        memory_service: Any,
    ) -> dict[str, Any]:
        """Delegate to PlanStore."""
        return self._plan_store.replace_plan(parsed_submitted, existing_plan, memory_service)

    def _install_first_plan(self, plan_text: str, memory_service: Any) -> dict[str, Any]:
        """Delegate to PlanStore."""
        return self._plan_store.install_first_plan(plan_text, memory_service)

    def _upsert_existing_plan(
        self,
        plan_text: str,
        existing_plan: dict[str, Any],
        memory_service: Any,
    ) -> dict[str, Any]:
        """Delegate to PlanStore."""
        return self._plan_store.upsert_existing_plan(plan_text, existing_plan, memory_service)

    def _complete_plan(self, plan_id: str, memory_id: str, memory_service: Any) -> dict[str, Any]:
        """Delegate to PlanStore."""
        return self._plan_store.complete_plan(plan_id, memory_id, memory_service)

    def _upsert_new_plan(self, plan_text: str, memory_service: Any) -> dict[str, Any]:
        """Delegate to PlanStore."""
        return self._plan_store.upsert_new_plan(plan_text, memory_service)

    def create_extended_plan(
        self,
        goal: str,
        topic: str | None = None,
        context: str | None = None,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Compile a detailed multi-step execution plan.

        The plan is stored in two places:
        1. **Knowledge base** — full plan with arguments, chunked by ``## Step``
           headers so each step's action definition is individually retrievable.
        2. **Focused memory** — compact summary (no arguments) for navigation,
           pinned in the ACTING session's buffer (JOS-02).
           The model retrieves step arguments from the knowledge base on demand.
        """
        if not goal:
            raise FrameworkError(
                message="goal is required",
                error_code=ErrorCode.PARAMETER_ERROR,
            )

        memory_service = self._session_memory(session_id)

        # 1. Generate plan via thinking model
        plan_messages = self._assemble_plan_prompt(goal, topic=topic, context=context)
        self.logger.info(f"Plan messages: {len(plan_messages)} messages for goal: {goal[:80]}")
        task_result = self.create_task(
            title=goal,
            prompt=goal,
            task_type=TASK_TYPE_PLAN,
            messages=plan_messages,
        )
        task_id: str = task_result["task_id"]
        plan_content: str = task_result["response"]
        step_count = count_plan_steps(plan_content)

        # 2. Generate plan ID and write full plan to knowledge base.
        #    Each ## Step section becomes a retrievable chunk with arguments.
        plan_id = self._state_service.generate_id(prefix="pln-")
        kb_path = self._write_plan_to_knowledge_base(plan_id, plan_content)

        # 3. Store compact summary (no arguments) as focused memory (embed=False).
        #    Set kb_path tag to maintain the two-tier invariant: focused plan
        #    can always trace back to its full KB source.
        plan_summary = generate_plan_summary(plan_content, step_count)
        tags = ["plan", f"plan:{task_id}", f"plan:{plan_id}"]
        if kb_path:
            tags = self._set_kb_path_tag(tags, kb_path)
        remember_result: dict[str, Any] = memory_service.remember(
            content=plan_summary,
            tags=tags,
            embed=False,
        )
        memory_id: str = remember_result.get("memory_id", "")

        # 4. Focus plan (unfocus existing plans first, then focus new one).
        #    Plans are always focused — an unfocused plan is invisible in
        #    subsequent turns and cannot be executed via the ReAct loop.
        focused = self._focus_plan(memory_id, memory_service)

        # 5. Update task record with memory_id and plan_id
        task = self._get_task(task_id)
        task["memory_id"] = memory_id
        task["plan_id"] = plan_id
        self._save_task(task)

        return {
            "plan_id": plan_id,
            "task_id": task_id,
            "memory_id": memory_id,
            "focused": focused,
            "step_count": step_count,
            "kb_written": bool(kb_path),
        }

    def _focus_plan(self, memory_id: str, memory_service: Any) -> bool:
        """Delegate to PlanStore."""
        return self._plan_store.focus_plan(memory_id, memory_service)

    def _focus_artifact(self, memory_id: str, memory_service: Any) -> bool:
        """Focus a non-plan artifact without evicting the active plan.

        Artifact creation turns need the durable artifact and the active plan
        in focus at the same time so the next result-processor VERTEX can
        inject both the artifact content and the ``ACTIVE_PLAN`` window.

        If the focus buffer is full, evict older non-plan focused memories
        before retrying. Plans are preserved.
        """
        if not memory_id:
            return False

        if self._try_focus(memory_id, memory_service):
            return True

        # Focus buffer full: free non-plan slots only.
        return self._evict_and_focus_artifact(memory_id, memory_service)

    def _evict_and_focus_artifact(
        self,
        memory_id: str,
        memory_service: Any,
    ) -> bool:
        """Evict non-plan focused items to make room for an artifact."""
        focused_items: list[dict[str, Any]] = memory_service.get_focused()["memories"]
        evictable_ids = [str(item.get("memory_id", "")) for item in focused_items if "plan" not in item.get("tags", []) and str(item.get("memory_id", "")) != memory_id]
        for old_id in evictable_ids:
            if old_id:
                self._safe_unfocus(old_id, memory_service)
            if self._try_focus(memory_id, memory_service):
                return True
        raise RuntimeError(
            f"Focus buffer full: cannot focus artifact {memory_id} after "
            f"evicting {len(evictable_ids)} non-plan item(s). "
            f"{len(focused_items)} item(s) remain focused."
        )

    def _try_focus(self, memory_id: str, memory_service: Any) -> bool:
        """Attempt to focus a memory, returning False on failure."""
        try:
            memory_service.focus(memory_id)
            return True
        except FrameworkError:
            return False

    def update_plan(
        self, task_id: str, status_update: str, *, session_id: str,
    ) -> dict[str, Any]:
        """Report progress and get updated plan reasoning."""
        if not status_update:
            raise FrameworkError(
                message="status_update is required",
                error_code=ErrorCode.PARAMETER_ERROR,
            )

        memory_service = self._session_memory(session_id)

        # 1. Load task and check memory/focus state
        task = self._get_task(task_id)
        if task.get("task_type") != TASK_TYPE_PLAN:
            raise FrameworkError(
                message=f"Task {task_id} is not a plan (type: {task.get('task_type')})",
                error_code=ErrorCode.PARAMETER_ERROR,
            )

        old_memory_id: str = str(task.get("memory_id") or "")
        was_focused = bool(old_memory_id) and self._is_memory_focused(old_memory_id, memory_service)

        # 2. Continue reasoning
        continue_result = self.continue_task(task_id, status_update)
        updated_content: str = continue_result["response"]

        # 3. Update memory: forget old, remember new
        new_memory_id = old_memory_id
        if old_memory_id:
            if was_focused:
                memory_service.unfocus(old_memory_id)
            memory_service.forget(old_memory_id)

        remember_result: dict[str, Any] = memory_service.remember(
            content=updated_content,
            tags=["plan", f"plan:{task_id}"],
            embed=False,
        )
        new_memory_id = remember_result.get("memory_id", "")

        # 4. Re-focus if was focused
        refocused = False
        if was_focused and new_memory_id:
            memory_service.focus(new_memory_id)
            refocused = True

        # 5. Update task record
        task["memory_id"] = new_memory_id
        self._save_task(task)

        return {
            "plan_id": task_id,
            "memory_id": new_memory_id,
            "refocused": refocused,
            "step_count": count_plan_steps(updated_content),
            "content": updated_content,
        }

    @staticmethod
    def _row_to_plan_summary(
        row: dict[str, Any],
        focused_ids: set[str],
    ) -> tuple[dict[str, Any], bool]:
        memory_id = str(row.get("memory_id") or "")
        is_focused = memory_id in focused_ids if memory_id else False
        latest_response = str(row.get("latest_response") or "")
        plan_dict: dict[str, Any] = {
            "plan_id": row.get("id", ""),
            "title": row.get("title", ""),
            "status": row.get("status", ""),
            "memory_id": memory_id or None,
            "is_focused": is_focused,
            "created_at": row.get("created_at", ""),
            "latest_response": latest_response,
        }
        return plan_dict, is_focused

    def list_plans(
        self, status: str | None = None, *, session_id: str,
    ) -> dict[str, Any]:
        """List plans with optional status filter.

        Focus-state marking reflects the ACTING session's buffer (JOS-02) —
        a plan focused in another session lists as unfocused here.
        """
        if status and status not in VALID_STATUSES:
            raise FrameworkError(
                message=f"Invalid status: {status}",
                error_code=ErrorCode.PARAMETER_ERROR,
            )

        # Query plans
        filters: dict[str, Any] = {"is_deleted": 0, "task_type": TASK_TYPE_PLAN}
        if status:
            filters["status"] = status

        result = self._state_service.read_state(
            namespace=NAMESPACE,
            query={"table": "thinking_task", "filters": filters},
        )
        rows = result.get("data", {}).get("records", [])

        # Get the acting session's focused memories for focus-state marking
        memory_service = self._session_memory(session_id)
        focused_memories: list[dict[str, Any]] = memory_service.get_focused()["memories"]
        focused_ids: set[str] = {str(m.get("memory_id", "")) for m in focused_memories}

        plans: list[dict[str, Any]] = []
        focused_count = 0
        for row in rows:
            plan_dict, is_focused = DefaultThinkingPlugin._row_to_plan_summary(row, focused_ids)
            plans.append(plan_dict)
            if is_focused:
                focused_count += 1

        return {
            "plans": plans,
            "count": len(plans),
            "focused_count": focused_count,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # PLAYBOOK LIFECYCLE
    # ─────────────────────────────────────────────────────────────────────────

    def create_playbook(
        self,
        goal: str,
        constraints: str | None = None,
        investigation_context: str | None = None,
    ) -> dict[str, Any]:
        """Allocate a playbook, planning context, and metadata row.

        No inference. The planning inference loop runs in the
        ``process_planning_results`` VERTEX, triggered by the result
        processor on this EDGE's completion.
        """
        if not goal:
            raise FrameworkError(
                message="goal is required",
                error_code=ErrorCode.PARAMETER_ERROR,
            )

        # 1. Generate playbook ID
        playbook_id: str = self._state_service.generate_id(prefix="pbk-")

        # 2. Create planning context (same pattern as create_task)
        ctx_svc, _ = self._require_context_services()
        context_id: str = ctx_svc.registry.create_context(
            context_type="playbook",
            label=f"playbook: {goal[:80]}",
            metadata={"playbook_id": playbook_id, "goal": goal},
        )

        # 3. Store creation request as first INPUT event in planning context
        creation_request = self._build_playbook_creation_request(
            goal,
            constraints,
            investigation_context,
        )
        self._append_context_event(
            context_id,
            creation_request,
            ContextEventType.INPUT,
            ContextActorType.HUMAN,
        )

        # 4. Compute knowledge base path (will be written by process_planning_results)
        date_str = datetime.date.today().isoformat()
        kb_path = f"playbooks/{date_str}/{playbook_id}.md"

        # 5. Insert thinking_playbook row
        self._state_service.write_state(
            namespace=NAMESPACE,
            data={
                "table": "thinking_playbook",
                "record": {
                    "id": playbook_id,
                    "planning_context_id": context_id,
                    "status": STATUS_ACTIVE,
                    "title": goal[:200],
                    "knowledge_base_path": kb_path,
                },
            },
        )

        self.logger.info(
            "Created playbook %s with planning context %s",
            playbook_id,
            context_id,
        )
        return {
            "playbook_id": playbook_id,
            "planning_context_id": context_id,
            "status": STATUS_ACTIVE,
        }

    @staticmethod
    def _build_playbook_creation_request(
        goal: str,
        constraints: str | None,
        investigation_context: str | None,
    ) -> str:
        """Build the creation request stored as the first planning context event."""
        parts = [f"GOAL: {goal}"]
        if constraints:
            parts.append(f"CONSTRAINTS: {constraints}")
        if investigation_context:
            parts.append(f"INVESTIGATION CONTEXT:\n{investigation_context}")
        return "\n\n".join(parts)

    def get_playbook(self, playbook_id: str) -> dict[str, Any]:
        """Retrieve a playbook by ID."""
        record = self._get_playbook_record(playbook_id)
        content = self._read_playbook(playbook_id)
        return {
            "playbook_id": playbook_id,
            "content": content,
            "status": record.get("status", ""),
            "title": record.get("title", ""),
        }

    def get_playbook_section(
        self,
        playbook_id: str,
        section_id: str,
    ) -> dict[str, Any]:
        """Retrieve a single section from a playbook."""
        content = self._read_playbook_section(playbook_id, section_id)
        return {
            "playbook_id": playbook_id,
            "section_id": section_id,
            "content": content,
        }

    def list_playbooks(self, status: str | None = None) -> dict[str, Any]:
        """List playbooks with optional status filter."""
        if status and status not in VALID_STATUSES:
            raise FrameworkError(
                message=f"Invalid status: {status}",
                error_code=ErrorCode.PARAMETER_ERROR,
            )

        filters: dict[str, Any] = {"is_deleted": 0}
        if status:
            filters["status"] = status

        result = self._state_service.read_state(
            namespace=NAMESPACE,
            query={"table": "thinking_playbook", "filters": filters},
        )
        rows = result.get("data", {}).get("records", [])

        playbooks: list[dict[str, Any]] = [
            {
                "playbook_id": row.get("id", ""),
                "title": row.get("title", ""),
                "status": row.get("status", ""),
                "plan_id": row.get("plan_id"),
                "created_at": row.get("created_at", ""),
                "updated_at": row.get("updated_at", ""),
            }
            for row in rows
        ]

        return {
            "playbooks": playbooks,
            "count": len(playbooks),
        }

    def patch_playbook(
        self,
        playbook_id: str,
        patch_description: str,
    ) -> dict[str, Any]:
        """Load an existing playbook's planning context and store a patch request.

        No inference. The ``process_planning_results`` VERTEX resumes the
        planning context with the full prior history plus this new request.
        The result processor routes back to the planning VERTEX.
        """
        if not playbook_id:
            raise FrameworkError(
                message="playbook_id is required",
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        if not patch_description:
            raise FrameworkError(
                message="patch_description is required",
                error_code=ErrorCode.PARAMETER_ERROR,
            )

        # 1. Look up existing playbook
        record = self._get_playbook_record(playbook_id)
        planning_context_id: str = str(record["planning_context_id"])
        current_status: str = str(record.get("status", STATUS_ACTIVE))

        # 2. Reactivate if paused or abandoned
        if current_status != STATUS_ACTIVE:
            self._update_playbook_status(playbook_id, STATUS_ACTIVE)

        # 3. Store patch request as INPUT event in the existing planning context
        self._append_context_event(
            planning_context_id,
            f"PATCH REQUEST:\n{patch_description}",
            ContextEventType.INPUT,
            ContextActorType.HUMAN,
        )

        self.logger.info(
            "Patch request stored for playbook %s in context %s",
            playbook_id,
            planning_context_id,
        )
        return {
            "playbook_id": playbook_id,
            "planning_context_id": planning_context_id,
            "status": STATUS_ACTIVE,
        }

    def _update_playbook_status(self, playbook_id: str, status: str) -> None:
        """Update the lifecycle status of a playbook."""
        self._state_service.update_state(
            namespace=NAMESPACE,
            query={"table": "thinking_playbook", "filters": {"id": playbook_id}},
            updates={"status": status},
        )

    # ─────────────────────────────────────────────────────────────────────────
    # WORK MANIFEST / WORK BREAKDOWN STRUCTURE LIFECYCLE
    # ─────────────────────────────────────────────────────────────────────────

    def _upsert_focused_document(
        self,
        content: str,
        doc_tag: str,
        label: str,
        memory_service: Any,
    ) -> str:
        """Store or replace a focused document in the acting session's memory.

        Ensures that at most one focused memory item exists for a given
        ``doc_tag`` (e.g. ``work_manifest:wmf-ns06-001``).  Replaces the
        old item if one exists.

        Args:
            content: Full document content.
            doc_tag: Unique tag identifying this document (used for lookup).
            label: Category label (e.g. ``work_manifest``, ``work_breakdown_structure``).
            memory_service: The session-bound focus view (JOS-02).

        Returns:
            The memory_id of the focused artifact (empty string if focus failed).
        """

        # Find and remove ALL existing items with this doc_tag.
        # Multiple duplicates can accumulate if prior unfocus/forget
        # calls failed silently — remove every match, not just the first.
        focused_items: list[dict[str, Any]] = memory_service.get_focused()["memories"]
        evicted = 0
        for item in focused_items:
            tags: list[str] = item.get("tags", [])
            if doc_tag in tags:
                old_id: str = item.get("memory_id", "")
                if old_id:
                    self._safe_unfocus(old_id, memory_service)
                    try:
                        memory_service.forget(old_id)
                    except Exception:
                        self.logger.warning(
                            "Failed to forget %s document %s",
                            label,
                            old_id,
                        )
                    evicted += 1
        if evicted:
            self.logger.info(
                "Evicted %d prior %s document(s) (tag=%s)",
                evicted,
                label,
                doc_tag,
            )

        # Store new content as focused memory (embed=False: focus-only mirror,
        # the searchable version lives in the KB artifact path)
        result: dict[str, Any] = memory_service.remember(
            content=content,
            tags=[label, doc_tag],
            embed=False,
        )
        memory_id: str = result.get("memory_id", "")
        if memory_id:
            self._focus_artifact(memory_id, memory_service)
            self.logger.debug(
                "Focused %s document as %s (tag=%s)",
                label,
                memory_id,
                doc_tag,
            )
        return memory_id

    def _defocus_documents_by_label(
        self, label: str, memory_service: Any,
    ) -> None:
        """Defocus and forget the session's focused documents matching a label.

        Used to ensure only one document of a given category (e.g.
        ``work_breakdown_structure``) is focused at a time. Operates on the
        session-bound view (JOS-02).
        """
        for item in memory_service.get_focused()["memories"]:
            tags: list[str] = item.get("tags", [])
            if label in tags:
                old_id: str = item.get("memory_id", "")
                if old_id:
                    self._safe_unfocus(old_id, memory_service)
                    memory_service.forget(old_id)
                    self.logger.info(
                        "Defocused prior %s: %s",
                        label,
                        old_id,
                    )

    def create_resolved_intake_state(
        self,
        intake_id: str,
        content: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Persist an authored-by-value Resolved Intake State artifact."""
        return self._artifact_authoring_service(session_id).create_resolved_intake_state(
            intake_id,
            content,
        )

    # ------------------------------------------------------------------
    # Generic artifact authoring helpers (WBS authoring delegated to WbsAuthoringService)
    # ------------------------------------------------------------------

    def _assemble_plan_prompt(
        self,
        goal: str,
        topic: str | None = None,
        context: str | None = None,
    ) -> list[dict[str, str]]:
        """Build plan messages via the prompt assembly contract."""
        raw_messages = self._build_plan_messages(goal, topic=topic, context=context)
        return self._apply_thinking_serialization(raw_messages, "plan_generation")

    def _assemble_planning_prompt(
        self,
        planning_context_id: str,
    ) -> list[dict[str, str]]:
        """Build planning loop messages via the prompt assembly contract."""
        raw_messages = self._build_planning_messages(planning_context_id)
        return self._apply_thinking_serialization(raw_messages, "planning_loop")

    def _apply_thinking_serialization(
        self,
        raw_messages: list[dict[str, str]],
        action_name: str,
    ) -> list[dict[str, str]]:
        """Route pre-built messages through the assembly contract.

        Applies THINKING_PROFILE serialization (role merge, system
        consolidation) for consistency.  Resolves the prompt assembly
        service via ``ServiceName.PROMPT_ASSEMBLY_SERVICE`` — no
        concrete plugin lookup.
        """
        from ananta.core.orchestration.service_bindings import ServiceName
        from ananta.interfaces.prompt_assembly_interface import (
            PromptAssemblyServiceInterface,
        )
        from ananta.services.inference_service.assembly_types import (
            PromptAssemblyRequest,
        )

        if not self.orchestrator_ref:
            raise FrameworkError("Thinking plugin requires orchestrator for prompt assembly")

        service = self.orchestrator_ref.get_service(
            ServiceName.PROMPT_ASSEMBLY_SERVICE,
        )
        if service is None:
            raise FrameworkError("PromptAssemblyService not available — ServiceManager may not be initialized")

        assembly_service: PromptAssemblyServiceInterface = service  # type: ignore[assignment]
        request = PromptAssemblyRequest(
            profile_name="thinking",
            flow_id="",
            action_name=action_name,
            session_id="",
            pre_built_messages=tuple(raw_messages),
        )
        result = assembly_service.assemble_prompt(request)
        return list(result.messages)

    def _store_artifact(
        self,
        *,
        content: str,
        kb_path: str,
        db_table: str,
        db_record: dict[str, Any],
        focus_label: str,
        focus_tag: str,
        defocus_first: bool,
        memory_service: Any,
    ) -> str:
        """Store an authored artifact in knowledge base, state, and focus.

        Focus lands in the acting session's buffer via the bound
        ``memory_service`` view (JOS-02).

        Returns:
            The memory_id of the focused artifact (empty string if focus failed).
        """
        self._write_to_thinking_plans_kb(kb_path, content)

        self._state_service.write_state(
            namespace=NAMESPACE,
            data={"table": db_table, "record": db_record},
        )

        if defocus_first:
            self._defocus_documents_by_label(focus_label, memory_service)
        return self._upsert_focused_document(
            content,
            doc_tag=focus_tag,
            label=focus_label,
            memory_service=memory_service,
        )

    # ------------------------------------------------------------------
    # Delegated artifact creation methods
    # ------------------------------------------------------------------

    def create_work_manifest(
        self,
        content: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Persist an authored-by-value Work Manifest document.

        The manifest_id is derived deterministically from the focused
        Complete Brief Form; the caller never authors it.
        """
        return self._artifact_authoring_service(session_id).create_work_manifest(content)

    def patch_work_manifest(
        self,
        manifest_id: str,
        content: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Revise an existing Work Manifest document."""
        return self._artifact_authoring_service(session_id).patch_work_manifest(
            manifest_id, content,
        )

    def create_authored_artifact(
        self,
        artifact_type: str,
        content: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Persist an authored-by-value artifact document.

        ``artifact_id`` and ``parent_id`` are derived deterministically
        from the focused Work Manifest (or, for ``brief``, from the
        authored document itself). The caller never authors identifiers.

        ``pipeline_spec`` is structured-data and bypasses the markdown
        artifact pipeline — it routes to ``_create_pipeline_spec`` and
        is persisted to blob storage.
        """
        authoring = self._artifact_authoring_service(session_id)
        if artifact_type == "pipeline_spec":
            spec_id, manifest_id = authoring.derive_authored_ids(
                artifact_type, content,
            )
            return self._create_pipeline_spec(
                spec_id, manifest_id, content, session_id=session_id,
            )
        return authoring.create_authored_artifact(
            artifact_type,
            content,
        )

    def _create_pipeline_spec(
        self,
        spec_id: str,
        manifest_id: str,
        content: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Persist an authored Pipeline Spec — JSON payload, blob storage."""
        content = self._artifact_authoring_service(
            session_id,
        ).invoke_pipeline_spec_authoring(
            spec_id, manifest_id, content,
        )
        spec_dict = _parse_pipeline_spec_response(content, spec_id)
        style_family = str(
            spec_dict.get("piece", {}).get("style_family", ""),
        )
        if style_family:
            try:
                schema = self._load_pipeline_schema(style_family)
                self._validate_pipeline_spec_shapes(spec_dict, schema, spec_id)
                validate_pipeline_spec_raw_and_loaded(spec_dict, schema)
            except FrameworkError:
                raise
            except Exception as exc:
                raise FrameworkError(
                    message=f"PipelineSpec {spec_id!r} validation error: {exc}",
                    error_code=ErrorCode.PARAMETER_ERROR,
                ) from exc
        blob_namespace = "pipeline_specs"
        blob_metadata = {
            "filename": f"{spec_id}.json",
            "spec_id": spec_id,
            "manifest_id": manifest_id,
            "artifact_type": "pipeline_spec",
        }
        blob_service = self._require_blob_storage_service()
        store_result = blob_service.store_blob(
            namespace=blob_namespace,
            content=json.dumps(spec_dict, indent=2).encode("utf-8"),
            metadata=blob_metadata,
        )
        blob_id = str(store_result.get("data", {}).get("blob_id", ""))
        if not blob_id:
            raise FrameworkError(
                message=f"Pipeline Spec {spec_id!r}: blob storage returned no blob_id",
                error_code=ErrorCode.INTERNAL_ERROR,
            )
        self._state_service.upsert_state(
            namespace=NAMESPACE,
            data={
                "table": "thinking_pipeline_spec",
                "record": {
                    "id": spec_id,
                    "manifest_id": manifest_id,
                    "status": "active",
                    "blob_id": blob_id,
                    "blob_namespace": blob_namespace,
                },
                "conflict_columns": ["id"],
            },
        )
        self.logger.info(
            "Pipeline Spec %s stored as blob %s (namespace %s, %d bytes)",
            spec_id, blob_id, blob_namespace,
            len(json.dumps(spec_dict)),
        )
        return {
            "artifact_type": "pipeline_spec",
            "artifact_id": spec_id,
            "parent_id": manifest_id,
            "status": "created",
            "content": json.dumps(spec_dict, indent=2),
            "knowledge_base_path": "",
            "source_memory_id": "",
            "blob_id": blob_id,
            "blob_namespace": blob_namespace,
        }

    @staticmethod
    def _build_lc_map(raw_lc: Any) -> dict[str, dict[str, Any]]:
        """Build layer_type → properties dict from raw layer_configs."""
        lc_map: dict[str, dict[str, Any]] = {}
        if isinstance(raw_lc, list):
            for lc in raw_lc:
                if isinstance(lc, dict):
                    lc_map[str(lc.get("layer_type", ""))] = dict(lc.get("properties", {}))
        elif isinstance(raw_lc, dict):
            for lt, props in raw_lc.items():
                lc_map[str(lt)] = dict(props) if isinstance(props, dict) else {}
        return lc_map

    @staticmethod
    def _build_pg_map(raw_pg: Any) -> dict[str, Any]:
        """Build param_key → value dict from raw parameter_groups."""
        pg_map: dict[str, Any] = {}
        if isinstance(raw_pg, list):
            for pg in raw_pg:
                if isinstance(pg, dict):
                    for k, v in pg.get("properties", {}).items():
                        pg_map[str(k)] = v
        elif isinstance(raw_pg, dict):
            for _, props in raw_pg.items():
                if isinstance(props, dict):
                    for k, v in props.items():
                        pg_map[str(k)] = v
        return pg_map

    def _validate_layer_config_shapes(
        self,
        discovery: Any,
        lc_map: dict[str, dict[str, Any]],
        schema: dict[str, Any],
    ) -> list[str]:
        """Validate layer_config values; return a list of error strings."""
        errors: list[str] = []
        seen: set[tuple[str, str, str, str]] = set()
        for layer_type, process, arg_key, lc_key in collect_layer_config_sources(schema):
            triple = (layer_type, process, arg_key, lc_key)
            if triple in seen or lc_key not in lc_map.get(layer_type, {}):
                seen.add(triple)
                continue
            seen.add(triple)
            value = lc_map[layer_type][lc_key]
            try:
                process_data = discovery.get_process_by_key(process)
            except Exception:
                continue
            extracted = DefaultThinkingPlugin._extract_arg_properties(process_data) if isinstance(process_data, dict) else None
            if extracted is None:
                continue
            arg_schema = extracted[0].get(arg_key)
            if not isinstance(arg_schema, dict):
                continue
            path = f"layer_configs[{layer_type!r}].{lc_key} (→ {process}:{arg_key})"
            err = check_json_schema_value(value, arg_schema, path)
            if err:
                errors.append(err)
        return errors

    def _validate_parameter_group_shapes(
        self,
        discovery: Any,
        pg_map: dict[str, Any],
        schema: dict[str, Any],
    ) -> list[str]:
        """Validate parameter_group values; return a list of error strings."""
        errors: list[str] = []
        seen: set[tuple[str, str, str, str]] = set()
        for layer_type, process, arg_key, pg_key in collect_parameter_group_sources(schema):
            triple = (layer_type, process, arg_key, pg_key)
            if triple in seen or pg_key not in pg_map:
                seen.add(triple)
                continue
            seen.add(triple)
            value = pg_map[pg_key]
            try:
                process_data = discovery.get_process_by_key(process)
            except Exception:
                continue
            extracted = DefaultThinkingPlugin._extract_arg_properties(process_data) if isinstance(process_data, dict) else None
            if extracted is None:
                continue
            arg_schema = extracted[0].get(arg_key)
            if not isinstance(arg_schema, dict):
                continue
            path = f"parameter_groups[{pg_key!r}] (→ {process}:{arg_key})"
            err = check_json_schema_value(value, arg_schema, path)
            if err:
                errors.append(err)
        return errors

    def _validate_one_modulation_assignment(
        self,
        discovery: Any,
        ma: dict[str, Any],
    ) -> list[str]:
        """Validate one modulation_assignment entry; return error strings."""
        ma_process = str(ma.get("process", ""))
        ma_params = ma.get("params", {})
        if not isinstance(ma_params, dict) or not ma_process:
            return []
        try:
            process_data = discovery.get_process_by_key(ma_process)
        except Exception:
            return []
        extracted = DefaultThinkingPlugin._extract_arg_properties(process_data) if isinstance(process_data, dict) else None
        if extracted is None:
            return []
        ma_section = str(ma.get("section_name", "?"))
        ma_layer = str(ma.get("layer_type", "?"))
        errors: list[str] = []
        for param_name, value in ma_params.items():
            arg_schema = extracted[0].get(param_name)
            if not isinstance(arg_schema, dict):
                continue
            path = f"modulation_assignments[{ma_section!r}/{ma_layer!r}].{param_name} (→ {ma_process}:{param_name})"
            err = check_json_schema_value(value, arg_schema, path)
            if err:
                errors.append(err)
        return errors

    def _validate_modulation_assignment_shapes(
        self,
        discovery: Any,
        raw_ma: list[Any],
    ) -> list[str]:
        """Validate modulation_assignment params; return a list of error strings."""
        errors: list[str] = []
        for ma in raw_ma:
            if isinstance(ma, dict):
                errors.extend(self._validate_one_modulation_assignment(discovery, ma))
        return errors

    def _validate_pipeline_spec_shapes(
        self,
        spec_dict: dict[str, Any],
        schema: dict[str, Any],
        spec_id: str,
    ) -> None:
        """Validate layer_config, parameter_group, and modulation_assignment shapes.

        Raises ``FrameworkError`` listing all shape mismatches. Skips
        silently when the discovery service is unavailable.
        """
        try:
            discovery = self._get_discovery_service()
        except FrameworkError:
            return

        lc_map = self._build_lc_map(spec_dict.get("layer_configs", []))
        pg_map = self._build_pg_map(spec_dict.get("parameter_groups", []))
        raw_ma = spec_dict.get("modulation_assignments", [])
        errors = (
            self._validate_layer_config_shapes(discovery, lc_map, schema)
            + self._validate_parameter_group_shapes(discovery, pg_map, schema)
            + (self._validate_modulation_assignment_shapes(discovery, raw_ma) if isinstance(raw_ma, list) else [])
        )
        if errors:
            bullet_list = "\n".join(f"  - {e}" for e in errors)
            raise FrameworkError(
                message=(
                    f"PipelineSpec {spec_id!r} failed shape validation "
                    f"({len(errors)} error(s)):\n{bullet_list}"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )

    def create_movement_design(
        self,
        manifest_id: str,
        movement_type: str,
        packet_content: str,
        ledger_content: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Persist an authored Movement Design Packet and Phrase Design Ledger."""
        return self._artifact_authoring_service(session_id).create_movement_design(
            manifest_id,
            movement_type,
            packet_content,
            ledger_content,
        )

    def patch_authored_artifact(
        self,
        artifact_type: str,
        artifact_id: str,
        content: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Revise an existing authored artifact document."""
        return self._artifact_authoring_service(session_id).patch_authored_artifact(
            artifact_type,
            artifact_id,
            content,
        )

    def validate_authored_work_breakdown_structure(
        self,
        content: str,
        wbs_id: str,
        phase_number: int,
        manifest_id: str | None = None,  # reserved for cross-checks per the Phase 3 spec
    ) -> dict[str, Any]:
        """Validate an agent-authored-by-value WBS document (dry run).

        Non-mutating: nothing is stored, no thinking model is invoked. The
        report collects every finding per the Q4 error/warning tiers (see
        ``authored_validation``).
        """
        report = validate_authored_wbs(
            content, wbs_id, phase_number, _PluginProcessSchemaLookup(self),
        )
        return {
            "valid": report.valid,
            "errors": list(report.errors),
            "warnings": list(report.warnings),
            "wbs_id": wbs_id,
        }

    def register_authored_work_breakdown_structure(
        self,
        content: str,
        wbs_id: str,
        manifest_id: str,
        phase_number: int,
        phase_name: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Register an agent-authored-by-value WBS document.

        Validates first (same collector as the validate verb, DRY) and
        hard-fails BEFORE any storage side effect when the report carries
        errors. Storage reuses the thinking-model authoring path with
        ``provenance='authored_by_value'``.
        """
        report = validate_authored_wbs(
            content, wbs_id, phase_number, _PluginProcessSchemaLookup(self),
        )
        if not report.valid:
            bullet_list = "\n".join(f"  - {e}" for e in report.errors)
            raise FrameworkError(
                message=(
                    f"Authored WBS {wbs_id!r} failed validation "
                    f"({len(report.errors)} error(s)) — nothing was stored:\n"
                    f"{bullet_list}"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        return self._wbs_authoring_service(session_id).store_authored_wbs(
            wbs_id=wbs_id,
            manifest_id=manifest_id,
            phase_number=phase_number,
            phase_name=phase_name,
            content=content,
        )

    def validate_authored_joseki(
        self,
        content: str,
        joseki_key: str | None = None,
    ) -> dict[str, Any]:
        """Validate an agent-authored-by-value joseki KB card (dry run).

        Non-mutating: nothing is stored, no thinking model is invoked.
        Card-header rules are additive; the step body reuses the WBS
        validation machinery (see ``authored_validation``).
        """
        report = validate_authored_joseki_card(
            content, _PluginProcessSchemaLookup(self), joseki_key,
        )
        return {
            "valid": report.valid,
            "errors": list(report.errors),
            "warnings": list(report.warnings),
            "joseki_key": parse_joseki_key(content),
        }

    def register_authored_joseki(
        self,
        content: str,
    ) -> dict[str, Any]:
        """Register an agent-authored-by-value joseki KB card.

        Validates first (same collector as the validate verb, DRY) and
        hard-fails BEFORE any storage side effect. Storage is Q14 dual:
        the card lands in the thinking-plans knowledge base (the KB is
        the registry) plus one ``thinking_authored_joseki`` lifecycle row
        at state 'draft'.
        """
        report = validate_authored_joseki_card(
            content, _PluginProcessSchemaLookup(self),
        )
        if not report.valid:
            bullet_list = "\n".join(f"  - {e}" for e in report.errors)
            raise FrameworkError(
                message=(
                    f"Authored joseki card failed validation "
                    f"({len(report.errors)} error(s)) — nothing was stored:\n"
                    f"{bullet_list}"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        registrar = AuthoredJosekiRegistrar(
            knowledge_writer=_PluginJosekiCardWriter(self),
            state_store=_PluginStateStore(self._state_service, NAMESPACE),
            namespace=NAMESPACE,
        )
        return registrar.register(
            joseki_key=parse_joseki_key(content), content=content,
        )

    def get_joseki_run_gateway(self) -> JosekiRunGateway:
        """Composed joseki-run domain surface for the core-side driver engine.

        The engine (``ananta.services.thinking_service.joseki_run_engine``)
        orchestrates runs; everything domain-shaped is composed HERE from
        already-landed plugin pieces. Constructed per call (stateless
        adapters; the registrar / lifecycle pattern).
        """
        return JosekiRunGateway(
            lifecycle=self._authored_joseki_lifecycle(),
            cards=_PluginJosekiCardReader(self),
            registrar=_RunWbsRegistrarAdapter(self),
            run_store=JosekiRunStore(
                state_store=_PluginStateStore(self._state_service, NAMESPACE),
                namespace=NAMESPACE,
            ),
            plan_buffer=_PluginPlanBufferAdapter(self),
        )

    def _joseki_run_engine(self) -> Any:
        """The run engine over this plugin's gateway + orchestrator (v3.1)."""
        from ananta.services.thinking_service.joseki_run_wiring import (
            build_joseki_run_engine,
        )

        return build_joseki_run_engine(
            gateway=self.get_joseki_run_gateway(),
            orchestrator=self.orchestrator_ref,
        )

    def run_joseki(
        self,
        joseki_key: str,
        bindings: dict[str, Any],
        label: str = "",
    ) -> dict[str, Any]:
        """Execute a registered joseki card platform-side (Track A driver)."""
        return self._joseki_run_engine().run_joseki(
            joseki_key=joseki_key, bindings=bindings, label=label,
        )

    def complete_joseki_run(self, wbs_id: str) -> dict[str, Any]:
        """Terminal run step: CAS → completed + exactly-once run evidence."""
        return self._joseki_run_engine().complete_joseki_run(wbs_id=wbs_id)

    def get_joseki_run(self, run_id: str) -> dict[str, Any]:
        """Run row projection by id (found=False when absent)."""
        return self._joseki_run_engine().get_joseki_run(run_id=run_id)

    def list_joseki_runs(
        self,
        status: str | None = None,
        joseki_key: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Bounded run listing (status/joseki_key filters)."""
        return self._joseki_run_engine().list_joseki_runs(
            status=status, joseki_key=joseki_key, limit=limit,
        )

    def reconcile_joseki_runs(self) -> dict[str, Any]:
        """Sweep every running run once (EDGE_SINK cron-sibling shape)."""
        engine = self._joseki_run_engine()
        listing = engine.list_joseki_runs(status="running", limit=100)
        results = [
            engine.reconcile_run(run_id=str(run["run_id"]))
            for run in listing.get("runs", [])
        ]
        return {"swept": len(results), "results": results}

    def _authored_joseki_lifecycle(self) -> AuthoredJosekiLifecycle:
        """Lifecycle state machine over the authored-joseki row (Phase 6 §4.3).

        Constructed per call (stateless; adapters resolve services at
        use-time), mirroring the joseki registrar / pull-execution pattern.
        The candidate gate validates the stored card against the LIVE process
        registry via the same validator the register/validate verbs use.
        """
        return AuthoredJosekiLifecycle(
            state_store=_PluginStateStore(self._state_service, NAMESPACE),
            card_reader=_PluginJosekiCardReader(self),
            card_writer=_PluginJosekiCardWriter(self),
            validate=lambda content, expected: validate_authored_joseki_card(
                content, _PluginProcessSchemaLookup(self), expected,
            ),
            now=lambda: datetime.datetime.now(datetime.UTC),
            namespace=NAMESPACE,
        )

    def transition_authored_joseki(
        self,
        joseki_key: str,
        target_state: str,
        superseded_by: str | None = None,
    ) -> dict[str, Any]:
        """Advance an authored joseki along its lifecycle (Phase 6 §4.3).

        Manual targets are 'candidate' (validation-gated), 'superseded'
        (requires ``superseded_by``) and 'archived' (retire). 'proven' is
        earned via ``record_authored_joseki_run``, not set here.
        """
        return self._authored_joseki_lifecycle().transition(
            joseki_key=joseki_key,
            target_state=target_state,
            superseded_by=superseded_by,
        )

    def record_authored_joseki_run(
        self,
        joseki_key: str,
        wbs_id: str | None = None,
    ) -> dict[str, Any]:
        """Record one successful run of an authored joseki (run evidence).

        Increments run evidence and advances a 'candidate' to 'proven' in the
        same predicated update (the §4.3 proven gate: ≥1 recorded run).
        """
        return self._authored_joseki_lifecycle().record_run(
            joseki_key=joseki_key, wbs_id=wbs_id,
        )

    def get_authored_joseki(self, joseki_key: str) -> dict[str, Any]:
        """Read an authored joseki's lifecycle row (state + run evidence)."""
        return self._authored_joseki_lifecycle().get(joseki_key=joseki_key)

    def reconcile_authored_joseki_row(self, joseki_key: str) -> dict[str, Any]:
        """Normalise an authored joseki's stored ``knowledge_base_path``.

        Row maintenance: repairs a card whose lifecycle row still carries a
        pre-migration path so it points at the canonical
        ``<joseki_key>.md`` in the authored_joseki knowledge base. Idempotent.
        """
        return self._authored_joseki_lifecycle().reconcile_row(
            joseki_key=joseki_key,
        )

    # ==========================================================================
    # PLAN-TEMPLATE CURATION LIFECYCLE (SUB-01, POR §4.5 GOAL)
    # ==========================================================================

    def _plan_template_lifecycle(self) -> PlanTemplateLifecycle:
        """Curation-lifecycle state machine over a plan template's card.

        Constructed per call (stateless; the card adapters resolve the
        knowledge service at use-time), mirroring the joseki lifecycle
        factory. State lives in the card's front-matter — there is NO state
        store and NO compare-and-set (the deliberate §4.5 asymmetry: a
        template does not run, so it earns no run-evidence row).
        """
        return PlanTemplateLifecycle(
            card_reader=_PluginPlanTemplateCardReader(self),
            card_writer=_PluginPlanTemplateCardWriter(self),
        )

    def transition_plan_template(
        self,
        template_key: str,
        target_state: str,
        superseded_by: str | None = None,
    ) -> dict[str, Any]:
        """Advance a plan template along its curation lifecycle (SUB-01, §4.5).

        Manual targets are 'active' (endorse a draft as the canonical skeleton
        to fork), 'superseded' (requires ``superseded_by`` naming a registered,
        non-archived replacement) and 'archived' (retire). 'draft' is the
        authoring origin, not a manual target. A transition is a validated
        front-matter edit persisted through the knowledge service.
        """
        return self._plan_template_lifecycle().transition(
            template_key=template_key,
            target_state=target_state,
            superseded_by=superseded_by,
        )

    def get_plan_template(self, template_key: str) -> dict[str, Any]:
        """Read a plan template's curation view (state + discovery axes).

        Returns the front-matter lifecycle state plus the discovery-by-intent
        axes (goal/domain/outcome) — the searchable curation surface — not the
        program skeleton. ``found`` is False when no card exists.
        """
        return self._plan_template_lifecycle().get(template_key=template_key)

    def _pull_execution_service(self, session_id: str) -> PullExecutionService:
        """Pull-mode execution service over the durable substrates.

        Constructed per call (stateless; adapters resolve services at
        use-time), mirroring the joseki registrar pattern. The focus
        manager is session-bound (JOS-02).
        """
        return PullExecutionService(
            state_service=_PluginStateStore(self._state_service, NAMESPACE),
            work_product_state_service=self._state_service,
            knowledge_store=_PluginKnowledgeWriter(self),
            focus_manager=_PluginFocusManager(self, session_id),
            namespace=NAMESPACE,
        )

    def _artifact_authoring_service(
        self, session_id: str,
    ) -> ArtifactAuthoringService:
        """Per-call, session-bound artifact authoring service (JOS-02)."""
        return ArtifactAuthoringService(
            knowledge_writer=_PluginKnowledgeWriter(self),
            state_store=_PluginStateStore(self._state_service, NAMESPACE),
            focus_manager=_PluginFocusManager(self, session_id),
            namespace=NAMESPACE,
        )

    def _wbs_authoring_service(self, session_id: str) -> WbsAuthoringService:
        """Per-call, session-bound WBS authoring service (JOS-02)."""
        return WbsAuthoringService(
            knowledge_writer=_PluginKnowledgeWriter(self),
            state_store=_PluginStateStore(self._state_service, NAMESPACE),
            focus_manager=_PluginFocusManager(self, session_id),
            namespace=NAMESPACE,
            process_schema_lookup=_PluginProcessSchemaLookup(self),
        )

    def start_wbs_execution(
        self, wbs_id: str, *, session_id: str,
    ) -> dict[str, Any]:
        """Start or resume a pull-mode WBS execution session (idempotent)."""
        return self._pull_execution_service(session_id).start_wbs_execution(wbs_id)

    def get_next_wbs_step(
        self, wbs_id: str, *, session_id: str,
    ) -> dict[str, Any]:
        """Return the pull-mode envelope for the next unexecuted step."""
        return self._pull_execution_service(session_id).get_next_wbs_step(wbs_id)

    def record_wbs_step_observation(
        self,
        wbs_id: str,
        step_number: int,
        process_key: str,
        result: dict[str, Any],
        state_summary: str | None = None,
        output_artifacts: list[str] | None = None,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Validate a pull-mode observation; record + advance only when valid."""
        return self._pull_execution_service(session_id).record_wbs_step_observation(
            wbs_id=wbs_id,
            step_number=step_number,
            process_key=process_key,
            result=result,
            state_summary=state_summary,
            output_artifacts=output_artifacts,
        )

    def advance_wbs_execution(
        self, wbs_id: str, *, session_id: str,
    ) -> dict[str, Any]:
        """Q15 advance evaluation (auto_safe / agent_review / complete)."""
        return self._pull_execution_service(session_id).advance_wbs_execution(wbs_id)

    def patch_work_breakdown_structure(
        self,
        wbs_id: str,
        content: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Revise an existing Work Breakdown Structure document.

        Replaces the full content of the WBS document in the
        ``thinking_plans`` knowledge base, updates the tracking record,
        and stores the content in focused memory so it appears in the
        model context.
        """
        if not wbs_id:
            raise FrameworkError(
                message="wbs_id is required",
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        if not content:
            raise FrameworkError(
                message="content is required",
                error_code=ErrorCode.PARAMETER_ERROR,
            )

        # Reject WBS content that still contains unresolved template tokens.
        validate_no_unresolved_placeholders(content, wbs_id)

        # Look up the WBS record to get the KB path
        record = self._get_wbs_record(wbs_id)
        kb_path: str = str(record.get("knowledge_base_path", ""))
        if not kb_path:
            kb_path = f"wbs/{wbs_id}.md"

        # Write content to knowledge base
        self._write_to_thinking_plans_kb(kb_path, content)

        # Store in the acting session's focused memory for context injection
        self._upsert_focused_document(
            content,
            doc_tag=f"work_breakdown_structure:{wbs_id}",
            label="work_breakdown_structure",
            memory_service=self._session_memory(session_id),
        )

        self.logger.info("Work Breakdown Structure %s updated at %s", wbs_id, kb_path)
        return {
            "wbs_id": wbs_id,
            "status": "updated",
        }

    def generate_section_stem_wbs(
        self,
        wbs_id: str,
        manifest_id: str,
        phase_number: int,
        phase_name: str,
        style_family: str,
        artifact_prefix: str,
        pipeline_spec_id: str | None = None,
        pipeline_spec: dict[str, Any] | None = None,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Deterministically generate a per-section pipeline WBS."""
        canonical_wbs_id = _derive_wbs_id(manifest_id, phase_number)
        if wbs_id != canonical_wbs_id:
            self.logger.warning(
                "WBS ID override: model passed %r, derived %r from %s phase %d",
                wbs_id, canonical_wbs_id, manifest_id, phase_number,
            )
        try:
            spec_dict = self._resolve_pipeline_spec_argument(
                pipeline_spec_id, pipeline_spec,
            )
        except FrameworkError:
            if pipeline_spec_id is None:
                raise
            spec_dict = self._load_pipeline_spec_by_manifest(manifest_id)
            self.logger.warning(
                "generate_section_stem_wbs: pipeline_spec_id %r not found; "
                "resolved by manifest_id %r instead",
                pipeline_spec_id,
                manifest_id,
            )
        schema = self._load_pipeline_schema(style_family)
        return self._wbs_authoring_service(session_id).generate_section_stem_wbs(
            wbs_id=canonical_wbs_id,
            manifest_id=manifest_id,
            phase_number=phase_number,
            phase_name=phase_name,
            style_family=style_family,
            artifact_prefix=artifact_prefix,
            pipeline_spec=spec_dict,
            schema=schema,
        )

    def _resolve_pipeline_spec_argument(
        self,
        pipeline_spec_id: str | None,
        pipeline_spec: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Resolve pipeline_spec_id or pipeline_spec into a dict."""
        if pipeline_spec is not None and pipeline_spec_id:
            raise FrameworkError(
                message=(
                    "Provide either pipeline_spec_id or pipeline_spec, "
                    "not both"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        if pipeline_spec is not None:
            return pipeline_spec
        if pipeline_spec_id:
            return self._load_pipeline_spec_artifact(pipeline_spec_id)
        raise FrameworkError(
            message=(
                "Either pipeline_spec_id or pipeline_spec is required"
            ),
            error_code=ErrorCode.PARAMETER_ERROR,
        )

    def _build_pipeline_schemas_context(self) -> str:
        """Render every available pipeline schema as a block in the directive.

        The thinking model copies canonical names (schema_id, arc names,
        layer types, modulation processes) directly from these blocks
        into the spec. Without this context the model fabricates names
        and the strict validator rejects the spec.
        """
        kb_roots = self._get_knowledge_base_roots()
        blocks: list[str] = []
        for kb_name, kb_root in kb_roots:
            schema_path = kb_root / "03_templates" / "pipeline_schema.json"
            if not schema_path.is_file():
                continue
            try:
                schema_text = schema_path.read_text()
                schema = json.loads(schema_text)
            except (OSError, json.JSONDecodeError):
                continue
            schema_id = str(schema.get("schema_id", ""))
            style_family = str(schema.get("style_family", kb_name))
            blocks.append(
                f"\n\n## Pipeline Schema for {style_family} "
                f"(schema_id={schema_id})\n\n"
                f"Use these exact canonical names — copy `schema_id`, "
                f"every `arcs` key, every `layer_types` key, and every "
                f"`modulation.applies_to` entry verbatim. The platform "
                f"validates the spec against this schema strictly.\n\n"
                f"```json\n{schema_text}\n```\n"
            )
        if not blocks:
            return ""
        header = (
            "\n\n# Available Pipeline Schemas\n\n"
            "Pick the schema whose `style_family` matches this work, "
            "then copy its `schema_id` into your spec's `schema_id` "
            "field exactly. Use only the arc names and layer_type keys "
            "the chosen schema declares.\n"
        )
        return header + "".join(blocks)

    def _require_blob_storage_service(self) -> Any:
        """Look up the blob storage service or raise a clear error."""
        if self.orchestrator_ref is None:
            raise FrameworkError(
                message="orchestrator_ref not available; blob storage unreachable",
                error_code=ErrorCode.INTERNAL_ERROR,
            )
        service = self.orchestrator_ref.get_service("blob_storage_service")
        if service is None:
            raise FrameworkError(
                message="blob_storage_service is not registered",
                error_code=ErrorCode.INTERNAL_ERROR,
            )
        return service

    def _load_pipeline_spec_artifact(self, spec_id: str) -> dict[str, Any]:
        """Load a Pipeline Spec artifact from blob storage by ID."""
        result = self._state_service.read_state(
            namespace=NAMESPACE,
            query={
                "table": "thinking_pipeline_spec",
                "filters": {"id": spec_id, "is_deleted": 0},
            },
        )
        records = result.get("data", {}).get("records") or []
        if not records:
            raise FrameworkError(
                message=f"Pipeline Spec {spec_id!r} not found",
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        record = records[0]
        blob_id = str(record.get("blob_id", ""))
        if not blob_id:
            raise FrameworkError(
                message=(
                    f"Pipeline Spec {spec_id!r} has no blob_id in "
                    f"its state record"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        blob_service = self._require_blob_storage_service()
        blob_result = blob_service.retrieve_blob(blob_id)
        raw_content = blob_result.get("data", {}).get("content")
        content = _decode_blob_content(raw_content, spec_id, blob_id)
        return _parse_pipeline_spec_response(content, spec_id)

    def _load_pipeline_spec_by_manifest(self, manifest_id: str) -> dict[str, Any]:
        """Load the most recent active Pipeline Spec for a given manifest."""
        result = self._state_service.read_state(
            namespace=NAMESPACE,
            query={
                "table": "thinking_pipeline_spec",
                "filters": {"manifest_id": manifest_id, "is_deleted": 0},
            },
        )
        records = result.get("data", {}).get("records") or []
        if not records:
            raise FrameworkError(
                message=(
                    f"No Pipeline Spec found for manifest {manifest_id!r}"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        record = records[-1]
        spec_id = str(record.get("id", ""))
        blob_id = str(record.get("blob_id", ""))
        if not blob_id:
            raise FrameworkError(
                message=(
                    f"Pipeline Spec {spec_id!r} for manifest {manifest_id!r} "
                    f"has no blob_id"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        blob_service = self._require_blob_storage_service()
        blob_result = blob_service.retrieve_blob(blob_id)
        raw_content = blob_result.get("data", {}).get("content")
        content = _decode_blob_content(raw_content, spec_id, blob_id)
        return _parse_pipeline_spec_response(content, spec_id)

    def _load_pipeline_schema(self, style_family: str) -> dict[str, Any]:
        """Load the pipeline schema JSON for a style family."""
        if not style_family:
            raise FrameworkError(
                message="style_family is required to load pipeline schema",
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        kb_roots = self._get_knowledge_base_roots()
        for kb_name, kb_root in kb_roots:
            if kb_name != style_family:
                continue
            schema_path = kb_root / "03_templates" / "pipeline_schema.json"
            if not schema_path.is_file():
                raise FrameworkError(
                    message=(
                        f"pipeline_schema.json not found at "
                        f"{schema_path} for style_family {style_family!r}"
                    ),
                    error_code=ErrorCode.PARAMETER_ERROR,
                )
            schema: dict[str, Any] = json.loads(schema_path.read_text())
            return schema
        raise FrameworkError(
            message=(
                f"Knowledge base {style_family!r} not found — cannot load "
                "pipeline schema"
            ),
            error_code=ErrorCode.PARAMETER_ERROR,
        )

    # ── Explicit WBS outline/detail/assembly processes ────────────────

    def record_work_breakdown_structure_step_state(
        self,
        wbs_id: str,
        step_number: int,
        status: str,
        state_summary: str | None = None,
        output_artifacts: list[str] | None = None,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Record step-level execution state in a WBS (session-scoped focus)."""
        from ananta.core.plans.wbs_lifecycle import record_step_state

        return record_step_state(
            wbs_id,
            step_number,
            status,
            state_summary,
            output_artifacts,
            state_service=_PluginStateStore(self._state_service, NAMESPACE),
            knowledge_store=_PluginKnowledgeWriter(self),
            focus_manager=_PluginFocusManager(self, session_id),
            namespace=NAMESPACE,
        )

    def record_work_manifest_phase_state(
        self,
        manifest_id: str,
        phase_number: int,
        status: str,
        outcome_summary: str,
        approved_artifacts: list[str] | None = None,
        next_phase_instruction: str | None = None,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Record phase-level outcome in a Work Manifest (session-scoped focus)."""
        from ananta.core.plans.wbs_lifecycle import record_phase_state

        return record_phase_state(
            manifest_id,
            phase_number,
            status,
            outcome_summary,
            approved_artifacts,
            next_phase_instruction,
            knowledge_store=_PluginKnowledgeWriter(self),
            focus_manager=_PluginFocusManager(self, session_id),
        )


    def graft_work_breakdown_structure_segment(
        self,
        wbs_id: str,
        anchor_step_number: int,
        segment: str = "",
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Graft a WBS-derived execution segment into the active plan tail.

        **Deterministic projection:** reads the WBS document from the
        knowledge base and mechanically projects the next incomplete
        work item's execution steps.  The model provides only ``wbs_id``
        and ``anchor_step_number``; the ``segment`` parameter exists
        in the public schema for symmetry but is platform-injected and
        deliberately ignored — the projector reads the WBS instead.
        Operates on the ACTING session's plan (JOS-02).
        """
        del segment  # platform-injected; projection is from the WBS
        if not wbs_id:
            raise FrameworkError(
                message="wbs_id is required",
                error_code=ErrorCode.PARAMETER_ERROR,
            )

        memory_service = self._session_memory(session_id)
        existing_plan = self._plan_store.find_focused_plan(memory_service)
        if not existing_plan:
            raise FrameworkError(
                message="No focused plan to graft into",
                error_code=ErrorCode.PARAMETER_ERROR,
            )

        # Read WBS content from the focused WBS document (authoritative).
        wbs_content, actual_wbs_id = self._read_wbs_content_for_graft(
            wbs_id, session_id,
        )
        projected_segment = _project_next_work_item(wbs_content, wbs_id=actual_wbs_id)

        # Two-tier: read FULL plan from KB for splicing (focused memory
        # may contain only a compact excerpt after the two-tier change).
        from .plan_store import extract_kb_path

        kb_path = extract_kb_path(existing_plan)
        kb_content: str | None = None
        if kb_path:
            kb_content = self._plan_store.read_plan_from_kb(kb_path)
        full_content: str = kb_content if kb_content is not None else existing_plan.get("content", "")

        projected = splice_execution_tail(
            full_content,
            anchor_step_number,
            projected_segment,
        )
        projected = complete_graft_step(projected)
        projected = ensure_active_marker(projected, anchor_step_number)
        projected = inject_wbs_headers(projected, wbs_content, actual_wbs_id)

        # Log the projected plan for debugging
        self.logger.info(
            "GRAFT_PROJECTED: %d chars, steps preview: %s",
            len(projected),
            " | ".join(line.strip()[:60] for line in projected.split("\n") if line.strip().startswith("["))[:500],
        )

        self._plan_store.upsert_existing_plan(
            projected,
            existing_plan,
            memory_service,
        )

        # Prime the graft guard so the NEXT advancement call skips.
        # The graft moved [>] to a new execution step that hasn't been
        # presented to the model yet.  Without this guard, the
        # vertex-entry advance call would move past the just-grafted
        # step before the model executes it.  After the model runs the
        # grafted step successfully, the result-processing advance call
        # must be allowed through so the plan moves to the next step —
        # using skip_count = 2 here would absorb that result-processing
        # call and leave the plan stuck on the (now-completed) grafted
        # step, causing the platform to re-run the same action and fail
        # on idempotency constraints.
        self._graft_skips[session_id] = 1

        # Initialize empty work-product register for this WBS run
        self._initialize_work_product_register(actual_wbs_id)

        # Unfocus planning artifacts that were consumed during scoping.
        # The WBS step descriptions contain everything the execution model
        # needs — carrying the full manifest, sketch, and intake state
        # bloats execution prompts from ~12K to ~45K chars and causes
        # runaway token generation.
        self._unfocus_planning_artifacts(memory_service)

        self.logger.info(
            "GRAFT: Spliced execution tail after step %d, wbs_id=%s, plan preserved",
            anchor_step_number,
            actual_wbs_id,
        )
        return {
            "wbs_id": actual_wbs_id,
            "status": "grafted",
        }

    # Tag prefixes for planning artifacts that should be unfocused at
    # the scoping→execution transition.  These are consumed during
    # scoping — the WBS step descriptions carry everything execution needs.
    # Artifacts to unfocus at graft (scoping→execution transition).
    _PLANNING_ARTIFACT_TAG_PREFIXES = (
        "resolved_intake_state:",
        "work_manifest:",
        "pipeline_spec:",
        "authored_artifact:",
    )

    def _unfocus_planning_artifacts(self, memory_service: Any) -> None:
        """Unfocus scoping artifacts at the execution transition."""
        from ananta.core.plans.wbs_lifecycle import unfocus_planning_artifacts

        unfocus_planning_artifacts(
            memory_service,
            artifact_tag_prefixes=self._PLANNING_ARTIFACT_TAG_PREFIXES,
        )

    def _initialize_work_product_register(self, wbs_id: str) -> None:
        """Create or advance work-product register for a WBS run."""
        from ananta.core.plans.wbs_lifecycle import initialize_work_product_register

        initialize_work_product_register(
            wbs_id,
            state_service=self._state_service,
        )

    _WBS_ID_LINE_RE: re.Pattern[str] = re.compile(
        r"^WBS ID:\s*(\S+)",
        re.MULTILINE,
    )

    def _read_wbs_content_for_graft(
        self,
        model_wbs_id: str,
        session_id: str,
    ) -> tuple[str, str]:
        """Read WBS content for grafting.

        The knowledge base is the authoritative source because
        ``record_step_state`` writes accumulated step-completion
        annotations there.  Focused memory upserts are lossy (the
        forget/remember/focus cycle has a race condition), so the
        KB is the reliable source for graft projection.

        Fallback order:

        1. **Knowledge base file** — has accumulated step-completion
           annotations written by ``record_step_state``.
        2. **Focused memory** — fallback when KB path lookup fails
           (e.g. WBS ID mismatch between model and platform).
        """
        from ananta.core.plans.wbs_lifecycle import read_wbs_content_for_graft

        # 1. Try knowledge base first (authoritative for step annotations)
        try:
            record = self._get_wbs_record(model_wbs_id)
        except FrameworkError:
            record = {}
        kb_path = str(record.get("knowledge_base_path", ""))
        if not kb_path:
            kb_path = f"wbs/{model_wbs_id}.md"

        kb_content = self._read_from_thinking_plans_kb(kb_path)
        if kb_content:
            wbs_id_match = self._WBS_ID_LINE_RE.search(kb_content)
            if wbs_id_match:
                found_id = wbs_id_match.group(1)
                annotation_count = kb_content.count("<!-- Step ")
                self.logger.info(
                    "WBS %s: read from knowledge base at %s (len=%d, annotations=%d)",
                    found_id,
                    kb_path,
                    len(kb_content),
                    annotation_count,
                )
                return kb_content, found_id

        # 2. Fallback to the acting session's focused memory (WBS ID resolution)
        try:
            result = read_wbs_content_for_graft(
                model_wbs_id,
                self._session_memory(session_id),
            )
            self.logger.info(
                "WBS %s: read from focused memory (len=%d, actual_id=%s)",
                model_wbs_id,
                len(result[0]),
                result[1],
            )
            return result
        except FrameworkError as focus_err:
            raise FrameworkError(
                message=f"WBS {model_wbs_id} not found in knowledge base ({kb_path}) or focused memory: {focus_err.message}",
                error_code=ErrorCode.PARAMETER_ERROR,
            ) from focus_err

    def _read_from_thinking_plans_kb(self, path: str) -> str:
        """Read a file from the thinking_plans knowledge base."""
        knowledge_service = self._get_knowledge_service()
        if knowledge_service is None:
            return ""
        try:
            result = knowledge_service.read_file(
                name=KB_THINKING_PLANS,
                path=path,
            )
            return str(result.get("content", "")) if isinstance(result, dict) else ""
        except FileNotFoundError:
            return ""

    def _write_to_thinking_plans_kb(
        self,
        path: str,
        content: str,
    ) -> None:
        """Write or update a file in the thinking_plans knowledge base."""
        self._write_to_kb(KB_THINKING_PLANS, path, content)

    def _read_from_authored_joseki_kb(self, path: str) -> str:
        """Read a card from the authored_joseki knowledge base (empty if absent)."""
        knowledge_service = self._get_knowledge_service()
        if knowledge_service is None:
            return ""
        try:
            result = knowledge_service.read_file(
                name=KB_AUTHORED_JOSEKI,
                path=path,
            )
            return str(result.get("content", "")) if isinstance(result, dict) else ""
        except FileNotFoundError:
            return ""

    def _write_to_authored_joseki_kb(
        self,
        path: str,
        content: str,
    ) -> None:
        """Write or update a file in the authored_joseki knowledge base.

        Dedicated SEMANTICALLY-SEARCHABLE home for authored-by-value joseki
        — see ``KB_AUTHORED_JOSEKI`` in constants for why these cards must
        NOT land in the search-excluded thinking_plans KB.
        """
        self._write_to_kb(KB_AUTHORED_JOSEKI, path, content)

    def _read_from_plan_templates_kb(self, path: str) -> str:
        """Read a card from the plan_templates knowledge base (empty if absent)."""
        knowledge_service = self._get_knowledge_service()
        if knowledge_service is None:
            return ""
        try:
            result = knowledge_service.read_file(
                name=KB_PLAN_TEMPLATES,
                path=path,
            )
            return str(result.get("content", "")) if isinstance(result, dict) else ""
        except FileNotFoundError:
            return ""

    def _write_to_plan_templates_kb(
        self,
        path: str,
        content: str,
    ) -> None:
        """Write or update a card in the plan_templates knowledge base.

        Dedicated SEMANTICALLY-SEARCHABLE home for plan-template cards — a
        template is searched like a joseki card, and its curation-lifecycle
        state lives in the card's front-matter (SUB-01, POR §4.5). Same
        rationale as ``KB_AUTHORED_JOSEKI``: NOT the search-excluded
        thinking_plans KB.
        """
        self._write_to_kb(KB_PLAN_TEMPLATES, path, content)

    def _write_to_kb(
        self,
        kb_name: str,
        path: str,
        content: str,
    ) -> None:
        """Write or update a file in a named knowledge base."""
        knowledge_service = self._get_knowledge_service()
        if knowledge_service is None:
            self.logger.warning(
                "%s knowledge base not installed — skipping",
                kb_name,
            )
            return
        try:
            knowledge_service.create_file(
                name=kb_name,
                path=path,
                content=content,
            )
        except FileExistsError:
            knowledge_service.edit_file(
                name=kb_name,
                path=path,
                content=content,
            )

    def _inject_support_articles(
        self,
        messages: list[dict[str, str]],
        article_filenames: list[str] | None,
    ) -> None:
        """Load and inject support articles as assistant context messages.

        Articles are loaded from the knowledge base by searching for
        their filename. Each found article becomes an assistant message
        so the thinking model sees authoritative content directly.
        """
        if not article_filenames:
            return
        knowledge_service = self._get_knowledge_service()
        if knowledge_service is None:
            return
        for filename in article_filenames:
            content = self._load_article_by_filename(knowledge_service, filename)
            if content:
                messages.append({"role": "assistant", "content": content})
                self.logger.info(
                    "SUPPORT_ARTICLE: Injected %s (%d chars)",
                    filename,
                    len(content),
                )

    def _load_article_by_filename(
        self,
        knowledge_service: Any,
        filename: str,
    ) -> str:
        """Load an article from any knowledge base by filename or title.

        Tries three strategies:
        1. Exact filename match via ``rglob``
        2. If filename looks like ``kb_name — Title``, extract the title
           and search the target KB for a markdown file whose ``# Title``
           line matches
        3. If filename looks like ``kb_name``, skip (it's a KB name, not
           a file)
        """
        kb_roots = self._get_knowledge_base_roots()
        self.logger.info(
            "SUPPORT_ARTICLE_LOOKUP: searching %d KB roots for %s",
            len(kb_roots),
            filename,
        )

        for strategy in (
            self._load_article_with_explicit_kb_prefix,
            self._load_article_by_rglob,
            self._load_article_by_kb_title_separator,
        ):
            content = strategy(knowledge_service, kb_roots, filename)
            if content:
                return content

        self.logger.warning(
            "Support article %s not found in any knowledge base",
            filename,
        )
        return ""

    def _load_article_by_rglob(
        self,
        knowledge_service: Any,
        kb_roots: list[tuple[str, Path]],
        filename: str,
    ) -> str:
        """Strategy 1: exact filename match via rglob across all KBs.

        Picks the first match. When a filename exists in multiple KBs,
        callers should use the explicit ``<kb_name>/<rel_path>`` form
        instead — see :meth:`_load_article_with_explicit_kb_prefix`.
        """
        for kb_name, kb_root in kb_roots:
            match = self._find_file_in_dir(kb_root, filename)
            if match is None:
                continue
            rel_path = str(match.relative_to(kb_root))
            try:
                result = knowledge_service.read_file(
                    name=kb_name, path=rel_path,
                )
            except FileNotFoundError:
                continue
            content = (
                result.get("content", "")
                if isinstance(result, dict) else ""
            )
            if isinstance(content, str) and content.strip():
                return str(strip_article_metadata_preamble(content)).strip()
        return ""

    def _load_article_by_kb_title_separator(
        self,
        knowledge_service: Any,
        kb_roots: list[tuple[str, Path]],
        filename: str,
    ) -> str:
        """Strategy 2: ``kb_name — Title`` / ``kb_name - Title`` form."""
        for sep in (" — ", " - "):
            if sep not in filename:
                continue
            target_kb, _, target_title = filename.partition(sep)
            result = self._find_article_by_title(
                knowledge_service, kb_roots,
                target_kb.strip(), target_title.strip(),
            )
            if result:
                return result
        return ""

    def _load_article_with_explicit_kb_prefix(
        self,
        knowledge_service: Any,
        kb_roots: list[tuple[str, Path]],
        filename: str,
    ) -> str:
        """Resolve a ``<kb_name>/<rel_path>`` reference inside the named KB only.

        Returns ``""`` when ``filename`` is not in this form, when the
        KB name doesn't match an installed KB, or when the file is
        missing from that KB.
        """
        target = self._resolve_explicit_kb_target(kb_roots, filename)
        if target is None:
            return ""
        kb_name, rel_path = target
        try:
            result = knowledge_service.read_file(name=kb_name, path=rel_path)
        except FileNotFoundError:
            return ""
        content = result.get("content", "") if isinstance(result, dict) else ""
        if not (isinstance(content, str) and content.strip()):
            return ""
        self.logger.info(
            "SUPPORT_ARTICLE_LOOKUP: resolved %s by explicit KB prefix → %s/%s",
            filename, kb_name, rel_path,
        )
        return str(strip_article_metadata_preamble(content)).strip()

    @staticmethod
    def _resolve_explicit_kb_target(
        kb_roots: list[tuple[str, Path]],
        filename: str,
    ) -> tuple[str, str] | None:
        """Parse ``<kb_name>/<rel_path>`` and verify the file exists."""
        if "/" not in filename:
            return None
        head, _, tail = filename.partition("/")
        if not tail:
            return None
        target_kb_root = next(
            (root for name, root in kb_roots if name == head),
            None,
        )
        if target_kb_root is None:
            return None
        if not (target_kb_root / tail).is_file():
            return None
        return head, tail

    def _read_kb_file_content(
        self,
        knowledge_service: Any,
        kb_name: str,
        kb_root: Path,
        md_file: Path,
        target_title: str,
    ) -> str:
        """Read a knowledge base file and return its content, or empty string."""
        rel_path = str(md_file.relative_to(kb_root))
        try:
            result = knowledge_service.read_file(name=kb_name, path=rel_path)
            content = result.get("content", "") if isinstance(result, dict) else ""
            if isinstance(content, str) and content.strip():
                self.logger.info(
                    "SUPPORT_ARTICLE_LOOKUP: resolved '%s' by title match → %s/%s",
                    target_title, kb_name, rel_path,
                )
                return str(strip_article_metadata_preamble(content)).strip()
        except FileNotFoundError:
            pass
        return ""

    def _find_article_by_title(
        self,
        knowledge_service: Any,
        kb_roots: list[tuple[str, Path]],
        target_kb: str,
        target_title: str,
    ) -> str:
        """Find an article by its ``# Title`` line within a specific KB."""
        title_line = f"# {target_title}"
        for kb_name, kb_root in kb_roots:
            if kb_name != target_kb:
                continue
            for md_file in kb_root.rglob("*.md"):
                if not md_file.is_file():
                    continue
                try:
                    first_line = md_file.open().readline().strip()
                except OSError:
                    continue
                if first_line == title_line:
                    content = self._read_kb_file_content(
                        knowledge_service, kb_name, kb_root, md_file, target_title,
                    )
                    if content:
                        return content
        return ""

    def _get_knowledge_base_roots(self) -> list[tuple[str, Path]]:
        """Return (kb_name, root_path) for all installed knowledge bases."""
        app_home = getattr(self.orchestrator_ref, "APP_HOME", "") or ""
        kb_parent = Path(app_home).parent / "knowledge_bases"
        if not kb_parent.is_dir():
            return []
        roots: list[tuple[str, Path]] = []
        for manifest in kb_parent.rglob("manifest.yaml"):
            kb_dir = manifest.parent
            roots.append((kb_dir.name, kb_dir))
        return roots

    @staticmethod
    def _find_file_in_dir(root: Path, filename: str) -> Path | None:
        """Find a file by name anywhere under root."""
        for match in root.rglob(filename):
            if match.is_file():
                return match
        return None

    def _get_wbs_record(self, wbs_id: str) -> dict[str, Any]:
        """Look up a WBS tracking record by ID."""
        result = self._state_service.read_state(
            namespace=NAMESPACE,
            query={
                "table": "thinking_wbs",
                "filters": {"id": wbs_id, "is_deleted": 0},
            },
        )
        data = result.get("data")
        records: list[dict[str, Any]] = data.get("records", []) if isinstance(data, dict) else []
        if not records:
            raise FrameworkError(
                message=f"Work Breakdown Structure not found: {wbs_id}",
                error_code=ErrorCode.WBS_NOT_FOUND,
            )
        return records[0]

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        """Retrieve a plan by ID from the knowledge base.

        Resolves the date-based KB path from either a ``thinking_task`` row
        (plans created by ``create_extended_plan``) or a ``thinking_playbook``
        row (plans created by ``_finalize_playbook``).
        """
        if not plan_id:
            raise FrameworkError(
                message="plan_id is required",
                error_code=ErrorCode.PARAMETER_ERROR,
            )

        knowledge_service = self._get_knowledge_service()
        if knowledge_service is None:
            raise FrameworkError(
                message="Knowledge service unavailable",
                error_code=ErrorCode.BACKEND_NOT_AVAILABLE,
            )

        created_at = self._resolve_plan_created_at(plan_id)

        # Read plan from knowledge base using date-based path convention
        # Plans are stored at plans/<YYYY-MM-DD>/<plan_id>.md
        date_str = created_at[:10] if len(created_at) >= 10 else datetime.date.today().isoformat()
        path = f"plans/{date_str}/{plan_id}.md"

        try:
            file_result = knowledge_service.get_file(name="thinking_plans", path=path)
            content: str = file_result.get("content", "")
            return {"plan_id": plan_id, "content": content}
        except Exception as exc:
            raise FrameworkError(
                message=f"Failed to read plan {plan_id}: {exc}",
                error_code=ErrorCode.OPERATION_FAILED,
            ) from exc

    def _resolve_plan_created_at(self, plan_id: str) -> str:
        """Resolve created_at timestamp for a plan from available metadata.

        Checks ``thinking_task`` first (plans from ``create_extended_plan``),
        then ``thinking_playbook`` (plans from ``_finalize_playbook``).

        Returns:
            ISO timestamp string (at least 10 chars for date extraction).

        Raises:
            FrameworkError: If plan_id is not found in either table.
        """
        # 1. Check thinking_task (existing plan path)
        result = self._state_service.read_state(
            namespace=NAMESPACE,
            query={
                "table": "thinking_task",
                "filters": {"plan_id": plan_id, "is_deleted": 0},
            },
        )
        records = result.get("data", {}).get("records", [])
        if records:
            return str(records[0].get("created_at", ""))

        # 2. Check thinking_playbook (playbook-created plan path)
        pb_result = self._state_service.read_state(
            namespace=NAMESPACE,
            query={
                "table": "thinking_playbook",
                "filters": {"plan_id": plan_id, "is_deleted": 0},
            },
        )
        pb_records = pb_result.get("data", {}).get("records", [])
        if pb_records:
            return str(pb_records[0].get("created_at", ""))

        raise FrameworkError(
            message=f"Plan not found: {plan_id}",
            error_code=ErrorCode.TASK_NOT_FOUND,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # PLANNING INFERENCE VERTEX
    # ─────────────────────────────────────────────────────────────────────────

    _PLANNING_RESULT_PROCESSOR_TARGET = "service_interface::thinking_service::process_planning_results"
    _PURPOSE_PLAYBOOK_PLANNING = "playbook_planning"
    _RESUME_PROCESS_KEY = (
        "service_interface::thinking_service::resume_thinking_completion"
    )

    def process_planning_results(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Planning inference VERTEX — runs in the playbook's planning context.

        Loads the planning context history, builds messages with the
        planning system prompt, and routes the completion per INF-02
        (session-PRIMARY): when the request goes to the ``sys:autonomic``
        holder or the durable queue, this turn returns ``awaiting_completion``
        and ``resume_thinking_completion`` re-enters with the served text;
        only the operator-enabled provider fallback runs the completion
        synchronously here (then submits action definitions with the
        ``result_processor_target`` override, or writes the final playbook +
        plan artifacts — same as the resume path).
        """
        observation = self._extract_planning_observation(params)
        planning_context_id = self._resolve_planning_context_id(observation, state)
        playbook_id = self._resolve_playbook_id_from_context(planning_context_id)

        # Store observation as INPUT event in planning context
        observation_text = json.dumps(observation, indent=2) if observation else ""
        if observation_text:
            self._append_context_event(
                planning_context_id,
                observation_text,
                ContextEventType.INPUT,
                ContextActorType.SYSTEM,
            )

        # Build messages: system + history + current observation
        messages = self._assemble_planning_prompt(planning_context_id)

        # INF-02 session-PRIMARY routing: forwarded/deferred → this turn ends
        # awaiting the resume continuation; provider_fallback → sync below.
        routed = self._route_planning_completion(
            messages, planning_context_id, playbook_id,
        )
        if routed is not None:
            return routed

        # Operator-enabled provider fallback: the bound provider serves the
        # completion synchronously (the pre-INF-02 path).
        completion = self._generate_thinking_completion(
            messages, purpose=self._PURPOSE_PLAYBOOK_PLANNING,
        )
        return self._continue_planning_with_completion(
            completion, planning_context_id, playbook_id,
            session_id=str(state.get("session_id") or ""),
        )

    def _route_planning_completion(
        self,
        messages: list[dict[str, str]],
        planning_context_id: str,
        playbook_id: str,
    ) -> ActionResult | None:
        """Submit the planning completion request; ``None`` = run sync fallback.

        The verdict contract (``InferenceService.submit_completion_request``):
        ``session``/``deferred`` → the request is durably owned by the
        platform (forwarded to the live holder, or queued for the next
        claim's drain) — return the awaiting ActionResult; the flow turn
        terminates cleanly and ``resume_thinking_completion`` re-enters.
        ``provider_fallback`` → the operator explicitly opted the bound
        provider in — the caller runs its own synchronous path.
        """
        service = (
            self.orchestrator_ref.get_service("inference_service")
            if self.orchestrator_ref
            else None
        )
        if service is None:
            raise FrameworkError(
                message="inference_service not available for planning completion",
                error_code=ErrorCode.BACKEND_NOT_AVAILABLE,
            )
        verdict = service.submit_completion_request(
            purpose=self._PURPOSE_PLAYBOOK_PLANNING,
            messages=messages,
            resume_process_key=self._RESUME_PROCESS_KEY,
            correlation={
                "context_id": planning_context_id,
                "playbook_id": playbook_id,
            },
        )
        routing = str(verdict.get("routing") or "")
        if routing == "provider_fallback":
            return None
        request_id = str(verdict.get("request_id") or "")
        self.logger.info(
            "Planning VERTEX awaiting completion (routing=%s request=%s "
            "playbook=%s)", routing, request_id, playbook_id,
        )
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {
                "planning_status": "awaiting_completion",
                "routing": routing,
                "request_id": request_id,
                "playbook_id": playbook_id,
                "planning_context_id": planning_context_id,
            },
            "actions": [],
            "error": None,
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        }

    def resume_thinking_completion(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Re-enter the planning loop with a served completion (INF-02).

        Submitted by the agent_messaging serve verb as the resume
        continuation after ``submit_autonomic_completion``'s CAS win. The
        served row is the argument authority (platform-owned): correlation
        and completion text are re-read from the durable request, never
        trusted from caller params beyond the ``request_id`` key.
        """
        raw = params.get("parameters", params)
        request_id = str(raw.get("request_id", "")).strip()
        if not request_id:
            raise FrameworkError(
                message="resume_thinking_completion requires 'request_id'",
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        row = self._load_served_planning_request(request_id)
        completion, planning_context_id, playbook_id = (
            self._extract_resume_fields(row, request_id)
        )
        self.logger.info(
            "Planning VERTEX resuming from served completion %s "
            "(playbook=%s)", request_id, playbook_id,
        )
        # The resume action fires in the ORIGINATING flow (INF-02 completion
        # routing preserves session identity — JOS-02 V-8), so state carries
        # the planning session.
        return self._continue_planning_with_completion(
            completion, planning_context_id, playbook_id,
            session_id=str(state.get("session_id") or ""),
        )

    def _load_served_planning_request(self, request_id: str) -> dict[str, Any]:
        """Fetch the durable request row; typed rejection unless served + ours."""
        service = (
            self.orchestrator_ref.get_service("inference_service")
            if self.orchestrator_ref
            else None
        )
        if service is None:
            raise FrameworkError(
                message="inference_service not available for completion resume",
                error_code=ErrorCode.BACKEND_NOT_AVAILABLE,
            )
        row = service.get_completion_request(request_id)
        if row is None:
            raise FrameworkError(
                message=f"completion request not found: {request_id}",
                error_code=ErrorCode.TASK_NOT_FOUND,
            )
        status = str(row.get("status") or "")
        purpose = str(row.get("purpose") or "")
        if status != "served" or purpose != self._PURPOSE_PLAYBOOK_PLANNING:
            raise FrameworkError(
                message=(
                    f"completion request {request_id} not resumable: "
                    f"status={status!r} purpose={purpose!r}"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        return dict(row)

    def _extract_resume_fields(
        self, row: dict[str, Any], request_id: str,
    ) -> tuple[str, str, str]:
        """(completion, planning_context_id, playbook_id) — typed if incomplete."""
        correlation = json.loads(str(row.get("correlation") or "{}"))
        planning_context_id = str(correlation.get("context_id") or "")
        playbook_id = str(correlation.get("playbook_id") or "")
        completion = str(row.get("result_text") or "")
        if not planning_context_id or not playbook_id or not completion:
            raise FrameworkError(
                message=(
                    f"completion request {request_id} row is incomplete "
                    "(missing correlation ids or result_text)"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        return completion, planning_context_id, playbook_id

    def _continue_planning_with_completion(
        self,
        completion: str,
        planning_context_id: str,
        playbook_id: str,
        *,
        session_id: str,
    ) -> ActionResult:
        """The planning loop's post-completion half (sync + resume paths).

        Stores the OUTPUT event, then either submits the parsed action
        definitions (with the ``result_processor_target`` override so their
        results re-enter the vertex) or finalizes the playbook + plan
        (whose focus lands in the acting session's buffer — JOS-02).
        """
        self._append_context_event(
            planning_context_id,
            completion,
            ContextEventType.OUTPUT,
            ContextActorType.AGENT,
        )
        actions = self._parse_planning_actions(completion)
        if actions:
            return self._submit_planning_actions(
                actions,
                planning_context_id,
                playbook_id,
            )
        return self._finalize_playbook(completion, playbook_id, session_id)

    def _extract_planning_observation(self, params: dict[str, Any]) -> dict[str, Any]:
        """Extract observation data from VERTEX params."""
        prompt = params.get("prompt", {})
        if isinstance(prompt, dict):
            obs = prompt.get("observation", {})
            if isinstance(obs, dict):
                return obs
        # Fallback: check for direct action_result in params
        if "action_result" in params:
            return {"action_result": params["action_result"]}
        return {}

    def _resolve_planning_context_id(
        self,
        observation: dict[str, Any],
        state: dict[str, Any],
    ) -> str:
        """Determine the planning context ID from observation or state."""
        # From create_playbook result
        action_result = observation.get("action_result", {})
        if isinstance(action_result, dict):
            data = action_result.get("data", action_result)
            if isinstance(data, dict):
                ctx_id = data.get("planning_context_id", "")
                if ctx_id:
                    return str(ctx_id)

        # From action context_id (carried through result_processor_target chain)
        ctx_id = state.get("context_id", "")
        if ctx_id:
            return str(ctx_id)

        raise FrameworkError(
            message="Cannot determine planning_context_id from observation or state",
            error_code=ErrorCode.PARAMETER_ERROR,
        )

    def _resolve_playbook_id_from_context(self, planning_context_id: str) -> str:
        """Look up playbook ID from planning context ID."""
        result = self._state_service.read_state(
            namespace=NAMESPACE,
            query={
                "table": "thinking_playbook",
                "filters": {"planning_context_id": planning_context_id, "is_deleted": 0},
            },
        )
        records = result.get("data", {}).get("records", [])
        if not records:
            raise FrameworkError(
                message=f"No playbook found for planning context {planning_context_id}",
                error_code=ErrorCode.PLAYBOOK_NOT_FOUND,
            )
        return str(records[0].get("id", ""))

    def _build_planning_messages(
        self,
        planning_context_id: str,
    ) -> list[dict[str, str]]:
        """Build the message array for the planning inference loop."""
        messages: list[dict[str, str]] = []

        # System prompt: planning instructions + playbook structure template
        planning_system_prompt = self._get_planning_system_prompt()
        if planning_system_prompt:
            messages.append({"role": "system", "content": planning_system_prompt})

        # Load planning context history (prior turns)
        history = self._load_context_messages(planning_context_id)
        messages.extend(history)

        return messages

    def _get_planning_system_prompt(self) -> str:
        """Return the system prompt for the planning inference loop."""
        return (
            "You are a strategic planning model. Your job is to research the "
            "user's goal, investigate available capabilities, and produce two "
            "artifacts: a PLAYBOOK and an executable PLAN.\n\n"
            "## Available Actions\n"
            "You may submit action definitions to gather information:\n"
            '- search: {"process": {"provider_type": "service_interface", '
            '"provider": "knowledge_service", "function_name": "search"}, '
            '"arguments": {"query": "...", "top_k": 5}}\n'
            '- get_file: {"process": {"provider_type": "service_interface", '
            '"provider": "knowledge_service", "function_name": "get_file"}, '
            '"arguments": {"name": "...", "path": "..."}}\n\n'
            "When you need information, output a JSON block:\n"
            '```json\n{"actions": [<action_definition>]}\n```\n\n'
            "## Final Output\n"
            "When you have enough information, output the playbook and plan:\n"
            "```playbook\n<full playbook markdown with <!-- section: id --> markers>\n```\n\n"
            "```plan\n<executable plan with PLAYBOOK and PLAYBOOK_SECTION metadata>\n```\n\n"
            "## Playbook Section Format\n"
            "Every ## section must have a <!-- section: <id> --> marker on the preceding line.\n"
            "Section IDs are lowercase \\w+ strings (letters, digits, underscores).\n"
        )

    def _parse_planning_actions(self, text: str) -> list[dict[str, Any]]:
        """Parse action definitions from thinking model output.

        Looks for JSON blocks containing an ``actions`` array.
        Returns empty list if no actions found.
        """
        # Look for ```json ... ``` blocks with actions
        json_block_re = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)
        for match in json_block_re.finditer(text):
            block = match.group(1).strip()
            parsed = json.loads(block)
            if isinstance(parsed, dict) and "actions" in parsed:
                actions = parsed["actions"]
                if isinstance(actions, list) and actions:
                    return [a for a in actions if isinstance(a, dict)]
        return []

    def _submit_planning_actions(
        self,
        actions: list[dict[str, Any]],
        planning_context_id: str,
        playbook_id: str,
    ) -> ActionResult:
        """Submit action definitions with result_processor_target override."""
        submitted: list[dict[str, Any]] = []
        for action_def in actions:
            # Inject result_processor_target to route results back to this VERTEX
            action_def["result_processor_target"] = self._PLANNING_RESULT_PROCESSOR_TARGET
            # Carry planning context through the action chain
            action_def["context_id"] = planning_context_id
            submitted.append(action_def)

        self.logger.info(
            "Planning VERTEX submitting %d actions for playbook %s",
            len(submitted),
            playbook_id,
        )
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "actions": submitted,
            "data": {
                "playbook_id": playbook_id,
                "planning_context_id": planning_context_id,
                "planning_status": "in_progress",
                "actions_submitted": len(submitted),
            },
        }

    def _finalize_playbook(
        self,
        text: str,
        playbook_id: str,
        session_id: str,
    ) -> ActionResult:
        """Parse and write the final playbook + plan artifacts."""
        playbook_content = extract_fenced_block(text, "playbook")
        plan_content = extract_fenced_block(text, "plan")

        if not playbook_content:
            raise FrameworkError(
                message="Planning output missing ```playbook``` block",
                error_code=ErrorCode.OPERATION_FAILED,
            )
        if not plan_content:
            raise FrameworkError(
                message="Planning output missing ```plan``` block",
                error_code=ErrorCode.OPERATION_FAILED,
            )

        # Write playbook to knowledge base
        kb_path = self._write_playbook_to_knowledge_base(playbook_id, playbook_content)

        # Update playbook record with KB path if it changed
        if kb_path:
            self._state_service.update_state(
                namespace=NAMESPACE,
                query={"table": "thinking_playbook", "filters": {"id": playbook_id}},
                updates={"knowledge_base_path": kb_path},
            )

        # Mark old plan complete if this is a patch (phase transition)
        self._retire_old_plan(playbook_id)

        # Generate plan ID and write plan
        plan_id: str = self._state_service.generate_id(prefix="pln-")
        plan_kb_path = self._write_plan_to_knowledge_base(plan_id, plan_content)

        # Parse and focus the plan in the acting session's memory (JOS-02)
        memory_service = self._session_memory(session_id)
        plan_text = normalize_content(plan_content)
        parsed_plan = parse(plan_text)
        plan_text = normalize_for_new_plan_install(parsed_plan)

        plan_window = _build_plan_window(plan_text, plan_id)
        tags = ["plan", f"plan:{plan_id}", f"playbook:{playbook_id}"]
        if plan_kb_path:
            tags = self._set_kb_path_tag(tags, plan_kb_path)
        remember_result: dict[str, Any] = memory_service.remember(
            content=plan_window,
            embed=False,
            tags=tags,
        )
        memory_id: str = remember_result.get("memory_id", "")
        focused = self._focus_plan(memory_id, memory_service)

        # Update playbook record with plan_id
        self._state_service.update_state(
            namespace=NAMESPACE,
            query={"table": "thinking_playbook", "filters": {"id": playbook_id}},
            updates={"plan_id": plan_id},
        )

        self.logger.info(
            "Finalized playbook %s with plan %s (focused=%s)",
            playbook_id,
            plan_id,
            focused,
        )
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {
                "playbook_id": playbook_id,
                "plan_id": plan_id,
                "memory_id": memory_id,
                "focused": focused,
                "kb_written": bool(plan_kb_path),
                "planning_status": "completed",
            },
        }

    def _retire_old_plan(self, playbook_id: str) -> None:
        """Mark the playbook's current plan as completed (phase transition).

        If the playbook already has a plan_id linked, marks the associated
        thinking_task as completed. This frees the plan slot for the new plan.
        No-op if no plan is currently linked.
        """
        record = self._get_playbook_record(playbook_id)
        old_plan_id = record.get("plan_id")
        if not old_plan_id:
            return

        # Find the thinking_task that owns this plan_id
        result = self._state_service.read_state(
            namespace=NAMESPACE,
            query={
                "table": "thinking_task",
                "filters": {"plan_id": str(old_plan_id), "is_deleted": 0},
            },
        )
        records = result.get("data", {}).get("records", [])
        if not records:
            return

        task_id = records[0].get("id")
        if task_id:
            self._state_service.update_state(
                namespace=NAMESPACE,
                query={"table": "thinking_task", "filters": {"id": task_id}},
                updates={"status": STATUS_COMPLETED},
            )
            self.logger.info(
                "Retired old plan %s (task %s) for playbook %s",
                old_plan_id,
                task_id,
                playbook_id,
            )
