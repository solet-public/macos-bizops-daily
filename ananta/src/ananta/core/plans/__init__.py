"""Canonical plan parser and normalization.

Single source of truth for plan text interpretation.  All plan consumers
(thinking plugin, inference plugin, context stage) import from here.
"""

from __future__ import annotations

from ananta.core.plans.parser import (
    advance_plan_markers,
    build_skip_set,
    normalize_content,
    normalize_for_completed_step,
    normalize_for_new_plan_install,
    parse,
    preserve_existing_markers,
    render_plan_steps,
    validate_planning_extension_rewrite,
)
from ananta.core.plans.types import (
    COMPANION_SUFFIXES,
    BoundSubStep,
    ComposedReference,
    LayerPolicy,
    ParsedPlan,
    ParsedPlanStep,
)
from ananta.core.plans.windowing import (
    build_plan_window,
    extract_playbook_ref_from_plan,
    has_completed_wbs_before_step,
    has_wbs_execution_steps,
    render_plan_sections,
)

__all__ = [
    "BoundSubStep",
    "COMPANION_SUFFIXES",
    "ComposedReference",
    "LayerPolicy",
    "ParsedPlan",
    "ParsedPlanStep",
    "advance_plan_markers",
    "build_plan_window",
    "build_skip_set",
    "extract_playbook_ref_from_plan",
    "has_completed_wbs_before_step",
    "has_wbs_execution_steps",
    "normalize_content",
    "normalize_for_completed_step",
    "normalize_for_new_plan_install",
    "parse",
    "preserve_existing_markers",
    "render_plan_sections",
    "render_plan_steps",
    "validate_planning_extension_rewrite",
]
