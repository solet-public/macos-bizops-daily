"""Discovery Service Public API.

AI-discoverable process discovery operations with @service_interface_process decorators.
All methods in this interface are indexed for process discovery.
"""

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.services.service_interface_decorator import service_interface_process


class DiscoveryServiceAPI(ABC):
    """Public discovery operations - AI-discoverable via process registry.

    This interface defines discovery operations that can be discovered and
    invoked by the AI orchestration system:

    1. query_process_registry - Search for processes by semantic query
    2. get_service_health - Get discovery service health and statistics

    Each method is decorated with complete metadata for process registry.
    """

    @service_interface_process(
        name="query_process_registry",
        work_count_impact=0,  # Non-terminal: flow must continue to use discovered process
        provider="discovery_service",
        parameters={
            "query": ParameterMetadata(
                description=(
                    "Natural language description of what needs to be accomplished. "
                    "Examples: 'generate a sine tone', 'create image from text', "
                    "'search knowledge base', 'schedule task for later'"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "max_results": ParameterMetadata(
                description="Maximum number of results to return",
                required=False,
                type=ParameterType.INTEGER,
                default=10,
            ),
            "state": ParameterMetadata(
                description=(
                    "Optional runtime state (e.g., flow_id, session_id) for correlation. "
                    "This is automatically injected by ActionProcessor and is not required "
                    "for discovery to run."
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Discovery result with ranked list of matching processes (lightweight metadata for selection)",
            type=ParameterType.OBJECT,
            properties={
                "process_keys": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Array of process key strings for reference",
                    required=False,
                ),
                "process_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Total number of matching processes found",
                    required=False,
                ),
                "processes": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Process list with full schemas. Each entry has: process_key, provider_type, provider, "
                        "function_name, description, and invocation_schema (full JSON Schema for constructing "
                        "valid action arguments including all parameters, types, and validation constraints)."
                    ),
                    required=False,
                ),
                "query": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="The search query that was executed",
                    required=False,
                ),
                "match_type": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Type of matching used (e.g., text_with_disambiguation, no_matches)",
                    required=False,
                ),
                "timestamp": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="ISO timestamp when the search was performed",
                    required=False,
                ),
            },
            usage_patterns=[
                "Find appropriate tools for user tasks",
                "Discover available capabilities",
                "Use description to select the right process",
                "Use invocation_schema to construct valid action arguments",
            ],
        ),
        is_enabled=True,
        chaining_guidance=[
            "WORKFLOW:",
            "  1. query_process_registry: Find matching processes with full invocation_schema",
            "  2. Use processes[*].invocation_schema to construct valid arguments",
            "  3. If required arguments are missing, ask via post_message",
            "  4. If no matches, retry with different keywords or inform the user",
            "invocation_schema contains full parameter definitions with validation constraints.",
            "ALWAYS follow validation patterns - e.g., filename pattern requires file extension.",
            "If discovery returns no matches, inform the user - do NOT invent a process.",
            "NEVER use the query text as a process_key - only use actual process keys.",
        ],
        result_processor_customizations=MergeResultProcessorCustomizations(
            # Structure-only schema - no oneOf enumeration of processes
            # The model reads process schemas from SYSTEM message (built-in) and
            # USER message (discovery results) to determine which process to invoke.
            # See: knowledge_base/2026-02-02_inference_and_discord_troubleshooting.md
            output_schema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "required": ["reasoning", "actions"],
                "properties": {
                    "reasoning": {
                        "type": "string",
                        "description": "Brief explanation of intent and action choice",
                    },
                    "actions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 1,
                        "description": "Actions to execute",
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
                            "additionalProperties": False,
                        },
                    },
                },
                "additionalProperties": False,
            }
        ),
    )
    @abstractmethod
    def query_process_registry(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Search for processes by semantic query using vector embeddings.

        This is the plugin-level interface. Plugins receive bundled params.
        Internally calls the provider's process discovery method.

        APP_HOME is obtained from self.orchestrator_ref.APP_HOME in prepare_for_readiness().
        """
        ...

    @service_interface_process(
        name="get_service_health",
        provider="discovery_service",
        parameters={},
        return_value_schema=ReturnValueSchema(
            description="Platform statistics and service health metrics",
            type=ParameterType.OBJECT,
            properties={
                "is_healthy": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="Whether the discovery service is healthy",
                    required=True,
                ),
                "total_processes": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Total number of processes registered in the process registry",
                    required=True,
                ),
                "total_usage_records": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Total number of usage tracking records",
                    required=True,
                ),
                "index_last_built": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="When the discovery index was last rebuilt",
                    required=False,
                ),
            },
            usage_patterns=[
                "Answer 'how many processes are registered?'",
                "Get platform capability statistics",
                "Monitor system health",
            ],
        ),
        is_enabled=True,
    )
    @abstractmethod
    def get_service_health(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Get discovery service health and statistics.

        This is the plugin-level interface. Plugins receive bundled params.
        Internally calls the provider's health check method.

        APP_HOME is obtained from self.orchestrator_ref.APP_HOME in prepare_for_readiness().
        """
        ...

    @service_interface_process(
        name="execute_embeddings_search",
        provider="discovery_service",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        work_count_impact=0,
        parameters={
            "query": ParameterMetadata(
                description="Semantic query for process discovery (e.g., 'generate audio')",
                required=True,
                type=ParameterType.STRING,
            ),
            "original_input": ParameterMetadata(
                description="User's original input for context preservation",
                required=True,
                type=ParameterType.STRING,
            ),
            "state": ParameterMetadata(
                description="Current application state (automatically injected)",
                required=False,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Embeddings search results with matched processes",
            properties={
                "processes": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Matched processes with schemas",
                    required=False,
                ),
                "process_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of matched processes",
                    required=False,
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            # Structure-only schema - no oneOf enumeration of processes
            # The model reads process schemas from SYSTEM message (built-in) and
            # USER message (discovery results) to determine which process to invoke.
            # See: knowledge_base/2026-02-02_inference_and_discord_troubleshooting.md
            output_schema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "required": ["reasoning", "actions"],
                "properties": {
                    "reasoning": {
                        "type": "string",
                        "description": "Brief explanation of intent and action choice",
                    },
                    "actions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 1,
                        "description": "Actions to execute",
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
                            "additionalProperties": False,
                        },
                    },
                },
                "additionalProperties": False,
            }
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    @abstractmethod
    def execute_embeddings_search(
        self, params: dict[str, Any], state: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute vector embeddings search for process discovery.

        Returns matched processes for the result processor.

        Args:
            params: Contains 'query' (operation) and 'original_input'
            state: Current application state

        Returns:
            Discovery results with matched processes
        """
        ...

    @service_interface_process(
        name="get_process_schema",
        work_count_impact=0,  # Non-terminal: flow continues to execute the discovered process
        provider="discovery_service",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "process_key": ParameterMetadata(
                description=(
                    "The fully-qualified process identifier. "
                    "Format: 'provider_type::provider::function_name' "
                    "(e.g., 'plugin::audio_processing_plugin::generate_tone')"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Process schema with invocation details (wrapped in data field)",
            type=ParameterType.OBJECT,
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: 'completed' or 'error'",
                    required=True,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Contains process_key, description, and invocation_schema",
                    required=True,
                ),
            },
            usage_patterns=[
                "Get full parameter schema after discovery selects a process",
                "Understand exact argument requirements before execution",
                "Construct valid action payloads using schema definitions",
            ],
        ),
        is_enabled=True,
        chaining_guidance=[
            "Call this AFTER query_process_registry has selected a process.",
            "Use the returned data.invocation_schema to construct your action arguments.",
            "Do NOT guess argument structure - use the schema definitions.",
            "After getting the schema, construct and execute the action.",
        ],
        result_processor_customizations=MergeResultProcessorCustomizations(
            # Custom schema allowing arbitrary arguments for discovered process invocation
            output_schema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "required": ["reasoning", "actions"],
                "properties": {
                    "reasoning": {"type": "string"},
                    "actions": {
                        "type": "array",
                        "minItems": 1,
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
                                "arguments": {
                                    "type": "object",
                                    # Allow ANY arguments - schema is defined by invocation_schema
                                    "additionalProperties": True,
                                },
                            },
                            "additionalProperties": False,
                        },
                    },
                },
                "additionalProperties": False,
            }
        ),
    )
    @abstractmethod
    def get_process_schema(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Retrieve the full invocation schema for a process.

        This is Step 2 of the two-step discovery workflow:
        1. query_process_registry - find matching processes (lightweight metadata)
        2. get_process_schema - get full schema for selected process

        Args:
            params: Contains 'process_key' (required)
            state: Current application state

        Returns:
            ActionResult with process_key, description, and invocation_schema
        """
        ...
