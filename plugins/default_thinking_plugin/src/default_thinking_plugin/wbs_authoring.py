"""Work Breakdown Structure storage and deterministic generation.

The qwen push-authoring path (outline / detail / joseki / full-WBS
generation via the thinking model) was retired per DEP-01 — WBS
documents are authored by the calling agent and registered through
``store_authored_wbs`` (Phase 3 Seam A), or generated deterministically
from a Pipeline Spec via ``generate_section_stem_wbs``.
"""

from __future__ import annotations

import logging
from typing import Any

from ananta.error_handling import FrameworkError

from default_thinking_plugin.artifact_authoring import (
    FocusManager,
    KnowledgeWriter,
    StateStore,
)
from default_thinking_plugin.artifact_helpers import (
    build_section_index,
    parse_wbs_substep_bindings,
    validate_arguments_labels,
    validate_section_sizes,
)
from default_thinking_plugin.constants import PROVENANCE_AUTHORED_BY_VALUE, ErrorCode
from default_thinking_plugin.pipeline_resolver import (
    ProcessIOMap,
    collect_all_schema_process_keys,
    generate_wbs,
    resolve_pipeline,
    validate_pipeline_spec_raw_and_loaded,
)
from default_thinking_plugin.pipeline_spec import pipeline_spec_from_dict
from default_thinking_plugin.wbs_authoring_helpers import (
    ProcessSchemaLookup,
    _normalize_wbs_id_line,
    _require_non_empty,
)

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────

_NAMESPACE = "default_thinking_plugin"


