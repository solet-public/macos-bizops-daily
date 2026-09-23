"""Invocation schema + introspection metadata generation.

Extracted from `ProcessRegistryBuilder` during the Step 9.A decomposition
(`workbench/2026-05-25_plugin_god_class_remediation.md` §9.1).

This collaborator owns:
  - Per-process JSON-Schema generation that tells the LLM how to invoke a
    process inside a plan step (`generate`).
  - Whole-registry introspection metadata (discovery hints, AI-usage guide,
    schema version) attached as top-level registry keys
    (`add_introspection_metadata`).
  - Service-interface-side metadata extractors that flow into per-entry
    `planning_docs` / `error_handling_docs` / `response_handling_docs`
    structures (`generate_input_contract_from_metadata`,
    `generate_action_blueprint_from_metadata`,
    `extract_planning_docs_from_metadata`,
    `extract_error_handling_docs_from_metadata`,
    `extract_response_handling_docs_from_metadata`).

No internal state — every public entry threads its inputs explicitly. The
collaborator is constructed once by the orchestrator and shared with
`PluginProcessScanner`, `ServiceInterfaceScanner`, and
`KnowledgeBaseOverlayLoader`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from ananta.core.process_registry.introspector import ProcessRegistryIntrospector
from ananta.core.services.service_interface_decorator import ServiceInterfaceActionMetadata

logger = logging.getLogger(__name__)


class InvocationSchemaGenerator:
    """Generates JSON-Schema for process invocations + per-entry introspection.

    Construct once per `build_process_registry` call. Stateless — every
    method takes the data it needs explicitly. Shared by plugin scanner,
    service-interface scanner, and knowledge-base overlay loader.
    """

    def generate(
        self,
        process_key: str,
        parameters_dict: dict[str, object],
    ) -> dict[str, object]:
        """Generate JSON Schema for invoking this process in a plan step.

        Creates an unambiguous schema that tells the LLM exactly how to structure
        a plan step for this process, including required arguments.

        Args:
            process_key: The full process key (e.g., 'plugin::jsonrpc_plugin::post_message')
            parameters_dict: Dict of parameter name -> parameter metadata dict

        Returns:
            JSON Schema dict defining the exact structure for invoking this process
        """
        # Map internal types to JSON Schema types
        type_mapping = {
            "string": "string",
            "int": "integer",
            "float": "number",
            "bool": "boolean",
            "dict": "object",
            "list": "array",
            "object": "object",
        }

        # Build arguments schema from parameters
        required_args: list[str] = []
        arg_properties: dict[str, object] = {}

        # TODO: Consolidate with _build_parameter_property and extract_planning_docs_from_metadata
        # All three methods build JSON Schema properties with inconsistent field handling
        for param_name, param_meta in parameters_dict.items():
            if isinstance(param_meta, dict):
                param_type = param_meta.get("type", "string")
                param_desc = param_meta.get("description", "")
                param_required = param_meta.get("required", False)
                param_default = param_meta.get("default")
                param_examples = param_meta.get("examples", [])
                param_validation = param_meta.get("validation")

                # Build property schema
                json_type = type_mapping.get(param_type, "string")
                prop_schema: dict[str, object] = {
                    "type": json_type,
                    "description": param_desc,
                }

                # Array item shape comes from the plugin's
                # ParameterMetadata.validation field (e.g. validation={"items":
                # {"type": "object", "required": [...]}} or {"items": {}} for
                # any item type). Per the platform's "Validation ≤ Execution"
                # rule, we never fabricate constraints the runtime does not
                # impose — defaulting array items to string here previously
                # caused arrays of objects (notes, harmonics, breakpoints,
                # event_schedule entries) to be rejected by the PipelineSpec
                # shape validator even though the runtime accepted them.
                # Plugins that want strict item validation declare it via
                # validation={"items": ...}; otherwise items are unconstrained.

                if param_default is not None:
                    prop_schema["default"] = param_default

                if param_examples:
                    prop_schema["examples"] = param_examples

                # Merge validation constraints (e.g., pattern, minimum, maximum) into schema
                if isinstance(param_validation, dict):
                    prop_schema.update(param_validation)

                arg_properties[param_name] = prop_schema

                if param_required:
                    required_args.append(param_name)

        # Parse process_key into structured components
        parts = process_key.split("::")
        if len(parts) >= 3:
            provider_type, provider, function_name = parts[0], parts[1], parts[2]
        else:
            # Fallback for malformed keys
            provider_type, provider, function_name = "unknown", "unknown", process_key

        # Build the full invocation schema with structured process object
        invocation_schema: dict[str, object] = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "description": f"Schema for invoking {process_key}",
            "required": ["process", "reason", "arguments"],
            "properties": {
                "process": {
                    "type": "object",
                    "description": "Structured process identifier",
                    "required": ["provider_type", "provider", "function_name"],
                    "properties": {
                        "provider_type": {
                            "type": "string",
                            "const": provider_type,
                        },
                        "provider": {
                            "type": "string",
                            "const": provider,
                        },
                        "function_name": {
                            "type": "string",
                            "const": function_name,
                        },
                    },
                    "additionalProperties": False,
                },
                "reason": {
                    "type": "string",
                    "description": "Brief explanation of why this step is needed",
                },
                "arguments": {
                    "type": "object",
                    "required": required_args,
                    "properties": arg_properties,
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        }

        return invocation_schema

    def add_introspection_metadata(self, registry: dict[str, object]) -> None:
        """Attach discovery hints, AI usage guide, and schema-version metadata.

        Top-level registry keys populated:
          - `discovery`: surface for downstream introspection / search
          - `ai_usage_guide`: strategy guidance for LLM consumers
          - `introspection`: schema version + capability flags
        """
        # Type narrow processes to dict
        processes = registry["processes"]
        if not isinstance(processes, dict):
            raise TypeError("Registry processes must be a dict")

        # Create introspector instance
        introspector = ProcessRegistryIntrospector(registry)

        # Add discovery metadata
        # Note: by_capability and by_category methods have been removed as dead infrastructure
        registry["discovery"] = {
            "description": "This process registry is self-describing. Use these methods to discover and understand available processes.",
            "discovery_methods": {
                "by_text_search": {
                    "description": "Search process names, descriptions, and documentation using semantic matching",
                    "example_query": "query database tables",
                    "tips": [
                        "Use natural language to describe what you want to do",
                        "Include keywords like 'read', 'write', 'generate', 'search'",
                        "Mention specific technologies or domains",
                    ],
                },
                "by_provider": {
                    "description": "Find processes by their provider (plugin or service interface)",
                    "example_query": "state_service",
                    "available_providers": list(introspector.provider_index.keys()),
                },
            },
            "common_workflows": [
                {
                    "name": "Data Discovery Workflow",
                    "pattern": "list_namespaces → describe_schema → read_state",
                    "description": "Discover available data, understand structure, then query specific data",
                    "use_when": "User asks about available data or wants to explore the database",
                },
                {
                    "name": "Content Generation Workflow",
                    "pattern": "define_requirements → generate_content → review_output",
                    "description": "Generate images, text, or other content based on user requirements",
                    "use_when": "User wants to create or generate something",
                },
                {
                    "name": "System Analysis Workflow",
                    "pattern": "read_system_state → analyze_data → report_findings",
                    "description": "Analyze system state, logs, or operational data",
                    "use_when": "User asks about system status, errors, or performance",
                },
            ],
        }

        # Add AI usage guidance for the registry itself
        registry["ai_usage_guide"] = {
            "description": "Guidelines for AI systems using this process registry",
            "discovery_strategy": [
                "1. If user request is unclear, use text search to find relevant processes",
                "2. If user mentions specific capabilities (read, write, generate), search by capability",
                "3. For data-related requests, start with list_namespaces and describe_schema",
                "4. Always check process examples and ai_guidance before using a process",
                "5. Use parameter ai_hints to understand how to format parameters correctly",
            ],
            "parameter_strategy": [
                "1. Always check required vs optional parameters",
                "2. Use parameter examples as templates for your values",
                "3. Read ai_hints for parameter-specific guidance",
                "4. For complex objects, start with simple examples and expand",
                "5. Validate parameter formats using format_hint when available",
            ],
            "error_handling": [
                "1. If a process fails, check the possible_errors in its metadata",
                "2. Use troubleshooting_tips from ai_guidance for common issues",
                "3. For data operations, verify namespace and table existence first",
                "4. Check dependencies and prerequisites before using a process",
            ],
            "best_practices": [
                "1. Use the most specific process for the task (read_state vs query_state)",
                "2. Follow common_patterns from ai_guidance for complex workflows",
                "3. Always provide clear, descriptive parameters",
                "4. Use estimated_duration to set user expectations for long operations",
                "5. Check related_processes for alternative approaches",
            ],
        }

        # Add schema version and introspection capabilities
        registry["introspection"] = {
            "schema_version": "1.0.0",
            "supports_discovery": True,
            "supports_documentation": True,
            "supports_examples": True,
            "supports_ai_guidance": True,
            "last_updated": datetime.now(UTC).isoformat(),
            "total_processes": len(processes),
            "self_description": "This registry follows OpenAPI-like self-description principles for AI consumption",
        }

    def generate_input_contract_from_metadata(
        self, metadata: ServiceInterfaceActionMetadata
    ) -> dict[str, object]:
        """Generate input_contract from ServiceInterfaceActionMetadata.

        Args:
            metadata: ServiceInterfaceActionMetadata instance

        Returns:
            Input contract dict with parameters, context requirements, and result shape
        """
        # Build parameters dict
        parameters_dict: dict[str, object] = {}
        for param_name, param_meta in metadata.parameters.items():
            parameters_dict[param_name] = param_meta.to_dict()

        contract: dict[str, object] = {
            "parameters": parameters_dict,
            "context_requirements": ["session_id"] if parameters_dict else [],
            "result_shape": metadata.return_value_schema.to_dict(),
        }

        return contract

    def generate_action_blueprint_from_metadata(
        self, process_key: str, metadata: ServiceInterfaceActionMetadata
    ) -> dict[str, object]:
        """Generate action_blueprint from ServiceInterfaceActionMetadata.

        Args:
            process_key: Full process key
            metadata: ServiceInterfaceActionMetadata instance

        Returns:
            Action blueprint dict with complete default structure
        """
        # Build default arguments from parameters
        default_args: dict[str, object] = {}
        for param_name, param_meta in metadata.parameters.items():
            if param_meta.default is not None:
                default_args[param_name] = param_meta.default
            elif param_meta.required:
                # Required param needs placeholder
                if param_name == "prompt" or "prompt" in param_name.lower():
                    default_args[param_name] = "<<USER_INPUT>>"
                elif param_name == "message":
                    default_args[param_name] = "<<MESSAGE>>"
                else:
                    default_args[param_name] = f"<<{param_name.upper()}>>"

        blueprint: dict[str, object] = {
            "process_key": process_key,
            "arguments": default_args,
            "context_overrides": {},
            "metadata": {
                "is_inference_capable": metadata.is_inference_capable,
                "estimated_duration": "< 1s",  # Could be made configurable
                "version": metadata.version,
            },
            "post_processing": {},
        }

        return blueprint

    def extract_planning_docs_from_metadata(
        self, process_key: str, metadata: ServiceInterfaceActionMetadata
    ) -> dict[str, object]:
        """Extract planning-relevant documentation from ServiceInterfaceActionMetadata."""
        # Build proper JSON Schema format for parameters
        parameters_schema: dict[str, object] = {"type": "object", "required": [], "properties": {}}

        for param_name, param_meta in metadata.parameters.items():
            prop = {
                "type": param_meta.type.value
                if hasattr(param_meta.type, "value")
                else str(param_meta.type),
                "description": param_meta.description,
            }
            if param_meta.required:
                parameters_schema["required"].append(param_name)  # type: ignore[attr-defined]
            if param_meta.default is not None:
                prop["default"] = param_meta.default  # type: ignore[assignment]
            if hasattr(param_meta, "validation") and param_meta.validation:
                prop.update(param_meta.validation)  # type: ignore[arg-type]
            if hasattr(param_meta, "examples") and param_meta.examples:
                prop["examples"] = param_meta.examples  # type: ignore[assignment]
            parameters_schema["properties"][param_name] = prop  # type: ignore[index]

        docs: dict[str, object] = {
            "process_key": process_key,
            "summary": metadata.display_name,
            "description": metadata.description,
            "parameters": parameters_schema,
        }

        return docs

    def extract_error_handling_docs_from_metadata(
        self, process_key: str, metadata: ServiceInterfaceActionMetadata
    ) -> dict[str, object]:
        """Extract error handling documentation from ServiceInterfaceActionMetadata."""
        docs: dict[str, object] = {
            "process_key": process_key,
            "summary": metadata.display_name,
        }

        return docs

    def extract_response_handling_docs_from_metadata(
        self, process_key: str, metadata: ServiceInterfaceActionMetadata
    ) -> dict[str, object]:
        """Extract response handling documentation from ServiceInterfaceActionMetadata."""
        docs: dict[str, object] = {
            "process_key": process_key,
            "summary": metadata.display_name,
            "return_value_schema": metadata.return_value_schema.to_dict(),
        }

        return docs
