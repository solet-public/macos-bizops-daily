"""Layer-aware retrieval helpers.

The knowledge service ``search`` action accepts four layer-related
parameters:

- ``knowledge_layers``: list of allowed layer integers (exact set).
- ``min_knowledge_layer`` / ``max_knowledge_layer``: range constraints.
- ``include_unlayered``: when true, chunks without a layer tag are also
  returned.

This module owns:

- ``LayerConstraint``: the validated, normalized constraint passed
  through the search pipeline.
- ``build_layer_constraint``: validation, range expansion with a hard
  fan-out cap, and the rejection of ambiguous parameter combinations.

The constraint is *strict* across every retrieval tier (process-key,
user-tag, semantic, diversity-fill). The implementation runs one recall
per allowed layer tag and merges; we never pass multiple layer tags
into a single recall because memory tag filters use AND semantics.

The validation rule on individual layer values is ``value >= 1``. The
system supports any positive integer; new layers are added by editing
the layer registry article and the ``Article Layer:`` annotation
pipeline. The fan-out cap (``DEFAULT_RANGE_SPAN_CAP``) prevents an
absurd ``max_knowledge_layer=100000`` from spawning 100000 recall
calls.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    TAG_LAYER_UNLABELED,
    knowledge_layer_tag,
)

DEFAULT_RANGE_SPAN_CAP = 16
"""Maximum number of distinct layer integers allowed in one query.

