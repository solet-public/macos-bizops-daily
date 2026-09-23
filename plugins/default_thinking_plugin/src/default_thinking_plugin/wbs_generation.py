# pyright: reportUnusedFunction=false
"""WBS markdown generator extracted from ``pipeline_resolver``.

``generate_wbs`` walks the resolved ``PipelineSpec`` + pipeline schema
and emits the executable Work Breakdown Structure markdown document
the platform consumes for Phase 2 execution.  The bulk of the work
lives in the internal ``_WBSBuilder`` class.

The resolver-side helpers this module consumes (``_resolve_param_dict``,
``_build_arc_lookup``, ``_evaluate_condition``, ``_apply_audibility_caps``,
``_process_short_name``, ``_generation_filename``,
``_section_stem_filename``) remain in ``pipeline_resolver`` so this
module only ever pulls from there — never the other direction.
"""

from __future__ import annotations

import json
from typing import Any

from ananta.core.result_processing import ResultProcessorKind
from ananta.error_handling import FrameworkError

from .constants import ErrorCode
from .pipeline_resolver import (
    _apply_audibility_caps,
    _build_arc_lookup,
    _evaluate_condition,
    _generation_filename,
    _per_layer_finishing_active,
    _process_short_name,
    _resolve_param_dict,
    _section_stem_filename,
)
from .pipeline_spec import (
    LayerConfig,
    ParameterGroup,
    PipelineSpec,
    ResolvedScheduledLayer,
    ScheduledWindow,
    Segment,
    SegmentLayer,
)

ProcessIOMap = dict[str, tuple[str | None, str, bool]]


def generate_wbs(
    spec: PipelineSpec,
    schema: dict[str, Any],
    *,
    wbs_id: str,
    manifest_id: str,
    phase_number: int,
    phase_name: str,
    artifact_prefix: str,
    process_io_map: ProcessIOMap | None = None,
) -> str:
    """Generate a complete WBS markdown string from a resolved spec."""
    builder = _WBSBuilder(
        spec=spec,
        schema=schema,
        wbs_id=wbs_id,
        manifest_id=manifest_id,
        phase_number=phase_number,
        phase_name=phase_name,
        artifact_prefix=artifact_prefix,
        process_io_map=process_io_map or {},
    )
    return builder.build()


