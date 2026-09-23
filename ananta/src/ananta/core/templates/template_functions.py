import json
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, cast

from ananta.constants import CONTEXT_KEY_FLOW_ID, CONTEXT_KEY_SESSION_ID
from ananta.core.contexts.action_contexts import TemplateFunctionContext
from ananta.core.domain.types import ActionResult
from ananta.core.services.state_result_formatter import StateResultFormatter
from ananta.core.services.template_argument_parser import TemplateArgumentParser
from ananta.core.templates.template_exceptions import (
    TemplateFunctionError,
    UnknownTemplateFunctionError,
)
from ananta.interfaces.state_service_protocol import StateServiceProtocol

__all__ = [
    "DiscoveryServiceProtocol",
    "MemoryServiceProtocol",
    "PluginManagerProtocol",
    "PluginProtocol",
    "StateAwarePluginProtocol",
    "StateServiceProtocol",
    "TEMPLATE_FUNCTION_PATTERN",
    "TemplateFunctionRegistry",
]

if TYPE_CHECKING:
    from ananta.core.services.prompt_context_builder import LLMContext

logger = logging.getLogger(__name__)

# Pattern for template function calls: <<<:function_name(args)>>>
# Matches the pattern defined in template_patterns.json under "function_execution"
TEMPLATE_FUNCTION_PATTERN = re.compile(r"<<<:(.+?)>>>")


class StateAwarePluginProtocol(Protocol):
    """Protocol for plugins that can receive state service injection."""

    def set_state_service(self, state_service: "StateServiceProtocol") -> None: ...


class PluginProtocol(Protocol):
    """Protocol for Plugin interface used by template functions."""

    def supports_template_functions(self) -> bool: ...

    def execute_template_function(
        self, function_name: str, args: str, context: TemplateFunctionContext
    ) -> str: ...


class PluginManagerProtocol(Protocol):
    """Protocol for PluginManager interface used by template functions."""

    plugins: dict[str, object]

    def get_plugin(self, plugin_name: str) -> object: ...


class DiscoveryServiceProtocol(Protocol):
    """Protocol for DiscoveryService interface used by template functions."""

    def get_process_schemas(self, max_processes: int = 200) -> str: ...
    def query_process_registry(
        self,
        query: str,
        max_results: int = 10,
        state: dict[str, object] | None = None,
    ) -> dict[str, object]: ...
    def get_process_by_key(self, process_key: str) -> dict[str, object] | None: ...


class MemoryServiceProtocol(Protocol):
    """Protocol for MemoryService interface used by template functions.

    Updated to v1.2.0 interface with tags, exclude_ids, and reinforce support.
    """

    def get_recent_memory(
        self,
        session_id: str | None = None,
        max_events: int = 20,
        max_age_hours: int | None = None,
        namespace_filter: str | None = None,
    ) -> dict[str, object]: ...

    def recall(
        self,
        query: str,
        top_k: int = 5,
        memory_type: str = "all",
        include_archived: bool = False,
        tags: list[str] | None = None,
        exclude_ids: list[str] | None = None,
    ) -> dict[str, object]: ...

    def reinforce(self, memory_id: str) -> dict[str, object]: ...


