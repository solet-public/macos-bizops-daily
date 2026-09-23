# pyright: reportUnusedFunction=false, reportUnusedImport=false
"""Pipeline resolver, segment table renderer, and WBS generator.

The resolver reads a PipelineSpec (piece-level decisions filled by the
thinking model) and a pipeline schema (layer types, process mappings,
arc definitions), and produces fully resolved sections with concrete
SegmentLayer instances. The WBS generator emits an executable Work
Breakdown Structure markdown document from the resolved spec.

All functions are pure — no I/O, no logging side effects beyond
exceptions.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from ananta.error_handling import FrameworkError

from .constants import ErrorCode
from .pipeline_spec import (
    LayerConfig,
    ParameterArc,
    ParameterGroup,
    PipelineSpec,
    ResolvedScheduledLayer,
    ScheduledWindow,
    Segment,
    SegmentLayer,
    SegmentLayerOverride,
)

logger = logging.getLogger(__name__)

# ── dB normalisation ───────────────────────────────────────────────────


_DB_SUFFIXES: tuple[str, ...] = ("dB", "db", "DB", "Db")


def _parse_db(value: str | float | int) -> float:
    """Normalise a level_db value to a float for cap comparisons.

    Accepts ``"-10dB"``, ``"-10db"``, ``-10``, ``-10.0``. Raises
    ``ValueError`` on anything that does not parse cleanly. Used by
    the validators (Phase 0) and the renderer (Phase B) so the
    canonical in-memory form is always float; the ``"<n>dB"`` string
    form is rebuilt only at the ffmpeg argument boundary.
    """
    if isinstance(value, bool):
        raise ValueError(
            f"level_db must be a number or '<n>dB' string; got "
            f"bool {value!r}",
        )
    if isinstance(value, str):
        stripped = value.strip()
        for suffix in _DB_SUFFIXES:
            if stripped.endswith(suffix):
                stripped = stripped[: -len(suffix)].strip()
                break
        try:
            return float(stripped)
        except ValueError as exc:
            raise ValueError(
                f"level_db value {value!r} is not a number or "
                f"'<n>dB' string",
            ) from exc
    return float(value)


def _format_db(value: float) -> str:
    """Render a level_db float as a ``"<n>dB"`` string for ffmpeg.

    The renderer emits this form ONLY at the ``ffmpeg_volume.volume``
    argument boundary. Trailing zeros are kept (``-10.0dB`` rather
    than ``-10dB``) for visual consistency with hand-authored values.
    """
    return f"{value:.1f}dB"


# ── Source resolution ──────────────────────────────────────────────────


class _LookupContext:
    """Bundle every input the namespace lookups can consult.

    Avoids long argument lists on every namespace handler. Constructed
    once at ``_lookup_source`` entry, then handed to each registry
    entry as a single value.
    """

    __slots__ = (
        "section", "parameter_group", "layer_config", "override",
        "section_arcs", "current_window",
    )

    def __init__(
        self,
        section: Segment,
        parameter_group: ParameterGroup | None,
        layer_config: LayerConfig | None,
        override: SegmentLayerOverride | None,
        section_arcs: dict[str, Any],
        current_window: ScheduledWindow | None,
    ) -> None:
        self.section = section
        self.parameter_group = parameter_group
        self.layer_config = layer_config
        self.override = override
        self.section_arcs = section_arcs
        self.current_window = current_window


_NamespaceLookup = Callable[[str, "_LookupContext"], tuple[bool, Any]]


def _ns_segment_layer(key: str, ctx: _LookupContext) -> tuple[bool, Any]:
    return _dict_lookup(
        ctx.override.properties if ctx.override else None, key,
    )


def _ns_layer_config(key: str, ctx: _LookupContext) -> tuple[bool, Any]:
    return _dict_lookup(
        ctx.layer_config.properties if ctx.layer_config else None, key,
    )


def _ns_parameter_group(key: str, ctx: _LookupContext) -> tuple[bool, Any]:
    return _lookup_parameter_group(ctx.parameter_group, key)


def _ns_section(key: str, ctx: _LookupContext) -> tuple[bool, Any]:
    return _dict_lookup(ctx.section.properties, key)


def _ns_arc(key: str, ctx: _LookupContext) -> tuple[bool, Any]:
    return _dict_lookup(ctx.section_arcs, key)


def _ns_computed(key: str, ctx: _LookupContext) -> tuple[bool, Any]:
    del key, ctx  # resolved via _resolve_with_override
    return False, None


def _ns_event_schedule_entry(key: str, ctx: _LookupContext) -> tuple[bool, Any]:
    if not key:
        raise ValueError(
            "event_schedule_entry source path is malformed "
            "(field name is empty after the dot)",
        )
    if ctx.current_window is None:
        raise ValueError(
            f"event_schedule_entry.{key} referenced outside of "
            f"scheduled-window resolution scope",
        )
    return _dict_lookup(ctx.current_window.overrides, key)


_NAMESPACE_LOOKUPS: dict[str, _NamespaceLookup] = {
    "segment_layer": _ns_segment_layer,
    "layer_config": _ns_layer_config,
    "parameter_group": _ns_parameter_group,
    "section": _ns_section,
    "arc": _ns_arc,
    "computed": _ns_computed,
    "event_schedule_entry": _ns_event_schedule_entry,
}


def _lookup_source(
    source_path: str,
    section: Segment,
    parameter_group: ParameterGroup | None,
    layer_config: LayerConfig | None,
    override: SegmentLayerOverride | None,
    section_arcs: dict[str, Any],
    current_window: ScheduledWindow | None = None,
) -> tuple[bool, Any]:
    """Resolve a source path to a value.

    Returns ``(found, value)``. ``found`` is False when the path does
    not resolve — caller decides whether that is an error.

    The ``event_schedule_entry.<field>`` namespace is only valid when
    the resolver is in scheduled-window scope (``current_window`` is
    not None). When in scope but the window has no override for the
    field, returns ``(False, None)`` so the precedence chain falls
    through to ``segment_layer``/``layer_config``/schema default.
    Misuse — referencing the namespace without a window in scope, or
    a malformed prefix with no field after the dot — raises
    ``ValueError``.
    """
    if "." not in source_path:
        if source_path == "event_schedule_entry":
            raise ValueError(
                "event_schedule_entry source path is malformed "
                "(no '.<field>' suffix)",
            )
        return False, None
    namespace, key = source_path.split(".", 1)
    handler = _NAMESPACE_LOOKUPS.get(namespace)
    if handler is None:
        return False, None
    ctx = _LookupContext(
        section, parameter_group, layer_config, override,
        section_arcs, current_window,
    )
    return handler(key, ctx)


def _dict_lookup(
    source: dict[str, Any] | None,
    key: str,
) -> tuple[bool, Any]:
    if source is None or key not in source:
        return False, None
    return True, source[key]


def _lookup_parameter_group(
    parameter_group: ParameterGroup | None,
    key: str,
) -> tuple[bool, Any]:
    """Resolve a parameter_group.<key> source path."""
    if parameter_group is None:
        return False, None
    if key == "label":
        return True, parameter_group.label
    if key == "root_hz":
        return True, parameter_group.root_hz
    if key == "intervals_semitones":
        return True, parameter_group.intervals_semitones
    if key in parameter_group.properties:
        return True, parameter_group.properties[key]
    return False, None


def _resolve_param(
    spec_value: Any,
    section: Segment,
    parameter_group: ParameterGroup | None,
    layer_config: LayerConfig | None,
    override: SegmentLayerOverride | None,
    section_arcs: dict[str, Any],
    schema: dict[str, Any],
    *,
    field_name: str = "",
    current_window: ScheduledWindow | None = None,
) -> Any:
    """Resolve one schema param spec into a concrete value."""
    if not isinstance(spec_value, dict):
        return spec_value
    if "source" not in spec_value:
        return spec_value
    primary = spec_value["source"]
    override_path = spec_value.get("override")

    found, value = _resolve_with_override(
        primary, override_path,
        section, parameter_group, layer_config, override, section_arcs,
        schema,
        current_window=current_window,
    )
    if not found:
        raise ValueError(
            f"Source path {primary!r} did not resolve "
            f"for section {section.name!r} field {field_name!r}",
        )
    if "map" in spec_value and isinstance(spec_value["map"], dict):
        if value not in spec_value["map"]:
            raise ValueError(
                f"Map for source {primary!r} (section {section.name!r}) "
                f"has no entry for value {value!r}",
            )
        return spec_value["map"][value]
    return value


def _resolve_with_override(
    primary_path: str,
    override_path: str | None,
    section: Segment,
    parameter_group: ParameterGroup | None,
    layer_config: LayerConfig | None,
    override: SegmentLayerOverride | None,
    section_arcs: dict[str, Any],
    schema: dict[str, Any],
    *,
    current_window: ScheduledWindow | None = None,
) -> tuple[bool, Any]:
    """Resolve primary source with optional per-section override."""
    if override_path is not None:
        ov_found, ov_value = _lookup_source(
            override_path, section, parameter_group, layer_config,
            override, section_arcs, current_window,
        )
        if ov_found:
            return True, ov_value
    if primary_path.startswith("computed."):
        name = primary_path.split(".", 1)[1]
        return True, _compute_value(name, section, schema)
    return _lookup_source(
        primary_path, section, parameter_group, layer_config,
        override, section_arcs, current_window,
    )


def _resolve_param_dict(
    spec_params: dict[str, Any],
    section: Segment,
    parameter_group: ParameterGroup | None,
    layer_config: LayerConfig | None,
    override: SegmentLayerOverride | None,
    section_arcs: dict[str, Any],
    schema: dict[str, Any],
    *,
    current_window: ScheduledWindow | None = None,
) -> dict[str, Any]:
    """Resolve every param in a schema params dict."""
    out: dict[str, Any] = {}
    for key, spec_value in spec_params.items():
        out[key] = _resolve_param(
            spec_value, section, parameter_group, layer_config,
            override, section_arcs, schema, field_name=key,
            current_window=current_window,
        )
    return out


# ── Condition evaluation ───────────────────────────────────────────────


def _evaluate_condition(
    condition: dict[str, Any],
    section: Segment,
    section_arcs: dict[str, Any],
) -> bool:
    """Evaluate a structured predicate against a section."""
    if "arc" in condition:
        arc_name = str(condition["arc"])
        value = section_arcs.get(arc_name)
        return _apply_predicate(condition, value)
    if "property" in condition:
        prop = str(condition["property"])
        if "contains_layer_type" in condition:
            return _evaluate_contains_layer_type(condition, section, prop)
        if condition.get("is_set") is True:
            return prop in section.properties and section.properties[prop] not in (None, "")
        if condition.get("is_set") is False:
            return prop not in section.properties or section.properties[prop] in (None, "")
        value = section.properties.get(prop)
        return _apply_predicate(condition, value)
    raise ValueError(f"Unknown condition shape: {condition!r}")


def _evaluate_contains_layer_type(
    condition: dict[str, Any],
    section: Segment,
    prop: str,
) -> bool:
    """Evaluate ``{"property": "<list_prop>", "contains_layer_type": <type>}``.

    True iff ``section.properties[prop]`` (default empty list) contains
    an entry whose ``layer_type`` matches the named type.
    """
    target = condition.get("contains_layer_type")
    if not isinstance(target, str) or not target:
        raise ValueError(
            f"Predicate with property={prop!r} declares "
            f"'contains_layer_type' but value is missing or not a "
            f"non-empty string: {condition!r}",
        )
    entries = section.properties.get(prop, [])
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if isinstance(entry, dict) and entry.get("layer_type") == target:
            return True
    return False


_PREDICATE_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "in":     lambda value, target: value in target,
    "not_in": lambda value, target: value not in target,
    "eq":     lambda value, target: value == target,
    "gt":     lambda value, target: value is not None and value > target,
    "lt":     lambda value, target: value is not None and value < target,
    "gte":    lambda value, target: value is not None and value >= target,
    "lte":    lambda value, target: value is not None and value <= target,
}


def _apply_predicate(condition: dict[str, Any], value: Any) -> bool:
    """Apply the comparison operator from a condition."""
    for op_name, op_fn in _PREDICATE_OPS.items():
        if op_name in condition:
            return op_fn(value, condition[op_name])
    raise ValueError(f"No comparison operator in condition: {condition!r}")


# ── Computed values ────────────────────────────────────────────────────


def _compute_envelope_breakpoints(
    section: Segment,
    schema_params: dict[str, Any],
) -> list[dict[str, float]]:
    # Section stems sustain at full amplitude through their end. Phase 3
    # ffmpeg_acrossfade overlaps adjacent stems and handles the inter-section
    # transition; a tail fade-to-zero here would make both inner edges of any
    # crossfade silent and produce audible silence between sections. The
    # final master fade-out lives in the Phase 3 master assembly chain.
    duration = float(section.properties["duration_s"])
    fade_in = max(
        float(schema_params["fade_in_min_s"]),
        min(
            float(schema_params["fade_in_max_s"]),
            duration * float(schema_params["fade_in_fraction"]),
        ),
    )
    return [
        {"time_s": 0.0, "amplitude": 0.0},
        {"time_s": round(fade_in, 2), "amplitude": 1.0},
        {"time_s": round(duration, 2), "amplitude": 1.0},
    ]


def _compute_stereo_delay_from_width(
    section: Segment,
    schema_params: dict[str, Any],
) -> float:
    width = float(section.properties.get("width", 0.0))
    base = float(schema_params["base_delay_ms"])
    scale = float(schema_params["scale_factor"])
    return round(base + (width * scale), 2)


def _compute_ir_dry_from_wet(
    section: Segment,
    schema_params: dict[str, Any],
) -> float:
    del schema_params  # math, not configurable — params are unused
    wet = float(section.properties.get("ir_wet", 0.0))
    return round(1.0 - wet, 3)


_COMPUTED_VALUES: dict[
    str, Callable[[Segment, dict[str, Any]], Any],
] = {
    "envelope_breakpoints": _compute_envelope_breakpoints,
    "stereo_delay_from_width": _compute_stereo_delay_from_width,
    "ir_dry_from_wet": _compute_ir_dry_from_wet,
}


def _compute_value(name: str, section: Segment, schema: dict[str, Any]) -> Any:
    """Evaluate a named computed value using schema-provided constants.

    The resolver pre-promotes arc values into ``section.properties``
    before calling this, so computed-value functions can read arcs the
    same way they read other section properties.
    """
    fn = _COMPUTED_VALUES.get(name)
    if fn is None:
        raise ValueError(f"Unknown computed value: {name!r}")
    schema_params = schema.get("computed_value_params", {}).get(name, {})
    return fn(section, schema_params)


# ── Resolver ───────────────────────────────────────────────────────────


def resolve_pipeline(
    spec: PipelineSpec,
    schema: dict[str, Any],
) -> PipelineSpec:
    """Resolve section layers from piece-level decisions and the schema.

    Validates that every layer's required source paths resolve; raises
    ``ValueError`` on the first failure.
    """
    pg_by_label = {pg.label: pg for pg in spec.parameter_groups}
    layer_config_by_type = {lc.layer_type: lc for lc in spec.layer_configs}
    section_arcs_by_name = _build_arc_lookup(spec.arcs)
    new_sections: list[Segment] = []
    for section in spec.sections:
        promoted = _promote_arcs_to_properties(
            section, section_arcs_by_name.get(section.name, {}),
        )
        merged_overrides = _merge_overrides_by_layer(promoted.layer_overrides)
        section_arcs = section_arcs_by_name.get(section.name, {})
        pg = _select_parameter_group(promoted, pg_by_label)
        layers = _resolve_section_layers(
            promoted,
            schema,
            pg,
            layer_config_by_type,
            section_arcs,
            spec.modulation_assignments,
        )
        scheduled = _resolve_scheduled_layers(promoted, schema, section_arcs)
        new_sections.append(
            Segment(
                name=promoted.name,
                properties=promoted.properties,
                layer_overrides=list(merged_overrides.values()),
                layers=layers,
                scheduled_layers=scheduled,
            ),
        )
    return PipelineSpec(
        schema_id=spec.schema_id,
        piece=spec.piece,
        parameter_groups=spec.parameter_groups,
        arcs=spec.arcs,
        layer_configs=spec.layer_configs,
        sections=new_sections,
        modulation_assignments=spec.modulation_assignments,
    )


def _build_arc_lookup(
    arcs: list[ParameterArc],
) -> dict[str, dict[str, Any]]:
    """Index arc values by section name."""
    out: dict[str, dict[str, Any]] = {}
    for arc in arcs:
        for sec_name, value in arc.values.items():
            out.setdefault(sec_name, {})[arc.name] = value
    return out


def _promote_arcs_to_properties(
    section: Segment,
    section_arcs: dict[str, Any],
) -> Segment:
    """Promote arc values into section.properties for computed lookups.

    Computed value functions read e.g. ``section.properties['width']``;
    we promote arc values onto a copy of the section so those lookups
    succeed without complicating the resolver's surface API.
    """
    merged: dict[str, Any] = dict(section.properties)
    for arc_name, value in section_arcs.items():
        merged.setdefault(arc_name, value)
    return Segment(
        name=section.name,
        properties=merged,
        layer_overrides=section.layer_overrides,
        layers=section.layers,
    )


def _select_parameter_group(
    section: Segment,
    pg_by_label: dict[str, ParameterGroup],
) -> ParameterGroup | None:
    label = section.properties.get("parameter_group")
    if not label:
        return None
    pg = pg_by_label.get(str(label))
    if pg is None:
        raise ValueError(
            f"Section {section.name!r} references parameter_group "
            f"{label!r} which is not defined in spec.parameter_groups",
        )
    return pg


def _resolve_section_layers(
    section: Segment,
    schema: dict[str, Any],
    parameter_group: ParameterGroup | None,
    layer_config_by_type: dict[str, LayerConfig],
    section_arcs: dict[str, Any],
    modulation_assignments: list[Any],
) -> list[SegmentLayer]:
    """Resolve every active section-spanning layer for one section.

    Layers whose schema entry declares ``scheduling: event_schedule``
    (or analogous) are intentionally skipped here — they are emitted
    via ``_resolve_scheduled_layers`` so the renderer can place them
    on the timeline rather than as a section-spanning render.
    """
    overrides_by_layer = _merge_overrides_by_layer(section.layer_overrides)
    out: list[SegmentLayer] = []
    for layer_type, layer_def in schema["layer_types"].items():
        if layer_def.get("scheduling"):
            continue
        if not _layer_active(layer_def, section, section_arcs):
            continue
        layer_config = layer_config_by_type.get(layer_type)
        override = overrides_by_layer.get(layer_type)
        layer = _resolve_one_layer(
            layer_type, layer_def, section,
            parameter_group, layer_config, override,
            section_arcs, schema,
        )
        _attach_modulation(
            layer, section.name, modulation_assignments, schema,
        )
        out.append(layer)
    return out


def _resolve_scheduled_layers(
    section: Segment,
    schema: dict[str, Any],
    section_arcs: dict[str, Any],
) -> tuple[ResolvedScheduledLayer, ...]:
    """Group active scheduled layers by layer_type into resolved windows.

    The schema names the section property each scheduled layer reads
    via its ``scheduling`` field (e.g.
    ``scheduling: event_schedule`` → reads
    ``section.properties.event_schedule``). Window entries whose
    ``layer_type`` matches the layer are collected into a
    ``ResolvedScheduledLayer`` for emission.
    """
    out: list[ResolvedScheduledLayer] = []
    for layer_type, layer_def in schema["layer_types"].items():
        scheduling_key = layer_def.get("scheduling")
        if not scheduling_key:
            continue
        if not _layer_active(layer_def, section, section_arcs):
            continue
        windows = _collect_scheduled_windows(
            section, str(scheduling_key), str(layer_type),
        )
        if not windows:
            continue
        out.append(ResolvedScheduledLayer(
            layer_type=str(layer_type),
            windows=windows,
        ))
    return tuple(out)


def _collect_scheduled_windows(
    section: Segment,
    scheduling_key: str,
    layer_type: str,
) -> tuple[ScheduledWindow, ...]:
    """Filter ``section.properties[scheduling_key]`` to entries for one layer."""
    raw = section.properties.get(scheduling_key, [])
    if not isinstance(raw, list):
        return ()
    out: list[ScheduledWindow] = []
    base_keys = {"layer_type", "t_start_s", "t_end_s"}
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        if entry.get("layer_type") != layer_type:
            continue
        try:
            t_start = float(entry["t_start_s"])
            t_end = float(entry["t_end_s"])
        except (KeyError, TypeError, ValueError):
            continue
        overrides = {
            k: v for k, v in entry.items() if k not in base_keys
        }
        out.append(ScheduledWindow(
            index=index,
            t_start_s=t_start,
            t_end_s=t_end,
            overrides=overrides,
        ))
    return tuple(out)


def _merge_overrides_by_layer(
    overrides: list[SegmentLayerOverride],
) -> dict[str, SegmentLayerOverride]:
    """Merge multiple SegmentLayerOverride entries for the same layer.

    Later entries overwrite earlier keys for the same layer_type — the
    thinking model can express section-level overrides as multiple
    grouped entries (one per concern) without the resolver caring.
    """
    out: dict[str, SegmentLayerOverride] = {}
    for ov in overrides:
        existing = out.get(ov.layer_type)
        if existing is None:
            out[ov.layer_type] = SegmentLayerOverride(
                layer_type=ov.layer_type,
                properties=dict(ov.properties),
            )
            continue
        existing.properties.update(ov.properties)
    return out


def _layer_active(
    layer_def: dict[str, Any],
    section: Segment,
    section_arcs: dict[str, Any],
) -> bool:
    if layer_def.get("required") is True:
        return True
    condition = layer_def.get("active_when")
    if condition is None:
        return bool(layer_def.get("required", False))
    return _evaluate_condition(condition, section, section_arcs)


_CADENCE_CARRIER_LAYER_TYPE = "cadence_carrier"
_PROSODIC_PHRASE_MODE = "prosodic_phrase"
_GENERATE_CADENCE_PHRASES_PROCESS = (
    "plugin::generative_composition_plugin::generate_cadence_phrases"
)
_RENDER_MIDI_FILE_PROCESS = (
    "plugin::musical_synthesis_plugin::render_midi_file"
)


def _resolve_one_layer(
    layer_type: str,
    layer_def: dict[str, Any],
    section: Segment,
    parameter_group: ParameterGroup | None,
    layer_config: LayerConfig | None,
    override: SegmentLayerOverride | None,
    section_arcs: dict[str, Any],
    schema: dict[str, Any],
) -> SegmentLayer:
    """Resolve one layer's params and post-processing chain.

    Each post_processing step is skipped when its required sources do
    not resolve — that's interpreted as "the spec did not commit to
    this optional processing step." Required layer params (the layer's
    own ``params``) still raise; missing those is a real spec bug.

    Special case: cadence_carrier with
    ``layer_config.phrase_pattern_type == "prosodic_phrase"`` is
    rewritten to dispatch
    ``generate_cadence_phrases`` (MIDI generation) +
    ``render_midi_file`` (SoundFont synthesis) ahead of the schema's
    declared envelope/volume chain. This lets the same schema entry
    serve both the literal-notes mode (``render_note_sequence``) and
    the generative breath-phrase mode without duplicating the
    layer_types declaration.
    """
    if (
        layer_type == _CADENCE_CARRIER_LAYER_TYPE
        and _phrase_pattern_type(layer_config, override) == _PROSODIC_PHRASE_MODE
    ):
        return _resolve_prosodic_cadence_layer(
            layer_def, section, parameter_group, layer_config, override,
            section_arcs, schema,
        )

    source_type = str(layer_def.get("source_type", "generate"))
    params = _resolve_param_dict(
        layer_def.get("params", {}),
        section, parameter_group, layer_config, override,
        section_arcs, schema,
    )
    post_processing: list[tuple[str, dict[str, Any]]] = []
    for step in layer_def.get("post_processing", []):
        try:
            step_params = _resolve_param_dict(
                step.get("params", {}),
                section, parameter_group, layer_config, override,
                section_arcs, schema,
            )
        except ValueError:
            # Optional post-processing whose source the spec did not
            # commit. Skip the step entirely.
            continue
        if "output_format" in step:
            step_params["output_audio_format"] = step["output_format"]
        post_processing.append((str(step["process"]), step_params))
    return SegmentLayer(
        layer_type=layer_type,
        process=str(layer_def.get("process", "")),
        source_type=source_type,
        params=params,
        post_processing=post_processing,
    )


def _phrase_pattern_type(
    layer_config: LayerConfig | None,
    override: SegmentLayerOverride | None,
) -> str | None:
    """Return phrase_pattern_type from override or layer_config, or None."""
    if override is not None:
        value = override.properties.get("phrase_pattern_type")
        if isinstance(value, str):
            return value
    if layer_config is not None:
        value = layer_config.properties.get("phrase_pattern_type")
        if isinstance(value, str):
            return value
    return None


def _resolve_prosodic_cadence_layer(
    layer_def: dict[str, Any],
    section: Segment,
    parameter_group: ParameterGroup | None,
    layer_config: LayerConfig | None,
    override: SegmentLayerOverride | None,
    section_arcs: dict[str, Any],
    schema: dict[str, Any],
) -> SegmentLayer:
    """Build the SegmentLayer for cadence_carrier prosodic_phrase mode."""
    if layer_config is None:
        raise ValueError(
            f"cadence_carrier prosodic_phrase mode requires a "
            f"layer_configs entry for {_CADENCE_CARRIER_LAYER_TYPE!r}",
        )
    phrase_params = _phrase_generation_params(layer_config, override)
    duration_s = section.properties.get("duration_s")
    if duration_s is None:
        raise ValueError(
            f"section {section.name!r} missing duration_s; required "
            f"for cadence_carrier prosodic_phrase mode",
        )
    generation_args: dict[str, Any] = {
        "scale_root": phrase_params["scale_root"],
        "scale_type": phrase_params["scale_type"],
        "pitch_range": list(phrase_params["pitch_range"]),
        "total_duration_s": float(duration_s),
        "phrase_duration_range_s": list(
            phrase_params["phrase_duration_range_s"],
        ),
        "inter_phrase_rest_s": list(phrase_params["inter_phrase_rest_s"]),
        "motif_cell": list(phrase_params["motif_cell"]),
        "velocity_contour": list(phrase_params["velocity_contour"]),
    }
    if "contour_bias" in phrase_params:
        generation_args["contour_bias"] = phrase_params["contour_bias"]
    if "seed" in phrase_params:
        generation_args["seed"] = phrase_params["seed"]

    render_args: dict[str, Any] = {"output_audio_format": "wav"}
    program = layer_config.properties.get("program")
    if program is not None:
        render_args["program_overrides"] = {"0": int(program)}
    soundfont = layer_config.properties.get("soundfont_name")
    if soundfont is not None:
        render_args["soundfont_name"] = str(soundfont)

    post_processing: list[tuple[str, dict[str, Any]]] = [
        (_RENDER_MIDI_FILE_PROCESS, render_args),
    ]
    for step in layer_def.get("post_processing", []):
        try:
            step_params = _resolve_param_dict(
                step.get("params", {}),
                section, parameter_group, layer_config, override,
                section_arcs, schema,
            )
        except ValueError:
            continue
        if "output_format" in step:
            step_params["output_audio_format"] = step["output_format"]
        post_processing.append((str(step["process"]), step_params))

    return SegmentLayer(
        layer_type=_CADENCE_CARRIER_LAYER_TYPE,
        process=_GENERATE_CADENCE_PHRASES_PROCESS,
        source_type="generate",
        params=generation_args,
        post_processing=post_processing,
    )


_PHRASE_REQUIRED_KEYS: tuple[str, ...] = (
    "scale_root", "scale_type", "pitch_range",
    "phrase_duration_range_s", "inter_phrase_rest_s",
    "motif_cell", "velocity_contour",
)


def _phrase_generation_params(
    layer_config: LayerConfig,
    override: SegmentLayerOverride | None,
) -> dict[str, Any]:
    """Merge phrase_generation_params from layer_config with override."""
    base: dict[str, Any] = {}
    config_block = layer_config.properties.get("phrase_generation_params")
    if isinstance(config_block, dict):
        base.update(config_block)
    if override is not None:
        override_block = override.properties.get("phrase_generation_params")
        if isinstance(override_block, dict):
            base.update(override_block)
    missing = [k for k in _PHRASE_REQUIRED_KEYS if k not in base]
    if missing:
        raise ValueError(
            f"cadence_carrier phrase_generation_params missing required "
            f"keys: {missing!r}",
        )
    return base


def _attach_modulation(
    layer: SegmentLayer,
    section_name: str,
    modulation_assignments: list[Any],
    schema: dict[str, Any],
) -> None:
    """Append modulation entries from assignments to the layer's chain."""
    allowed = set(schema.get("modulation", {}).get("applies_to", []) or [])
    for ma in modulation_assignments:
        if ma.section_name != section_name or ma.layer_type != layer.layer_type:
            continue
        if allowed and ma.layer_type not in allowed:
            raise ValueError(
                f"Modulation assigned to layer {ma.layer_type!r} "
                f"in section {section_name!r}, but schema "
                f"modulation.applies_to does not list it",
            )
        layer.post_processing.append((ma.process, dict(ma.params)))


