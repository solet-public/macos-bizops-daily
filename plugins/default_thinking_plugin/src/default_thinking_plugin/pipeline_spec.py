"""Pipeline specification dataclasses.

A PipelineSpec captures the piece-level decisions a thinking model makes
for a per-section pipeline phase. The pipeline_resolver derives concrete
SegmentLayer instances from a spec plus a pipeline schema, and the
WBS generator emits executable steps from the resolved spec.

The dataclasses are deliberately generic — no genre-specific named
fields. Style-family-specific concepts (substrate, harmonic_bed,
binaural beat_hz, IR name) live in the schema and in generic
properties dicts on the dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParameterGroup:
    """A named set of musical parameters (a harmonic region, voice profile, etc.).

    Sections reference a parameter group by ``label``. The schema's
    ``parameter_groups.properties`` definition declares which keys are
    expected in ``properties``; ``root_hz`` and ``intervals_semitones``
    are surfaced as named fields because the resolver looks them up
    directly when a layer's ``params.source`` resolves to
    ``parameter_group.<key>``.
    """

    label: str
    root_hz: float
    intervals_semitones: list[int]
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParameterArc:
    """A named parameter curve over sections.

    ``values`` maps section_name -> value. The value type depends on
    the arc definition in the pipeline schema (enum, float, etc.).
    """

    name: str
    values: dict[str, Any] = field(default_factory=dict)


@dataclass
class LayerConfig:
    """Piece-level configuration for a layer type.

    ``properties`` holds layer-type-specific values (e.g.,
    ``carrier_hz`` for rhythmic_pulse, ``program`` for harmonic_bed).
    The schema defines what properties each layer type expects.
    """

    layer_type: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class SegmentLayerOverride:
    """Per-section override for a specific layer's parameters.

    Overrides the piece-level LayerConfig values for one section.
    Only the overridden keys are present — others fall through to the
    LayerConfig defaults.
    """

    layer_type: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class SegmentLayer:
    """One resolved layer within one section.

    Output of the resolver, not an input from the thinking model. The
    resolver populates these from the schema, arcs, layer configs, and
    section overrides.

    Per-layer processing (modulation, stagger, post-effects) lives in
    ``post_processing`` as ordered (process_key, params) entries — no
    special fields for genre-specific concepts.
    """

    layer_type: str
    process: str
    source_type: str = "generate"  # "generate" or "reference"
    params: dict[str, Any] = field(default_factory=dict)
    post_processing: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list,
    )


@dataclass
class ModulationAssignment:
    """Per-section per-layer modulation assignment.

    The thinking model provides these to assign different modulation
    types and rates per section per layer. The resolver attaches each
    assignment to the corresponding SegmentLayer's ``post_processing``
    chain.
    """

    section_name: str
    layer_type: str
    process: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScheduledWindow:
    """One window of a scheduled-window primitive.

    Represents a single ``event_schedule`` entry (or a single
    ``parameter_group_sequence`` entry). The resolver groups windows
    by layer_type into ``ResolvedScheduledLayer`` instances.

    ``index`` is the entry's position within its containing
    section_property list, used to disambiguate same-time windows in
    deterministic filenames.

    ``overrides`` is the per-window ``{key: value}`` map of any
    additional fields beyond ``layer_type``/``t_start_s``/``t_end_s``
    (e.g. ``note_count``, ``level_db``, ``label``). Resolver lookups
    in scheduled-window scope read this via the
    ``event_schedule_entry.<field>`` namespace.
    """

    index: int
    t_start_s: float
    t_end_s: float
    overrides: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResolvedScheduledLayer:
    """A scheduled layer resolved into its windows for one section.

    Output of the resolver, parallel to ``SegmentLayer`` for
    section-spanning layers. Phase B's emission method walks
    ``windows`` and produces one source chain plus one ``ffmpeg_adelay``
    per window, then a single ``ffmpeg_amix`` joining the delayed
    stems.
    """

    layer_type: str
    windows: tuple[ScheduledWindow, ...] = ()


@dataclass
class Segment:
    """One section of the composition.

    ``properties`` holds section-level values defined by the schema's
    ``segment_properties`` (duration_s, parameter_group, foreground,
    etc.). No genre-specific named fields.

    ``layer_overrides`` holds per-section parameter overrides for
    specific layers. The thinking model uses these when a layer's
    parameter genuinely varies per section (e.g., rhythmic_pulse beat_hz
    changes across sections, or a layer's stagger
    timing differs per section).

    ``layers`` is populated by the resolver — not by the thinking
    model. Holds section-spanning layers only.

    ``scheduled_layers`` is populated by the resolver alongside
    ``layers``. Holds layers whose schema entry declares
    ``scheduling: event_schedule`` (or analogous); these render as
    timed windows rather than spanning the section.
    """

    name: str
    properties: dict[str, Any] = field(default_factory=dict)
    layer_overrides: list[SegmentLayerOverride] = field(
        default_factory=list,
    )
    layers: list[SegmentLayer] = field(default_factory=list)
    scheduled_layers: tuple[ResolvedScheduledLayer, ...] = ()


@dataclass
class PipelineSpec:
    """The pipeline specification.

    The thinking model fills: ``schema_id``, ``piece``,
    ``parameter_groups``, ``arcs``, ``layer_configs``, ``sections``
    (name + properties + layer_overrides), and
    ``modulation_assignments``.

    The resolver fills: ``sections[*].layers`` with concrete
    ``SegmentLayer`` instances derived from the schema.
    """

    schema_id: str = ""
    piece: dict[str, Any] = field(default_factory=dict)
    parameter_groups: list[ParameterGroup] = field(default_factory=list)
    arcs: list[ParameterArc] = field(default_factory=list)
    layer_configs: list[LayerConfig] = field(default_factory=list)
    sections: list[Segment] = field(default_factory=list)
    modulation_assignments: list[ModulationAssignment] = field(
        default_factory=list,
    )


def pipeline_spec_from_dict(data: dict[str, Any]) -> PipelineSpec:
    """Reconstruct a PipelineSpec from a JSON-decoded dict.

    Validation of required fields and cross-references is the
    resolver's responsibility. This loader only converts shape.

    Accepts two input shapes for ``arcs[i].values``:

    1. **Dict** keyed by section name (canonical):
       ``{"orientation": "low", "induction": "medium", ...}``
    2. **List** positional, in section order:
       ``["low", "medium", ...]`` — zipped with ``data["sections"]``
       in declaration order.

    Accepts two input shapes for ``layer_configs``:

    1. **List of records** (canonical):
       ``[{"layer_type": "substrate", "properties": {...}}, ...]``
    2. **Dict** keyed by layer_type:
       ``{"substrate": {...}, "harmonic_bed": {...}}``

    Both alternative shapes are reasonable readings of the schema and
    the thinking model emits either depending on prompt phrasing.
    """
    parameter_groups = [
        ParameterGroup(
            label=str(pg["label"]),
            root_hz=float(pg["root_hz"]),
            intervals_semitones=list(pg.get("intervals_semitones", [])),
            properties=dict(pg.get("properties", {})),
        )
        for pg in data.get("parameter_groups", [])
    ]
    section_names_in_order = [
        str(sec.get("name", "")) for sec in data.get("sections", [])
    ]
    arcs = _normalize_arcs(data.get("arcs", []), section_names_in_order)
    layer_configs = _normalize_layer_configs(data.get("layer_configs", []))
    sections: list[Segment] = []
    for sec in data.get("sections", []):
        overrides = [
            SegmentLayerOverride(
                layer_type=str(o["layer_type"]),
                properties=dict(o.get("properties", {})),
            )
            for o in sec.get("layer_overrides", [])
        ]
        sections.append(
            Segment(
                name=str(sec["name"]),
                properties=dict(sec.get("properties", {})),
                layer_overrides=overrides,
            )
        )
    modulation_assignments = [
        ModulationAssignment(
            section_name=str(m["section_name"]),
            layer_type=str(m["layer_type"]),
            process=str(m["process"]),
            params=dict(m.get("params", {})),
        )
        for m in data.get("modulation_assignments", [])
    ]
    return PipelineSpec(
        schema_id=str(data.get("schema_id", "")),
        piece=dict(data.get("piece", {})),
        parameter_groups=parameter_groups,
        arcs=arcs,
        layer_configs=layer_configs,
        sections=sections,
        modulation_assignments=modulation_assignments,
    )


def _normalize_arcs(
    raw: Any,
    section_names_in_order: list[str],
) -> list[ParameterArc]:
    """Coerce ``arcs`` into a list of ParameterArc records.

    Accepts either a list of ``{"name": ..., "values": {...}}``
    (canonical) or a dict mapping arc_name to its values dict.
    Both shapes are reasonable outputs from the thinking model.
    """
    if isinstance(raw, list):
        return [
            ParameterArc(
                name=str(arc["name"]),
                values=_normalize_arc_values(
                    arc.get("values", {}), section_names_in_order, str(arc["name"]),
                ),
            )
            for arc in raw
        ]
    if isinstance(raw, dict):
        return [
            ParameterArc(
                name=str(name),
                values=_normalize_arc_values(values, section_names_in_order, str(name)),
            )
            for name, values in raw.items()
        ]
    raise ValueError(
        f"arcs must be a list of records or a dict keyed by arc name; "
        f"got {type(raw).__name__}",
    )


def _normalize_arc_values(
    raw: Any,
    section_names_in_order: list[str],
    arc_name: str,
) -> dict[str, Any]:
    """Coerce arc ``values`` into a section-keyed dict.

    Accepts either a dict (already section-keyed) or a list (positional,
    zipped with ``section_names_in_order``).
    """
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, list):
        if len(raw) != len(section_names_in_order):
            raise ValueError(
                f"arc {arc_name!r}: positional values list has "
                f"{len(raw)} entries but spec.sections has "
                f"{len(section_names_in_order)} sections — "
                f"counts must match for positional shape, or use a "
                f"section-keyed dict",
            )
        return dict(zip(section_names_in_order, raw, strict=True))
    raise ValueError(
        f"arc {arc_name!r}: values must be a dict (section_name -> "
        f"value) or a list (positional in section order); got "
        f"{type(raw).__name__}",
    )


def _normalize_layer_configs(raw: Any) -> list[LayerConfig]:
    """Coerce layer_configs into a list of LayerConfig records.

    Accepts either a list of ``{"layer_type": ..., "properties": {...}}``
    (canonical) or a dict mapping ``layer_type`` to its properties.
    """
    if isinstance(raw, list):
        return [
            LayerConfig(
                layer_type=str(lc["layer_type"]),
                properties=dict(lc.get("properties", {})),
            )
            for lc in raw
        ]
    if isinstance(raw, dict):
        return [
            LayerConfig(
                layer_type=str(layer_type),
                properties=dict(props) if isinstance(props, dict) else {},
            )
            for layer_type, props in raw.items()
        ]
    raise ValueError(
        f"layer_configs must be a list of records or a dict keyed by "
        f"layer_type; got {type(raw).__name__}",
    )