class TemplateFunctionRegistry:
    def __init__(
        self,
        state_service: StateServiceProtocol | None = None,
        action_manager: object | None = None,
        plugin_manager: PluginManagerProtocol | None = None,
        discovery_service: DiscoveryServiceProtocol | None = None,
        memory_service: MemoryServiceProtocol | None = None,
        knowledge_service: object | None = None,
    ):
        self.functions: dict[str, Callable[[str, TemplateFunctionContext], str]] = {}
        self.function_descriptions: dict[str, str] = {}
        self.state_service = state_service
        self.action_manager = action_manager
        self.plugin_manager = plugin_manager
        self.discovery_service = discovery_service
        self.memory_service = memory_service
        self.knowledge_service = knowledge_service

        # Initialize complexity reduction services
        self.argument_parser = TemplateArgumentParser()
        self.result_formatter = StateResultFormatter()

        self._register_core_functions()

        # Initialize StateAware plugins for template functions
        if plugin_manager and state_service:
            for plugin_name, plugin in plugin_manager.plugins.items():
                if hasattr(plugin, "set_state_service") and callable(
                    getattr(plugin, "set_state_service", None)
                ):
                    try:
                        # Runtime-verified via hasattr check above
                        state_aware_plugin = cast(StateAwarePluginProtocol, plugin)
                        state_aware_plugin.set_state_service(state_service)
                    except Exception as e:
                        logger.error(
                            f"Failed to initialize plugin '{plugin_name}' with state service: {e}"
                        )
        else:
            pass

    def register_function(
        self,
        name: str,
        handler: Callable[[str, TemplateFunctionContext], str],
        description: str | None = None,
    ) -> None:
        if name in self.functions:
            logger.error(f"Template function '{name}' already registered, overwriting")

        self.functions[name] = handler
        self.function_descriptions[name] = description or f"Template function: {name}"

    def execute_function(self, func_call: str, context: TemplateFunctionContext) -> str:
        """Execute a template function with typed context.

        Args:
            func_call: The function call string (e.g., "function_name(args)")
            context: Typed context with required flow_id and other execution data

        Returns:
            The result of the function execution as a string
        """
        name: str = "unknown"
        args: str = func_call
        try:
            name, args = self._parse_function_call(func_call)

            # Check if this is a process registry key format: provider_type::provider::function
            if "::" in name:
                return self._execute_process_function(name, args, context)

            # Legacy function registry lookup
            if name not in self.functions:
                available_functions = list(self.functions.keys())
                raise UnknownTemplateFunctionError(name, available_functions)

            result = self.functions[name](args, context)

            return result

        except (UnknownTemplateFunctionError, TemplateFunctionError):
            raise
        except Exception as e:
            raise TemplateFunctionError(
                function_name=name,
                function_args=args,
                error_message=str(e),
            ) from e

    def _parse_function_call(self, func_call: str) -> tuple[str, str]:
        if func_call.startswith(":"):
            func_call = func_call[1:]

        match = re.match(r"^([a-zA-Z_][a-zA-Z0-9_:]*)\((.*)\)$", func_call, re.DOTALL)
        if not match:
            raise TemplateFunctionError(
                function_name="unknown",
                function_args=func_call,
                error_message=f"Invalid function call syntax: {func_call}. Expected: function_name(arguments)",
            )

        function_name = match.group(1)
        arguments = match.group(2)

        return function_name, arguments

    # Dispatch table: service provider name → method name for service_interface providers
    _SERVICE_DISPATCH: dict[str, str] = {
        "state_service": "_execute_state_service_function",
        "discovery_service": "_execute_discovery_service_function",
        "memory_service": "_execute_memory_service_function",
        "flow_service": "_execute_flow_service_function",
        "knowledge_service": "_execute_knowledge_service_function",
    }

    def _execute_process_function(
        self, process_key: str, args: str, context: TemplateFunctionContext
    ) -> str:
        provider_type, provider, function_name = self._parse_process_key(process_key, args)

        logger.debug(
            f"TEMPLATE_FUNCTION_PROCESS: provider_type='{provider_type}', provider='{provider}', function_name='{function_name}'"
        )

        if provider_type == "service_interface":
            method_name = self._SERVICE_DISPATCH.get(provider)
            if method_name is None:
                raise TemplateFunctionError(
                    function_name=process_key,
                    function_args=args,
                    error_message=f"Template execution not yet implemented for provider: {provider_type}::{provider}",
                )
            method: Callable[[str, str, str, TemplateFunctionContext], str] = getattr(
                self, method_name,
            )
            return method(process_key, function_name, args, context)

        if provider_type == "plugin":
            return self._execute_plugin_template_function(
                process_key, provider, function_name, args, context
            )

        raise TemplateFunctionError(
            function_name=process_key,
            function_args=args,
            error_message=f"Template execution not yet implemented for provider: {provider_type}::{provider}",
        )

    def _parse_process_key(self, process_key: str, args: str) -> tuple[str, str, str]:
        """Parse process key into provider_type, provider, and function_name components."""
        try:
            provider_type, provider, function_name = process_key.split("::", 2)
            return provider_type, provider, function_name
        except ValueError as e:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message=f"Invalid process key format: '{process_key}'",
            ) from e

    def _execute_state_service_function(
        self, process_key: str, function_name: str, args: str, context: TemplateFunctionContext
    ) -> str:
        """Execute state service template functions."""
        if function_name == "read_state":
            return self._execute_read_state_function(args, context)
        raise TemplateFunctionError(
            function_name=process_key,
            function_args=args,
            error_message=f"State service function '{function_name}' not supported in templates",
        )

    def _execute_discovery_service_function(
        self,
        process_key: str,
        function_name: str,
        args: str,
        context: TemplateFunctionContext,
    ) -> str:
        """Execute discovery service template functions."""
        if not self.discovery_service:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message="Discovery service not available for template function execution",
            )

        if function_name == "get_process_schemas":
            return self._execute_get_process_schemas(args)
        if function_name == "query_process_registry":
            return self._execute_query_process_registry(process_key, args, context)
        if function_name == "get_process_by_key":
            return self._execute_get_process_by_key(process_key, args)

        raise TemplateFunctionError(
            function_name=process_key,
            function_args=args,
            error_message=f"Discovery service function '{function_name}' not supported in templates",
        )

    def _execute_get_process_schemas(self, args: str) -> str:
        """Execute get_process_schemas via discovery service."""
        max_processes = self._parse_get_process_schemas_args(args)

        logger.debug(
            f"TEMPLATE_DISCOVERY: Calling get_process_schemas(max_processes={max_processes})"
        )
        if self.discovery_service is None:
            raise TemplateFunctionError(
                function_name="get_process_schemas",
                function_args=args,
                error_message="Discovery service not available",
            )
        result = self.discovery_service.get_process_schemas(max_processes=max_processes)
        logger.debug(f"TEMPLATE_DISCOVERY: Returned {len(result)} characters")
        return result

    def _parse_get_process_schemas_args(self, args: str) -> int:
        """Parse arguments for get_process_schemas."""
        max_processes = 200

        if not args:
            return max_processes

        max_processes_match = re.search(r"max_processes\s*=\s*(\d+)", args)
        if max_processes_match:
            max_processes = int(max_processes_match.group(1))

        return max_processes

    def _execute_query_process_registry(
        self, process_key: str, args: str, context: TemplateFunctionContext
    ) -> str:
        """Execute query_process_registry via discovery service."""
        import json

        query, max_results = self._parse_query_process_registry_args(process_key, args)

        logger.debug(
            f"TEMPLATE_DISCOVERY: Calling query_process_registry(query='{query}', max_results={max_results})"
        )
        if self.discovery_service is None:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message="Discovery service not available",
            )

        # Build state from template function context for intent classification
        state: dict[str, object] = {
            "flow_id": context.flow_id,
            "session_id": context.session_id,
            "context_id": context.context_id,
        }

        result_dict = self.discovery_service.query_process_registry(
            query=query, max_results=max_results, state=state
        )
        processes = result_dict.get("processes", [])
        process_count = len(processes) if isinstance(processes, list) else 0
        logger.debug(
            f"TEMPLATE_DISCOVERY: query_process_registry returned {process_count} processes"
        )
        return json.dumps(result_dict)

    def _parse_query_process_registry_args(self, process_key: str, args: str) -> tuple[str, int]:
        """Parse arguments for query_process_registry."""
        query_match = re.search(r"query\s*=\s*['\"]([^'\"]+)['\"]", args)
        query = query_match.group(1) if query_match else ""

        if not query:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message="query_process_registry requires 'query' argument",
            )

        max_results = 10
        max_results_match = re.search(r"max_results\s*=\s*(\d+)", args)
        if max_results_match:
            max_results = int(max_results_match.group(1))

        return query, max_results

    def _execute_get_process_by_key(self, process_key: str, args: str) -> str:
        """Execute get_process_by_key via discovery service."""
        import json

        target_key = self._parse_get_process_by_key_args(process_key, args)

        logger.debug(
            f"TEMPLATE_DISCOVERY: Calling get_process_by_key(process_key='{target_key}')"
        )
        if self.discovery_service is None:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message="Discovery service not available",
            )
        full_process_data = self.discovery_service.get_process_by_key(target_key)

        if not full_process_data:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message=f"Process not found: {target_key}",
            )

        clean_metadata = {
            "process_key": target_key,
            "description": full_process_data.get("description", ""),
            "invocation_schema": full_process_data.get("invocation_schema", {}),
        }
        logger.debug(
            f"TEMPLATE_DISCOVERY: get_process_by_key returned metadata for {target_key}"
        )
        return json.dumps(clean_metadata)

    def _parse_get_process_by_key_args(self, process_key: str, args: str) -> str:
        """Parse arguments for get_process_by_key."""
        key_match = re.search(r"process_key\s*=\s*['\"]([^'\"]+)['\"]", args)
        target_key = key_match.group(1) if key_match else ""

        if not target_key:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message="get_process_by_key requires 'process_key' argument",
            )

        return target_key

    def _execute_memory_service_function(
        self, process_key: str, function_name: str, args: str, context: TemplateFunctionContext
    ) -> str:
        """Execute memory service template functions."""
        if not self.memory_service:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message="Memory service not available for template function execution",
            )

        # Resolve template variables in args (e.g., SESSION_ID -> actual session_id)
        resolved_args = self._resolve_template_variables_in_args(args, context)

        if function_name == "get_recent_memory":
            return self._execute_get_recent_memory(process_key, resolved_args, context)
        elif function_name == "recall":
            return self._execute_recall(process_key, resolved_args, context)
        elif function_name == "build_llm_context":
            return self._execute_build_llm_context(process_key, resolved_args, context)
        else:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message=f"Memory service function '{function_name}' not supported in templates",
            )

    def _execute_get_recent_memory(
        self, process_key: str, args: str, context: TemplateFunctionContext
    ) -> str:
        """Execute get_recent_memory via memory service."""
        if not self.memory_service:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message="Memory service not available",
            )

        session_id_parsed, max_events, max_age_hours, namespace_filter = (
            self._parse_get_recent_memory_args(args)
        )
        session_id = self._resolve_session_id_for_memory(
            session_id_parsed, context, process_key, args
        )

        logger.debug(
            f"TEMPLATE_MEMORY: Calling get_recent_memory(session_id={session_id}, "
            f"max_events={max_events}, max_age_hours={max_age_hours}, "
            f"namespace_filter={namespace_filter})"
        )

        result = self.memory_service.get_recent_memory(
            session_id=session_id,
            max_events=max_events,
            max_age_hours=max_age_hours,
            namespace_filter=namespace_filter,
        )

        return self._format_recent_memory_result(result)

    def _parse_get_recent_memory_args(
        self, args: str
    ) -> tuple[str | None, int, int | None, str | None]:
        """Parse arguments for get_recent_memory.

        Returns:
            Tuple of (session_id, max_events, max_age_hours, namespace_filter)
        """
        session_id: str | None = None
        max_events: int = 20
        max_age_hours: int | None = None
        namespace_filter: str | None = None

        if not args:
            return session_id, max_events, max_age_hours, namespace_filter

        session_id = self._extract_arg_value(args, r'session_id\s*=\s*["\']?([^"\',\)]+)["\']?')
        max_events_str = self._extract_arg_value(args, r"max_events\s*=\s*(\d+)")
        if max_events_str:
            max_events = int(max_events_str)
        max_age_str = self._extract_arg_value(args, r"max_age_hours\s*=\s*(\d+)")
        if max_age_str:
            max_age_hours = int(max_age_str)
        namespace_filter = self._extract_arg_value(
            args, r'namespace_filter\s*=\s*["\']([^"\']+)["\']'
        )

        return session_id, max_events, max_age_hours, namespace_filter

    def _resolve_session_id_for_memory(
        self,
        session_id: str | None,
        context: TemplateFunctionContext,
        _process_key: str,
        _args: str,
    ) -> str | None:
        """Resolve session_id from args or typed context.

        Returns None if not found - memories are global by default.
        Session_id is an optional filter, not required.
        """
        if session_id:
            return session_id

        # Use typed context attribute directly if available
        if context.session_id:
            return context.session_id

        # No session_id - that's OK, memories are global
        return None

    def _format_recent_memory_result(self, result: dict[str, object]) -> str:
        """Format get_recent_memory result for output."""
        history = result.get("history", "")
        return str(history) if history else ""

    def _execute_recall(
        self, process_key: str, args: str, _context: TemplateFunctionContext
    ) -> str:
        """Execute recall (long-term memory search) via memory service.

        This enables identity and other persistent information to be injected
        into prompts at template resolution time.
        """
        if not self.memory_service:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message="Memory service not available",
            )

        query, top_k = self._parse_recall_args(process_key, args)

        logger.debug(f"TEMPLATE_MEMORY: Calling recall(query='{query}', top_k={top_k})")
        result = self.memory_service.recall(query=query, top_k=top_k)

        return self._format_recall_result(result)

    def _parse_recall_args(self, process_key: str, args: str) -> tuple[str, int]:
        """Parse arguments for recall function."""
        query: str | None = None
        top_k: int = 5

        if args:
            query = self._extract_arg_value(args, r"query\s*=\s*['\"]([^'\"]+)['\"]")
            top_k_str = self._extract_arg_value(args, r"top_k\s*=\s*(\d+)")
            if top_k_str:
                top_k = int(top_k_str)

        if not query:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message="query is required for recall",
            )

        return query, top_k

    def _format_recall_result(self, result: dict[str, object]) -> str:
        """Format recall result for inclusion in prompts."""
        if "error" in result:
            return f"Memory recall error: {result['error']}"

        memories_raw = result.get("memories", [])
        if not memories_raw or not isinstance(memories_raw, list):
            return "No identity memories found."

        memories: list[dict[str, object]] = memories_raw
        memory_contents = [str(m.get("content", "")) for m in memories if m.get("content")]
        formatted = " ".join(memory_contents)

        logger.debug(
            f"TEMPLATE_MEMORY: recall returned {len(memories)} memories, "
            f"{len(formatted)} characters"
        )
        return formatted

    def _execute_build_llm_context(
        self, process_key: str, args: str, context: TemplateFunctionContext
    ) -> str:
        """Execute build_llm_context to get memory context for prompts."""
        if not self.memory_service:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message="Memory service not available",
            )

        from ananta.core.services.prompt_context_builder import PromptContextBuilder

        session_id, query, max_recent, relevant_top_k = (
            self._parse_llm_context_args(args, context, process_key)
        )

        logger.debug(
            f"TEMPLATE_MEMORY: Building LLM context for session={session_id}, "
            f"query='{query[:50]}...'"
        )

        builder = PromptContextBuilder(self.memory_service)
        llm_context = builder.build_context(
            session_id=session_id,
            user_input=query,
            max_recent_events=max_recent,
            relevant_top_k=relevant_top_k,
            reinforce_memories=True,
        )

        formatted = self._format_llm_context_output(llm_context)

        logger.debug(
            f"TEMPLATE_MEMORY: build_llm_context returned {len(formatted)} characters, "
            f"{len(llm_context.relevant_memories)} relevant memories, "
            f"{len(llm_context.identity_memories)} identity memories"
        )

        return formatted

    def _parse_llm_context_args(
        self, args: str, context: TemplateFunctionContext, process_key: str
    ) -> tuple[str, str, int, int]:
        """Parse arguments for build_llm_context template function.

        Returns:
            Tuple of (session_id, query, max_recent, relevant_top_k)
        """
        session_id: str | None = None
        query: str | None = None
        max_recent: int = 12
        relevant_top_k: int = 5

        if args:
            session_id = self._extract_arg_value(args, r'session_id\s*=\s*["\']?([^"\',\)]+)["\']?')
            query = self._extract_arg_value(args, r"query\s*=\s*['\"]([^'\"]+)['\"]")

            max_recent_str = self._extract_arg_value(args, r"max_recent\s*=\s*(\d+)")
            if max_recent_str:
                max_recent = int(max_recent_str)

            top_k_str = self._extract_arg_value(args, r"relevant_top_k\s*=\s*(\d+)")
            if top_k_str:
                relevant_top_k = int(top_k_str)

        # Use typed context attribute directly
        if not session_id and context.session_id:
            session_id = context.session_id

        if not session_id:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message="session_id is required for build_llm_context",
            )

        if not query:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message="query is required for build_llm_context",
            )

        return session_id, query, max_recent, relevant_top_k

    def _extract_arg_value(self, args: str, pattern: str) -> str | None:
        """Extract argument value using regex pattern."""
        match = re.search(pattern, args)
        return match.group(1).strip() if match else None

    def _format_llm_context_output(self, llm_context: "LLMContext") -> str:
        """Format LLMContext into prompt-ready string."""
        sections: list[str] = []

        self._append_recent_conversation_section(sections, llm_context)
        self._append_relevant_context_section(sections, llm_context)
        self._append_identity_section(sections, llm_context)

        return "\n\n".join(sections)

    def _append_recent_conversation_section(
        self, sections: list[str], llm_context: "LLMContext"
    ) -> None:
        """Append recent conversation section if present."""
        if llm_context.recent_conversation:
            sections.append("## Recent Conversation\n" + llm_context.recent_conversation)

    def _append_relevant_context_section(
        self, sections: list[str], llm_context: "LLMContext"
    ) -> None:
        """Append relevant context section if memories exist."""
        if not llm_context.relevant_memories:
            return
        memory_texts = self._extract_memory_contents(llm_context.relevant_memories)
        if memory_texts:
            sections.append("## Relevant Context\n" + "\n".join(memory_texts))

    def _append_identity_section(self, sections: list[str], llm_context: "LLMContext") -> None:
        """Append identity section if identity memories exist."""
        if not llm_context.identity_memories:
            return
        identity_texts = self._extract_memory_contents(llm_context.identity_memories)
        if identity_texts:
            sections.append("## Identity\n" + " ".join(identity_texts))

    def _extract_memory_contents(self, memories: list[dict[str, object]]) -> list[str]:
        """Extract content strings from memory list."""
        return [str(m.get("content", "")) for m in memories if m.get("content")]

    def _execute_knowledge_service_function(
        self, process_key: str, function_name: str, args: str, context: TemplateFunctionContext
    ) -> str:
        """Execute knowledge service template functions.

        Handles errors gracefully — returns text instead of raising.
        This is called from the error recovery path (process_error observation),
        so crashing here would make error recovery worse.
        """
        if self.knowledge_service is None:
            return "Knowledge service not configured"

        if function_name == "search":
            return self._execute_knowledge_search(process_key, args, context)

        return f"Knowledge service function '{function_name}' not supported in templates"

    def _execute_knowledge_search(
        self, _process_key: str, args: str, _context: TemplateFunctionContext
    ) -> str:
        """Execute knowledge search with graceful error handling.

        Returns formatted text (never raises) because this runs in the error
        recovery path where failures should be visible, not fatal.
        """
        try:
            query = self._extract_arg_value(args, r"query\s*=\s*['\"]([^'\"]+)['\"]")
            process_key_filter = self._extract_arg_value(
                args, r"process_key\s*=\s*['\"]([^'\"]+)['\"]"
            )
            top_k_str = self._extract_arg_value(args, r"top_k\s*=\s*(\d+)")
            top_k = int(top_k_str) if top_k_str else 3

            if not query:
                return "Knowledge search requires a 'query' argument"

            # Call knowledge_service.search() — the service handles all filtering
            result = self.knowledge_service.search(  # type: ignore[union-attr]
                query=query,
                top_k=top_k,
                process_key=process_key_filter,
            )

            results_list = result.get("results", [])
            if not results_list:
                return "No relevant knowledge base documentation found for this error."

            # Format results for inclusion in observation
            formatted_parts: list[str] = []
            for item in results_list:
                kb_name = item.get("knowledge_base", "unknown")
                file_path = item.get("file_path", "unknown")
                content = item.get("content", "")
                tier = item.get("tier", "semantic")
                formatted_parts.append(
                    f"[{kb_name}/{file_path} ({tier})]:\n{content}"
                )

            return "\n---\n".join(formatted_parts)

        except Exception as e:
            return f"KNOWLEDGE_RETRIEVAL_FAILED: {e}"

    def _execute_flow_service_function(
        self, process_key: str, function_name: str, args: str, context: TemplateFunctionContext
    ) -> str:
        """Execute flow service template functions using state_service."""
        if not self.state_service:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message="State service not available for flow service template function execution",
            )

        # Resolve template variables in args (e.g., FLOW_ID -> actual flow_id)
        resolved_args = self._resolve_template_variables_in_args(args, context)

        if function_name == "get_flow_input":
            return self._execute_get_flow_input(process_key, resolved_args, context)
        elif function_name == "get_flow_input_for_presentation":
            return self._execute_get_flow_input_for_presentation(process_key, resolved_args, context)
        else:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message=f"Flow service function '{function_name}' not supported in templates",
            )

    def _execute_get_flow_input(
        self, process_key: str, args: str, context: TemplateFunctionContext
    ) -> str:
        """Execute get_flow_input by querying flow record from state service.

        Uses context.flow_id directly - flow_id is now required in TemplateFunctionContext.
        """
        if not self.state_service:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message="State service not available",
            )

        # Use typed context attribute directly - flow_id is required
        flow_id = context.flow_id
        logger.debug(f"TEMPLATE_FLOW: Querying flow input for flow_id={flow_id}")

        try:
            result = self.state_service.read_state(
                namespace="core",
                query={"table": "flows", "filters": {"id": flow_id}},
            )
            return self._extract_user_input_from_flow_result(result, flow_id)

        except Exception as e:
            logger.error(f"TEMPLATE_FLOW: Error querying flow {flow_id}: {e}")
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message=f"Error querying flow {flow_id}: {e}",
            ) from e

    def _execute_get_flow_input_for_presentation(
        self, process_key: str, args: str, context: TemplateFunctionContext
    ) -> str:
        """Execute get_flow_input_for_presentation - same as get_flow_input but excludes attachments.

        Used in result/error presentation prompts to prevent LLM from confusing
        input attachments with output attachments.
        """
        if not self.state_service:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message="State service not available",
            )

        flow_id = context.flow_id
        logger.debug(f"TEMPLATE_FLOW: Querying flow input for presentation, flow_id={flow_id}")

        try:
            result = self.state_service.read_state(
                namespace="core",
                query={"table": "flows", "filters": {"id": flow_id}},
            )
            return self._extract_user_input_from_flow_result_for_presentation(result, flow_id)

        except Exception as e:
            logger.error(f"TEMPLATE_FLOW: Error querying flow {flow_id}: {e}")
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message=f"Error querying flow {flow_id}: {e}",
            ) from e

    def _extract_user_input_from_flow_result_for_presentation(
        self, result: ActionResult, flow_id: str
    ) -> str:
        """Extract user input from flow result, excluding attachments.

        Same as _extract_user_input_from_flow_result but intentionally excludes
        attachments to prevent LLM confusion in result/error presentation prompts.
        """
        flow_record = self._validate_and_get_flow_record(result, flow_id)
        if flow_record is None:
            return ""

        trigger_data = self._parse_trigger_data(flow_record.get("trigger_data", {}))
        user_input = trigger_data.get("user_input") or trigger_data.get("input") or ""

        # Return only original_input, never attachments
        flow_input_data: dict[str, object] = {"original_input": str(user_input)}
        self._inject_io_metadata(trigger_data, flow_input_data)
        return json.dumps(flow_input_data)

    def _extract_user_input_from_flow_result(self, result: ActionResult, flow_id: str) -> str:
        """Extract user input and attachments from flow query result.

        Always returns a JSON string containing:
        - original_input: The user's text input (empty string for system flows)
        - flow_id: The flow identifier
        - attachments: List of attachment objects (optional, only if present)

        Contract:
        - User flows: JSON with original_input containing user text
        - System flows: JSON with original_input = "" (enables skip-recall path)
        - Error cases: Empty string "" (triggers fail-fast in consumer)
        """
        flow_record = self._validate_and_get_flow_record(result, flow_id)
        if flow_record is None:
            return ""

        trigger_data = self._parse_trigger_data(flow_record.get("trigger_data", {}))
        return self._build_flow_input_json(trigger_data)

    def _validate_and_get_flow_record(
        self, result: ActionResult, flow_id: str
    ) -> dict[str, object] | None:
        """Validate flow result and extract flow record."""
        action_status = str(result.get("action_status", "")).lower()
        if action_status != "completed":
            logger.error(f"TEMPLATE_FLOW: Flow query failed for {flow_id} (status={action_status})")
            return None

        data = result.get("data", {})
        records = data.get("records", [])
        if not records or not isinstance(records, list):
            logger.error(f"TEMPLATE_FLOW: No flow record found for {flow_id}")
            return None

        flow_record = records[0]
        if not isinstance(flow_record, dict):
            logger.error(f"TEMPLATE_FLOW: Malformed flow record for {flow_id}")
            return None

        return flow_record

    @staticmethod
    def _inject_io_metadata(
        trigger_data: dict[str, object],
        flow_input_data: dict[str, object],
    ) -> None:
        """Inject IO source metadata from trigger_data into the flow_input dict.

        Adds source_namespace, source, sender_name, and session_id when present
        in trigger_data.  These fields are used by the inference plugin to build
        event metadata for context event trailers (namespace, source/destination,
        session_id, posted_at).
        """
        source_namespace = trigger_data.get("source_namespace", "")
        if source_namespace:
            flow_input_data["source_namespace"] = str(source_namespace)
        source = trigger_data.get("source", "")
        if source:
            flow_input_data["source"] = str(source)
        sender_name = trigger_data.get("sender_name", "")
        if sender_name:
            flow_input_data["sender_name"] = str(sender_name)
        session_id = trigger_data.get("session_id", "")
        if session_id:
            flow_input_data["session_id"] = str(session_id)

    def _build_flow_input_json(
        self,
        trigger_data: dict[str, object],
    ) -> str:
        """Build JSON string from flow trigger data.

        Note: flow_id is intentionally NOT included in the output.
        It's an internal routing identifier that the LLM doesn't need.
        Including it caused confusion where LLM used it as attachment names.
        """
        user_input = trigger_data.get("user_input") or trigger_data.get("input") or ""
        attachments = trigger_data.get("attachments", [])

        flow_input_data: dict[str, object] = {
            "original_input": str(user_input) if user_input else "",
        }
        if attachments:
            flow_input_data["attachments"] = attachments
        self._inject_io_metadata(trigger_data, flow_input_data)
        return json.dumps(flow_input_data)

    def _parse_trigger_data(self, trigger_data_raw: object) -> dict[str, object]:
        """Parse trigger_data from raw value (may be JSON string or dict)."""
        if isinstance(trigger_data_raw, dict):
            return trigger_data_raw
        if isinstance(trigger_data_raw, str):
            try:
                parsed = json.loads(trigger_data_raw)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        return {}

    def _execute_plugin_template_function(
        self,
        process_key: str,
        provider: str,
        function_name: str,
        args: str,
        context: TemplateFunctionContext,
    ) -> str:
        """Execute plugin template functions with validation and error handling.

        Note: Plugin interface still uses dict context (P1 task to update).
        We convert the typed context to dict for plugin calls.
        """
        if not self.plugin_manager:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message="Plugin manager not available for template function execution",
            )

        plugin = self.plugin_manager.get_plugin(provider)
        if not plugin:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message=f"Plugin '{provider}' not found",
            )

        # Type narrow plugin to ensure it has template function support
        if not hasattr(plugin, "supports_template_functions") or not callable(
            getattr(plugin, "supports_template_functions", None)
        ):
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message=f"Plugin '{provider}' does not implement template function protocol",
            )

        # Cast to PluginProtocol after validation
        typed_plugin: PluginProtocol = plugin  # type: ignore[assignment]
        supports_templates: bool = typed_plugin.supports_template_functions()
        if not supports_templates:
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message=f"Plugin '{provider}' does not support template functions",
            )

        # Type narrow plugin to ensure it has execute_template_function method
        if not hasattr(plugin, "execute_template_function") or not callable(
            getattr(plugin, "execute_template_function", None)
        ):
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message=f"Plugin '{provider}' does not implement execute_template_function method",
            )

        try:
            logger.debug(
                f"TEMPLATE_FUNCTION_EXECUTE: Calling {provider}.execute_template_function('{function_name}', '{args}')"
            )

            resolved_args = self._resolve_template_variables_in_args(args, context)
            logger.debug(f"TEMPLATE_FUNCTION_ARGS_RESOLVED: '{args}' -> '{resolved_args}'")

            # Pass typed context directly to plugin
            result: str = typed_plugin.execute_template_function(
                function_name, resolved_args, context
            )
            logger.debug(
                f"TEMPLATE_FUNCTION_RESULT: {provider}::{function_name} returned: {str(result)[:100]}..."
            )
            return result
        except NotImplementedError as e:
            raise TemplateFunctionError(
                function_name=process_key, function_args=args, error_message=str(e)
            ) from e
        except Exception as e:
            logger.error(
                f"TEMPLATE_FUNCTION_ERROR: Plugin '{provider}' function '{function_name}' failed: {str(e)}"
            )
            raise TemplateFunctionError(
                function_name=process_key,
                function_args=args,
                error_message=f"Plugin template function execution failed: {str(e)}",
            ) from e

    def _execute_read_state_function(self, args: str, _context: TemplateFunctionContext) -> str:
        if not self.state_service:
            raise TemplateFunctionError("read_state", str(args), "State service not available")

        try:
            # Parse arguments using dedicated service
            namespace, query = self.argument_parser.parse_read_state_args(args)

            # Execute the read_state operation
            result = self.state_service.read_state(namespace=namespace, query=query)

            # CRITICAL DEBUG: Log the actual result structure
            logger.debug(f"READ_STATE_DEBUG: result type = {type(result)}")
            logger.debug(f"READ_STATE_DEBUG: result = {result}")
            if "data" in result:
                logger.debug(f"READ_STATE_DEBUG: result['data'] = {result['data']}")
                logger.debug(f"READ_STATE_DEBUG: result['data'] type = {type(result['data'])}")

            # Format result using dedicated service
            return self.result_formatter.format_result(result)

        except Exception as e:
            raise TemplateFunctionError(
                "read_state", str(args), f"Failed to execute read_state: {str(e)}"
            ) from e

    def _register_core_functions(self) -> None:
        pass

    def get_available_functions(self) -> list[str]:
        return list(self.functions.keys())

    def get_function_info(self) -> dict[str, str]:
        return self.function_descriptions.copy()

    def _resolve_template_variables_in_args(
        self, args: str, context: TemplateFunctionContext
    ) -> str:
        """Resolve template variables like SESSION_ID, FLOW_ID in function arguments.

        Uses typed context attributes directly - no dict access needed.
        IMPORTANT: Placeholders are UPPERCASE (SESSION_ID, FLOW_ID) and must NOT be
        matched case-insensitively, because parameter names like 'session_id=' would
        also match.
        """
        resolved_args = self._resolve_session_id_placeholder(args, context)
        resolved_args = self._resolve_flow_id_placeholder(resolved_args, context)
        return resolved_args

    def _resolve_session_id_placeholder(self, args: str, context: TemplateFunctionContext) -> str:
        """Replace uppercase SESSION_ID placeholder with actual session_id from typed context."""
        session_id_pattern = re.compile(r"\bSESSION_ID\b")
        if not session_id_pattern.search(args):
            return args

        # Use typed context attribute directly
        if not context.session_id:
            return args

        return session_id_pattern.sub(context.session_id, args)

    def _resolve_flow_id_placeholder(self, args: str, context: TemplateFunctionContext) -> str:
        """Replace uppercase FLOW_ID placeholder with actual flow_id from typed context.

        Note: flow_id is required in TemplateFunctionContext, so this should always succeed.
        """
        flow_id_pattern = re.compile(r"\bFLOW_ID\b")
        if not flow_id_pattern.search(args):
            return args

        # Use typed context attribute directly - flow_id is required
        return flow_id_pattern.sub(context.flow_id, args)

    def resolve_in_data_structure(self, data: object, context: TemplateFunctionContext) -> object:
        """Recursively resolve <<<:...>>> template function patterns in data structures.

        This resolves template functions like:
            <<<:service_interface::discovery_service::get_process_by_key(...)>>>

        Before the data is passed to the LLM, ensuring the LLM never sees
        the template syntax (which could cause it to generate similar patterns).

        Args:
            data: Data structure containing potential template function patterns
            context: Typed context with required flow_id and other execution data

        Returns:
            Resolved data with template functions replaced by their results
        """
        if isinstance(data, dict):
            return {
                key: self.resolve_in_data_structure(value, context) for key, value in data.items()
            }
        if isinstance(data, list):
            return [self.resolve_in_data_structure(item, context) for item in data]
        if isinstance(data, str):
            return self._resolve_string_templates(data, context)
        return data

    def _resolve_string_templates(self, data: str, context: TemplateFunctionContext) -> object:
        """Resolve template patterns in a string value."""
        working_str = self._substitute_variable_placeholders(data, context)

        matches = TEMPLATE_FUNCTION_PATTERN.findall(working_str)
        if not matches:
            return working_str

        full_match = TEMPLATE_FUNCTION_PATTERN.fullmatch(working_str.strip())
        if full_match:
            return self._resolve_single_template_function(full_match.group(1), context)

        return self._resolve_embedded_template_functions(working_str, matches, context)

    def _substitute_variable_placeholders(self, data: str, context: TemplateFunctionContext) -> str:
        """Substitute <<VARIABLE>> patterns from typed context variables."""
        import json as json_module

        if "<<" not in data or ">>" not in data:
            return data

        # Build lookup dict from typed context variables
        variables: dict[str, object] = {}
        variables.update(context.global_variables)
        variables.update(context.local_variables)
        # Add core context values
        variables[CONTEXT_KEY_SESSION_ID] = context.session_id
        variables[CONTEXT_KEY_FLOW_ID] = context.flow_id
        if context.action_id:
            variables["action_id"] = context.action_id

        def substitute_variable(match: re.Match[str]) -> str:
            var_name = match.group(1)
            if var_name not in variables:
                return match.group(0)
            value = variables[var_name]
            if isinstance(value, dict | list):
                return json_module.dumps(value)
            return str(value) if value is not None else ""

        return re.sub(r"<<(\w+)>>", substitute_variable, data)

    def _resolve_single_template_function(
        self, func_call: str, context: TemplateFunctionContext
    ) -> object:
        """Resolve a single template function, preserving object return type."""
        import json as json_module

        result = self.execute_function(func_call, context)

        try:
            return json_module.loads(result)
        except json_module.JSONDecodeError:
            return result

    def _resolve_embedded_template_functions(
        self, working_str: str, matches: list[str], context: TemplateFunctionContext
    ) -> str:
        """Resolve multiple or embedded template functions via string replacement."""
        result_str = working_str
        for func_call in matches:
            resolved = self.execute_function(func_call, context)
            result_str = result_str.replace(f"<<<:{func_call}>>>", str(resolved))
        return result_str
