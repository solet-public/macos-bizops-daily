from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ParameterFormat(Enum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"


@dataclass
class ParameterConstraints:
    name: str
    type: str
    required: bool = False
    description: str = ""
    default: object = None
    format: ParameterFormat | None = None
    constraints: Optional["ParameterConstraints"] = None
    examples: list[object] = field(default_factory=list)
    ai_hints: list[str] = field(default_factory=list)  # Specific guidance for AI

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "description": self.description,
        }

        if self.default is not None:
            result["default"] = self.default

        if self.format:
            result["format"] = self.format.value

        if self.constraints:
            result["constraints"] = self.constraints.to_dict()

        if self.examples:
            result["examples"] = self.examples

        if self.ai_hints:
            result["ai_hints"] = self.ai_hints

        return result


@dataclass
class ProcessUsageExample:
    required_plugins: list[str] = field(default_factory=list)
    required_services: list[str] = field(default_factory=list)
    required_permissions: list[str] = field(default_factory=list)
    required_config: list[str] = field(default_factory=list)
    environment_requirements: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "required_plugins": self.required_plugins,
            "required_services": self.required_services,
            "required_permissions": self.required_permissions,
            "required_config": self.required_config,
            "environment_requirements": self.environment_requirements,
        }


@dataclass
class ProcessError:
    when_to_use: list[str] = field(default_factory=list)
    when_not_to_use: list[str] = field(default_factory=list)
    parameter_selection_tips: list[str] = field(default_factory=list)
    output_interpretation: list[str] = field(default_factory=list)
    common_patterns: list[str] = field(default_factory=list)
    troubleshooting_tips: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "when_to_use": self.when_to_use,
            "when_not_to_use": self.when_not_to_use,
            "parameter_selection_tips": self.parameter_selection_tips,
            "output_interpretation": self.output_interpretation,
            "common_patterns": self.common_patterns,
            "troubleshooting_tips": self.troubleshooting_tips,
        }


@dataclass
class SelfDescribingProcessMetadata:
    process_key: str
    name: str
    display_name: str
    description: str
    version: str
    provider_type: str
    provider: str
    function_name: str
    parameters: list[ParameterConstraints] = field(default_factory=list)
    output_schema: dict[str, object] | None = None
    examples: list[ProcessUsageExample] = field(default_factory=list)
    ai_guidance: ProcessError | None = None
    dependencies: ProcessUsageExample | None = None
    possible_errors: list[ProcessError] = field(default_factory=list)
    is_async: bool = False
    estimated_duration: str | None = None
    rate_limits: dict[str, object] | None = None
    is_inference_capable: bool | None = None
    created_at: str | None = None
    last_updated: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "process_key": self.process_key,
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "version": self.version,
            "provider_type": self.provider_type,
            "provider": self.provider,
            "function_name": self.function_name,
            "parameters": {param.name: param.to_dict() for param in self.parameters},
            "output_schema": self.output_schema,
            "examples": [example.to_dict() for example in self.examples],
            "ai_guidance": self.ai_guidance.to_dict() if self.ai_guidance else None,
            "dependencies": self.dependencies.to_dict() if self.dependencies else None,
            "possible_errors": [error.to_dict() for error in self.possible_errors],
            "is_async": self.is_async,
            "estimated_duration": self.estimated_duration,
            "rate_limits": self.rate_limits,
            "is_inference_capable": self.is_inference_capable,
            "created_at": self.created_at,
            "last_updated": self.last_updated,
        }


def create_enhanced_process_metadata(
    process_key: str,
    name: str,
    display_name: str,
    description: str,
    provider_type: str,
    provider: str,
    function_name: str,
    **kwargs: object,
) -> SelfDescribingProcessMetadata:
    return SelfDescribingProcessMetadata(
        process_key=process_key,
        name=name,
        display_name=display_name,
        description=description,
        provider_type=provider_type,
        provider=provider,
        function_name=function_name,
        **kwargs,  # type: ignore[arg-type]
    )


class ProcessRegistryIntrospector:
    def __init__(self, registry: dict[str, object]):
        self.registry = registry

    def get_process_documentation(self, process_key: str) -> dict[str, object] | None:
        processes = self.registry.get("processes", {})
        if not isinstance(processes, dict):
            return None
        process = processes.get(process_key)
        if not process:
            return None
        if not isinstance(process, dict):
            return None

        return {
            "process_info": process,
            "usage_guide": self._generate_usage_guide(process),
            "parameter_reference": self._generate_parameter_reference(process),
            "examples": process.get("examples", []),
            "ai_guidance": process.get("ai_guidance", {}),
            "troubleshooting": self._generate_troubleshooting_guide(process),
        }

    def _generate_usage_guide(self, process: dict[str, object]) -> dict[str, object]:
        param_ref: dict[str, object] = {}
        parameters = process.get("parameters", {})
        if isinstance(parameters, dict):
            for name, param in parameters.items():
                if isinstance(param, dict):
                    param_ref[name] = {
                        "type": param.get("type"),
                        "required": param.get("required", False),
                        "description": param.get("description", ""),
                        "examples": param.get("examples", []),
                        "constraints": param.get("constraints", {}),
                        "ai_hints": param.get("ai_hints", []),
                    }
        return param_ref

    def _generate_parameter_reference(
        self, _process: dict[str, object]
    ) -> dict[str, object]:  # Stub implementation
        return {}

    def _generate_troubleshooting_guide(
        self, _process: dict[str, object]
    ) -> dict[str, object]:  # Stub implementation
        return {}