class WbsAuthoringService:
    """WBS storage and deterministic generation (no model invocation)."""

    def __init__(
        self,
        knowledge_writer: KnowledgeWriter,
        state_store: StateStore,
        focus_manager: FocusManager,
        *,
        namespace: str = _NAMESPACE,
        process_schema_lookup: ProcessSchemaLookup | None = None,
    ) -> None:
        self._knowledge_writer = knowledge_writer
        self._state_store = state_store
        self._focus_manager = focus_manager
        self._namespace = namespace
        self._process_schema_lookup = process_schema_lookup

    # ── Public API ──────────────────────────────────────────────────

    def generate_section_stem_wbs(
        self,
        *,
        wbs_id: str,
        manifest_id: str,
        phase_number: int,
        phase_name: str,
        style_family: str,
        artifact_prefix: str,
        pipeline_spec: dict[str, Any],
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Deterministically generate a per-section pipeline WBS.

        Reads ``pipeline_spec`` (already-decoded dict from the model)
        and ``schema`` (already-loaded pipeline schema for the style
        family), resolves layers, and emits a complete WBS markdown
        document. No thinking model call.
        """
        _require_non_empty(wbs_id, "wbs_id")
        _require_non_empty(manifest_id, "manifest_id")
        _require_non_empty(style_family, "style_family")
        _require_non_empty(artifact_prefix, "artifact_prefix")
        if not pipeline_spec:
            raise FrameworkError(
                message="pipeline_spec is required",
                error_code=ErrorCode.PARAMETER_ERROR,
            )

        validate_pipeline_spec_raw_and_loaded(pipeline_spec, schema)
        spec = pipeline_spec_from_dict(pipeline_spec)
        if not spec.schema_id:
            spec.schema_id = str(schema.get("schema_id", ""))

        process_io_map = self._build_process_io_map(schema)
        resolved = resolve_pipeline(spec, schema)
        content = generate_wbs(
            resolved,
            schema,
            wbs_id=wbs_id,
            manifest_id=manifest_id,
            phase_number=phase_number,
            phase_name=phase_name,
            artifact_prefix=artifact_prefix,
            process_io_map=process_io_map,
        )

        kb_path = f"wbs/{wbs_id}.md"
        artifact_memory_id = self._store_wbs(
            wbs_id=wbs_id,
            manifest_id=manifest_id,
            phase_number=phase_number,
            phase_name=phase_name,
            content=content,
            kb_path=kb_path,
        )
        logger.info(
            "Section stem WBS %s generated deterministically for "
            "manifest %s phase %d (style %s)",
            wbs_id, manifest_id, phase_number, style_family,
        )
        return {
            "wbs_id": wbs_id,
            "manifest_id": manifest_id,
            "phase_number": phase_number,
            "phase_name": phase_name,
            "status": "created",
            "content": content,
            "knowledge_base_path": kb_path,
            "source_memory_id": artifact_memory_id,
        }

    def _build_process_io_map(self, schema: dict[str, Any]) -> ProcessIOMap:
        """Build process I/O field map from discovery service and schema.

        For each process key in the schema, asks the discovery service
        for the process's argument schema and derives:
        - input_key: "input_midi_file", "input_audio_file", or None
        - output_key: "output_midi_file" or "output_audio_file"
        - emit_format_arg: whether to emit output_audio_format: wav

        Returns an empty map (audio I/O fallback) if no lookup available.
        """
        if self._process_schema_lookup is None:
            return {}
        result: ProcessIOMap = {}
        for process_key in collect_all_schema_process_keys(schema):
            arg_props = self._process_schema_lookup.get_arg_properties(process_key)
            if not arg_props:
                continue
            if "input_midi_file" in arg_props:
                in_key: str | None = "input_midi_file"
            elif "input_audio_file" in arg_props:
                in_key = "input_audio_file"
            else:
                in_key = None
            out_key = (
                "output_midi_file"
                if "output_midi_file" in arg_props
                else "output_audio_file"
            )
            emit_format = "output_audio_format" in arg_props
            result[process_key] = (in_key, out_key, emit_format)
        return result

    def read_kb_content(self, path: str) -> str:
        """Read content from the thinking_plans knowledge base.

        Raises FrameworkError if the content cannot be loaded.
        """
        content = self._read_kb_content(path)
        if not content:
            raise FrameworkError(
                message=f"Knowledge base content at {path!r} is empty or unreadable",
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        return content

    def store_authored_wbs(
        self,
        *,
        wbs_id: str,
        manifest_id: str,
        phase_number: int,
        phase_name: str,
        content: str,
    ) -> dict[str, Any]:
        """Persist an agent-authored-by-value WBS (Phase 3, Seam A).

        The caller validates the document BEFORE calling (the register verb
        hard-fails on any validation error). Storage rides the exact same
        path as thinking-model-authored documents (``_store_wbs``: KB write,
        ``thinking_wbs`` record, focus), marked with
        ``provenance='authored_by_value'``. Registration never overwrites:
        an existing ``wbs_id`` is a hard error — revising a registered WBS
        is ``patch_work_breakdown_structure``'s job.
        """
        existing = self._state_store.read_state(
            namespace=self._namespace,
            query={
                "table": "thinking_wbs",
                "filters": {"id": wbs_id, "is_deleted": 0},
                "limit": 1,
            },
        )
        if existing.get("data", {}).get("records"):
            raise FrameworkError(
                message=(
                    f"WBS {wbs_id!r} is already registered — registration "
                    f"never overwrites; use patch_work_breakdown_structure "
                    f"to revise it"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )

        kb_path = f"wbs/{wbs_id}.md"
        artifact_memory_id = self._store_wbs(
            wbs_id=wbs_id,
            manifest_id=manifest_id,
            phase_number=phase_number,
            phase_name=phase_name,
            content=content,
            kb_path=kb_path,
            provenance=PROVENANCE_AUTHORED_BY_VALUE,
        )
        logger.info(
            "Authored-by-value WBS %s registered for manifest %s phase %d",
            wbs_id, manifest_id, phase_number,
        )
        return {
            "wbs_id": wbs_id,
            "status": "registered",
            "knowledge_base_path": kb_path,
            "source_memory_id": artifact_memory_id,
        }

    # ── Storage helpers ─────────────────────────────────────────────

    def _validate_wbs_arguments(self, content: str, wbs_id: str) -> None:
        """Validate every Arguments block in the WBS against invocation schemas.

        Raises ``FrameworkError`` listing every required-argument violation.
        Skips silently when ``process_schema_lookup`` is unavailable.
        """
        if self._process_schema_lookup is None:
            return
        # ◆F1: per-sub-step Composed correlation. A
        # required argument is satisfied when it is in the sub-step's own
        # ``Arguments:`` JSON OR bound by a ``Composed:`` reference ON THAT
        # sub-step. This supersedes Rev-B's B1-fix document-wide composed-target
        # union, under which sub-step Y's ``Composed: <arg>`` masked a
        # genuinely-missing required ``<arg>`` on sub-step X (deferring the
        # failure to plugin execution-time). Core ``parse()`` is still
        # deliberately avoided here — it would newly enforce RPK-presence on
        # RPK-omitting joseki-authoring documents. Under-extraction of a
        # non-canonical Composed line only re-fires the pre-B1 false-positive
        # (the safe direction), never masks.
        errors: list[str] = []
        for process_key, args, composed_targets in parse_wbs_substep_bindings(content):
            arg_props = self._process_schema_lookup.get_arg_properties(process_key)
            if not arg_props:
                continue
            satisfied = set(args) | composed_targets
            for arg_name, prop in arg_props.items():
                required = bool(prop.get("required", False))
                if required and arg_name not in satisfied:
                    errors.append(
                        f"{process_key}: required argument {arg_name!r} is missing",
                    )
        if errors:
            bullet_list = "\n".join(f"  - {e}" for e in errors)
            raise FrameworkError(
                message=(
                    f"WBS {wbs_id!r} failed argument validation "
                    f"({len(errors)} error(s)):\n{bullet_list}"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )

    def _store_wbs(
        self,
        *,
        wbs_id: str,
        manifest_id: str,
        phase_number: int,
        phase_name: str,
        content: str,
        kb_path: str,
        joseki_key: str | None = None,
        work_item_id: str | None = None,
        provenance: str | None = None,
    ) -> str:
        """Write WBS to KB, state, and focused memory."""
        content = _normalize_wbs_id_line(content, wbs_id)
        validate_section_sizes(content, wbs_id)
        validate_arguments_labels(content, wbs_id)
        self._validate_wbs_arguments(content, wbs_id)
        index = build_section_index(content, wbs_id, "wbs")
        logger.info(
            "SECTION_INDEX: %s sections=%d max_chars=%d",
            wbs_id, len(index),
            max((s["char_count"] for s in index), default=0),
        )
        self._knowledge_writer.write(kb_path, content)

        db_record: dict[str, Any] = {
            "id": wbs_id,
            "manifest_id": manifest_id,
            "phase_number": phase_number,
            "phase_name": phase_name,
            "status": "drafted",
            "knowledge_base_path": kb_path,
        }
        if joseki_key is not None:
            db_record["joseki_key"] = joseki_key
        if work_item_id is not None:
            db_record["work_item_id"] = work_item_id
        if provenance is not None:
            db_record["provenance"] = provenance

        self._state_store.write_state(
            namespace=self._namespace,
            data={"table": "thinking_wbs", "record": db_record},
        )

        self._focus_manager.defocus_by_label("work_breakdown_structure")
        return self._focus_manager.upsert(
            content,
            doc_tag=f"work_breakdown_structure:{wbs_id}",
            label="work_breakdown_structure",
        )

    def _read_kb_content(self, path: str) -> str:
        """Read content from the thinking_plans knowledge base."""
        try:
            return self._knowledge_writer.read(path)
        except Exception:
            logger.warning("Failed to read KB path: %s", path)
            return ""
