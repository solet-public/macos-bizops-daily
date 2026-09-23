# pyright: reportUnusedFunction=false
"""PipelineSpec validators extracted from ``pipeline_resolver``.

Three public entry points:

* ``validate_pipeline_spec_against_schema`` — loaded-spec validator.
* ``validate_pipeline_spec_dict_against_schema`` — raw-dict validator.
* ``validate_pipeline_spec_raw_and_loaded`` — runs both, raises
  ``FrameworkError`` on any error.

Helpers are pure; they only call back into ``pipeline_resolver`` for
the resolution/lookup primitives the validators need (``_resolve_param``,
``_build_arc_lookup``, etc.).  Imports are uni-directional: this
module pulls from ``pipeline_resolver``; ``pipeline_resolver``
re-exports the public validators from this module at its bottom.
"""

from __future__ import annotations

import logging
from typing import Any

from ananta.error_handling import FrameworkError

from .constants import ErrorCode
from .pipeline_resolver import (
    _CADENCE_CARRIER_LAYER_TYPE,
    _PHRASE_REQUIRED_KEYS,
    _PROSODIC_PHRASE_MODE,
    _build_arc_lookup,
    _layer_active,
    _merge_overrides_by_layer,
    _parse_db,
    _phrase_pattern_type,
    _promote_arcs_to_properties,
    _resolve_param,
)
from .pipeline_spec import (
    LayerConfig,
    ParameterGroup,
    PipelineSpec,
    ScheduledWindow,
    Segment,
    SegmentLayerOverride,
    pipeline_spec_from_dict,
)

logger = logging.getLogger(__name__)


def validate_pipeline_spec_against_schema(
    spec: PipelineSpec,
    schema: dict[str, Any],
) -> list[str]:
    """Validate a loaded ``PipelineSpec`` against its schema.

    Returns a list of error strings (empty if clean). **Never raises**;
    aggregation and raising is the caller's job (see
    ``validate_pipeline_spec_raw_and_loaded``).

    Checks: schema_id match; required piece_properties; non-empty
    parameter_groups; arcs cover every section; required
    segment_properties; parameter_group references; modulation
    assignments target known sections + allowed layers; layer_override
    targets known layer_types; for each active layer in each section,
    every required param's source resolves through the precedence
    chain (override → primary → schema default).
    """
    errors: list[str] = []
    errors.extend(_validate_schema_id(spec, schema))
    errors.extend(_validate_piece_properties(spec, schema))
    errors.extend(_validate_parameter_groups(spec))
    errors.extend(_validate_arcs_complete(spec, schema))
    errors.extend(_validate_sections(spec, schema))
    errors.extend(_validate_modulation_assignments(spec, schema))
    errors.extend(_validate_layer_overrides(spec, schema))
    errors.extend(_validate_active_layer_sources(spec, schema))
    return errors


def _validate_schema_id(
    spec: PipelineSpec, schema: dict[str, Any],
) -> list[str]:
    expected = str(schema.get("schema_id", ""))
    if spec.schema_id != expected:
        return [
            f"pipeline_spec.schema_id {spec.schema_id!r} does not "
            f"match loaded schema {expected!r}",
        ]
    return []


