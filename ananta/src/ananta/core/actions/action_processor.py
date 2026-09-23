"""
ActionProcessor - Executes individual actions via the plugin system.

Simple, reliable action execution that replaces the complex trigger-based approach.

Phase 1 Enhancement: ExecutionContext integration for runtime placeholder resolution.
"""

import dataclasses
import json
import logging
import re
from typing import TYPE_CHECKING, Any, Final, Protocol

from ananta.core.actions.action_factory import validate_process_arguments
from ananta.core.domain.enums import ActionStatus
from ananta.core.domain.status import is_status_match
from ananta.core.orchestration import placeholder_utils
from ananta.core.plugins.plugin_manager import PluginManager
from ananta.core.services.call_context import CallContext
from ananta.core.state.execution_token_context import action_execution_context
from ananta.core.templates.template_functions import TemplateFunctionRegistry
from ananta.error_handling import FrameworkError
from ananta.services.state_service import StateService

if TYPE_CHECKING:
    from ananta.core.orchestration.execution_context import ExecutionContextManager
    from ananta.services.context_management.content_storage import FileContextContentStorage
    from ananta.services.context_management.service import ContextManagementService


class IOInterfaceRegistryProtocol(Protocol):
    """Protocol for IOInterfaceRegistry used for IO plugin detection."""

    def is_registered(self, namespace: str) -> bool:
        """Check if namespace is a registered IO interface plugin."""
        ...

logger = logging.getLogger(__name__)

# Non-secret ROUTING/display identity a bridge process_call flow carries into
# the handler's ``state`` (never an authenticated principal — that rides
# ``authenticated_principal`` and is lifted separately). Two families:
# ``inference_vertex_*`` is the originating bridge's OWN registered identity
# (REL-01 Fork 4); ``caller_attribution_*`` is the server-derived provenance of
# a caller holding no registered bridge identity of its own (§34.6, the local
# CLI). Every value is a plain string, so one copy loop lifts both.
_CALLER_IDENTITY_KEYS: Final[tuple[str, ...]] = (
    "inference_vertex_role",
    "inference_vertex_session_id",
    "caller_attribution_agent_id",
    "caller_attribution_instance_id",
    "caller_attribution_label",
    "caller_attribution_role",
)

# Raw bridge-delivery envelopes must stay byte-identical to their producer's
# result. Keep these literals byte-identical to
# agent_messaging_plugin.platform_surface._DELIVER_RESULT_PROCESS_KEY and
# _DELIVER_ERROR_PROCESS_KEY. The opaque-relay smoke detects drift; core must
# not import a plugin merely to perform a runtime cross-check.
_OPAQUE_RELAY_PROCESS_KEYS: Final[frozenset[str]] = frozenset(
    {
        "plugin::agent_messaging_plugin::deliver_result",
        "plugin::agent_messaging_plugin::deliver_error",
    }
)


class ActionRecorderProtocol(Protocol):
    """Protocol for action recorder interface."""

    def update_action_result(self, action_id: str, result: dict[str, object]) -> None:
        """Update action result in database."""
        ...


class OrchestratorProtocol(Protocol):
    """Protocol for Orchestrator interface - enables service-agnostic architecture."""

    @property
    def APP_HOME(self) -> str:  # noqa: N802
        """Application home directory."""
        ...

    @property
    def action_recorder(self) -> ActionRecorderProtocol | None:
        """Action recorder for correlation tracking."""
        ...

    def get_service(self, service_name: str) -> object | None:
        """Get service instance by name."""
        ...


class InferenceServiceProtocol(Protocol):
    """Protocol for InferenceService interface.

    Note: process_inference_request was removed per no-back-compat policy.
    Use process_error or process_results instead.
    """



# Protocol for action parameter type to avoid circular imports
class QueuedActionProtocol(Protocol):
    """Protocol for queued action objects."""

    id: str
    process_key: str
    parameters: str
    created_at: str
    session_id: str | None
    flow_id: str | None
    context_id: str | None  # Platform context ID for OUTPUT event correlation
    result_processor: str | None
    template_namespace: str | None
    flow_token_id: str | None
    # W-VAULT-INTERFACE-EXTEND (P0 Tier 1): plugin-originated enqueue paths
    # stamp the submitting plugin's identity here. External / operator MCP
    # calls leave None and the principal_kind derives from the
    # authenticated_principal injected via state. CallContext.calling_plugin
    # is NEVER inferred from the process key's provider.
    # Stamping happens at enqueue time, not here. The protocol carries the
    # field so consumers (ActionProcessor) read it. A queued-action shape that
    # predates the field returns None via the getattr() defensive read in
    # `_build_call_context` — harmless for non-call-context methods.
    source_plugin: str | None


