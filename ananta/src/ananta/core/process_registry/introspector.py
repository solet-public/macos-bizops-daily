import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TypedDict

logger = logging.getLogger(__name__)


class ProcessDiscoveryMode(Enum):
    pass


class ProcessDict(TypedDict, total=False):
    name: str
    description: str
    embedding_description: str
    is_discoverable: bool
    provider: str
    examples: list["ExampleDict"]
    parameters: dict[str, "ParameterDict"]
    ai_guidance: "AIGuidanceDict"


class ExampleDict(TypedDict, total=False):
    scenario: str
    parameters: dict[str, object]
    expected_output: str


class ParameterDict(TypedDict, total=False):
    type: str
    required: bool
    description: str
    examples: list[object]
    ai_hints: list[str]
    format_hint: str
    constraints: dict[str, object]
    default: object


class AIGuidanceDict(TypedDict, total=False):
    common_patterns: list[str]


@dataclass
class ProcessDiscoveryResult:
    query: str
    mode: ProcessDiscoveryMode
    matches: list[dict[str, object]]
    total_count: int
    suggestions: list[str]


class ProcessRegistryIntrospector:
    def __init__(self, process_registry: dict[str, object]):
        self.registry = process_registry
        processes_raw = self.registry.get("processes", {})
        if isinstance(processes_raw, dict):
            self.processes: dict[str, ProcessDict] = processes_raw
        else:
            self.processes = {}
        self._build_search_indexes()

    def _build_search_indexes(self) -> None:
        self.provider_index: dict[str, list[str]] = {}
        self.text_index: dict[str, str] = {}

        for process_key, process in self.processes.items():
            self._index_process(process_key, process)

    def _index_process(self, process_key: str, process: ProcessDict) -> None:
        """Index a single process into all search indexes."""
        self._index_provider(process_key, process)
        self._index_text(process_key, process)

    def _index_provider(self, process_key: str, process: ProcessDict) -> None:
        """Index process by provider."""
        provider = process.get("provider", "")
        self._add_to_index(self.provider_index, provider, process_key)

    def _index_text(self, process_key: str, process: ProcessDict) -> None:
        """Index process by searchable text."""
        searchable_text = self._build_searchable_text(process)
        self.text_index[process_key] = searchable_text.lower()

    def _add_to_index(self, index: dict[str, list[str]], key: str, process_key: str) -> None:
        """Add a process key to an index under the given key."""
        if key not in index:
            index[key] = []
        index[key].append(process_key)

    def _build_searchable_text(self, process: ProcessDict) -> str:
        """Build searchable text for text-based discovery.

        Uses embedding_description if available (keyword-dense for search),
        falls back to description during migration.
        """
        text_parts: list[object] = []
        text_parts.append(process.get("name", ""))

        # Prefer embedding_description for search (keyword-dense)
        if embedding_desc := process.get("embedding_description"):
            text_parts.append(embedding_desc)
        else:
            text_parts.append(process.get("description", ""))

        text_parts.append(process.get("provider", ""))
        return " ".join(str(part) for part in text_parts if part)

    def _discover_by_provider(self, provider: str) -> list[str]:
        return self.provider_index.get(provider, [])

    def _suggest_search_terms(self, query: str) -> list[str]:
        terms = []
        query_lower = query.lower()
        for _process_key, text in self.text_index.items():
            if query_lower in text:
                terms.extend(text.split())
        return list(set(terms))[:10]

    def _generate_usage_guide(self, process: ProcessDict) -> dict[str, object]:
        example_guide = self._extract_example_usage_guide(process)
        if example_guide:
            return example_guide

        minimal_params = self._build_minimal_params(process)
        return {
            "scenario": "Minimal usage example",
            "parameters": minimal_params,
            "expected_result": "Check process output for results",
        }

    def _extract_example_usage_guide(self, process: ProcessDict) -> dict[str, object] | None:
        """Extract usage guide from first example if available."""
        examples = process.get("examples", [])
        if not examples:
            return None

        first_example = examples[0]

        scenario = first_example.get("scenario", "Basic usage")
        parameters = first_example.get("parameters", {})
        expected_output = first_example.get("expected_output", "")
        return {
            "scenario": scenario,
            "parameters": parameters,
            "expected_result": expected_output,
        }

    def _build_minimal_params(self, process: ProcessDict) -> dict[str, object]:
        """Build minimal parameters from required process parameters."""
        minimal_params: dict[str, object] = {}
        process_parameters: object = process.get("parameters", {})
        if not isinstance(process_parameters, Mapping):
            return minimal_params

        for name, param_value in process_parameters.items():
            param_placeholder = self._get_required_param_placeholder(param_value)
            if param_placeholder is not None:
                minimal_params[name] = param_placeholder

        return minimal_params

    def _get_required_param_placeholder(self, param_value: object) -> object | None:
        """Get placeholder value for a required parameter, or None if not required."""
        if not isinstance(param_value, Mapping):
            return None

        required = param_value.get("required", False)
        if not isinstance(required, bool) or not required:
            return None

        param_examples = param_value.get("examples", [])
        if isinstance(param_examples, list) and param_examples:
            first_example: object = param_examples[0]
            return first_example

        param_type = param_value.get("type", "value")
        return f"<{param_type if isinstance(param_type, str) else 'value'}>"

    def _generate_parameter_reference(self, process: ProcessDict) -> dict[str, object]:
        param_ref: dict[str, object] = {}

        process_parameters: object = process.get("parameters", {})
        if isinstance(process_parameters, Mapping):
            for name, param_value in process_parameters.items():
                if isinstance(param_value, Mapping):
                    param_ref[name] = {
                        "type": param_value.get("type"),
                        "required": param_value.get("required", False),
                        "description": param_value.get("description", ""),
                        "examples": param_value.get("examples", []),
                        "ai_hints": param_value.get("ai_hints", []),
                        "format_hint": param_value.get("format_hint"),
                        "constraints": param_value.get("constraints", {}),
                        "default": param_value.get("default"),
                    }

        return param_ref

    def _discover_by_text_search(self, query: str) -> list[str]:
        query_lower = query.lower()
        matches = []

        for process_key, searchable_text in self.text_index.items():
            if query_lower in searchable_text:
                matches.append(process_key)

        return matches

    def get_capability_overview(self) -> dict[str, object]:
        """Get overview of process registry capabilities.

        Returns counts by provider. Category and capability fields have been removed
        as part of the process decorator refactor (dead infrastructure cleanup).
        """
        by_provider: dict[str, int] = {}

        # Count processes by provider
        for provider, process_keys in self.provider_index.items():
            by_provider[provider] = len(process_keys)

        overview: dict[str, object] = {
            "total_processes": len(self.processes),
            "by_provider": by_provider,
            "available_providers": list(self.provider_index.keys()),
        }

        return overview