def _validate_piece_properties(
    spec: PipelineSpec, schema: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    for key, definition in schema.get("piece_properties", {}).items():
        if definition.get("required") and key not in spec.piece:
            errors.append(
                f"piece is missing required property {key!r} declared "
                f"in schema.piece_properties",
            )
    return errors


def _validate_parameter_groups(spec: PipelineSpec) -> list[str]:
    if not spec.parameter_groups:
        return ["spec.parameter_groups must be non-empty"]
    return []


def _clamp_enum_value(value: str, allowed: list[str]) -> str:
    """Snap an invalid enum value to the closest allowed value by token overlap.

    Splits both strings on '_' and picks the allowed entry with the most
    shared tokens.  Falls back to the first allowed value when nothing matches.
    """
    value_tokens = set(value.split("_"))
    best = allowed[0]
    best_score = -1
    for candidate in allowed:
        score = len(value_tokens & set(candidate.split("_")))
        if score > best_score:
            best_score = score
            best = candidate
    return best


def _validate_arcs_complete(
    spec: PipelineSpec, schema: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    section_names = {section.name for section in spec.sections}
    spec_arcs = {arc.name: arc for arc in spec.arcs}
    for arc_name, arc_defn in schema.get("arcs", {}).items():
        arc = spec_arcs.get(arc_name)
        if arc is None:
            errors.append(
                f"spec.arcs is missing arc {arc_name!r} declared in "
                f"schema.arcs",
            )
            continue
        missing = section_names - set(arc.values.keys())
        if missing:
            errors.append(
                f"arc {arc_name!r} is missing values for sections "
                f"{sorted(missing)!r}",
            )
        if arc_defn.get("type") == "enum":
            allowed = arc_defn.get("values", [])
            for section_name, value in arc.values.items():
                if value not in allowed:
                    clamped = _clamp_enum_value(value, allowed)
                    logger.warning(
                        "arc %r value %r for section %r not in allowed values %r"
                        " — clamping to %r",
                        arc_name, value, section_name, sorted(allowed), clamped,
                    )
                    arc.values[section_name] = clamped
    return errors


def _validate_sections(
    spec: PipelineSpec, schema: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    pg_labels = {pg.label for pg in spec.parameter_groups}
    required_segment_props = {
        key for key, defn in schema.get("segment_properties", {}).items()
        if defn.get("required")
    }
    for section in spec.sections:
        for prop in required_segment_props:
            if prop not in section.properties:
                errors.append(
                    f"section {section.name!r} is missing required "
                    f"property {prop!r}",
                )
        pg_ref = section.properties.get("parameter_group")
        if pg_ref is not None and str(pg_ref) not in pg_labels:
            errors.append(
                f"section {section.name!r} references parameter_group "
                f"{pg_ref!r} which is not declared in "
                f"spec.parameter_groups",
            )
    return errors


def _validate_modulation_assignments(
    spec: PipelineSpec, schema: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    section_names = {section.name for section in spec.sections}
    allowed_layers = set(
        schema.get("modulation", {}).get("applies_to", []) or [],
    )
    for ma in spec.modulation_assignments:
        if ma.section_name not in section_names:
            errors.append(
                f"modulation_assignment targets unknown section "
                f"{ma.section_name!r}",
            )
        if allowed_layers and ma.layer_type not in allowed_layers:
            errors.append(
                f"modulation_assignment targets layer "
                f"{ma.layer_type!r} which is not in "
                f"schema.modulation.applies_to",
            )
    return errors


def _validate_layer_overrides(
    spec: PipelineSpec, schema: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    declared_layers = set(schema.get("layer_types", {}).keys())
    for section in spec.sections:
        for override in section.layer_overrides:
            if override.layer_type not in declared_layers:
                errors.append(
                    f"section {section.name!r} layer_override targets "
                    f"layer_type {override.layer_type!r} which is not "
                    f"in schema.layer_types",
                )
    return errors


def _validate_active_layer_sources(
    spec: PipelineSpec, schema: dict[str, Any],
) -> list[str]:
    """Verify every active layer's required params resolve.

    For each section, evaluates which layers are active per the
    schema's ``active_when`` predicates. For each active layer, walks
    its ``params`` (always required) and ``post_processing[*].params``
    (required only when the step declares ``required: true``). For
    each param whose ``source`` references ``layer_config.<key>``, the
    validator walks the schema's source-precedence order (override
    paths first, then primary). One error per (section, layer) when
    the layer activates but has no matching ``layer_configs`` entry
    AND its required params reference ``layer_config.<key>``.
    """
    errors: list[str] = []
    pg_by_label = {pg.label: pg for pg in spec.parameter_groups}
    layer_config_by_type = {lc.layer_type: lc for lc in spec.layer_configs}
    section_arcs_by_name = _build_arc_lookup(spec.arcs)
    layer_types = schema.get("layer_types", {})
    if not isinstance(layer_types, dict):
        return errors
    for section in spec.sections:
        section_arcs = section_arcs_by_name.get(section.name, {})
        promoted = _promote_arcs_to_properties(section, section_arcs)
        merged = _merge_overrides_by_layer(promoted.layer_overrides)
        for layer_type, layer_def in layer_types.items():
            if not isinstance(layer_def, dict):
                continue
            try:
                if not _layer_active(layer_def, promoted, section_arcs):
                    continue
            except ValueError as exc:
                errors.append(
                    f"section {section.name!r} layer {layer_type!r} "
                    f"active_when evaluation failed: {exc}",
                )
                continue
            errors.extend(
                _validate_active_layer(
                    section.name,
                    layer_type,
                    layer_def,
                    promoted,
                    pg_by_label,
                    layer_config_by_type,
                    merged,
                    section_arcs,
                    schema,
                ),
            )
    return errors


def _validate_active_layer(
    section_name: str,
    layer_type: str,
    layer_def: dict[str, Any],
    section: Segment,
    pg_by_label: dict[str, ParameterGroup],
    layer_config_by_type: dict[str, LayerConfig],
    merged_overrides: dict[str, SegmentLayerOverride],
    section_arcs: dict[str, Any],
    schema: dict[str, Any],
) -> list[str]:
    """Per-layer source-resolution check; returns errors.

    Walks the param precedence chain (override → primary). When the
    layer activates with no ``layer_configs`` entry AND at least one
    required source fails to resolve, the per-param errors are
    folded into one summary error naming the missing layer_config —
    so the author sees the root cause rather than a list of per-param
    symptoms.

    Scheduled layers (those declaring ``scheduling: event_schedule``
    in the schema) are validated with a stub empty
    ``ScheduledWindow`` in scope so the
    ``event_schedule_entry.<field>`` override paths fall through to
    their primary sources without triggering misuse errors. The
    real per-window override values are not known until emission;
    the validator only checks the precedence chain's defaults.
    """
    layer_config = layer_config_by_type.get(layer_type)
    parameter_group: ParameterGroup | None = None
    pg_ref = section.properties.get("parameter_group")
    if pg_ref is not None:
        parameter_group = pg_by_label.get(str(pg_ref))
    override = merged_overrides.get(layer_type)
    if (
        layer_type == _CADENCE_CARRIER_LAYER_TYPE
        and _phrase_pattern_type(layer_config, override) == _PROSODIC_PHRASE_MODE
    ):
        return _validate_prosodic_cadence_layer(
            section_name, section, layer_config,
        )
    is_scheduled = bool(layer_def.get("scheduling"))
    stub_window: ScheduledWindow | None = (
        ScheduledWindow(index=0, t_start_s=0.0, t_end_s=0.0)
        if is_scheduled else None
    )
    context = f"section {section_name!r} layer {layer_type!r}"
    param_errors = _check_param_block_resolves(
        layer_def.get("params", {}),
        section, parameter_group, layer_config, override,
        section_arcs, schema, context, required=True,
        current_window=stub_window,
    ) + _check_post_processing_resolves(
        layer_def.get("post_processing", []),
        section, parameter_group, layer_config, override,
        section_arcs, schema, context,
        current_window=stub_window,
    )
    if not param_errors:
        return []
    if layer_config is None:
        return [
            f"{context}: spec.layer_configs has no entry for "
            f"{layer_type!r}; unresolved sources: "
            + "; ".join(param_errors),
        ]
    return param_errors


def _validate_prosodic_cadence_layer(
    section_name: str,
    section: Segment,
    layer_config: LayerConfig | None,
) -> list[str]:
    """Validate prosodic_phrase mode for the cadence_carrier layer."""
    errors: list[str] = []
    context = (
        f"section {section_name!r} layer "
        f"{_CADENCE_CARRIER_LAYER_TYPE!r} (prosodic_phrase mode)"
    )
    if layer_config is None:
        errors.append(
            f"{context}: spec.layer_configs has no entry for "
            f"{_CADENCE_CARRIER_LAYER_TYPE!r}",
        )
        return errors
    block = layer_config.properties.get("phrase_generation_params")
    if not isinstance(block, dict):
        errors.append(
            f"{context}: layer_config.phrase_generation_params is "
            f"required when phrase_pattern_type='prosodic_phrase'",
        )
        return errors
    missing = [k for k in _PHRASE_REQUIRED_KEYS if k not in block]
    if missing:
        errors.append(
            f"{context}: phrase_generation_params missing required "
            f"keys: {missing!r}",
        )
    if section.properties.get("duration_s") is None:
        errors.append(
            f"{context}: section.duration_s is required",
        )
    return errors


def _check_param_block_resolves(
    params: Any,
    section: Segment,
    parameter_group: ParameterGroup | None,
    layer_config: LayerConfig | None,
    override: SegmentLayerOverride | None,
    section_arcs: dict[str, Any],
    schema: dict[str, Any],
    context: str,
    *,
    required: bool,
    current_window: ScheduledWindow | None = None,
) -> list[str]:
    """Check every param source resolves; return errors when required."""
    if not isinstance(params, dict):
        return []
    errors: list[str] = []
    for arg, spec_value in params.items():
        if _param_source_resolves(
            spec_value, section, parameter_group, layer_config,
            override, section_arcs, schema, current_window,
        ):
            continue
        if required:
            errors.append(_format_unresolved_param(context, arg, spec_value))
    return errors


def _format_unresolved_param(context: str, arg: str, spec_value: Any) -> str:
    """Build a per-param 'no source resolves' message with source hint."""
    source_hint = ""
    if isinstance(spec_value, dict):
        source = spec_value.get("source")
        if isinstance(source, str):
            source_hint = f" (source {source!r})"
    return f"{context} param {arg!r}{source_hint}: no source resolves"


def _check_post_processing_resolves(
    steps: Any,
    section: Segment,
    parameter_group: ParameterGroup | None,
    layer_config: LayerConfig | None,
    override: SegmentLayerOverride | None,
    section_arcs: dict[str, Any],
    schema: dict[str, Any],
    context: str,
    *,
    current_window: ScheduledWindow | None = None,
) -> list[str]:
    if not isinstance(steps, list):
        return []
    errors: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_required = bool(step.get("required"))
        process = str(step.get("process", "?"))
        errors.extend(_check_param_block_resolves(
            step.get("params", {}),
            section, parameter_group, layer_config, override,
            section_arcs, schema,
            f"{context} step {process!r}",
            required=step_required,
            current_window=current_window,
        ))
    return errors


def _param_source_resolves(
    spec_value: Any,
    section: Segment,
    parameter_group: ParameterGroup | None,
    layer_config: LayerConfig | None,
    override: SegmentLayerOverride | None,
    section_arcs: dict[str, Any],
    schema: dict[str, Any],
    current_window: ScheduledWindow | None = None,
) -> bool:
    """Mirror of ``_resolve_param`` that returns True/False instead of value."""
    if not isinstance(spec_value, dict) or "source" not in spec_value:
        return True
    try:
        _resolve_param(
            spec_value, section, parameter_group, layer_config, override,
            section_arcs, schema, field_name="",
            current_window=current_window,
        )
    except ValueError:
        return False
    return True


# ── Raw-spec validator + public wrapper ────────────────────────────────


_ALLOWED_SECTION_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {"name", "properties", "layer_overrides"},
)

_AUDIBILITY_FIELDS_BY_ROLE: dict[str, tuple[tuple[str, str, bool], ...]] = {
    # (lc_key, cap_key, is_db_value)
    "foreground": (
        ("level_db", "level_db", True),
        ("delay_mix", "delay_mix_max", False),
        ("delay_feedback", "delay_feedback_max", False),
    ),
    "midground": (
        ("level_db", "level_db", True),
        ("delay_mix", "delay_mix_max", False),
        ("delay_feedback", "delay_feedback_max", False),
    ),
    "background": (
        ("level_db", "level_db", True),
        ("delay_mix", "delay_mix_max", False),
        ("delay_feedback", "delay_feedback_max", False),
    ),
    "submerged": (
        ("level_db", "level_db", True),
        ("delay_mix", "delay_mix_max", False),
        ("delay_feedback", "delay_feedback_max", False),
    ),
}


def validate_pipeline_spec_dict_against_schema(
    raw: dict[str, Any],
    schema: dict[str, Any],
) -> list[str]:
    """Validate a raw (JSON-decoded) pipeline_spec dict against the schema.

    Returns a list of error strings (empty if clean). **Never raises.**

    Catches the bug class the loaded validator cannot see: misplaced
    section keys (``event_schedule`` at section top level instead of
    under ``properties``), invalid window bounds, unknown layer types
    in event_schedule entries, ``foreground`` enum violations
    (including empty string and null), and audibility-cap violations
    on ``layer_configs[*].properties``.
    """
    errors: list[str] = []
    sections = raw.get("sections", [])
    if isinstance(sections, list):
        for index, section in enumerate(sections):
            if not isinstance(section, dict):
                continue
            errors.extend(
                _validate_raw_section(index, section, schema),
            )
    errors.extend(_validate_raw_layer_configs(raw, schema))
    return errors


_STATIC_STASIS_LIMIT_S: float = 120.0


def _validate_raw_section(
    index: int,
    section: dict[str, Any],
    schema: dict[str, Any],
) -> list[str]:
    """Check one raw section's top-level shape and timeline entries."""
    errors: list[str] = []
    section_name = str(section.get("name", f"<index {index}>"))
    segment_props = schema.get("segment_properties", {}) or {}
    if not isinstance(segment_props, dict):
        segment_props = {}
    properties = section.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    duration_s = _read_section_duration(properties)
    errors.extend(
        _validate_section_top_level_keys(
            index, section_name, section, segment_props,
        ),
    )
    errors.extend(_validate_section_foreground(
        section_name, properties, segment_props,
    ))
    errors.extend(_validate_event_schedule_block(
        section_name, properties.get("event_schedule"),
        duration_s, schema,
    ))
    errors.extend(_validate_parameter_group_sequence_block(
        section_name, properties.get("parameter_group_sequence"),
        duration_s,
    ))
    errors.extend(_validate_static_stasis_rule(
        section_name, properties, duration_s,
    ))
    return errors


def _validate_static_stasis_rule(
    section_name: str,
    properties: dict[str, Any],
    duration_s: float | None,
) -> list[str]:
    """Reject long sections that have no internal motion source.

    A section longer than ``_STATIC_STASIS_LIMIT_S`` must declare at
    least one of: a non-empty ``parameter_group_sequence`` (in-section
    harmonic motion), a non-empty ``event_schedule`` (timed events),
    OR a ``foreground`` (continuous foreground voice). Otherwise the
    section is a sustained chord pad with no internal motion — the
    "no static stasis" violation from the brief.
    """
    if duration_s is None or duration_s <= _STATIC_STASIS_LIMIT_S:
        return []
    pgs = properties.get("parameter_group_sequence")
    es = properties.get("event_schedule")
    fg = properties.get("foreground")
    has_pgs = isinstance(pgs, list) and len(pgs) > 0
    has_es = isinstance(es, list) and len(es) > 0
    has_fg = isinstance(fg, str) and bool(fg)
    if has_pgs or has_es or has_fg:
        return []
    return [
        f"section {section_name!r}: duration_s {duration_s} > "
        f"{_STATIC_STASIS_LIMIT_S} s but no parameter_group_sequence, "
        f"no event_schedule, and no foreground — long sections must "
        f"declare an internal motion source",
    ]


def _read_section_duration(properties: dict[str, Any]) -> float | None:
    raw_duration = properties.get("duration_s")
    if raw_duration is None:
        return None
    try:
        return float(raw_duration)
    except (TypeError, ValueError):
        return None


def _validate_section_top_level_keys(
    index: int,
    section_name: str,
    section: dict[str, Any],
    segment_props: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    for key in section.keys():
        if key in _ALLOWED_SECTION_TOP_LEVEL_KEYS:
            continue
        if key in segment_props:
            errors.append(
                f"section {section_name!r} (sections[{index}]) has "
                f"misplaced segment property {key!r} at the section "
                f"top level — should be at "
                f"sections[{index}].properties.{key}",
            )
        else:
            errors.append(
                f"section {section_name!r} (sections[{index}]) has "
                f"unknown top-level key {key!r}; allowed keys are "
                f"{sorted(_ALLOWED_SECTION_TOP_LEVEL_KEYS)!r}",
            )
    return errors


def _validate_section_foreground(
    section_name: str,
    properties: dict[str, Any],
    segment_props: dict[str, Any],
) -> list[str]:
    if "foreground" not in properties:
        return []
    foreground_def = segment_props.get("foreground", {})
    if not isinstance(foreground_def, dict):
        return []
    enum = foreground_def.get("enum")
    if not isinstance(enum, list):
        return []
    value = properties["foreground"]
    if value in enum:
        return []
    return [
        f"section {section_name!r}: foreground value {value!r} is "
        f"not in the allowed enum {enum!r} — omit the field entirely "
        f"when the section has no foreground (do not set to empty "
        f"string or null)",
    ]


def _validate_event_schedule_block(
    section_name: str,
    entries: Any,
    duration_s: float | None,
    schema: dict[str, Any],
) -> list[str]:
    if entries is None:
        return []
    if not isinstance(entries, list):
        return [
            f"section {section_name!r}: event_schedule must be a "
            f"list, got {type(entries).__name__}",
        ]
    layer_types = schema.get("layer_types", {}) or {}
    errors: list[str] = []
    for i, entry in enumerate(entries):
        path = f"section {section_name!r} event_schedule[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{path}: must be an object")
            continue
        errors.extend(_validate_window_bounds(path, entry, duration_s))
        errors.extend(_validate_event_schedule_entry_layer(
            path, entry, layer_types,
        ))
    return errors


def _validate_event_schedule_entry_layer(
    path: str,
    entry: dict[str, Any],
    layer_types: dict[str, Any],
) -> list[str]:
    layer_type = entry.get("layer_type")
    if not isinstance(layer_type, str) or not layer_type:
        return [f"{path}: missing required string 'layer_type'"]
    layer_def = layer_types.get(layer_type)
    if not isinstance(layer_def, dict):
        return [
            f"{path}: layer_type {layer_type!r} is not declared in "
            f"schema.layer_types",
        ]
    scheduling = layer_def.get("scheduling")
    if scheduling != "event_schedule":
        return [
            f"{path}: layer_type {layer_type!r} does not declare "
            f"scheduling: event_schedule in the schema",
        ]
    return []


def _validate_parameter_group_sequence_block(
    section_name: str,
    entries: Any,
    duration_s: float | None,
) -> list[str]:
    if entries is None:
        return []
    if not isinstance(entries, list):
        return [
            f"section {section_name!r}: parameter_group_sequence must "
            f"be a list, got {type(entries).__name__}",
        ]
    errors: list[str] = []
    for i, entry in enumerate(entries):
        path = f"section {section_name!r} parameter_group_sequence[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{path}: must be an object")
            continue
        errors.extend(_validate_window_bounds(path, entry, duration_s))
        if not isinstance(entry.get("parameter_group"), str):
            errors.append(
                f"{path}: missing required string 'parameter_group'",
            )
    return errors


def _validate_window_bounds(
    path: str,
    entry: dict[str, Any],
    duration_s: float | None,
) -> list[str]:
    t_start = _read_float(entry.get("t_start_s"))
    t_end = _read_float(entry.get("t_end_s"))
    return (
        _check_window_field_present(path, "t_start_s", t_start)
        + _check_window_field_present(path, "t_end_s", t_end)
        + _check_window_ordering(path, t_start, t_end, duration_s)
    )


def _check_window_field_present(
    path: str, field_name: str, value: float | None,
) -> list[str]:
    if value is None:
        return [f"{path}: missing or non-numeric {field_name!r}"]
    return []


def _check_window_ordering(
    path: str,
    t_start: float | None,
    t_end: float | None,
    duration_s: float | None,
) -> list[str]:
    errors: list[str] = []
    if t_start is not None and t_start < 0:
        errors.append(f"{path}: t_start_s must be >= 0, got {t_start}")
    if t_start is not None and t_end is not None and t_start >= t_end:
        errors.append(
            f"{path}: t_start_s ({t_start}) must be < "
            f"t_end_s ({t_end})",
        )
    if t_end is not None and duration_s is not None and t_end > duration_s:
        errors.append(
            f"{path}: t_end_s ({t_end}) exceeds section duration_s "
            f"({duration_s})",
        )
    return errors


def _read_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _validate_raw_layer_configs(
    raw: dict[str, Any],
    schema: dict[str, Any],
) -> list[str]:
    """Audibility-cap enforcement on layer_configs[*].properties."""
    layer_types = schema.get("layer_types", {}) or {}
    caps = (
        schema.get("audibility_caps", {}) or {}
    ).get("by_role", {}) or {}
    if not isinstance(layer_types, dict) or not isinstance(caps, dict):
        return []
    items = _enumerate_layer_config_items(raw.get("layer_configs", []))
    errors: list[str] = []
    for layer_type, properties in items:
        if not isinstance(properties, dict):
            continue
        errors.extend(_validate_layer_config_caps(
            str(layer_type), properties, layer_types, caps,
        ))
    return errors


def _enumerate_layer_config_items(
    layer_configs: Any,
) -> list[tuple[Any, Any]]:
    """Yield ``(layer_type, properties)`` pairs from either input shape."""
    if isinstance(layer_configs, dict):
        return list(layer_configs.items())
    if isinstance(layer_configs, list):
        return [
            (lc.get("layer_type"), lc.get("properties", {}))
            for lc in layer_configs if isinstance(lc, dict)
        ]
    return []


def _validate_layer_config_caps(
    layer_type: str,
    properties: dict[str, Any],
    layer_types: dict[str, Any],
    caps: dict[str, Any],
) -> list[str]:
    """Per-layer_config audibility-cap check."""
    layer_def = layer_types.get(layer_type)
    if not isinstance(layer_def, dict):
        return []
    role = layer_def.get("mix_role")
    if not isinstance(role, str):
        return []
    role_caps = caps.get(role)
    if not isinstance(role_caps, dict):
        return []
    fields = _AUDIBILITY_FIELDS_BY_ROLE.get(role, ())
    errors: list[str] = []
    for lc_key, _cap_prefix, is_db in fields:
        if lc_key not in properties:
            continue
        raw_value = properties[lc_key]
        try:
            value = _parse_db(raw_value) if is_db else float(raw_value)
        except (TypeError, ValueError) as exc:
            errors.append(
                f"layer_config {layer_type!r} property {lc_key!r}: "
                f"{exc}",
            )
            continue
        errors.extend(_check_cap(
            layer_type, role, lc_key, value, role_caps, is_db,
        ))
    return errors


def _check_cap(
    layer_type: str,
    role: str,
    lc_key: str,
    value: float,
    role_caps: dict[str, Any],
    is_db: bool,
) -> list[str]:
    if is_db:
        lo = role_caps.get("level_db_min")
        hi = role_caps.get("level_db_max")
        if isinstance(lo, (int, float)) and value < float(lo):
            return [
                f"layer_config {layer_type!r} (role {role!r}) "
                f"{lc_key} {value} dB is below the role floor "
                f"{lo} dB",
            ]
        if isinstance(hi, (int, float)) and value > float(hi):
            return [
                f"layer_config {layer_type!r} (role {role!r}) "
                f"{lc_key} {value} dB is above the role ceiling "
                f"{hi} dB",
            ]
        return []
    cap = role_caps.get(f"{lc_key}_max")
    if isinstance(cap, (int, float)) and value > float(cap):
        return [
            f"layer_config {layer_type!r} (role {role!r}) "
            f"{lc_key} {value} exceeds the role cap {cap}",
        ]
    return []


def validate_pipeline_spec_raw_and_loaded(
    raw: dict[str, Any],
    schema: dict[str, Any],
) -> None:
    """Public validator entry point: aggregate raw + loaded errors.

    Calls the raw validator, then loads the spec, then calls the
    loaded validator. Aggregates all errors and raises a single
    ``FrameworkError`` with one error per line if any are found.

    The raw validator catches misplaced section keys (``event_schedule``
    at section top level) that the loader silently drops; the loaded
    validator catches missing-layer_config and other shape problems
    visible only after parsing.
    """
    raw_errors = validate_pipeline_spec_dict_against_schema(raw, schema)
    try:
        spec = pipeline_spec_from_dict(raw)
    except (ValueError, KeyError, TypeError) as exc:
        prefix = ""
        if raw_errors:
            prefix = "\n".join(raw_errors) + "\n"
        raise FrameworkError(
            message=f"{prefix}pipeline_spec failed to load: {exc}",
            error_code=ErrorCode.PARAMETER_ERROR,
        ) from exc
    if not spec.schema_id:
        spec.schema_id = str(schema.get("schema_id", ""))
    loaded_errors = validate_pipeline_spec_against_schema(spec, schema)
    all_errors = raw_errors + loaded_errors
    if all_errors:
        raise FrameworkError(
            message="pipeline_spec validation failed:\n"
                    + "\n".join(all_errors),
            error_code=ErrorCode.PARAMETER_ERROR,
        )

