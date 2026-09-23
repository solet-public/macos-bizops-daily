"""Knowledge-base JSON overlay loader + post-merge validation.

Extracted from `ProcessRegistryBuilder` during the Step 9.A decomposition
(`workbench/2026-05-25_plugin_god_class_remediation.md` §9.1).

Responsibility: for every registered process, locate its companion
knowledge-base JSON file, validate the file's `process_key` matches,
merge the JSON's prose and structured fields into the live registry
entry, regenerate per-entry derived docs, and finally enforce the
EDGE-customizations contract against the FULLY MERGED entries (so a
decorator may legitimately omit result/error customizations when the
companion JSON supplies them).

Customization text-field frozensets live here as module-level constants
— they are the canonical mapping of which customization fields override
the decorator vs. fill in additively.

Depends on:
  - `InvocationSchemaGenerator` for invocation-schema regeneration in
    `_regenerate_derived_docs`.
  - `plugin_manager` for resolving plugin source paths to companion JSON files.
"""

from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path

from ananta.core.domain.enums import ErrorSeverity
from ananta.core.plugins.plugin_manager import PluginManager
from ananta.core.process_registry.invocation_schema_generator import (
    InvocationSchemaGenerator,
)
from ananta.error_handling import FrameworkError

logger = logging.getLogger(__name__)


# Knowledge base text fields for customization merging.
# These override the decorator-defined values when the JSON supplies them.
# Any customization field NOT in these sets is merged additively
# (decorator wins if set; JSON fills missing).
_RESULT_CUSTOMIZATION_TEXT_FIELDS = frozenset({
    "action_label", "result_type", "result_description",
    "presentation_guidance", "output_action_guidance",
})
_ERROR_CUSTOMIZATION_TEXT_FIELDS = frozenset({
    "action_context", "error_interpretation", "recovery_guidance",
})


def apply_deprecation(entry: dict[str, object], json_data: dict[str, object]) -> None:
    """Honor an optional ``deprecation`` block from a process JSON (Phase 6 §4.2).

    A deprecated process stays REGISTERED and CALLABLE — so joseki / WBS / plans
    that still name it do not dangle — while ``active_retrieval: false`` DERIVES
    ``is_discoverable = false`` on the entry, which the discovery service already
    honors by skipping vector storage (excluded from ``process_search``). Making
    "deprecate" one gesture (set the block; retrieval follows) keeps the operator
    to a single knob. The block itself is stored on the entry, so a consumer that
    reads the full entry (e.g. ``discovery_service.get_process_by_key``) sees the
    pre-removal tombstone (replacement key, superseded date, migration note).
    NOTE: the agent-facing ``get_process_schema`` verb currently projects a fixed
    field set and does NOT include ``deprecation`` — surfacing it there is a
    small, out-of-Tier-2-lane follow-on in ``discovery_service``.

    Shape (all keys optional except that the block, when present, is an object)::

        "deprecation": {
            "replacement_key": "service_interface::…::new_verb" | null,
            "superseded_date": "2026-07-02",
            "migration_note": "how to migrate",
            "active_retrieval": false   // default true; false drops it from search
        }

    Fails loud (``FrameworkError``) on a malformed block — a typo that silently
    failed to demote would be worse than a build error, and the registry build
    is fail-fast by design.
    """
    if "deprecation" not in json_data:
        return
    deprecation = json_data["deprecation"]
    if not isinstance(deprecation, dict):
        raise FrameworkError(
            message=(
                "'deprecation' must be an object with keys replacement_key / "
                "superseded_date / migration_note / active_retrieval"
            ),
            error_code="process_registry.malformed_deprecation",
            severity=ErrorSeverity.CRITICAL,
        )
    active_retrieval = deprecation.get("active_retrieval", True)
    if not isinstance(active_retrieval, bool):
        raise FrameworkError(
            message="'deprecation.active_retrieval' must be a boolean",
            error_code="process_registry.malformed_deprecation",
            severity=ErrorSeverity.CRITICAL,
        )
    entry["deprecation"] = deprecation
    if not active_retrieval:
        entry["is_discoverable"] = False


