"""Compatibility facade for setup planning, decisions, and preview rendering."""

from .decision_resolution import (
    active_discovered_decision_ids,
    decision_ids_for_stages,
    decision_order,
    order_decision_prompts,
    static_decision_prompts,
    unresolved_decision_ids_for_stages,
)
from .plan_builder import PlannedOperation, SetupPlan, build_setup_plan
from .preview_rendering import (
    active_probe_ids,
    approval_fingerprint,
    canonical_planned_actions,
    render_consents,
    render_decisions,
)
from .stage_activation import (
    current_frontier_stage_ids,
    initial_probe_activations,
    initial_stage_probe_statuses,
    reconcile_stage_probe_activation,
    validate_stage_probe_keys,
    validate_stage_probe_state,
)

__all__ = [
    "PlannedOperation",
    "SetupPlan",
    "active_discovered_decision_ids",
    "active_probe_ids",
    "approval_fingerprint",
    "build_setup_plan",
    "canonical_planned_actions",
    "current_frontier_stage_ids",
    "decision_ids_for_stages",
    "decision_order",
    "initial_stage_probe_statuses",
    "initial_probe_activations",
    "order_decision_prompts",
    "reconcile_stage_probe_activation",
    "render_consents",
    "render_decisions",
    "static_decision_prompts",
    "unresolved_decision_ids_for_stages",
    "validate_stage_probe_keys",
    "validate_stage_probe_state",
]
