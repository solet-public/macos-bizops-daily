"""
Result Processing Manager Service

Responsibility: Handle all result processing and formatting operations for action responses
Dependencies: TemplateEngine, StateManager, logging, UUID generation, datetime utilities
Complexity: Medium-High - focused on complex result processing with template resolution

Extracted from ActionManager god class (B8 + B6 complexity result processing methods)
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Protocol, cast

from ananta.core.domain.error_codes import ErrorCode
from ananta.core.plugins.plugin_contracts import ActionStatus
from ananta.error_handling import FrameworkError

logger = logging.getLogger(__name__)


class TemplateEngineProtocol(Protocol):
    """Protocol for template engine interface."""

    def resolve_post_execution_templates(
        self, action_def: dict[str, object], execution_result: object
    ) -> dict[str, object]: ...


class StateManagerProtocol(Protocol):
    """Protocol for state manager interface."""

    async def save(self, state: dict[str, object]) -> None: ...


class IOProcessKeyResolverProtocol(Protocol):
    """Protocol for resolving the active IO plugin process key from flow context.

    Implementations look up the flow's trigger_data.source_namespace and build
    the plugin-addressed process key (plugin::<namespace>::post_message).
    """

    def resolve_io_post_message_key(self, flow_id: str) -> str | None:
        """Resolve the active IO post_message process key for a flow.

        Args:
            flow_id: Flow identifier to look up trigger_data from.

        Returns:
            Process key string (e.g., 'plugin::discord_plugin::post_message'),
            or None if the source_namespace cannot be resolved.
        """
        ...


class OrchestratorProtocol(Protocol):
    """Protocol for Orchestrator interface used for flow service resolution."""

    def get_service(self, service_name: str) -> object: ...


class FlowBasedIOProcessKeyResolver:
    """Resolves the active IO plugin post_message key from flow trigger_data.

    Looks up the flow's source_namespace via FlowService.get_flow_input() and builds
    the plugin-addressed process key (plugin::<namespace>::post_message).

    This is the concrete implementation of IOProcessKeyResolverProtocol.
    """

    def __init__(self, orchestrator: OrchestratorProtocol) -> None:
        self._orchestrator = orchestrator

    def resolve_io_post_message_key(self, flow_id: str) -> str | None:
        """Resolve the active IO post_message process key for a flow."""
        try:
            flow_service = self._orchestrator.get_service("flow_service")
            if not flow_service:
                logger.warning("flow_service not available, cannot resolve IO process key")
                return None

            get_flow_input = getattr(flow_service, "get_flow_input", None)
            if not callable(get_flow_input):
                logger.warning("flow_service missing get_flow_input method")
                return None

            result = get_flow_input(flow_id)
            if not isinstance(result, dict):
                return None

            # Navigate: result -> data -> result -> source_namespace
            data = result.get("data")
            if not isinstance(data, dict):
                return None

            inner_result = data.get("result")
            if not isinstance(inner_result, dict):
                return None

            source_namespace = inner_result.get("source_namespace")
            if not source_namespace or not isinstance(source_namespace, str):
                logger.warning(
                    "No source_namespace in flow input for flow_id=%s, cannot resolve IO process key",
                    flow_id,
                )
                return None

            return f"plugin::{source_namespace}::post_message"

        except Exception:
            logger.error(
                "Failed to resolve IO process key for flow_id=%s",
                flow_id,
                exc_info=True,
            )
            return None


class ToDictProtocol(Protocol):
    """Protocol for objects with to_dict method (e.g., DiscoveryResult)."""

    def to_dict(self) -> dict[str, object]: ...


class ResultProcessingManager:
    """
    Service for managing result processing and formatting operations.

    ARCHITECTURAL ROLE: Supporting service that extracts result processing logic
    from ActionManager while maintaining action response formatting integrity.

    This service handles:
    - Service result formatting with proper action response structure
    - Result processor action handling with template resolution
    - Post-execution template processing (<<RESULT>> patterns)
    - Legacy template compatibility (<<<ACTION_RESULT>>> patterns)
    - State management integration for result processor queuing
    - Error handling and logging for result processing operations

    The exposure-filter step that used to sit between result arrival and
    template resolution was REMOVED 2026-07-15 (frontier-first consolidation):
    frontier agent sessions receive raw results via bridge_delivery by design,
    so per-field exposure redaction was dead machinery. See
    workbench/2026-07-15_frontier_first_result_processing_consolidation.md.
    """

    def __init__(
        self,
        template_engine: TemplateEngineProtocol | None = None,
        state_manager: StateManagerProtocol | None = None,
        io_process_key_resolver: IOProcessKeyResolverProtocol | None = None,
    ) -> None:
        """Initialize ResultProcessingManager with required dependencies."""
        self.template_engine = template_engine
        self.state_manager = state_manager
        self._io_process_key_resolver = io_process_key_resolver

    def format_service_result(self, result: object, timestamp: str) -> dict[str, object]:
        """
        Format service result into proper action response format.

        EXTRACTED FROM: ActionManager._format_service_result() - B(6) complexity

        This method handles comprehensive result formatting:
        1. Detects if result is already in proper action response format
        2. Handles special DiscoveryResult objects with to_dict() conversion
        3. Wraps raw results in standardized action response structure
        4. Ensures timestamp presence in all formatted results
        5. Provides detailed logging for service execution tracking

        Args:
            result: The raw result from service execution
            timestamp: ISO format timestamp for result metadata

        Returns:
            dict[str, object]: Properly formatted action response with:
                - action_status: Execution status indicator
                - data: The actual result data
                - actions: List of follow-up actions (empty for service results)
                - error: Error information (None for successful results)
                - timestamp: Result generation timestamp
        """
        # Ensure service result has proper action response format
        service_result: dict[str, object]
        if isinstance(result, dict) and "action_status" in result:
            # Already in proper format
            logger.debug("🔧 SERVICE_EXEC_008: Result already in action format")
            service_result = result
        else:
            logger.debug("🔧 SERVICE_EXEC_009: Wrapping raw result in action format")

            # Handle DiscoveryResult objects specially
            result_data: object
            if hasattr(result, "to_dict") and callable(getattr(result, "to_dict", None)):
                logger.debug("🔧 SERVICE_EXEC_009A: Converting DiscoveryResult to dict")
                # Runtime-verified via hasattr check above
                typed_result = cast(ToDictProtocol, result)
                result_data = typed_result.to_dict()
            else:
                result_data = result if result is not None else {}

            # Wrap raw result in action response format
            service_result = {
                "action_status": ActionStatus.COMPLETED.value,
                "data": result_data,
                "actions": [],
                "error": None,
                "timestamp": timestamp,
            }

        if "timestamp" not in service_result:
            service_result["timestamp"] = timestamp

        logger.debug(
            f"🔧 SERVICE_EXEC_010: Returning service result with status: {service_result.get('action_status')}"
        )
        logger.debug(f"🔧 SERVICE_EXEC_011: Final result keys: {list(service_result.keys())}")

        return service_result

    async def handle_result_processor(
        self,
        action_def: dict[str, object],
        action_name: str,
        result: dict[str, object],
        state: dict[str, object],
    ) -> None:
        """
        Handle result processor action with template resolution and state queuing.

        EXTRACTED FROM: ActionManager._handle_result_processor() - B(8) complexity

        This method handles comprehensive result processor workflow:
        1. Validates result processor structure and required fields
        2. Resolves post-execution templates using template engine (<<RESULT>> patterns)
        3. Falls back to legacy template handling for backward compatibility
        4. Creates unique action ID and metadata for processor action
        5. Queues processor action in state for execution pipeline
        6. Provides comprehensive error handling and logging throughout
        """
        try:
            if await self._handle_no_matches_shortcut(result, state, action_name):
                return

            result_processor = self._validate_result_processor(action_def, action_name)
            if not result_processor:
                return

            processor_name = result_processor.get("name")
            logger.debug(
                f"Processing result of '{action_name}' with result_processor '{processor_name}'"
            )

            processor_action = result_processor.copy()
            processor_action = self._resolve_processor_templates(processor_action, result)

            await self._queue_processor_action(processor_action, result, state, action_name)

        except Exception as e:
            logger.error(f"Error handling result processor for '{action_name}': {e}", exc_info=True)

    async def _handle_no_matches_shortcut(
        self, result: dict[str, object], state: dict[str, object], action_name: str
    ) -> bool:
        """Handle discovery 'no_matches' results directly. Returns True if handled.

        Only handles actual failed discovery (no_matches). Intent bypass results
        (intent_question, intent_smalltalk, etc.) flow through to inference normally.

        Note: With garbage cutoff (Step 3), confirmation phrases like "go ahead!" may
        trigger no_matches. This causes a "No matching processes found" response instead
        of continuing the prior action. This breaks the confirmation loop (desired) but
        provides suboptimal UX. For proper confirmation handling, implement Backup A
        (pending_action state machine).
        See: knowledge_base/2026-01-13_bad_example_prevention_implementation.md
        """
        if not (result.get("match_type") == "no_matches" and result.get("process_count") == 0):
            return False

        logger.debug(
            "RESULT_PROCESSOR: Discovery returned no_matches, replacing with direct post_message"
        )

        session_id = self._extract_session_id(state)
        if not session_id:
            logger.error("No session_id found for no_matches response, falling through")
            return False

        flow_id = self._extract_flow_id(state)
        try:
            io_process_key = self._resolve_io_post_message_key(flow_id)
        except FrameworkError as e:
            logger.warning(
                "RESULT_PROCESSOR: Cannot resolve IO key for no_matches shortcut "
                "(flow_id=%s): %s — falling through to normal processing",
                flow_id, e.message,
            )
            return False

        post_message_action: dict[str, object] = {
            "name": "no_matches_response",
            "process_key": io_process_key,
            "arguments": {
                "session_id": session_id,
                "message": (
                    "No matching processes found for your request. "
                    "Please describe what you'd like to do more specifically."
                ),
            },
            "action_id": str(uuid.uuid4()),
            "action_status": ActionStatus.QUEUED.value,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        if flow_id:
            post_message_action["flow_id"] = flow_id

        actions_obj = state.get("actions", [])
        if not isinstance(actions_obj, list):
            logger.error("State 'actions' is not a list, falling through to normal processing")
            return False

        actions_obj.append(post_message_action)
        state["actions"] = actions_obj

        if self.state_manager:
            await self.state_manager.save(state)
            logger.debug(f"Queued direct no_matches post_message for '{action_name}'")
        return True

    def _validate_result_processor(
        self, action_def: dict[str, object], action_name: str
    ) -> dict[str, object] | None:
        """Validate and return result_processor from action definition."""
        result_processor = action_def.get("result_processor")

        if not isinstance(result_processor, dict):
            raise FrameworkError(
                message=f"result_processor must be an action object, got {type(result_processor).__name__}",
                error_code="action_manager.invalid_result_processor_type",
                details={"action_name": action_name, "result_processor": result_processor},
            )

        if not result_processor:
            return None

        processor_name = result_processor.get("name")
        if not processor_name:
            raise FrameworkError(
                message="result_processor action object missing required 'name' field",
                error_code="action_manager.missing_result_processor_name",
                details={"action_name": action_name, "result_processor": result_processor},
            )

        return result_processor

    def _resolve_processor_templates(
        self,
        processor_action: dict[str, object],
        result: dict[str, object],
    ) -> dict[str, object]:
        """Resolve post-execution templates against the raw result."""
        processor_name = processor_action.get("name")
        logger.debug(
            f"RESULT_PROCESSOR_TEMPLATE: Resolving post-execution templates for {processor_name}"
        )

        logger.debug(f"RESULT_PROCESSOR_BEFORE: {processor_action}")

        if not self.template_engine:
            raise FrameworkError(
                message="Result processing requires template engine",
                error_code=ErrorCode.CONFIGURATION_ERROR,
                details={"processor": processor_name},
            )

        processor_action = self.template_engine.resolve_post_execution_templates(
            processor_action, result
        )
        logger.debug(f"RESULT_PROCESSOR_AFTER: {processor_action}")

        # Inject valid process_keys into schema if discovery result
        processor_action = self._inject_process_keys_into_schema(processor_action, result)

        return processor_action

    def _inject_process_keys_into_schema(
        self, processor_action: dict[str, object], result: dict[str, object]
    ) -> dict[str, object]:
        """Inject open process schema into the output schema.

        Uses an open schema that allows any valid process structure rather than
        a constrained oneOf. This enables the LLM to use any process it has seen
        in conversation history, not just those from the current discovery.

        Testing showed 100% accuracy with open schema vs 0% with oneOf constraint.
        See: plugins/default_inference_plugin/research/prompt_replay/test_oneof_constraint.py
        """
        # Only inject for discovery results that have processes with metadata
        processes = result.get("processes")
        if not processes or not isinstance(processes, list):
            return processor_action

        # Navigate to output_schema in arguments.prompt.user
        args = processor_action.get("arguments")
        if not isinstance(args, dict):
            return processor_action

        prompt = args.get("prompt")
        if not isinstance(prompt, dict):
            return processor_action

        user = prompt.get("user")
        if not isinstance(user, dict):
            return processor_action

        logger.debug(f"SCHEMA_INJECT: Using open schema for {len(processes)} discovered processes")

        # Create open schema that allows any valid process structure
        # The LLM can select from processes shown in discovery results OR
        # processes it has seen earlier in the conversation
        open_schema: dict[str, object] = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "required": ["reasoning", "actions"],
            "properties": {
                "reasoning": {"type": "string"},
                "actions": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": 10,
                    "items": {
                        "type": "object",
                        "required": ["process", "reason", "arguments"],
                        "properties": {
                            "process": {
                                "type": "object",
                                "required": ["provider_type", "provider", "function_name"],
                                "properties": {
                                    "provider_type": {
                                        "type": "string",
                                        "enum": ["plugin", "service_interface"],
                                    },
                                    "provider": {"type": "string"},
                                    "function_name": {"type": "string"},
                                },
                                "additionalProperties": False,
                            },
                            "reason": {"type": "string"},
                            "arguments": {"type": "object"},
                        },
                    },
                },
            },
            "additionalProperties": False,
        }

        user["output_schema"] = open_schema
        return processor_action

    def _resolve_io_post_message_key(self, flow_id: object) -> str:
        """Resolve the active IO plugin post_message process key for the given flow.

        Uses the io_process_key_resolver to look up the flow's source_namespace
        from trigger_data and build the plugin-addressed process key.

        Args:
            flow_id: Flow identifier (may be None or non-string).

        Returns:
            Process key string (e.g., 'plugin::discord_plugin::post_message').

        Raises:
            FrameworkError: If the resolver is not configured or resolution fails.
        """
        if not self._io_process_key_resolver:
            raise FrameworkError(
                message="IO process key resolver not configured on ResultProcessingManager",
                error_code=ErrorCode.CONFIGURATION_ERROR,
            )

        if not isinstance(flow_id, str) or not flow_id:
            raise FrameworkError(
                message="flow_id required to resolve IO process key for no_matches response",
                error_code=ErrorCode.CONFIGURATION_ERROR,
                details={"flow_id": flow_id},
            )

        resolved_key = self._io_process_key_resolver.resolve_io_post_message_key(flow_id)
        if not resolved_key:
            raise FrameworkError(
                message=f"Failed to resolve IO post_message key for flow_id={flow_id}",
                error_code=ErrorCode.CONFIGURATION_ERROR,
                details={"flow_id": flow_id},
            )

        return resolved_key

    def _extract_session_id(self, state: dict[str, object]) -> object:
        """Extract session_id from state or metadata."""
        session_id = state.get("session_id")
        if not session_id:
            metadata = state.get("metadata")
            if isinstance(metadata, dict):
                session_id = metadata.get("session_id")
        return session_id

    def _extract_flow_id(self, state: dict[str, object]) -> object:
        """Extract flow_id from state or metadata."""
        flow_id = state.get("flow_id")
        if not flow_id:
            metadata = state.get("metadata")
            if isinstance(metadata, dict):
                flow_id = metadata.get("flow_id")
        return flow_id

    async def _queue_processor_action(
        self,
        processor_action: dict[str, object],
        result: dict[str, object],
        state: dict[str, object],
        action_name: str,
    ) -> None:
        """Queue the processor action in state."""
        actions_obj = state.get("actions", [])
        if not isinstance(actions_obj, list):
            raise FrameworkError(
                message="State 'actions' must be a list",
                error_code="action_manager.invalid_state_structure",
                details={"action_name": action_name, "actions_type": type(actions_obj).__name__},
            )
        actions: list[object] = actions_obj

        processor_action["action_id"] = str(uuid.uuid4())
        processor_action["action_status"] = ActionStatus.QUEUED.value
        processor_action["timestamp"] = datetime.now(UTC).isoformat()

        session_id = self._extract_session_id(state)
        if session_id and "arguments" in processor_action:
            if isinstance(processor_action["arguments"], dict):
                processor_action["arguments"]["session_id"] = session_id

        processor_action["_parent_result"] = result
        processor_action["_is_result_processor"] = True

        actions.append(processor_action)
        state["actions"] = actions

        processor_name = processor_action.get("name")
        if self.state_manager:
            await self.state_manager.save(state)
            logger.debug(f"Queued result processor action '{processor_name}' for '{action_name}'")
        else:
            logger.error("State manager not available - cannot queue result processor action")