class KnowledgeBaseOverlayLoader:
    """Load per-process knowledge-base JSON overlays and validate post-merge state."""

    def __init__(
        self,
        plugin_manager: PluginManager,
        schema_generator: InvocationSchemaGenerator,
    ) -> None:
        self._plugin_manager = plugin_manager
        self._schema_generator = schema_generator

    def apply(self, registry: dict[str, object]) -> None:
        """Load knowledge-base JSON files and merge into registry.

        Public entry point. Runs the JSON-file load + merge pass. EDGE
        processor customizations are OPTIONAL post-merge (the former
        both-blocks-required FATAL was relaxed 2026-07-15, frontier-first
        consolidation — see
        workbench/2026-07-15_frontier_first_result_processing_consolidation.md).
        """
        self._load_knowledge_base_process_definitions(registry)

    def _load_knowledge_base_process_definitions(self, registry: dict[str, object]) -> None:
        """Load process definitions from knowledge base JSON files and merge text fields.

        For each registered process, resolves the corresponding JSON file, validates
        the process_key, and merges all prompt-facing text fields into the live
        registry entry. Regenerates derived docs after merge.

        Hard-fails if any registered process is missing its JSON file.
        """
        processes = registry["processes"]
        if not isinstance(processes, dict):
            return

        ananta_kb_processes_dir = (
            Path(__file__).parent.parent.parent.parent.parent / "knowledge_base" / "processes"
        )

        errors: list[str] = []
        merged_count = 0

        for process_key, entry in processes.items():
            if not isinstance(entry, dict):
                continue

            json_path = self._resolve_process_json_path(
                process_key, entry, ananta_kb_processes_dir,
            )

            if json_path is None:
                errors.append(f"{process_key}: could not resolve JSON path")
                continue

            if not json_path.exists():
                errors.append(f"{process_key}: JSON file not found at {json_path}")
                continue

            try:
                json_data = json.loads(json_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                errors.append(f"{process_key}: failed to read JSON: {e}")
                continue

            file_pk = json_data.get("process_key", "")
            if file_pk != process_key:
                errors.append(
                    f"{process_key}: process_key mismatch in {json_path.name} "
                    f"(got '{file_pk}')"
                )
                continue

            self._merge_process_json(entry, json_data)
            self._regenerate_derived_docs(process_key, entry)
            merged_count += 1

        if errors:
            for err in errors:
                logger.error(f"Knowledge base process definition error: {err}")
            raise FrameworkError(
                message=f"Failed to load {len(errors)} knowledge base process definitions",
                error_code="process_registry.knowledge_base_load_failed",
                details={"errors": errors, "merged_count": merged_count},
                severity=ErrorSeverity.CRITICAL,
            )

        logger.info(
            f"Loaded knowledge base definitions for {merged_count} processes"
        )

    def _resolve_process_json_path(
        self,
        process_key: str,
        entry: dict[str, object],
        ananta_kb_dir: Path,
    ) -> Path | None:
        """Resolve the knowledge base JSON file path for a process.

        Plugin processes: <plugin_dir>/knowledge_base/processes/<function_name>.json
        Service interfaces: ananta/knowledge_base/processes/<provider>/<function_name>.json
        """
        provider_type = str(entry.get("provider_type", ""))
        provider = str(entry.get("provider", ""))
        function_name = str(entry.get("function_name", ""))

        if provider_type == "service_interface":
            return ananta_kb_dir / provider / f"{function_name}.json"

        if provider_type == "plugin":
            plugin_instance = self._plugin_manager.plugins.get(provider)
            if plugin_instance is None:
                logger.warning(
                    f"Plugin '{provider}' not found for process '{process_key}'"
                )
                return None
            plugin_source = Path(inspect.getfile(type(plugin_instance)))
            # plugin_source: plugins/<name>/src/<name>/plugin.py
            # knowledge_base: plugins/<name>/knowledge_base/processes/
            plugin_root = plugin_source.parent.parent.parent
            return plugin_root / "knowledge_base" / "processes" / f"{function_name}.json"

        logger.warning(
            f"Unknown provider_type '{provider_type}' for process '{process_key}'"
        )
        return None

    def _merge_process_json(
        self, entry: dict[str, object], json_data: dict[str, object],
    ) -> None:
        """Merge knowledge base JSON fields into a registry entry.

        Merges core text fields, top-level prose fields
        (parameters / return_value_schema / complete_examples /
        error_cases / output_description), action_definition_template
        arguments, and result/error processor customization fields.

        JSON is the canonical source for prose; decorator values are
        overridden when JSON provides them. Structured customization fields
        (e.g. blob_fields, retryable) are filled in from JSON only when the
        decorator omits them — that additive merge lives in
        `_merge_customization_fields` and is invoked below. Customizations
        are OPTIONAL on EDGE processes (2026-07-15 relax).
        """
        self._merge_top_level_fields(entry, json_data)
        self._merge_action_definition_template(entry, json_data)
        self._merge_customization_fields(
            entry, json_data,
            "result_processor_customizations", _RESULT_CUSTOMIZATION_TEXT_FIELDS,
        )
        self._merge_customization_fields(
            entry, json_data,
            "error_processor_customizations", _ERROR_CUSTOMIZATION_TEXT_FIELDS,
        )
        apply_deprecation(entry, json_data)

    def _merge_top_level_fields(
        self, entry: dict[str, object], json_data: dict[str, object],
    ) -> None:
        """Merge all top-level JSON keys that override decorator values.

        Covers the original core text fields plus the prose fields lifted
        out of @platform_process decorators by the metadata migration
        (parameters / return_value_schema / complete_examples /
        error_cases / output_description), the prompt_contract override,
        and the processor_policy_category override. Each is "JSON wins
        when present, decorator otherwise."
        """
        top_level_fields = (
            "display_name",
            "description",
            "embedding_description",
            "parameters",
            "return_value_schema",
            "complete_examples",
            "error_cases",
            "output_description",
            "prompt_contract",
            "processor_policy_category",
        )
        for field in top_level_fields:
            if field in json_data:
                entry[field] = json_data[field]
        # Keep the legacy `parameter_schema` alias in lockstep with `parameters`
        # so consumers reading either path see the merged values.
        if "parameters" in json_data:
            entry["parameter_schema"] = json_data["parameters"]

    def _merge_action_definition_template(
        self, entry: dict[str, object], json_data: dict[str, object],
    ) -> None:
        """Apply the action_definition_template_arguments JSON override.

        Knowledge-base JSON is authoritative: when present, it replaces
        the decorator-defined template["arguments"] wholesale. This is
        how runtime overrides such as prompt/model defaults are applied.
        """
        if "action_definition_template_arguments" not in json_data:
            return
        template = entry.get("action_definition_template")
        if not isinstance(template, dict):
            return
        template["arguments"] = json_data["action_definition_template_arguments"]

    def _merge_customization_fields(
        self,
        entry: dict[str, object],
        json_data: dict[str, object],
        entry_key: str,
        text_fields: frozenset[str],
    ) -> None:
        json_custs = json_data.get(entry_key)
        if isinstance(json_custs, dict):
            existing = entry.get(entry_key)
            if isinstance(existing, dict):
                # Merge text fields (override decorator defaults with JSON values)
                for field in text_fields:
                    if field in json_custs:
                        existing[field] = json_custs[field]
                # Merge structured fields not in text_fields (e.g. message_rendering)
                # JSON is authoritative for fields the decorator doesn't define.
                for field, value in json_custs.items():
                    if field not in text_fields and field not in existing:
                        existing[field] = value
            else:
                entry[entry_key] = json_custs

    def _regenerate_derived_docs(
        self, process_key: str, entry: dict[str, object],
    ) -> None:
        """Regenerate derived documentation fields after knowledge base merge.

        Updates planning_docs, error_handling_docs, response_handling_docs
        with merged display_name/description, and regenerates invocation_schema.
        """
        display_name = str(entry.get("display_name", ""))
        description = str(entry.get("description", ""))
        parameters_dict = entry.get("parameters")

        # Update planning_docs
        planning_docs = entry.get("planning_docs")
        if isinstance(planning_docs, dict):
            planning_docs["summary"] = display_name
            planning_docs["description"] = description

        # Update error_handling_docs
        error_docs = entry.get("error_handling_docs")
        if isinstance(error_docs, dict):
            error_docs["summary"] = display_name

        # Update response_handling_docs
        response_docs = entry.get("response_handling_docs")
        if isinstance(response_docs, dict):
            response_docs["summary"] = display_name

        # Regenerate invocation_schema with merged parameters
        if isinstance(parameters_dict, dict):
            entry["invocation_schema"] = self._schema_generator.generate(
                process_key=process_key,
                parameters_dict=parameters_dict,
            )