Range expansion (``min_knowledge_layer=2, max_knowledge_layer=N``) that
would produce more than this many recall calls raises a
``ValueError`` rather than fanning out. Callers asking for such a wide
span should pass ``knowledge_layers`` explicitly with a smaller list,
or narrow the range.
"""


@dataclass(frozen=True)
class LayerConstraint:
    """Validated and normalized layer filter for a single search call.

    ``allowed_layers`` is None when the search has no layer constraint
    (the legacy backward-compatible behavior — return any layer
    including unlabeled). When non-None, the search returns only
    chunks tagged with one of the listed layers, plus the unlabeled
    chunks if ``include_unlayered`` is true.
    """

    allowed_layers: tuple[int, ...] | None = None
    include_unlayered: bool = False

    @property
    def active(self) -> bool:
        """True if any layer-related filter applies to this search."""
        return self.allowed_layers is not None

    def tag_alternatives(self) -> list[str]:
        """Return one chunk-tag string per allowed-layer recall pass.

        Memory recall tag filters require ALL supplied tags. To express
        ``layer in {1, 2}`` we run two recalls — one with
        ``knowledge:layer:1`` and one with ``knowledge:layer:2`` — and
        merge the results. This helper enumerates the per-pass tag
        strings.

        When ``include_unlayered`` is true and the constraint is active,
        the unlabeled tag is appended; an extra recall surfaces chunks
        that have no layer annotation.
        """
        if self.allowed_layers is None:
            return []
        out: list[str] = [knowledge_layer_tag(n) for n in self.allowed_layers]
        if self.include_unlayered:
            out.append(TAG_LAYER_UNLABELED)
        return out

    def matches(self, layer: int | None) -> bool:
        """True if a single chunk's layer satisfies the constraint.

        Used by the final defensive filter — even if every recall tier
        respected the constraint, we double-check the formatted result
        list before returning it. Catches accidental leakage if a tier
        is missed.
        """
        if not self.active:
            return True
        if layer is None:
            return self.include_unlayered
        return self.allowed_layers is not None and layer in self.allowed_layers


def build_layer_constraint(
    knowledge_layers: list[int] | None = None,
    min_knowledge_layer: int | None = None,
    max_knowledge_layer: int | None = None,
    include_unlayered: bool = False,
    span_cap: int = DEFAULT_RANGE_SPAN_CAP,
) -> LayerConstraint:
    """Validate and normalize the four layer-related search arguments.

    Raises ``ValueError`` for any of:

    - non-integer or non-positive layer values;
    - ``min_knowledge_layer > max_knowledge_layer``;
    - both ``knowledge_layers`` and ``min_knowledge_layer``/
      ``max_knowledge_layer`` supplied (ambiguous combination);
    - a range whose width exceeds ``span_cap``.

    When no layer arguments are supplied the returned constraint has
    ``allowed_layers=None`` and ``active=False`` — backward-compatible
    no-op.
    """
    has_exact = knowledge_layers is not None
    has_range = (
        min_knowledge_layer is not None or max_knowledge_layer is not None
    )

    if has_exact and has_range:
        raise ValueError(
            "knowledge_layers and min_knowledge_layer/max_knowledge_layer "
            "cannot be combined; pass exactly one form"
        )

    if not has_exact and not has_range:
        # Possibly include_unlayered=True alone, but without any allowed
        # layers there is no constraint to enforce — return the
        # backward-compatible no-op constraint.
        return LayerConstraint()

    if has_exact:
        assert knowledge_layers is not None  # narrowed by has_exact above
        return _build_constraint_from_exact_layers(
            knowledge_layers, include_unlayered, span_cap,
        )

    return _build_constraint_from_range(
        min_knowledge_layer, max_knowledge_layer, include_unlayered, span_cap,
    )


def _build_constraint_from_exact_layers(
    knowledge_layers: list[int],
    include_unlayered: bool,
    span_cap: int,
) -> LayerConstraint:
    """Build a constraint from an explicit list of layer integers."""
    if len(knowledge_layers) == 0:
        raise ValueError(
            "knowledge_layers must be a non-empty list of positive integers"
        )
    layers: list[int] = []
    for raw in knowledge_layers:
        value = _coerce_positive_int(raw, "knowledge_layers entry")
        if value not in layers:
            layers.append(value)
    if len(layers) > span_cap:
        raise ValueError(
            f"knowledge_layers has {len(layers)} entries; cap is {span_cap}"
        )
    return LayerConstraint(
        allowed_layers=tuple(sorted(layers)),
        include_unlayered=include_unlayered,
    )


def _build_constraint_from_range(
    min_knowledge_layer: int | None,
    max_knowledge_layer: int | None,
    include_unlayered: bool,
    span_cap: int,
) -> LayerConstraint:
    """Build a constraint from a min/max layer range."""
    lo = _coerce_positive_int(
        min_knowledge_layer if min_knowledge_layer is not None else 1,
        "min_knowledge_layer",
    )
    if max_knowledge_layer is None:
        # An open-ended range (only ``min_knowledge_layer`` supplied) cannot
        # be expanded without knowing the upper bound. Reject explicitly so
        # callers tighten the request rather than silently fanning out.
        raise ValueError(
            "max_knowledge_layer is required when min_knowledge_layer is set; "
            "open-ended ranges are not supported"
        )
    hi = _coerce_positive_int(max_knowledge_layer, "max_knowledge_layer")
    if lo > hi:
        raise ValueError(
            f"min_knowledge_layer ({lo}) must not exceed max_knowledge_layer ({hi})"
        )
    span = hi - lo + 1
    if span > span_cap:
        raise ValueError(
            f"layer range {lo}..{hi} spans {span} layers; cap is {span_cap}. "
            "Narrow the range or pass knowledge_layers explicitly."
        )
    return LayerConstraint(
        allowed_layers=tuple(range(lo, hi + 1)),
        include_unlayered=include_unlayered,
    )


def _coerce_positive_int(raw: object, what: str) -> int:
    """Validate ``raw`` is a positive integer; raise ValueError otherwise."""
    if isinstance(raw, bool):
        raise ValueError(f"{what} must be an integer; got bool {raw!r}")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        try:
            value = int(raw.strip())
        except ValueError as exc:
            raise ValueError(
                f"{what} must parse as a positive integer; got {raw!r}"
            ) from exc
    else:
        raise ValueError(
            f"{what} must be a positive integer; got {type(raw).__name__} {raw!r}"
        )
    if value < 1:
        raise ValueError(f"{what} must be a positive integer; got {value!r}")
    return value
