"""Inference Service Public API - AI-discoverable operations."""

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.domain.types import ActionResult
from ananta.core.services.service_interface_decorator import service_interface_process
from ananta.services.inference_service.prompts import load_prompt


class InferenceServiceAPI(ABC):
    """Public inference operations - AI-discoverable via vector search."""

    @service_interface_process(
        name="qualify",
        provider="inference_service",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Bounded live-provider qualification result",
            properties={
                "provider": ParameterMetadata(type=ParameterType.STRING),
                "model": ParameterMetadata(type=ParameterType.STRING),
                "completed": ParameterMetadata(type=ParameterType.BOOLEAN),
                "structured_result_valid": ParameterMetadata(type=ParameterType.BOOLEAN),
            },
        ),
    )
    def qualify(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Issue one bounded structured request against the bound provider.

        SessionInferenceProvider inherits this decorated registration surface
        but cannot perform a local provider probe; the concrete service wrapper
        owns the bound-provider implementation.
        """
        del params, state
        raise NotImplementedError("qualify is implemented by InferenceService")

    @service_interface_process(
        name="process_error",
        provider="inference_service",
        processor_policy_category=ProcessorPolicyCategory.VERTEX,
        parameters={
            "params": ParameterMetadata(
                description="Parameters dict (user-provided arguments passed through at execution)",
                required=False,  # Not required at submission - user provides action arguments
                type=ParameterType.OBJECT,
            ),
            "state": ParameterMetadata(
                description="Current application state (automatically injected at execution time)",
                required=False,  # Not required at submission - injected by ActionProcessor
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Error analysis with recovery suggestions",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Error analysis payload (analysis string + recovery_actions array)",
                    required=False,
                ),
            },
            usage_patterns=[
                "Analyze and recover from errors",
                "Suggest corrective actions",
            ],
        ),
        is_inference_capable=True,
        work_count_impact=0,  # Inference is neutral
        action_definition_template={
            "name": "process_action_error",
            "description": "Handle action error and determine recovery",
            "process": {
                "provider_type": "service_interface",
                "provider": "inference_service",
                "function_name": "process_error",
            },
            "arguments": {
                "params": {
                    # Keep the model object present so inference validation passes,
                    # but let runtime policy resolve temperature/max_tokens.
                    "model": {},
                    "prompt": {
                    "observation": {
                        "process_key": "<<FAILED_PROCESS_KEY>>",
                        "action_result": {
                            "action_status": "error",
                            "data": None,
                            "error": "<<ERROR>>",
                            "_completed_arguments": "<<ACTION_ARGUMENTS>>",
                        },
                    },
                    "user": {
                        "instructions": load_prompt("process_error_recovery.md"),
                        "output_schema": {
                            "$schema": "http://json-schema.org/draft-07/schema#",
                            "type": "object",
                            "required": ["reasoning", "actions"],
                            "properties": {
                                "reasoning": {
                                    "type": "string",
                                    "description": "Explain what went wrong and your recovery strategy",
                                },
                                "actions": {
                                    "type": "array",
                                    "minItems": 0,
                                    "maxItems": 1,
                                    "items": {
                                        "type": "object",
                                        "required": ["process", "reason", "arguments"],
                                        "properties": {
                                            "process": {
                                                "type": "object",
                                                "description": "Choose the best recovery action from available processes",
                                                "required": [
                                                    "provider_type",
                                                    "provider",
                                                    "function_name",
                                                ],
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
                        },
                        "session_id": "<<SESSION_ID>>",
                        "flow_input": "<<<:service_interface::flow_service::get_flow_input_for_presentation()>>>",
                    },
                    },
                },
            },
        }
    )
    @abstractmethod
    def process_error(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Analyze error and suggest recovery."""
        ...

    @service_interface_process(
        name="process_results",
        provider="inference_service",
        processor_policy_category=ProcessorPolicyCategory.VERTEX,
        parameters={
            "params": ParameterMetadata(
                description="Parameters dict (user-provided arguments passed through at execution)",
                required=False,  # Not required at submission - user provides action arguments
                type=ParameterType.OBJECT,
            ),
            "state": ParameterMetadata(
                description="Current application state (automatically injected at execution time)",
                required=False,  # Not required at submission - injected by ActionProcessor
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Formatted results for presentation",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Formatted results payload (formatted_output string)",
                    required=False,
                ),
            },
            usage_patterns=[
                "Format results for user presentation",
                "Convert raw data to readable output",
            ],
        ),
        is_inference_capable=True,
        work_count_impact=0,  # Inference is neutral
        action_definition_template={
            "name": "process_action_result",
            "description": "Process action result and determine next steps",
            "process": {
                "provider_type": "service_interface",
                "provider": "inference_service",
                "function_name": "process_results",
            },
            "arguments": {
                "params": {
                    # Keep the model object present so inference validation passes,
                    # but let runtime policy resolve temperature/max_tokens.
                    "model": {},
                    "prompt": {
                    "observation": {
                        "action_result": {
                            "action_status": "completed",
                            "data": "<<RESULT>>",
                            "_completed_arguments": "<<ACTION_ARGUMENTS>>",
                        },
                    },
                    "user": {
                        "instructions": [],
                        "output_schema": {
                            "$schema": "http://json-schema.org/draft-07/schema#",
                            "type": "object",
                            "required": ["actions"],
                            "properties": {
                                "actions": {
                                    "type": "array",
                                    "minItems": 0,
                                    "maxItems": 1,
                                    "items": {
                                        "type": "object",
                                        "required": ["process", "reason", "arguments"],
                                        "properties": {
                                            "process": {
                                                "type": "object",
                                                "required": [
                                                    "provider_type",
                                                    "provider",
                                                    "function_name",
                                                ],
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
                        },
                        "session_id": "<<SESSION_ID>>",
                        "flow_input": "<<<:service_interface::flow_service::get_flow_input_for_presentation()>>>",
                    },
                    },
                },
            },
        }
    )
    @abstractmethod
    def process_results(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Format results for user presentation."""
        ...

    @service_interface_process(
        name="propose_name",
        provider="inference_service",
        processor_policy_category=ProcessorPolicyCategory.VERTEX,
        parameters={
            "params": ParameterMetadata(
                description="Parameters including intent_text, artifact_type, and optional input_filename",
                required=False,
                type=ParameterType.OBJECT,
            ),
            "state": ParameterMetadata(
                description="Current application state (automatically injected)",
                required=False,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Proposed name with confidence and flags",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Name proposal: display_name, extension, confidence, flags",
                    required=False,
                ),
            },
            usage_patterns=[
                "Generate descriptive filename for vague requests",
                "Propose names for generated audio, images, or other artifacts",
            ],
        ),
        is_inference_capable=True,
        work_count_impact=0,
        action_definition_template={
            "name": "propose_name",
            "description": "Propose a human-friendly name for a file or artifact",
            "process": {
                "provider_type": "service_interface",
                "provider": "inference_service",
                "function_name": "propose_name",
            },
            "arguments": {
                "model": {
                    "temperature": 0.3,
                    "max_tokens": 256,
                },
                "prompt": {
                    "user": {
                        "instructions": load_prompt("propose_name_instructions.md"),
                        "output_schema": {
                            "$schema": "http://json-schema.org/draft-07/schema#",
                            "type": "object",
                            "required": ["display_name", "extension", "confidence"],
                            "properties": {
                                "display_name": {
                                    "type": "string",
                                    "description": "Proposed base filename",
                                },
                                "extension": {
                                    "type": "string",
                                    "description": "File extension without dot",
                                },
                                "confidence": {
                                    "type": "string",
                                    "enum": ["high", "medium", "low"],
                                    "description": "Confidence in the proposed name",
                                },
                                "flags": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Flags for concerns about the name",
                                },
                            },
                        },
                        "intent_text": "<<INTENT_TEXT>>",
                        "artifact_type": "<<ARTIFACT_TYPE>>",
                        "input_filename": "<<INPUT_FILENAME>>",
                    },
                },
            },
        }
    )
    @abstractmethod
    def propose_name(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Propose a human-friendly name for a file or artifact."""
        ...