class ActionProcessor:
    """
    Processes individual actions by executing them via the plugin system.

    Much simpler than the previous trigger-based execution.
    """

    def __init__(
        self,
        plugin_manager: PluginManager,
        state_service: StateService | None = None,
        action_factory: object | None = None,
        discovery_service: object | None = None,
        execution_context_manager: "ExecutionContextManager | None" = None,
        blob_storage_service: object | None = None,
        orchestrator: OrchestratorProtocol | None = None,
        process_registry: dict[str, object] | None = None,
        memory_service: object | None = None,
        knowledge_service: object | None = None,
        app_home: str | None = None,
        io_interface_registry: "IOInterfaceRegistryProtocol | None" = None,
    ) -> None:
        """Initialize ActionProcessor with service-agnostic architecture.

        Services (inference, embedding, vector) are resolved dynamically via
        orchestrator.get_service() instead of being passed as constructor parameters.

        Args:
            app_home: Application home directory. Injected via DI, never read from os.environ.
        """
        self.plugin_manager = plugin_manager
        self.state_service = state_service
        # Dependencies for action creation (action_factory only, no action_recorder)
        self.action_factory = action_factory
        self.discovery_service = discovery_service
        self.execution_context_manager = execution_context_manager
        self.blob_storage_service = blob_storage_service
        self.orchestrator = orchestrator
        self.process_registry = process_registry or {"processes": {}}
        self.memory_service = memory_service
        self.knowledge_service = knowledge_service
        self._app_home = app_home
        self._io_interface_registry = io_interface_registry

        # Platform context management (lazy initialized)
        self._context_management_service: ContextManagementService | None = None
        self._content_storage: FileContextContentStorage | None = None
        self._context_initialized = False

        # Initialize template function registry for proper template resolution
        try:
            # Type narrowing for Protocol compatibility
            plugin_manager_arg: object = plugin_manager
            discovery_service_arg: object | None = discovery_service if discovery_service else None
            memory_service_arg: object | None = memory_service if memory_service else None
            knowledge_service_arg: object | None = knowledge_service if knowledge_service else None
            self.template_registry = TemplateFunctionRegistry(
                state_service=state_service,
                action_manager=None,  # Not needed for basic template resolution
                plugin_manager=plugin_manager_arg,  # type: ignore[arg-type]
                discovery_service=discovery_service_arg,  # type: ignore[arg-type]
                memory_service=memory_service_arg,  # type: ignore[arg-type]
                knowledge_service=knowledge_service_arg,
            )
        except Exception as e:
            logger.critical(f"TemplateFunctionRegistry initialization failed: {e}")
            raise

    @property
    def app_home(self) -> str:
        """Get APP_HOME from injected config.

        APP_HOME is injected via DI to avoid reading os.environ at runtime.
        Falls back to orchestrator.APP_HOME if not directly injected.
        """
        if self._app_home:
            return self._app_home

        # Fall back to orchestrator if available
        if self.orchestrator:
            return self.orchestrator.APP_HOME

        raise RuntimeError(
            "APP_HOME not available - ActionProcessor requires app_home injection or "
            "orchestrator with APP_HOME attribute"
        )

    def _ensure_context_initialized(self) -> None:
        """Lazily initialize context management components.

        Initializes context_management_service and shared content_storage on first use.
        These are needed for platform mode OUTPUT event storage.
        """
        if self._context_initialized:
            return

        self._context_initialized = True

        if not self.orchestrator:
            return

        # Get context_management_service from orchestrator
        ctx_mgmt = self.orchestrator.get_service("context_management_service")
        if ctx_mgmt:
            self._context_management_service = ctx_mgmt  # type: ignore[assignment]
            logger.debug("ActionProcessor: context_management_service initialized")

            # Use shared content storage from ContextManagementService
            # This ensures all context events (INPUT/OUTPUT) use the same storage location
            content_storage = getattr(self._context_management_service, "content_storage", None)
            if content_storage:
                self._content_storage = content_storage
                logger.debug("ActionProcessor: using shared content_storage from ContextManagementService")
            else:
                logger.warning("ContextManagementService missing content_storage attribute")

    def _prepare_arguments(self, action: QueuedActionProtocol) -> dict[str, object] | None:
        """Parse and prepare arguments for execution. Returns None on error."""
        parse_result = self._parse_action_parameters(action)
        if "error" in parse_result:
            return None

        arguments_value = parse_result["data"]
        if not isinstance(arguments_value, dict):
            return None

        arguments = arguments_value

        # The action's notes string (the agent's `reason`) is intentionally NOT
        # injected into the parameter dict. Doing so would collide with any
        # process that declares its own `notes` parameter (e.g. render_score,
        # render_note_sequence in musical_synthesis_plugin). Consumers that
        # need the action notes read them via `action.notes` directly.

        # Apply template resolution
        arguments = self._apply_templates_if_needed(arguments, action)

        return arguments

    def _resolve_placeholders(
        self, arguments: dict[str, object], action: QueuedActionProtocol
    ) -> dict[str, object]:
        """Resolve placeholders from ExecutionContext.

        FAIL-FAST: Raises on missing context or resolution failure.
        """
        if action.process_key in _OPAQUE_RELAY_PROCESS_KEYS:
            return arguments

        # FAIL-FAST: Require execution context manager
        if not self.execution_context_manager:
            raise FrameworkError(
                message="Placeholder resolution requires execution_context_manager",
                error_code="action_processor.placeholder_missing_context_manager",
                details={"action_id": action.id},
            )

        # FAIL-FAST: Require flow_id
        if not action.flow_id:
            raise FrameworkError(
                message="Placeholder resolution requires flow_id",
                error_code="action_processor.placeholder_missing_flow_id",
                details={"action_id": action.id},
            )

        context = self.execution_context_manager.get_context(action.flow_id)
        # FAIL-FAST: Require context to exist
        if not context:
            raise FrameworkError(
                message=f"ExecutionContext not found for flow_id={action.flow_id}",
                error_code="action_processor.placeholder_context_not_found",
                details={"action_id": action.id, "flow_id": action.flow_id},
            )

        resolved = placeholder_utils.replace_placeholders_recursive(
            arguments, context, stop_at_action_boundary=True
        )
        # FAIL-FAST: Require valid dict result
        if not isinstance(resolved, dict):
            raise FrameworkError(
                message=f"Placeholder resolution returned invalid type: {type(resolved)}",
                error_code="action_processor.placeholder_invalid_result_type",
                details={"action_id": action.id, "result_type": str(type(resolved))},
            )
        return resolved

    def _store_result_in_context(
        self, action: QueuedActionProtocol, result: dict[str, object]
    ) -> None:
        """Store result in ExecutionContext.

        FAIL-FAST: Raises on missing context or storage failure.
        """
        # FAIL-FAST: Require execution context manager
        if not self.execution_context_manager:
            raise FrameworkError(
                message="Result storage requires execution_context_manager",
                error_code="action_processor.store_result_missing_context_manager",
                details={"action_id": action.id},
            )

        # FAIL-FAST: Require flow_id
        if not action.flow_id:
            raise FrameworkError(
                message="Result storage requires flow_id",
                error_code="action_processor.store_result_missing_flow_id",
                details={"action_id": action.id},
            )

        context = self.execution_context_manager.get_context(action.flow_id)
        # FAIL-FAST: Require context to exist
        if not context:
            raise FrameworkError(
                message=f"ExecutionContext not found for flow_id={action.flow_id}",
                error_code="action_processor.store_result_context_not_found",
                details={"action_id": action.id, "flow_id": action.flow_id},
            )

        schema = self._get_return_value_schema(action.process_key)
        schema_dict = self._convert_schema_to_dict(schema)

        context.store_result(
            step_id=action.id,
            result=self._build_execution_context_payload(result, schema_dict),
            schema=schema_dict,
        )

    def _convert_schema_to_dict(self, schema: object) -> dict[str, object] | None:
        """Convert ReturnValueSchema object to dict if needed."""
        to_dict_method = getattr(schema, "to_dict", None)
        if callable(to_dict_method):
            result = to_dict_method()
            if isinstance(result, dict):
                return result
            return None
        return schema if isinstance(schema, dict) else None

    def execute_action(self, action: QueuedActionProtocol) -> dict[str, object]:
        """Execute a queued action and return the result."""
        try:
            provider_type, provider, method_name = self._parse_process_key(action.process_key)

            arguments = self._prepare_arguments(action)
            if arguments is None:
                return {"success": False, "error": "Failed to parse action parameters"}

            resolved = self._resolve_placeholders(arguments, action)
            if isinstance(resolved, str):
                return {"success": False, "error": resolved}
            arguments = resolved

            result = self._execute_by_provider_type(
                provider_type, provider, method_name, arguments, action
            )

            error_value = result.get("error")
            if error_value:
                return {"success": False, "error": error_value}

            self._store_result_in_context(action, result)
            # Inject action_status="completed" as the canonical success
            # marker. ``**result`` comes last so a service that explicitly
            # sets its own action_status keeps that value (e.g. a service
            # returning {"action_status": "error", ...} as a soft failure).
            # The default exists so service-interface methods that return a
            # plain payload dict — e.g. ``register_source`` returning
            # {"source_id", "outcome"} — pass the result-contract validator
            # at ``contracts.py:_check_result_status_completed`` instead of
            # tripping ``result_status_not_completed`` on a missing field.
            # Without this default, every service method that doesn't
            # hand-wrap an ActionResult envelope explodes at the validator
            # boundary (2026-05-31: ledger boot cascade traced here).
            return {
                "success": True,
                "action_status": ActionStatus.COMPLETED.value,
                **result,
            }

        except Exception as e:
            logger.error(f"Error executing action {action.id}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    def _parse_action_parameters(self, action: QueuedActionProtocol) -> dict[str, object]:
        """Parse JSON parameters from action."""
        try:
            arguments = json.loads(action.parameters) if action.parameters else {}
            return {"data": arguments}
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON parameters: {e}")
            return {"error": f"Invalid JSON parameters: {e}"}

    def _apply_templates_if_needed(
        self, arguments: dict[str, object], action: QueuedActionProtocol
    ) -> dict[str, object]:
        """Apply template resolution if template patterns are present.

        FAIL-FAST: Raises on template resolution failure (no silent fallback).
        """
        if action.process_key in _OPAQUE_RELAY_PROCESS_KEYS:
            return arguments

        if not self._has_template_patterns(arguments):
            return arguments

        resolved = self._resolve_all_templates(arguments, action)
        # Type narrow: ensure we got back a dict
        if isinstance(resolved, dict):
            return resolved
        else:
            raise FrameworkError(
                message=f"Template resolution returned unexpected type: {type(resolved)}",
                error_code="action_processor.template_resolution_invalid_type",
                details={"action_id": action.id, "result_type": str(type(resolved))},
            )

    def _execute_by_provider_type(
        self,
        provider_type: str,
        provider: str,
        method_name: str,
        arguments: dict[str, object],
        action: QueuedActionProtocol,
    ) -> dict[str, object]:
        """Execute action based on provider type."""
        if provider_type == "plugin":
            return self._execute_plugin_action(provider, method_name, arguments, action)
        elif provider_type == "service_interface":
            return self._execute_service_interface_action(provider, method_name, arguments, action)
        else:
            return {"success": False, "error": f"Unsupported provider_type '{provider_type}'"}

    def _execute_plugin_action(
        self,
        provider: str,
        method_name: str,
        arguments: dict[str, object],
        action: QueuedActionProtocol,
    ) -> dict[str, object]:
        """Execute plugin-based action."""
        plugin = self.plugin_manager.get_plugin(provider)
        if not plugin:
            return {"success": False, "error": f"Plugin '{provider}' not found"}

        result = self._execute_plugin_method(plugin, method_name, arguments, action)

        # ENFORCE PLUGIN CONTRACT: Plugins MUST return ActionResult structure
        # Required fields: action_status, data, actions, error, timestamp
        if not isinstance(result, dict):
            raise RuntimeError(
                f"PLUGIN CONTRACT VIOLATION: Plugin '{provider}.{method_name}' must return dict, "
                f"got {type(result).__name__}"
            )

        required_fields = ["action_status", "data", "actions", "error", "timestamp"]
        missing_fields = [f for f in required_fields if f not in result]

        if missing_fields:
            raise RuntimeError(
                f"PLUGIN CONTRACT VIOLATION: Plugin '{provider}.{method_name}' missing required fields: "
                f"{missing_fields}. Required: {required_fields}"
            )

        # Validate field types
        if not isinstance(result["data"], dict):
            raise RuntimeError(
                f"PLUGIN CONTRACT VIOLATION: Plugin '{provider}.{method_name}' field 'data' must be dict, "
                f"got {type(result['data']).__name__}"
            )

        if not isinstance(result["actions"], list):
            raise RuntimeError(
                f"PLUGIN CONTRACT VIOLATION: Plugin '{provider}.{method_name}' field 'actions' must be list, "
                f"got {type(result['actions']).__name__}"
            )

        # Store OUTPUT event for IO plugin post_message (conversation history)
        self._handle_plugin_post_message_output(provider, method_name, result, action)

        return result

    # --- Helper methods for _execute_service_interface_action (complexity reduction) ---

    def _resolve_service(self, provider: str) -> object:
        """Resolve service by provider name."""
        # Handle legacy services stored as instance variables
        if provider == "state_service":
            if not self.state_service:
                raise FrameworkError(f"Service '{provider}' not available")
            return self.state_service

        if provider == "discovery_service":
            if not self.discovery_service:
                raise FrameworkError(f"Service '{provider}' not available")
            return self.discovery_service

        if provider == "blob_storage_service":
            if not self.blob_storage_service:
                raise FrameworkError(f"Service '{provider}' not available")
            return self.blob_storage_service

        # Dynamic resolution via orchestrator
        if not self.orchestrator:
            raise FrameworkError(f"Service '{provider}' requested but orchestrator not available")

        resolved = self.orchestrator.get_service(provider)
        if not resolved:
            raise FrameworkError(f"Service '{provider}' not available in orchestrator")
        return resolved

    def _execute_vertex_inference(
        self,
        service: object,
        method_name: str,
        arguments: dict[str, object],
        action: QueuedActionProtocol,
    ) -> dict[str, object]:
        """Execute VERTEX inference methods (process_error, process_results).

        Both methods use standardized signature: method(params, state)

        Normal template flow: arguments = {"model": {...}, "prompt": {...}}
        Async completion flow: arguments = {"params": {"model": {...}, "prompt": {...}, ...}, "state": {...}}
        """
        # Unwrap "params" wrapper from async completion handlers
        params_candidate = arguments.get("params")
        if (
            isinstance(params_candidate, dict)
            and "model" in params_candidate
            and "prompt" in params_candidate
        ):
            args = params_candidate
        else:
            args = arguments
        state_dict: dict[str, object] = {
            "session_id": action.session_id,
            "flow_id": action.flow_id,
        }
        if action.context_id:
            state_dict["context_id"] = action.context_id
        method = getattr(service, method_name)

        # Inject action_name for process_error so the advancement guard
        # in advancement.py can skip plan advancement during error recovery.
        # Do NOT inject for process_results — the FLOW_COMPLETE guard at
        # inference_transaction.py:66 checks action_name == "process_results"
        # and would false-positive on new flows that have no plan yet.
        if method_name == "process_error":
            args["action_name"] = method_name

        # All VERTEX methods use standardized params signature
        result = method(params=args, state=state_dict)

        if not isinstance(result, dict):
            raise FrameworkError(
                f"Service method 'inference_service.{method_name}' must return dict"
            )
        return dict(result)

    def _needs_injection(
        self,
        param_name: str,
        parameters_schema: dict[str, object],
        filtered: dict[str, object],
    ) -> bool:
        """Check if a parameter needs injection."""
        return param_name in parameters_schema and param_name not in filtered

    def _filter_and_inject_arguments(
        self,
        arguments: dict[str, object],
        parameters_schema: dict[str, object],
        action: QueuedActionProtocol,
        process_def: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Validate registered arguments and inject session/flow context.

        W-VAULT-INTERFACE-EXTEND: when ``process_def`` declares
        ``requires_call_context: True`` (set via
        ``@service_interface_process(requires_call_context=True)``), the
        server constructs a fresh ``CallContext`` from the action's
        ``source_plugin`` (or the authenticated_principal injected via
        state) and ALWAYS overwrites any caller-supplied ``call_context``
        value. The server-stamped context is the only source of truth
        for per-method admin/operator gating + per-key namespace
        ownership checks downstream.
        """
        validate_process_arguments(action.process_key, parameters_schema, arguments)
        filtered: dict[str, object] = dict(arguments)

        # Inject standard context fields
        self._inject_session_context(filtered, parameters_schema, action)

        # W-VAULT-INTERFACE-EXTEND: server-side CallContext injection.
        # Runs AFTER session-context injection so the
        # authenticated_principal lifted into state is observable, but
        # CallContext construction reads action.source_plugin first and
        # falls back to the principal source.
        if process_def is not None:
            self._inject_call_context(filtered, process_def, action)

        return filtered

    def _inject_session_context(
        self,
        filtered: dict[str, object],
        parameters_schema: dict[str, object],
        action: QueuedActionProtocol,
    ) -> None:
        """Inject session_id, flow_id, and state into filtered arguments.

        M5 §14.8 (Codex B3): when ``state`` is injected, lift
        ``authenticated_principal`` from the flow's ``trigger_data``
        into the state dict so service handlers (e.g.,
        ``acknowledge_quarantine``, ``shipper_self_revoke``) can do
        server-side authz on bridge identity without trusting caller-
        supplied arguments. Service-handler methods that need this
        MUST declare ``state`` in their ``parameters=``; absent that
        declaration ``_needs_injection`` short-circuits and the
        principal never reaches the handler — the M1
        ``extract_authenticated_principal`` helper raises
        ``PermissionError`` so the failure is fail-loud at the handler,
        not silent.
        """
        if self._needs_injection("session_id", parameters_schema, filtered) and action.session_id:
            filtered["session_id"] = action.session_id

        if self._needs_injection("flow_id", parameters_schema, filtered) and action.flow_id:
            filtered["flow_id"] = action.flow_id

        # ``state`` is server context BY DEFINITION — no legitimate caller
        # supplies it as an argument. Injection therefore ALWAYS overwrites
        # any caller-supplied value (JOS-02 V-2; same defensive-overwrite
        # posture as ``call_context`` below), closing the session/principal
        # spoof channel that merge-if-absent left open.
        if "state" in parameters_schema:
            state_dict: dict[str, object] = {
                "session_id": action.session_id,
                "flow_id": action.flow_id,
            }
            if action.context_id:
                state_dict["context_id"] = action.context_id
            # M5 §14.8: lift authenticated_principal from the flow's
            # trigger_data when present (placed there by
            # PlatformSurface._build_process_call_trigger_data for any
            # bridge-driven call whose bridge carries a non-empty client_id).
            if action.flow_id:
                trigger_data = self._get_flow_trigger_data(action.flow_id)
                if isinstance(trigger_data, dict):
                    principal = trigger_data.get("authenticated_principal")
                    if isinstance(principal, dict):
                        state_dict["authenticated_principal"] = principal
            filtered["state"] = state_dict

    def _inject_call_context(
        self,
        filtered: dict[str, object],
        process_def: dict[str, object],
        action: QueuedActionProtocol,
    ) -> None:
        """W-VAULT-INTERFACE-EXTEND: server-build + inject CallContext.

        ALWAYS overwrites any caller-supplied ``call_context`` argument
        — the filter at the top of ``_filter_and_inject_arguments``
        already dropped it (``call_context`` is server-injected, not in
        the user-facing parameters_schema), but a defensive overwrite
        keeps the spoofing surface closed.

        Construction order:
          1. ``action.source_plugin`` — plugin-originated enqueue paths
             stamp the submitting plugin's identity at enqueue time.
          2. ``trigger_data.authenticated_principal.operator_equivalent``
             — bridge-authenticated principal with the operator-equivalent
             marker (per ``oauth_client.operator_equivalent``).
          3. ``trigger_data.authenticated_principal.client_id`` — any
             other authenticated external principal.
          4. ``CallContext.for_operator()`` default — operator-direct CLI
             / MCP path with no authenticated_principal in trigger_data.

        ``calling_plugin`` is NEVER inferred from the process key's
        provider; the only sources of truth are (1) the stamped
        source_plugin and (2) the authenticated_principal lifted into
        state. Spoofing-negative smoke covers caller-supplied
        ``call_context`` / ``source_plugin`` overwrite.
        """
        if not process_def.get("requires_call_context", False):
            return
        filtered["call_context"] = self._build_call_context(action)

    def _build_call_context(self, action: QueuedActionProtocol) -> CallContext:
        """Construct a CallContext for the given queued action. Server-only."""
        source_plugin = getattr(action, "source_plugin", None)
        if isinstance(source_plugin, str) and source_plugin:
            return CallContext.for_plugin(source_plugin)
        if not action.flow_id:
            return CallContext.for_operator()
        trigger_data = self._get_flow_trigger_data(action.flow_id)
        if not isinstance(trigger_data, dict):
            return CallContext.for_operator()
        principal = trigger_data.get("authenticated_principal")
        if not isinstance(principal, dict):
            return CallContext.for_operator()
        client_id = principal.get("client_id")
        client_id_str = client_id if isinstance(client_id, str) else None
        if principal.get("operator_equivalent"):
            return CallContext.for_operator_equivalent(client_id_str)
        return CallContext.for_external(client_id_str)

    def _check_compaction_after_event(self, context_id: str) -> None:
        """Check and trigger compaction after event append.

        Called after OUTPUT events are appended. Gets config from inference_service
        and delegates to ContextManagementService.check_and_trigger_compaction.

        FAIL-FAST: Raises if required services or config are missing (no silent skip).

        Raises:
            FrameworkError: If compaction fails or services/config unavailable.
        """
        from ananta.services.context_management.config import ContextManagementConfig

        if not self._context_management_service:
            raise FrameworkError(
                message="Compaction requires context_management_service but none available",
                error_code="compaction.context_service_required",
            )

        if not self.orchestrator:
            raise FrameworkError(
                message="Compaction requires orchestrator but none available",
                error_code="compaction.orchestrator_required",
            )

        # Get config from inference_service (v19 config path)
        inference_svc = self.orchestrator.get_service("inference_service")
        if not inference_svc:
            raise FrameworkError(
                message="Compaction requires inference_service but none available",
                error_code="compaction.inference_service_required",
            )

        get_config = getattr(inference_svc, "get_context_management_config", None)
        if not callable(get_config):
            raise FrameworkError(
                message="inference_service missing get_context_management_config method",
                error_code="compaction.config_method_required",
            )

        config = get_config()
        if not isinstance(config, ContextManagementConfig):
            raise FrameworkError(
                message="get_context_management_config returned invalid type",
                error_code="compaction.config_type_error",
                details={"actual_type": type(config).__name__},
            )

        # Delegate to context management service (fail-fast on error)
        compacted = self._context_management_service.check_and_trigger_compaction(
            context_id, config
        )
        if compacted:
            logger.info(f"Compaction triggered for context {context_id}")

    # --- End of helper methods ---

    def _execute_service_interface_action(
        self,
        provider: str,
        method_name: str,
        arguments: dict[str, object],
        action: QueuedActionProtocol,
    ) -> dict[str, object]:
        """Execute service interface action using service-agnostic architecture."""
        process_key = f"service_interface::{provider}::{method_name}"
        process_def = self._validate_process_registration(process_key)
        service = self._resolve_service(provider)

        # Dispatch to specialized handlers for inference service
        if provider == "inference_service":
            return self._dispatch_inference_action(service, method_name, arguments, action)

        # Standard service method execution
        return self._execute_standard_service_method(
            service, provider, method_name, process_def, arguments, action
        )

    def _validate_process_registration(self, process_key: str) -> dict[str, object]:
        """Validate process_key is registered and return its definition."""
        processes = self.process_registry.get("processes", {})
        if not isinstance(processes, dict):
            raise FrameworkError("Invalid process registry structure")

        if process_key not in processes:
            raise FrameworkError(
                f"Service method '{process_key}' not registered in process registry."
            )

        process_def = processes[process_key]
        if not isinstance(process_def, dict):
            raise FrameworkError(f"Invalid process definition for {process_key}")

        return process_def

    def _dispatch_inference_action(
        self,
        service: object,
        method_name: str,
        arguments: dict[str, object],
        action: QueuedActionProtocol,
    ) -> dict[str, object]:
        """Dispatch inference service actions to specialized handlers."""
        vertex_methods = {"process_error", "process_results"}
        if method_name in vertex_methods:
            return self._execute_vertex_inference(service, method_name, arguments, action)

        # REMOVED: process_inference_request - per no-back-compat policy
        # Use process_error or process_results instead
        if method_name == "process_inference_request":
            raise FrameworkError(
                message="process_inference_request was removed - use process_error or process_results",
                error_code="action_processor.deprecated_method_removed",
                details={"method_name": method_name},
            )

        # Fall through to standard execution for other inference methods
        process_key = f"service_interface::inference_service::{method_name}"
        process_def = self._validate_process_registration(process_key)
        return self._execute_standard_service_method(
            service, "inference_service", method_name, process_def, arguments, action
        )

    def _execute_standard_service_method(
        self,
        service: object,
        provider: str,
        method_name: str,
        process_def: dict[str, object],
        arguments: dict[str, object],
        action: QueuedActionProtocol,
    ) -> dict[str, object]:
        """Execute a standard service method with argument filtering."""
        parameters_schema_raw = process_def.get("parameters", {})
        parameters_schema = parameters_schema_raw if isinstance(parameters_schema_raw, dict) else {}

        filtered_args = self._filter_and_inject_arguments(
            arguments, parameters_schema, action, process_def=process_def,
        )
        method = getattr(service, method_name)
        result = method(**filtered_args)

        # Matrix lifecycle plugins (aws_midwife / aws_undertaker / aws_account_admin)
        # return frozen-dataclass DTOs (BirthResult / TeardownResult / AdminResult) per
        # the D2-closure design (workbench/2026-06-02_lifecycle_interfaces_design.md).
        # Auto-serialize dataclasses to dict here so the dispatch contract (dict only)
        # is preserved without forcing every plugin author to call asdict() manually.
        if dataclasses.is_dataclass(result) and not isinstance(result, type):
            result = dataclasses.asdict(result)

        if not isinstance(result, dict):
            raise FrameworkError(f"Service method '{provider}.{method_name}' must return dict")

        self._handle_post_message_output(provider, method_name, result, action, filtered_args)
        return result

    def _handle_post_message_output(
        self,
        provider: str,
        method_name: str,
        result: dict[str, object],
        action: QueuedActionProtocol,
        filtered_args: dict[str, object],
    ) -> None:
        """Store OUTPUT event for post_message if successful."""
        if provider != "io_interface_service" or method_name != "post_message":
            return

        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            return

        self._store_post_message_output(action, filtered_args)

    def _handle_plugin_post_message_output(
        self,
        provider: str,
        method_name: str,
        result: dict[str, object],
        action: QueuedActionProtocol,
    ) -> None:
        """Store OUTPUT event for IO plugin post_message if successful.

        Only fires for plugins registered in the IO interface registry,
        preventing false positives from non-IO plugins with post_message methods.

        Unlike _store_post_message_output (service_interface path), this does NOT
        write to memory_service — IO plugins already do that internally.
        Only the context_management_service OUTPUT event is needed.
        """
        if method_name != "post_message":
            return

        # Only store OUTPUT events for registered IO interface plugins
        if self._io_interface_registry is None or not self._io_interface_registry.is_registered(provider):
            return

        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            return

        # Extract message from the action's arguments (stored in parameters JSON)
        try:
            parameters = json.loads(action.parameters) if action.parameters else {}
        except (json.JSONDecodeError, AttributeError):
            return

        message = parameters.get("message")
        if not message or not isinstance(message, str):
            return

        # Store only the context event (no memory_service write — plugins handle that)
        context_id = action.context_id
        if context_id:
            self._ensure_context_initialized()
            io_metadata = self._get_flow_io_metadata(action)
            # session_id fallback: action always has session_id from REST submit
            io_metadata = self._ensure_session_id_in_metadata(io_metadata, action)
            self._append_output_context_event(context_id, message, io_metadata)
            logger.debug(f"Stored plugin post_message OUTPUT event for {provider}")

    def _build_execution_context_payload(
        self,
        action_result: dict[str, object],
        schema: dict[str, object] | None,
    ) -> dict[str, object]:
        """Merge plugin metadata with data payload so schema properties are available."""
        data_value = action_result.get("data", {})
        if isinstance(data_value, dict):
            data_dict: dict[str, object] = dict(data_value)
        else:
            data_dict = {"result": data_value}

        # Preserve original data object for schemas that expect <<DATA>>
        payload: dict[str, object] = dict(data_dict)
        payload.setdefault("data", data_value if isinstance(data_value, dict) else data_dict)

        if not schema:
            return payload

        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return payload

        for prop_name in properties.keys():
            if prop_name in payload:
                continue
            if prop_name in action_result:
                payload[prop_name] = action_result[prop_name]

        return payload

    def _parse_process_key(self, process_key: str) -> tuple[str, str, str]:
        """
        Parse process_key format: 'provider_type::provider::method_name'
        Supports both 'plugin::plugin_name::method_name' and 'service_interface::service_name::method_name'

        Returns:
            (provider_type, provider, method_name)
        """
        parts = process_key.split("::")
        if len(parts) != 3:
            raise ValueError(
                f"Invalid process_key format: {process_key} - expected 'provider_type::provider::method_name'"
            )

        provider_type, provider, method_name = parts

        # Validate provider_type is supported
        if provider_type not in ["plugin", "service_interface"]:
            raise ValueError(
                f"Invalid provider_type '{provider_type}' in process_key: {process_key} - must be 'plugin' or 'service_interface'"
            )

        return provider_type, provider, method_name

    def _lift_inference_vertex_identity(
        self,
        state: dict[str, object],
        action: QueuedActionProtocol,
    ) -> None:
        """Lift the caller's durable role + originating instance id into ``state``.

        REL-01 Fork 4. ``PlatformSurface._build_process_call_trigger_data`` stamps
        the originating session's durable role (``inference_vertex_role``) and its
        originating ``agent_instance_id`` (``inference_vertex_session_id``) onto
        every bridge ``process_call`` flow's ``trigger_data``. These are non-secret
        ROUTING identity (unlike ``authenticated_principal``), so they lift
        unconditionally here — the plugin-dispatch path builds ``state`` afresh and
        never runs ``_inject_session_context`` (service-interface only).
        ``peer_send_by_name`` reads them to stamp the sender by ROLE
        (reconnect-surviving) rather than ``system:scheduler``, making role-addressed
        sends two-way. Degrade-silent: a missing / roleless / lookup-failed
        ``trigger_data`` leaves ``state`` untouched — roleless is a valid path.

        §34.6 adds the sibling ``caller_attribution_*`` family, stamped by
        ``PlatformSurface._resolve_caller_attribution`` for a caller that holds
        no registered bridge identity of its own (the local CLI's one-shot
        bridge). Its content is likewise SERVER-DERIVED — read out of the peer
        registry, never out of the request — and it is a separate key family on
        purpose: the send verbs stamp a sender from it, while nothing routes
        inference by it.
        """
        if not action.flow_id:
            return
        try:
            trigger_data = self._get_flow_trigger_data(action.flow_id)
        except Exception:  # noqa: BLE001 — degrade-silent: this lift runs for EVERY plugin action, so a malformed / faulted trigger_data (json.loads, state read) must NEVER break a non-role flow
            return
        if not isinstance(trigger_data, dict):
            return
        for key in _CALLER_IDENTITY_KEYS:
            value = trigger_data.get(key)
            if isinstance(value, str) and value:
                state[key] = value

    def _execute_plugin_method(
        self,
        plugin: object,
        method_name: str,
        arguments: dict[str, object],
        action: QueuedActionProtocol,
    ) -> object:
        """
        Execute the specific plugin method with proper error handling.
        """
        # Plugins implementing a typed ABC contract (e.g. MidwifeServiceInterface,
        # MemoryServiceInterface) split the verb across two methods: a typed
        # implementation under the contract name (taking typed kwargs) and a
        # ``@platform_process``-decorated dispatch wrapper under the convention
        # ``<verb>_action`` (taking the standard ``(params, state)`` shape). The
        # plugin-namespace dispatcher resolves method_name from the verb portion
        # of process_key, which lands on the typed ABC method by default. Prefer
        # ``<verb>_action`` when it carries ``_platform_process_metadata`` (the
        # marker the decorator stamps); fall back to the bare name for plugins
        # without the split. Surfaced 2026-06-09 by the exclusive midwife/
        # undertaker service-interface retirement.
        action_wrapper_name = f"{method_name}_action"
        wrapper_candidate = getattr(plugin, action_wrapper_name, None)
        if wrapper_candidate is not None and hasattr(
            wrapper_candidate, "_platform_process_metadata"
        ):
            method_name = action_wrapper_name

        # Get method directly - fail fast if it doesn't exist
        method = getattr(plugin, method_name)

        # Set up plugin context if the plugin supports it
        self._setup_plugin_context(plugin, action)

        # Call the plugin method using the standard plugin interface
        # Plugins get APP_HOME from self.orchestrator_ref.APP_HOME internally
        params = arguments
        state: dict[str, object] = {
            "session_id": action.session_id,
            "flow_id": action.flow_id,
        }
        # Propagate context_id for platform context event correlation
        # This enables inference plugins to resolve context_id from state
        if action.context_id:
            state["context_id"] = action.context_id

        # REL-01 Fork 4: lift the caller's DURABLE role + originating instance
        # id from the flow's trigger_data so plugin verbs (peer_send_by_name)
        # can stamp the sender by ROLE — reconnect-surviving — instead of
        # system:scheduler, making role-addressed dispatch two-way. The plugin
        # path builds ``state`` afresh here and never runs
        # ``_inject_session_context`` (service-interface only), so the lift
        # lives on this path. Degrade-silent: roleless is a valid path.
        self._lift_inference_vertex_identity(state, action)

        # Slice-C §6.1: lift the SERVER-BUILT CallContext into state so an EDGE
        # verb (peer_claim_role's plugin-owner system-slot gate) reads the caller's
        # principal from server-built state — NEVER spoofable caller params. Reuses
        # the vault-hardened _build_call_context (server-stamped source_plugin →
        # for_plugin; else operator/external). Unconditional, mirroring the sibling
        # identity lift above; verbs that don't need it ignore it.
        state["call_context"] = self._build_call_context(action)

        # Get flow_token_id for context propagation
        flow_token_id = getattr(action, "flow_token_id", None)

        # Execute the method with FRG token context
        # Any AsyncJobManager.create_job() calls within this scope will
        # automatically capture the flow_token_id for job-token linking
        with action_execution_context(flow_token_id):
            result = method(params=params, state=state)

        # Template processing removed from ActionProcessor - processes call ActionFactory.submit_result_with_template() themselves

        # All plugin methods should return synchronously

        return result

    def _create_state_service_wrapper(self) -> object:
        """Create a wrapped state service with automatic context injection."""
        if not self.state_service:
            raise RuntimeError("state_service not available for wrapping")

        outer_service = self.state_service

        class ActionProcessorStateService:
            def __init__(self, wrapped_service: "StateService") -> None:
                self._wrapped = wrapped_service

            def __getattr__(self, name: str) -> object:
                attr = getattr(self._wrapped, name)
                if callable(attr) and name in ["write_state", "execute_sql"]:

                    def wrapper(*args: object, **kwargs: object) -> object:
                        if "calling_service" not in kwargs:
                            kwargs["calling_service"] = "ActionProcessor"
                        if "calling_namespace" not in kwargs:
                            kwargs["calling_namespace"] = "ananta.core.action_processor"
                        return attr(*args, **kwargs)

                    return wrapper
                return attr

        return ActionProcessorStateService(outer_service)

    def _inject_service_if_supported(
        self, plugin: object, method_name: str, service: object | None, service_name: str
    ) -> None:
        """Inject a service into plugin if it supports the setter method."""
        if not hasattr(plugin, method_name) or not service:
            return

        setter = getattr(plugin, method_name)
        if callable(setter):
            setter(service)

    def _setup_plugin_context(self, plugin: object, action: QueuedActionProtocol) -> None:
        """Set up plugin context for execution (e.g., state_service injection)."""
        # Inject state_service with context wrapper
        if hasattr(plugin, "set_state_service") and self.state_service:
            wrapped_service = self._create_state_service_wrapper()
            self._inject_service_if_supported(
                plugin, "set_state_service", wrapped_service, "state_service"
            )

        # Inject other services
        self._inject_service_if_supported(
            plugin, "set_action_factory", self.action_factory, "action_factory"
        )
        self._inject_service_if_supported(
            plugin, "set_discovery_service", self.discovery_service, "discovery_service"
        )

        # Set session context if supported
        setter = getattr(plugin, "set_session_context", None)
        if callable(setter):
            session_context: dict[str, object] = {
                "session_id": action.session_id,
                "flow_id": action.flow_id,
                "action_id": action.id,
            }
            if action.context_id:
                session_context["context_id"] = action.context_id
            setter(session_context)

    def _has_template_patterns(self, data: object) -> bool:
        """Check if data contains template patterns that need resolution"""
        data_str = json.dumps(data) if not isinstance(data, str) else data

        # Dual-pattern template recognition
        patterns = [
            r"<<[A-Z_][A-Z0-9_]*>>",  # LOCAL variables: <<USER_INPUT>>
            r"<<<[A-Z_][A-Z0-9_]*>>>",  # GLOBAL variables: <<<CONFIG_VAR>>>
            r"<<<@[^>]*>>>",  # Files: <<<@filename.json>>>
            r"<<<:[^>]*>>>",  # Functions: <<<:provider::func()>>>
        ]

        for pattern in patterns:
            if re.search(pattern, data_str):
                return True
        return False

    def _get_current_state(self, action: QueuedActionProtocol | None = None) -> dict[str, object]:
        """Get current state for dual-pattern template resolution"""
        try:
            state: dict[str, object] = {
                "local_variables": self._resolve_local_variables(action) if action else {},
                "global_variables": (
                    self._get_template_variables(action.template_namespace) if action else {}
                ),
                "APP_HOME": self.app_home,  # Use injected value, not os.environ
            }

            return state

        except Exception as e:
            logger.error(f"Failed to get current state: {e}")
            raise

    def _resolve_local_variables(self, action: QueuedActionProtocol) -> dict[str, object]:
        """
        Resolve LOCAL template variables from plugin_context (per-request isolation).
        NO database queries - direct context lookup for zero overhead.
        """
        # Extract plugin_context from action.parameters JSON (QueuedAction structure)
        plugin_context: dict[str, object] = {}

        if action.parameters:
            try:
                # Parse parameters JSON to access plugin_context
                parameters = json.loads(action.parameters)
                if not isinstance(parameters, dict):
                    logger.error(f"Expected parameters to be dict, got {type(parameters)}")
                    return {}

                plugin_context_value = parameters.get("plugin_context", {})
                if isinstance(plugin_context_value, dict):
                    plugin_context = plugin_context_value

                if plugin_context:
                    return plugin_context
                else:
                    pass

            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse action.parameters JSON: {e}")

        return {}

    def _parse_template_variable_records(
        self, result: dict[str, object], action_namespace: str
    ) -> dict[str, object]:
        """Parse template variables from a list_key_values result.

        The key-value verb returns the rows under ``data.values`` as dicts
        (``key``/``value``/…), replacing the positional ``data.records`` rows the
        raw SELECT returned.

        FAIL-FAST: a non-completed or malformed envelope RAISES (a DB error must
        not masquerade as "no GLOBAL variables" — the caller's contract is
        fail-fast). Only a valid completed result with an empty value list returns
        an empty dict.
        """
        if result.get("action_status") != ActionStatus.COMPLETED.value:
            raise RuntimeError(
                f"list_key_values did not complete for namespace "
                f"{action_namespace!r}: {result.get('error')!r}"
            )

        data = result.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(
                f"list_key_values returned malformed data for namespace "
                f"{action_namespace!r}: {data!r}"
            )

        values = data.get("values")
        if not isinstance(values, list):
            raise RuntimeError(
                f"list_key_values returned malformed values for namespace "
                f"{action_namespace!r}: {values!r}"
            )

        variables: dict[str, object] = {}
        for record in values:
            if not isinstance(record, dict):
                raise RuntimeError(
                    f"list_key_values returned a non-dict record for namespace "
                    f"{action_namespace!r}: {record!r}"
                )
            key = record.get("key")
            if not isinstance(key, str):
                raise RuntimeError(
                    f"list_key_values returned a record without a string key for "
                    f"namespace {action_namespace!r}: {record!r}"
                )
            variables[key] = record.get("value")

        return variables

    def _get_template_variables(self, action_namespace: str | None = None) -> dict[str, object]:
        """Get GLOBAL template variables from database (cross-plugin shared state)."""
        if not action_namespace:
            return {}

        if not self.state_service:
            logger.error("state_service not available - cannot retrieve GLOBAL variables")
            return {}

        try:
            # All key-value rows for the namespace (every scope, matching the old
            # `WHERE namespace = …` with no scope filter); the key-value verb also
            # removes the f-string-interpolated namespace (injection-shaped).
            result = self.state_service.list_key_values(namespace=action_namespace)
            # Convert ActionResult TypedDict to dict[str, object] for helper
            return self._parse_template_variable_records(dict(result), action_namespace)

        except Exception as e:
            # FAIL-FAST: a DB outage / malformed result must surface, not silently
            # degrade to "no GLOBAL variables" (the caller's explicit contract).
            logger.error(f"Failed to get GLOBAL variables from namespace {action_namespace}: {e}")
            raise

    # Legacy method removed - dual-pattern resolution handled by _resolve_all_templates

    def _resolve_all_templates(self, data: object, action: QueuedActionProtocol) -> object:
        """
        Dual-pattern template resolution system.
        - <<VAR>> patterns resolved from plugin_context (LOCAL, per-request)
        - <<<VAR>>> patterns resolved from database (GLOBAL, shared)
        - <<<:func()>>> patterns resolved via template functions
        """
        if isinstance(data, dict):
            return self._resolve_dict_templates(data, action)
        elif isinstance(data, list):
            return self._resolve_list_templates(data, action)
        elif isinstance(data, str):
            return self._resolve_string_templates(data, action)
        else:
            return data

    def _resolve_dict_templates(
        self, data: dict[str, object], action: QueuedActionProtocol
    ) -> dict[str, object]:
        """Resolve templates in dictionary values."""
        dict_result: dict[str, object] = {}
        for key, value in data.items():
            dict_result[key] = self._resolve_all_templates(value, action)
        return dict_result

    def _resolve_list_templates(
        self, data: list[object], action: QueuedActionProtocol
    ) -> list[object]:
        """Resolve templates in list items."""
        list_result: list[object] = []
        for item in data:
            list_result.append(self._resolve_all_templates(item, action))
        return list_result

    def _resolve_string_templates(self, data: str, action: QueuedActionProtocol) -> str:
        """Resolve templates in string values."""
        str_result = data

        # Apply each pattern resolution in sequence
        str_result = self._apply_local_variables(str_result, action)
        str_result = self._apply_global_variables(str_result, action)
        str_result = self._apply_template_functions(str_result, action)

        return str_result

    def _apply_local_variables(self, text: str, action: QueuedActionProtocol) -> str:
        """Apply LOCAL variables <<VAR>> from plugin_context."""
        local_variables = self._resolve_local_variables(action)
        for var_name, var_value in local_variables.items():
            local_pattern = f"<<{var_name}>>"
            if local_pattern in text:
                text = text.replace(local_pattern, str(var_value))
        return text

    def _apply_global_variables(self, text: str, action: QueuedActionProtocol) -> str:
        """Apply GLOBAL variables <<<VAR>>> from database."""
        global_variables = self._get_template_variables(action.template_namespace)
        for var_name, var_value in global_variables.items():
            global_pattern = f"<<<{var_name}>>>"
            if global_pattern in text:
                text = text.replace(global_pattern, str(var_value))
        return text

    def _apply_template_functions(self, text: str, action: QueuedActionProtocol) -> str:
        """Apply FUNCTION calls <<<:provider::func()>>>.

        FAIL-FAST: Raises on missing flow_id or function resolution failure.
        """
        from ananta.core.contexts.action_contexts import TemplateFunctionContext

        # FAIL-FAST: Template functions require flow_id
        if not action.flow_id:
            raise FrameworkError(
                message="Template functions require flow_id but none available",
                error_code="action_processor.template_function_missing_flow_id",
                details={"action_id": action.id, "process_key": action.process_key},
            )

        function_pattern = r"<<<:((service_interface|plugin)::[^>]*?)>>>"
        function_matches = re.finditer(function_pattern, text)

        replacements: list[tuple[str, str]] = []
        for match_obj in function_matches:
            full_match = match_obj.group(0)
            inner_match = match_obj.group(1)
            replacements.append((full_match, inner_match))

        # Build typed context once for all function calls
        template_context = TemplateFunctionContext(
            action_id=action.id,
            process_key=action.process_key,
            session_id=action.session_id,
            flow_id=action.flow_id,
            app_home=self.app_home,
            context_id=action.context_id,
            local_variables={},
            global_variables={},
        )

        for full_pattern, match in reversed(replacements):
            # FAIL-FAST: No silent fallback on function resolution failure
            resolved_value = self.template_registry.execute_function(match, template_context)
            text = text.replace(full_pattern, str(resolved_value))

        return text

    def _get_return_value_schema(self, process_key: str) -> dict[str, object] | None:
        """
        Get return_value_schema from process registry via discovery_service.

        Args:
            process_key: Process key to lookup

        Returns:
            Return value schema dict, or None if not found

        Note:
            Used by ExecutionContext integration to normalize result storage.
        """
        try:
            # Get process metadata from discovery service - fail fast if not available
            get_process_method = self.discovery_service.get_process_by_key  # type: ignore[union-attr]
            process_data = get_process_method(process_key)

            if not process_data:
                return None

            if not isinstance(process_data, dict):
                logger.error(f"ActionProcessor: process_data is not dict: {type(process_data)}")
                return None

            # Extract return_value_schema
            schema = process_data.get("return_value_schema")
            if schema:
                # Type narrow: if it's a dict, return it. Otherwise return as object for ReturnValueSchema type
                if isinstance(schema, dict):
                    return schema
                else:
                    # Assume it's a ReturnValueSchema object with to_dict() method
                    # Return as object (caller will handle conversion via hasattr check)
                    return schema  # type: ignore[no-any-return]
            else:
                return None

        except Exception as e:
            logger.error(f"ActionProcessor: Error getting schema for {process_key}: {e}")
            return None

    def _store_post_message_output(
        self,
        action: QueuedActionProtocol,
        arguments: dict[str, object],
    ) -> None:
        """Store user-visible post_message content as OUTPUT event.

        This replaces raw LLM JSON storage with user-visible text.
        Writes to both memory_service (non-platform) and context_management_service
        (platform mode) to ensure OUTPUT events are available regardless of mode.

        See: knowledge_base/2026-01-13_bad_example_prevention_implementation.md

        Args:
            action: The queued action being executed
            arguments: Filtered arguments passed to post_message
        """
        message = arguments.get("message")
        if not message or not isinstance(message, str):
            return

        session_id = action.session_id

        # Store to memory_service (non-platform mode) - fail-fast on error
        if self.memory_service and session_id:
            store_method = getattr(self.memory_service, "store_interaction", None)
            if store_method:
                store_method(
                    session_id=session_id,
                    source_namespace="io_interface_service",
                    event_type="assistant_response",
                    content=message,
                    metadata={"source": "post_message_output"},
                )
                logger.debug(f"Stored post_message output to memory for session {session_id}")

        # Store to context_management_service (platform mode)
        # Uses action.context_id (the platform ctx-... ID), NOT session_id
        self._ensure_context_initialized()
        context_id = action.context_id

        # Fail-fast: if platform mode is active (context_management_service injected),
        # context_id is REQUIRED for OUTPUT events to appear in conversation history
        is_platform_mode = self._context_management_service is not None
        if is_platform_mode and not context_id:
            raise ValueError(
                f"Platform mode requires context_id for OUTPUT events. "
                f"Action {action.id} (post_message) has no context_id - "
                f"this breaks conversation history."
            )

        if context_id:
            io_metadata = self._get_flow_io_metadata(action)
            # session_id fallback: action always has session_id from REST submit
            io_metadata = self._ensure_session_id_in_metadata(io_metadata, action)
            self._append_output_context_event(context_id, message, io_metadata)

    def _append_output_context_event(
        self,
        context_id: str,
        content: str,
        io_metadata: dict[str, str] | None = None,
    ) -> None:
        """Append OUTPUT event to context management for platform mode.

        Stores user-visible message content as an OUTPUT event, replacing
        the raw LLM JSON that was previously stored by the inference plugin.

        Args:
            context_id: Platform context ID (ctx-...) from action.context_id
            content: User-visible message content
            io_metadata: IO routing metadata (source_namespace, source address)
                from the flow's trigger_data. ContextStage renders source as
                "destination" for OUTPUT events.
        """
        from ananta.services.context_management.types import (
            ContextActorType,
            ContextEventType,
        )

        # Ensure context components are initialized
        self._ensure_context_initialized()

        # Fail-fast: this function is only called when context_id exists (platform mode),
        # so missing services/storage is a configuration error
        if not self._context_management_service:
            raise ValueError(
                "Platform mode requires context_management_service but it was not injected"
            )
        if not self._content_storage:
            raise ValueError(
                "Platform mode requires shared content_storage but it was not initialized"
            )

        # Store content to file and get path + char count (fail-fast on error)
        content_path, char_count = self._content_storage.store_event(context_id, content)

        # Build event metadata: IO routing info for metadata trailer rendering
        # When IO metadata is available, use source_namespace + source address.
        # ContextStage renders "source" as "destination" for OUTPUT events.
        event_metadata: dict[str, object] = {}
        if io_metadata:
            if io_metadata.get("source_namespace"):
                event_metadata["source_namespace"] = io_metadata["source_namespace"]
            if io_metadata.get("source"):
                event_metadata["source"] = io_metadata["source"]
            if io_metadata.get("session_id"):
                event_metadata["session_id"] = io_metadata["session_id"]
        if not event_metadata:
            event_metadata["source"] = "post_message_output"

        # Append event metadata to context management
        result = self._context_management_service.events.append_event(
            context_id=context_id,
            event_type=ContextEventType.OUTPUT.value,
            actor_type=ContextActorType.AGENT.value,
            content_path=content_path,
            content_char_count=char_count,
            metadata=event_metadata,
        )

        if result.get("action_status") != ActionStatus.COMPLETED.value:
            # DB insert failed - clean up orphaned file
            self._content_storage.delete(content_path)
            raise FrameworkError(
                message=f"Failed to append OUTPUT context event: {result.get('error')}",
                error_code="context_management.event_append_failed",
                details={"context_id": context_id, "error": result.get("error")},
            )

        logger.debug(f"Appended OUTPUT context event for context {context_id}")

        # Trigger compaction check after successful OUTPUT event (fail-fast)
        self._check_compaction_after_event(context_id)

    def _get_flow_io_metadata(self, action: QueuedActionProtocol) -> dict[str, str] | None:
        """Get IO routing metadata from the flow's trigger_data.

        Extracts source_namespace, source address, and session_id from the
        flow that triggered this action. Used for OUTPUT event metadata trailers.

        Args:
            action: Action with flow_id for flow lookup.

        Returns:
            Dict with source_namespace, source, and session_id if available,
            None otherwise.
        """
        if not action.flow_id:
            return None

        trigger_data = self._get_flow_trigger_data(action.flow_id)
        if trigger_data is None:
            return None

        return self._extract_io_metadata_fields(trigger_data)

    def _get_flow_trigger_data(self, flow_id: str) -> dict[str, Any] | None:
        """Look up a flow record and parse its trigger_data JSON.

        Args:
            flow_id: The flow ID to look up.

        Returns:
            Parsed trigger_data dict, or None if lookup/parse fails.
        """
        if not self.state_service:
            return None
        result = self.state_service.read_state(
            namespace="core",
            query={"table": "flows", "filters": {"id": flow_id}},
        )

        data = result.get("data", {})
        records = data.get("records", [])
        if not records or not isinstance(records, list):
            return None

        flow_record = records[0]
        if not isinstance(flow_record, dict):
            return None

        trigger_data_str = flow_record.get("trigger_data", "{}")
        trigger_data = (
            json.loads(trigger_data_str)
            if isinstance(trigger_data_str, str)
            else trigger_data_str
        )
        if not isinstance(trigger_data, dict):
            return None

        return trigger_data

    @staticmethod
    def _extract_io_metadata_fields(trigger_data: dict[str, Any]) -> dict[str, str] | None:
        """Extract IO routing fields from parsed trigger_data.

        Args:
            trigger_data: Parsed trigger_data dict from a flow record.

        Returns:
            Dict with source_namespace, source, and session_id if any are present,
            None otherwise.
        """
        metadata: dict[str, str] = {}
        source_ns = trigger_data.get("source_namespace", "")
        if source_ns:
            metadata["source_namespace"] = str(source_ns)
        source_addr = trigger_data.get("source", "")
        if source_addr:
            metadata["source"] = str(source_addr)
        session_id = trigger_data.get("session_id", "")
        if session_id:
            metadata["session_id"] = str(session_id)

        return metadata if metadata else None

    @staticmethod
    def _ensure_session_id_in_metadata(
        io_metadata: dict[str, str] | None,
        action: QueuedActionProtocol,
    ) -> dict[str, str] | None:
        """Ensure session_id is present in IO metadata for OUTPUT events.

        Uses action.session_id as fallback when trigger_data lacks session_id.

        Args:
            io_metadata: Existing IO metadata (may be None).
            action: Action with session_id from REST submit.

        Returns:
            Updated metadata dict with session_id, or None if no metadata.
        """
        sid = (io_metadata or {}).get("session_id") or action.session_id or ""
        if not sid:
            return io_metadata
        if io_metadata is None:
            return {"session_id": str(sid)}
        io_metadata["session_id"] = str(sid)
        return io_metadata
