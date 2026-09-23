"""Public foreign-target technology-fingerprint API.

The implementation is split by responsibility so each stable mechanic stays
small enough for the platform's structural quality gates.  This module owns
only orchestration and the additive public entry points.
"""

from __future__ import annotations

from collections.abc import Sequence

from ._technology_fingerprint_capabilities import (
    capability_needs,
    reconciliation,
    unreadable_gaps,
)
from ._technology_fingerprint_facts import (
    component_facts,
    config_facts,
    deno_runtime_scopes,
    route_inventories,
    supabase_topology,
)
from ._technology_fingerprint_probes import (
    MAX_BOUNDED_EXAMPLES,
    MAX_CONTENT_BYTES,
    probe_results,
)
from ._technology_fingerprint_report import (
    MAX_REPORT_ROUTE_INVENTORIES,
    render_technology_fingerprint_section,
)
from .source_roles import RoleOverride
from .targets import TargetTree

__all__ = [
    "MAX_BOUNDED_EXAMPLES",
    "MAX_CONTENT_BYTES",
    "MAX_REPORT_ROUTE_INVENTORIES",
    "build_technology_fingerprint",
    "render_technology_fingerprint_section",
]


def _source_role_policy() -> dict[str, object]:
    return {
        "confirmation_source": "parsed_role_overrides",
        "confirmed_non_product_supports_product_claims": False,
        "confirmed_non_product_routing_eligible": False,
        "unconfirmed_facts_capability_routing_eligible_on_first_touch": True,
        "unconfirmed_facts_support_product_claims": False,
        "unconfirmed_facts_support_findings": False,
        "unconfirmed_is_not_product": True,
        "path_conventions_move_facts": False,
        "platform_variant_counts": (
            "not_duplicated; see target.source_role_partition.platform_variants"
        ),
    }


def _execution_disclosure() -> dict[str, object]:
    return {
        "target": "mutable_worktree",
        "atomic_snapshot": False,
        "target_code_executed": False,
        "config_javascript_executed": False,
        "network_used": False,
        "subprocess_used": False,
        "installation_performed": False,
        "second_crawl_performed": False,
        "symlinks_followed": False,
        "routing_is_execution": False,
        "note": (
            "Enumeration and bounded reads are not atomic. The fingerprint records "
            "declarations, resolutions, topology, and routing needs; it does not "
            "execute or judge framework best practices."
        ),
    }


def build_technology_fingerprint(
    *,
    tree: TargetTree,
    overrides: Sequence[RoleOverride],
) -> dict[str, object]:
    """Build the additive foreign-target fingerprint from one TargetTree view."""

    if not tree.foreign:
        raise ValueError("technology_fingerprint is foreign-target-only")
    probes, successful = probe_results(tree)
    components = component_facts(successful, overrides)
    deno_scopes = deno_runtime_scopes(successful, overrides)
    supabase = supabase_topology(successful, overrides)
    configs, unmodeled = config_facts(successful, overrides)
    routes = route_inventories(tree, components, overrides)
    capabilities = capability_needs(
        components,
        deno_scopes,
        supabase,
        configs,
    )
    return {
        "policy_version": "technology-fingerprint-v1",
        "scope_model": "per_manifest_runtime",
        "enumeration_source": "TargetTree.enumerated",
        "probes": probes,
        "unreadable_gaps": unreadable_gaps(probes),
        "components": components,
        "deno_runtime_scopes": deno_scopes,
        "supabase_topology": supabase,
        "configs": configs,
        "recognized_but_unmodeled_configs": unmodeled,
        "route_inventories": routes,
        "capability_needs": capabilities,
        "reconciliation": reconciliation(probes, components, capabilities),
        "source_role_policy": _source_role_policy(),
        "execution_disclosure": _execution_disclosure(),
    }