class _WBSBuilder:
    """Internal builder that assembles a WBS markdown document."""

    def __init__(
        self,
        *,
        spec: PipelineSpec,
        schema: dict[str, Any],
        wbs_id: str,
        manifest_id: str,
        phase_number: int,
        phase_name: str,
        artifact_prefix: str,
        process_io_map: ProcessIOMap | None = None,
    ) -> None:
        self.spec = spec
        self.schema = schema
        self.wbs_id = wbs_id
        self.manifest_id = manifest_id
        self.phase_number = phase_number
        self.phase_name = phase_name
        self.artifact_prefix = artifact_prefix
        self.process_io_map: ProcessIOMap = process_io_map or {}
        self.lines: list[str] = []
        self.step = 1
        # Lookup tables for per-window source resolution at emission time.
        self._pg_by_label: dict[str, ParameterGroup] = {
            pg.label: pg for pg in spec.parameter_groups
        }
        self._layer_config_by_type: dict[str, LayerConfig] = {
            lc.layer_type: lc for lc in spec.layer_configs
        }
        self._section_arcs_by_name: dict[str, dict[str, Any]] = (
            _build_arc_lookup(spec.arcs)
        )

    def build(self) -> str:
        self._emit_header()
        for section_index, section in enumerate(self.spec.sections, start=1):
            self._emit_section(section_index, section)
        self._emit_phase_completion()
        return "\n".join(self.lines).rstrip() + "\n"

    # ── header / phase completion ──

    def _emit_header(self) -> None:
        self.lines.extend([
            "# Work Breakdown Structure",
            "",
            f"WBS ID: {self.wbs_id}",
            f"WORK_MANIFEST: {self.manifest_id}",
            "Status: ready",
            "",
            f"## Phase {self.phase_number}. {self.phase_name}",
            "",
        ])

    def _emit_phase_completion(self) -> None:
        self.lines.append(f"### Phase {self.phase_number} Completion")
        self.lines.append("")
        self._emit_simple_step(
            "#### Phase Completion Record",
            f"Record the completed Phase {self.phase_number} state",
            "service_interface::thinking_service::"
            "record_work_breakdown_structure_step_state",
            arguments={
                "wbs_id": self.wbs_id,
                "step_number": self.step,
                "status": "completed",
            },
        )
        self._emit_simple_step(
            "#### Phase Manifest Update",
            f"Record the completed Phase {self.phase_number} outcome in the Work Manifest",
            "service_interface::thinking_service::record_work_manifest_phase_state",
            arguments={
                "manifest_id": self.manifest_id,
                "phase_number": self.phase_number,
                "status": "completed",
                "outcome_summary": (
                    f"Phase {self.phase_number} ({self.phase_name}) complete; "
                    f"all section stems and the phase artifacts produced."
                ),
            },
        )

    # ── per-section ──

    def _emit_section(self, section_index: int, section: Segment) -> None:
        title = self._section_title(section_index, section)
        self.lines.append(title)
        self.lines.append("")
        layer_outputs: dict[str, str] = {}
        for layer in section.layers:
            self._emit_section_spanning_layer(section, layer, layer_outputs)
        for scheduled in section.scheduled_layers:
            self._emit_scheduled_layer(section, scheduled, layer_outputs)
        finishing_output = self._emit_finishing_chain(section, layer_outputs)
        self._emit_record_state(section, finishing_output)

    def _section_title(self, section_index: int, section: Segment) -> str:
        movement_name = section.properties.get("movement_name") or section.name
        return (
            f"### Work Item {self.phase_number}.{section_index}: "
            f"Build the {movement_name} section"
        )

    # ── layer emission ──

    def _emit_section_spanning_layer(
        self,
        section: Segment,
        layer: SegmentLayer,
        layer_outputs: dict[str, str],
    ) -> None:
        """Emit a section-spanning layer's full chain into the WBS."""
        if layer.source_type == "generate":
            current = self._emit_generation_step(section, layer)
        else:
            current = self._extract_reference_input(layer)
        layer_outputs[layer.layer_type] = current
        for process_key, params in layer.post_processing:
            current = self._emit_post_processing_step(
                section, layer.layer_type, current, process_key, params,
            )
        layer_outputs[layer.layer_type] = current

    # ── scheduled-layer emission (Phase B) ──

    def _emit_scheduled_layer(
        self,
        section: Segment,
        scheduled: ResolvedScheduledLayer,
        layer_outputs: dict[str, str],
    ) -> None:
        """Emit one source chain per window then join into a section stem.

        The join operation is read from the layer_def's
        ``scheduling_join`` field. Two joins are supported:

        - ``amix-with-offset`` (default): each window is delayed via
          ``ffmpeg_adelay`` to its ``t_start_s`` and all delayed
          files are mixed with ``ffmpeg_amix``. Used for
          ``event_schedule`` (motif fragments at scheduled times).
        - ``acrossfade``: consecutive window outputs are joined with
          ``ffmpeg_acrossfade`` at their boundary. Used for
          ``parameter_group_sequence`` (in-section harmonic motion).

        The final stem filename is published into ``layer_outputs``
        keyed by layer_type so the existing section-level mix picks
        it up uniformly with section-spanning layers.
        """
        if not scheduled.windows:
            return
        layer_def = self.schema.get("layer_types", {}).get(
            scheduled.layer_type,
        )
        if not isinstance(layer_def, dict):
            raise FrameworkError(
                message=(
                    f"scheduled layer {scheduled.layer_type!r} is "
                    f"not declared in schema.layer_types"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        join = str(layer_def.get("scheduling_join", "amix-with-offset"))
        chain_outputs: list[tuple[ScheduledWindow, str]] = []
        for window in scheduled.windows:
            chain_output = self._emit_window_chain(
                section, scheduled.layer_type, layer_def, window,
            )
            chain_outputs.append((window, chain_output))
        section_stem = self._join_window_outputs(
            section, scheduled.layer_type, join, chain_outputs,
        )
        layer_outputs[scheduled.layer_type] = section_stem

    def _join_window_outputs(
        self,
        section: Segment,
        layer_type: str,
        join: str,
        chain_outputs: list[tuple[ScheduledWindow, str]],
    ) -> str:
        if join == "amix-with-offset":
            return self._join_amix_with_offset(
                section, layer_type, chain_outputs,
            )
        if join == "acrossfade":
            return self._join_acrossfade(
                section, layer_type, chain_outputs,
            )
        raise FrameworkError(
            message=(
                f"layer {layer_type!r} declares unsupported "
                f"scheduling_join {join!r}; expected one of "
                f"'amix-with-offset', 'acrossfade'"
            ),
            error_code=ErrorCode.PARAMETER_ERROR,
        )

    def _join_amix_with_offset(
        self,
        section: Segment,
        layer_type: str,
        chain_outputs: list[tuple[ScheduledWindow, str]],
    ) -> str:
        delayed_files: list[str] = []
        for window, chain_output in chain_outputs:
            delayed = self._emit_window_adelay(
                layer_type, window, chain_output,
            )
            delayed_files.append(delayed)
        return self._emit_window_amix(
            section, layer_type, delayed_files,
        )

    def _join_acrossfade(
        self,
        section: Segment,
        layer_type: str,
        chain_outputs: list[tuple[ScheduledWindow, str]],
    ) -> str:
        process_key = "plugin::audio_processing_plugin::ffmpeg_acrossfade"
        if not chain_outputs:
            raise FrameworkError(
                message=(
                    f"layer {layer_type!r} acrossfade join called "
                    f"with no chain outputs"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        if len(chain_outputs) == 1:
            return chain_outputs[0][1]
        current = chain_outputs[0][1]
        for index, (window, next_output) in enumerate(chain_outputs[1:], start=1):
            crossfade_s = float(window.overrides.get("crossfade_s", 8.0))
            output_filename = f"{layer_type}_crossfade_{index}"
            params: dict[str, Any] = {
                "input_audio_files": [current, next_output],
                "output_audio_file": output_filename,
                "output_audio_format": "wav",
                "duration": crossfade_s,
            }
            title_phrase = (
                f"Crossfade the {section.name} {layer_type} segment "
                f"{index}"
            )
            header = (
                f"#### Crossfade {layer_type.title()} (segment {index})"
            )
            self._emit_full_step(
                header, title_phrase, process_key, params,
            )
            current = output_filename
        # Republish the final stem under the canonical name so the
        # finishing chain has a consistent reference (matches the
        # amix-with-offset branch).
        return current

    def _emit_window_chain(
        self,
        section: Segment,
        layer_type: str,
        layer_def: dict[str, Any],
        window: ScheduledWindow,
    ) -> str:
        """Emit primary process + post-processing for one window.

        Returns the filename of the last emitted step (the input the
        ``ffmpeg_adelay`` step then applies).
        """
        process_key = str(layer_def.get("process", ""))
        params = self._resolve_window_params(
            section, layer_type, layer_def.get("params", {}), window,
        )
        primary_filename = (
            f"{layer_type}_event_{window.index}_t{int(window.t_start_s)}"
        )
        full_params = dict(params)
        _, out_key, emit_format = self._get_io_keys(process_key)
        full_params[out_key] = primary_filename
        if emit_format:
            full_params.setdefault("output_audio_format", "wav")
        title_phrase = (
            f"Generate the {section.name} {layer_type} event "
            f"at t={int(window.t_start_s)}s"
        )
        header = f"#### Generate {layer_type.title()} (event {window.index})"
        self._emit_full_step(header, title_phrase, process_key, full_params)
        current = primary_filename
        for step in layer_def.get("post_processing", []):
            if not isinstance(step, dict):
                continue
            step_process = str(step["process"])
            step_params = self._resolve_window_params(
                section, layer_type, step.get("params", {}), window,
            )
            current = self._emit_post_processing_step(
                section, layer_type, current, step_process, step_params,
            )
        return current

    def _emit_window_adelay(
        self,
        layer_type: str,
        window: ScheduledWindow,
        input_filename: str,
    ) -> str:
        """Emit ``ffmpeg_adelay`` for one window. Returns the delayed filename."""
        delay_ms = int(window.t_start_s * 1000)
        process_key = "plugin::audio_processing_plugin::ffmpeg_adelay"
        output_filename = f"{input_filename}_delayed"
        params: dict[str, Any] = {
            "input_audio_file": input_filename,
            "output_audio_file": output_filename,
            "output_audio_format": "wav",
            "delays": f"{delay_ms}|{delay_ms}",
        }
        title_phrase = (
            f"Delay the {layer_type} event {window.index} to t="
            f"{int(window.t_start_s)}s"
        )
        header = f"#### Delay {layer_type.title()} (event {window.index})"
        self._emit_full_step(header, title_phrase, process_key, params)
        return output_filename

    def _emit_window_amix(
        self,
        section: Segment,
        layer_type: str,
        delayed_files: list[str],
    ) -> str:
        """Emit a single ``ffmpeg_amix`` joining all delayed window stems."""
        process_key = "plugin::audio_processing_plugin::ffmpeg_amix"
        output_filename = f"{layer_type}_section_stem"
        params: dict[str, Any] = {
            "input_audio_files": list(delayed_files),
            "output_audio_file": output_filename,
            "output_audio_format": "wav",
            "input_count": len(delayed_files),
        }
        title_phrase = (
            f"Mix the {section.name} {layer_type} events into one stem"
        )
        header = f"#### Mix {layer_type.title()} Events"
        self._emit_full_step(header, title_phrase, process_key, params)
        return output_filename

    def _resolve_window_params(
        self,
        section: Segment,
        layer_type: str,
        params_spec: Any,
        window: ScheduledWindow,
    ) -> dict[str, Any]:
        """Resolve schema params with scheduled-window scope.

        Threads ``current_window=window`` so
        ``event_schedule_entry.<field>`` overrides resolve to the
        window's per-entry values when present, and fall through to
        the layer_config default otherwise.
        """
        if not isinstance(params_spec, dict):
            return {}
        pg_label = section.properties.get("parameter_group")
        parameter_group: ParameterGroup | None = None
        if isinstance(pg_label, str):
            parameter_group = self._pg_by_label.get(pg_label)
        layer_config = self._layer_config_by_type.get(layer_type)
        override = next(
            (ov for ov in section.layer_overrides
             if ov.layer_type == layer_type),
            None,
        )
        section_arcs = self._section_arcs_by_name.get(section.name, {})
        return _resolve_param_dict(
            params_spec, section, parameter_group, layer_config,
            override, section_arcs, self.schema,
            current_window=window,
        )

    def _get_io_keys(
        self, process_key: str,
    ) -> tuple[str | None, str, bool]:
        """Return (input_key, output_key, emit_format_arg) for a process.

        Falls back to audio I/O when the process is not in the map.
        """
        entry = self.process_io_map.get(process_key)
        if entry is not None:
            return entry
        return "input_audio_file", "output_audio_file", True

    def _emit_generation_step(
        self,
        section: Segment,
        layer: SegmentLayer,
    ) -> str:
        output_filename = _generation_filename(
            self.artifact_prefix, self.phase_number,
            section.name, layer.layer_type,
        )
        params = dict(layer.params)
        _, out_key, emit_format = self._get_io_keys(layer.process)
        params[out_key] = output_filename
        if emit_format:
            params.setdefault("output_audio_format", "wav")
        title_phrase = f"Generate the {section.name} {layer.layer_type}"
        header = f"#### Generate {layer.layer_type.title()}"
        self._emit_full_step(header, title_phrase, layer.process, params)
        return output_filename

    def _extract_reference_input(self, layer: SegmentLayer) -> str:
        ref = layer.params.get("input_audio_file")
        if not ref:
            raise ValueError(
                f"Reference layer {layer.layer_type!r} requires "
                f"'input_audio_file' in params",
            )
        return str(ref)

    def _emit_post_processing_step(
        self,
        section: Segment,
        layer_type: str,
        current_input: str,
        process_key: str,
        params: dict[str, Any],
    ) -> str:
        short = _process_short_name(process_key)
        output_filename = f"{current_input}_{short}"
        capped = _apply_audibility_caps(
            process_key, layer_type, params, self.schema,
        )
        full_params = dict(capped)
        in_key, out_key, emit_format = self._get_io_keys(process_key)
        if in_key:
            full_params[in_key] = current_input
        full_params[out_key] = output_filename
        if emit_format:
            full_params.setdefault("output_audio_format", "wav")
        title_phrase = (
            f"Apply {short.replace('_', ' ')} to the "
            f"{section.name} {layer_type}"
        )
        header = f"#### {short.replace('_', ' ').title()} ({layer_type})"
        self._emit_full_step(header, title_phrase, process_key, full_params)
        return output_filename

    # ── finishing chain ──

    def _emit_finishing_chain(
        self,
        section: Segment,
        layer_outputs: dict[str, str],
    ) -> str:
        current = self._initial_finishing_input(layer_outputs)
        # The downstream phase reads the bare ``_section_stem.wav``
        # filename (no suffix). Identify the last finishing step that
        # actually runs for this section so its output can publish under
        # the bare name; intermediate steps keep their per-step suffix.
        chain_steps = self.schema.get("segment_finishing", [])
        active_publishing = [
            step for step in chain_steps
            if self._finishing_step_active(step, section)
            and not step.get("applies_to_each_layer")
        ]
        last_publishing_id = id(active_publishing[-1]) if active_publishing else None
        for step_def in chain_steps:
            if not self._finishing_step_active(step_def, section):
                continue
            name = str(step_def.get("name", ""))
            if step_def.get("applies_to_each_layer") is True:
                self._emit_per_layer_finishing(
                    section, step_def, layer_outputs,
                )
                continue
            is_final = id(step_def) == last_publishing_id
            if name == "mix":
                current = self._emit_mix(
                    section, layer_outputs, is_final=is_final,
                )
                continue
            current = self._emit_finishing_step(
                section, step_def, current, is_final=is_final,
            )
        return current

    def _initial_finishing_input(
        self,
        layer_outputs: dict[str, str],
    ) -> str:
        """Seed the finishing chain when there is no mix step.

        If the schema declares a mix step, mix sets the chain's input.
        Otherwise the single active layer's last output is the stem.
        With multiple layers and no mix, raise — schemas with multi-
        layer sections must declare how layers combine.
        """
        has_mix = any(
            step.get("name") == "mix"
            for step in self.schema.get("segment_finishing", [])
        )
        if has_mix:
            return ""
        if len(layer_outputs) == 1:
            return next(iter(layer_outputs.values()))
        if not layer_outputs:
            return ""
        raise ValueError(
            "Schema has multiple active layers but no mix step in "
            "segment_finishing. Add a mix step or restrict layers.",
        )

    def _finishing_step_active(
        self,
        step_def: dict[str, Any],
        section: Segment,
    ) -> bool:
        if step_def.get("required") is True:
            return True
        if step_def.get("applies_to_each_layer") is True:
            return True
        if step_def.get("name") == "mix":
            return True
        condition = step_def.get("active_when")
        if condition is None:
            return False
        return _evaluate_condition(condition, section, {})

    def _emit_mix(
        self,
        section: Segment,
        layer_outputs: dict[str, str],
        *,
        is_final: bool = False,
    ) -> str:
        process_key = self._mix_process_key()
        output_filename = _section_stem_filename(
            self.artifact_prefix, self.phase_number, section.name,
            "" if is_final else "raw",
        )
        input_files = list(layer_outputs.values())
        params = {
            "input_audio_files": input_files,
            "output_audio_file": output_filename,
            "output_audio_format": "wav",
            "input_count": len(input_files),
        }
        self._emit_full_step(
            "#### Mix Layers",
            f"Mix the {section.name} layers into one raw section stem",
            process_key,
            params,
        )
        return output_filename

    def _mix_process_key(self) -> str:
        for step in self.schema.get("segment_finishing", []):
            if step.get("name") == "mix":
                return str(step["process"])
        raise ValueError("Schema segment_finishing has no 'mix' entry")

    def _emit_finishing_step(
        self,
        section: Segment,
        step_def: dict[str, Any],
        current_input: str,
        *,
        is_final: bool = False,
    ) -> str:
        name = str(step_def.get("name", ""))
        process_key = str(step_def["process"])
        suffix = "" if is_final else self._finishing_suffix(name)
        output_filename = _section_stem_filename(
            self.artifact_prefix, self.phase_number, section.name, suffix,
        )
        resolved = _resolve_param_dict(
            step_def.get("params", {}),
            section, None, None, None, {}, self.schema,
        )
        params = dict(resolved)
        if current_input:
            params["input_audio_file"] = current_input
        params["output_audio_file"] = output_filename
        params.setdefault("output_audio_format", "wav")
        title_phrase = self._finishing_title_phrase(name, section.name)
        header = f"#### {name.title()}"
        self._emit_full_step(header, title_phrase, process_key, params)
        return output_filename

    def _finishing_suffix(self, name: str) -> str:
        suffixes = {
            "envelope": "shaped",
            "widen": "wide",
            "reverb": "reverb",
            "normalize": "complete",
        }
        return suffixes.get(name, name)

    def _finishing_title_phrase(self, name: str, section_name: str) -> str:
        return f"Apply {name} to the {section_name} section stem"

    def _emit_per_layer_finishing(
        self,
        section: Segment,
        step_def: dict[str, Any],
        layer_outputs: dict[str, str],
    ) -> None:
        name = str(step_def.get("name", ""))
        process_key = str(step_def["process"])
        for layer in section.layers:
            override = next(
                (ov for ov in section.layer_overrides
                 if ov.layer_type == layer.layer_type),
                None,
            )
            if not _per_layer_finishing_active(step_def, override):
                continue
            current_input = layer_outputs[layer.layer_type]
            output_filename = f"{current_input}_{name}"
            resolved = _resolve_param_dict(
                step_def.get("params", {}),
                section, None, None, override, {}, self.schema,
            )
            params = dict(resolved)
            params["input_audio_file"] = current_input
            params["output_audio_file"] = output_filename
            params.setdefault("output_audio_format", "wav")
            self._emit_full_step(
                f"#### {name.title()} ({layer.layer_type})",
                f"Apply {name} fade-in to the {section.name} {layer.layer_type}",
                process_key,
                params,
            )
            layer_outputs[layer.layer_type] = output_filename

    # ── record state ──

    def _emit_record_state(self, section: Segment, finishing_output: str) -> None:
        title = (
            f"Record the {section.name} section stem filename and step state"
        )
        if finishing_output:
            title = f"{title} ({finishing_output})"
        arguments: dict[str, Any] = {
            "wbs_id": self.wbs_id,
            "step_number": self.step,
            "status": "completed",
        }
        if finishing_output:
            arguments["output_artifacts"] = [f"{finishing_output}.wav"]
        self._emit_simple_step(
            "#### Record State",
            title,
            "service_interface::thinking_service::"
            "record_work_breakdown_structure_step_state",
            arguments=arguments,
        )

    # ── primitive emitters ──

    def _emit_full_step(
        self,
        header: str,
        title_phrase: str,
        process_key: str,
        params: dict[str, Any],
    ) -> None:
        self.lines.append(header)
        self.lines.append("")
        self.lines.append(f"[ ] {self.step}. {title_phrase}")
        self.lines.append(
            f"    RESULT_PROCESSOR_KIND: {ResultProcessorKind.INFERENCE.value}",
        )
        self.lines.append(
            f"    a) {title_phrase} ({process_key})",
        )
        self.lines.append("        Arguments:")
        self.lines.append(
            "        " + json.dumps(params, separators=(",", ":")),
        )
        self.lines.append("")
        self.step += 1

    def _emit_simple_step(
        self,
        header: str,
        title_phrase: str,
        process_key: str,
        arguments: dict[str, Any] | None,
    ) -> None:
        self.lines.append(header)
        self.lines.append("")
        self.lines.append(f"[ ] {self.step}. {title_phrase}")
        self.lines.append(
            f"    RESULT_PROCESSOR_KIND: {ResultProcessorKind.INFERENCE.value}",
        )
        self.lines.append(f"    a) {title_phrase} ({process_key})")
        if arguments is not None:
            self.lines.append("        Arguments:")
            self.lines.append(
                "        " + json.dumps(arguments, separators=(",", ":")),
            )
        self.lines.append("")
        self.step += 1