# ── Audibility-cap enforcement at emission ─────────────────────────────


_FFMPEG_VOLUME_PROCESS = "plugin::audio_processing_plugin::ffmpeg_volume"
_DELAY_PROCESS_KEYS: frozenset[str] = frozenset({
    "plugin::pedalboard_effects_plugin::apply_tempo_synced_delay",
    "plugin::pedalboard_effects_plugin::apply_delay",
})


def _apply_audibility_caps(
    process_key: str,
    layer_type: str,
    params: dict[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any]:
    """Enforce per-role audibility caps when emitting an audio step.

    For ``ffmpeg_volume``: parses the ``volume`` arg via
    ``_parse_db``, checks against the role's
    ``level_db_min``/``level_db_max``, and re-formats via
    ``_format_db`` for ffmpeg's string form.

    For delay processes (``apply_tempo_synced_delay`` etc.): checks
    ``mix`` and ``feedback`` against ``delay_mix_max`` and
    ``delay_feedback_max``.

    Raises ``FrameworkError`` on any cap violation. Returns a new
    params dict with the dB string form normalised.
    """
    role_caps = _role_caps_for_layer(layer_type, schema)
    if role_caps is None:
        return params
    if process_key == _FFMPEG_VOLUME_PROCESS:
        return _enforce_volume_cap(layer_type, params, role_caps)
    if process_key in _DELAY_PROCESS_KEYS:
        _enforce_delay_caps(layer_type, params, role_caps)
    return params


def _role_caps_for_layer(
    layer_type: str,
    schema: dict[str, Any],
) -> dict[str, Any] | None:
    layer_def = schema.get("layer_types", {}).get(layer_type)
    if not isinstance(layer_def, dict):
        return None
    role = layer_def.get("mix_role")
    if not isinstance(role, str):
        return None
    caps_block = (schema.get("audibility_caps", {}) or {}).get("by_role", {})
    if not isinstance(caps_block, dict):
        return None
    role_caps = caps_block.get(role)
    if not isinstance(role_caps, dict):
        return None
    return role_caps


def _enforce_volume_cap(
    layer_type: str,
    params: dict[str, Any],
    role_caps: dict[str, Any],
) -> dict[str, Any]:
    if "volume" not in params:
        return params
    try:
        value = _parse_db(params["volume"])
    except ValueError as exc:
        raise FrameworkError(
            message=(
                f"layer {layer_type!r} ffmpeg_volume.volume "
                f"{params['volume']!r}: {exc}"
            ),
            error_code=ErrorCode.PARAMETER_ERROR,
        ) from exc
    lo = role_caps.get("level_db_min")
    hi = role_caps.get("level_db_max")
    if isinstance(lo, (int, float)) and value < float(lo):
        raise FrameworkError(
            message=(
                f"layer {layer_type!r} ffmpeg_volume.volume "
                f"{value} dB is below the role floor {lo} dB"
            ),
            error_code=ErrorCode.PARAMETER_ERROR,
        )
    if isinstance(hi, (int, float)) and value > float(hi):
        raise FrameworkError(
            message=(
                f"layer {layer_type!r} ffmpeg_volume.volume "
                f"{value} dB is above the role ceiling {hi} dB"
            ),
            error_code=ErrorCode.PARAMETER_ERROR,
        )
    out = dict(params)
    out["volume"] = _format_db(value)
    return out


def _enforce_delay_caps(
    layer_type: str,
    params: dict[str, Any],
    role_caps: dict[str, Any],
) -> None:
    for arg, cap_key in (("mix", "delay_mix_max"), ("feedback", "delay_feedback_max")):
        if arg not in params:
            continue
        try:
            value = float(params[arg])
        except (TypeError, ValueError) as exc:
            raise FrameworkError(
                message=(
                    f"layer {layer_type!r} delay.{arg} "
                    f"{params[arg]!r} is not numeric"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            ) from exc
        cap = role_caps.get(cap_key)
        if isinstance(cap, (int, float)) and value > float(cap):
            raise FrameworkError(
                message=(
                    f"layer {layer_type!r} delay.{arg} {value} "
                    f"exceeds the role cap {cap}"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )


# ── Filename construction ──────────────────────────────────────────────


_SHORT_NAME_PREFIXES: tuple[str, ...] = ("apply_", "ffmpeg_", "generate_")


def _process_short_name(process_key: str) -> str:
    """Derive a short suffix for filename chaining from a process key."""
    last = process_key.rsplit("::", 1)[-1]
    for prefix in _SHORT_NAME_PREFIXES:
        if last.startswith(prefix):
            return last[len(prefix):]
    return last


def _generation_filename(prefix: str, phase_n: int, section: str, layer_type: str) -> str:
    return f"{prefix}_phase{phase_n}_{section}_{layer_type}"


def _section_stem_filename(prefix: str, phase_n: int, section: str, suffix: str) -> str:
    base = f"{prefix}_phase{phase_n}_{section}_section_stem"
    return f"{base}_{suffix}" if suffix else base


# ── Segment table renderer ─────────────────────────────────────────────


def render_segment_table(
    spec: PipelineSpec,
    schema: dict[str, Any],
) -> str:
    """Render the resolved spec as a markdown binding table.

    Columns: section name, every arc, every active layer's parameters
    (one column per layer-param pair). Rows: sections.
    """
    arc_names = list(schema.get("arcs", {}).keys())
    layer_params = _collect_layer_param_columns(spec)
    headers = ["section"] + arc_names + [
        f"{layer_type}.{key}" for layer_type, key in layer_params
    ]
    rows = [_render_table_row(section, arc_names, layer_params) for section in spec.sections]
    _ = schema  # schema columns are derived from spec; arg kept for symmetry
    return _render_markdown_table(headers, rows)


def _collect_layer_param_columns(spec: PipelineSpec) -> list[tuple[str, str]]:
    """Pick the layer-param pairs to surface as columns."""
    seen: list[tuple[str, str]] = []
    seen_set: set[tuple[str, str]] = set()
    for section in spec.sections:
        for layer in section.layers:
            for key in layer.params:
                pair = (layer.layer_type, key)
                if pair not in seen_set:
                    seen.append(pair)
                    seen_set.add(pair)
    return seen


def _render_table_row(
    section: Segment,
    arc_names: list[str],
    layer_params: list[tuple[str, str]],
) -> list[str]:
    cells = [section.name]
    for arc_name in arc_names:
        cells.append(_format_cell(section.properties.get(arc_name, "")))
    layer_index = {layer.layer_type: layer for layer in section.layers}
    for layer_type, key in layer_params:
        layer = layer_index.get(layer_type)
        if layer is None:
            cells.append("—")
            continue
        cells.append(_format_cell(layer.params.get(key, "—")))
    return cells


def _format_cell(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"))
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _render_markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    out: list[str] = []
    out.append("| " + " | ".join(headers) + " |")
    out.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


# ── WBS generator ──────────────────────────────────────────────────────

# Maps process_key → (input_key | None, output_key, emit_output_format_arg).
# Built by callers (wbs_authoring.py) from the discovery service so this
# module stays pure.  Fallback when absent: input_audio_file / output_audio_file.


def collect_all_schema_process_keys(schema: dict[str, Any]) -> set[str]:
    """Return all process keys referenced in a pipeline schema.

    Covers ``layer_types`` (primary process, ``alternate_processes`` for
    layers with conditional dispatch, and post_processing) and
    ``segment_finishing`` steps. ``alternate_processes`` lets a layer
    declare additional process keys that the resolver may swap in based
    on layer_config (e.g. cadence_carrier prosodic_phrase mode dispatches
    to ``generate_cadence_phrases`` instead of the static ``process``).
    Including these makes the discovery-driven IO map cover the swapped
    process so WBS emission resolves the correct output slot.
    """
    keys: set[str] = set()
    for layer_def in schema.get("layer_types", {}).values():
        keys.update(_collect_layer_process_keys(layer_def))
    keys.update(_collect_step_process_keys(schema.get("segment_finishing", [])))
    return keys


def _collect_layer_process_keys(layer_def: Any) -> set[str]:
    """All process keys reachable from one layer_types entry."""
    keys: set[str] = set()
    if not isinstance(layer_def, dict):
        return keys
    key = layer_def.get("process")
    if isinstance(key, str):
        keys.add(key)
    for alt in layer_def.get("alternate_processes", []):
        if isinstance(alt, str):
            keys.add(alt)
    keys.update(_collect_step_process_keys(layer_def.get("post_processing", [])))
    return keys


def _collect_step_process_keys(steps: list[Any]) -> set[str]:
    """Pull ``"process"`` strings out of a list of step dicts."""
    keys: set[str] = set()
    for step in steps:
        if isinstance(step, dict):
            k = step.get("process")
            if isinstance(k, str):
                keys.add(k)
    return keys


def _per_layer_finishing_active(
    step_def: dict[str, Any],
    override: SegmentLayerOverride | None,
) -> bool:
    """Per-layer finishing fires only when the override has the keys
    the step's params reference. Concretely: stagger requires
    ``entrance_delay_s`` to be present and > 0.
    """
    name = str(step_def.get("name", ""))
    if name != "stagger":
        return True
    if override is None:
        return False
    delay = override.properties.get("entrance_delay_s")
    if delay is None:
        return False
    try:
        return float(delay) > 0
    except (TypeError, ValueError):
        return False


# ── PipelineSpec shape validation ─────────────────────────────────────


def collect_layer_config_sources(
    schema: dict[str, Any],
) -> list[tuple[str, str, str, str]]:
    """Collect (layer_type, process, arg_key, lc_key) for layer_config sources.

    Walks every ``params`` dict in ``schema.layer_types`` (including
    ``post_processing`` params) and returns a tuple for every declaration
    of the form ``{"source": "layer_config.<lc_key>"}``.

    ``arg_key`` is the key in the process's invocation schema (the param
    name as the tool sees it). ``lc_key`` is the key to look up in
    ``layer_configs[layer_type].properties``.
    """
    result: list[tuple[str, str, str, str]] = []
    for layer_type, layer_def in schema.get("layer_types", {}).items():
        if not isinstance(layer_def, dict):
            continue
        primary_process = str(layer_def.get("process", ""))
        _collect_params(
            layer_def.get("params", {}),
            str(layer_type),
            primary_process,
            result,
        )
        for step in layer_def.get("post_processing", []):
            if not isinstance(step, dict):
                continue
            post_process = str(step.get("process", primary_process))
            _collect_params(
                step.get("params", {}),
                str(layer_type),
                post_process,
                result,
            )
    return result


def _collect_params(
    params: Any,
    layer_type: str,
    process: str,
    result: list[tuple[str, str, str, str]],
) -> None:
    if not isinstance(params, dict):
        return
    for arg_key, param_def in params.items():
        if not isinstance(param_def, dict):
            continue
        source = param_def.get("source", "")
        if not isinstance(source, str):
            continue
        if source.startswith("layer_config."):
            lc_key = source[len("layer_config."):]
            result.append((layer_type, process, str(arg_key), lc_key))


def collect_parameter_group_sources(
    schema: dict[str, Any],
) -> list[tuple[str, str, str, str]]:
    """Collect (layer_type, process, arg_key, pg_key) for parameter_group sources.

    Walks every ``params`` dict in ``schema.layer_types`` (including
    ``post_processing`` params) and returns a tuple for every declaration
    of the form ``{"source": "parameter_group.<pg_key>"}``.
    """
    result: list[tuple[str, str, str, str]] = []
    for layer_type, layer_def in schema.get("layer_types", {}).items():
        if not isinstance(layer_def, dict):
            continue
        primary_process = str(layer_def.get("process", ""))
        _collect_pg_params(
            layer_def.get("params", {}),
            str(layer_type),
            primary_process,
            result,
        )
        for step in layer_def.get("post_processing", []):
            if not isinstance(step, dict):
                continue
            post_process = str(step.get("process", primary_process))
            _collect_pg_params(
                step.get("params", {}),
                str(layer_type),
                post_process,
                result,
            )
    return result


def _collect_pg_params(
    params: Any,
    layer_type: str,
    process: str,
    result: list[tuple[str, str, str, str]],
) -> None:
    if not isinstance(params, dict):
        return
    for arg_key, param_def in params.items():
        if not isinstance(param_def, dict):
            continue
        source = param_def.get("source", "")
        if isinstance(source, str) and source.startswith("parameter_group."):
            pg_key = source[len("parameter_group."):]
            result.append((layer_type, process, str(arg_key), pg_key))


def _check_array_item(
    item: Any,
    index: int,
    item_type: str | None,
    required_props: list[str],
    path: str,
) -> str | None:
    """Validate one array element; return an error string or None."""
    if item_type and not _json_type_matches(item, item_type):
        return (
            f"{path}[{index}]: expected item type {item_type!r}, "
            f"got {type(item).__name__} = {item!r}"
        )
    if item_type == "object" and isinstance(item, dict):
        for req in required_props:
            if req not in item:
                return f"{path}[{index}]: missing required property {req!r}"
    return None


def check_json_schema_value(
    value: Any,
    arg_schema: dict[str, Any],
    path: str,
) -> str | None:
    """Validate ``value`` against a JSON Schema ``arg_schema``.

    Returns an error string on mismatch, ``None`` if the value is valid.
    Only checks ``type`` and, for arrays, ``items.type`` and
    ``items.required`` properties. Unknown types pass silently.
    """
    expected_type = arg_schema.get("type")
    if not expected_type:
        return None
    if not _json_type_matches(value, expected_type):
        return (
            f"{path}: expected JSON type {expected_type!r}, "
            f"got {type(value).__name__} = {value!r}"
        )
    if expected_type == "array" and isinstance(value, list):
        items_schema = arg_schema.get("items")
        if isinstance(items_schema, dict):
            item_type = items_schema.get("type")
            required_props: list[str] = items_schema.get("required", [])
            for i, item in enumerate(value):
                err = _check_array_item(item, i, item_type, required_props, path)
                if err:
                    return err
    return None


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "boolean":
        return isinstance(value, bool)
    return True

# ── Re-exports from sub-modules ─────────────────────────────────────
# Public symbols moved to coherence-focused sub-modules; re-exported here
# so external callers can keep `from .pipeline_resolver import ...` paths.
from .pipeline_validators import (  # noqa: E402,F401
    validate_pipeline_spec_against_schema,
    validate_pipeline_spec_dict_against_schema,
    validate_pipeline_spec_raw_and_loaded,
)
from .wbs_generation import ProcessIOMap, generate_wbs  # noqa: E402,F401
