"""Action schema constants and builders for model output decode.

Canonical argument schema, per-function argument properties, step-narrowed
schema construction, and schema envelope wrapping.  These are pure functions
with no plugin or service dependencies.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

__all__ = [
    "_CANONICAL_ARG_SCHEMA",
    "_ALL_ARG_PROPERTIES",
    "_FUNCTION_ARG_PROPERTIES",
    "_FUNCTION_REQUIRED_ARGS",
    "_MAX_PLUGIN_ARG_PROPERTIES_IN_OUTPUT_SCHEMA",
    "_MAX_TOTAL_ARG_PROPERTIES_IN_OUTPUT_SCHEMA",
    "_PREWARM_MIN_ITEMS",
    "_resolve_function_args",
    "_collect_arg_definitions",
    "_narrow_arg_schema",
    "_narrow_arg_schema_with_all_required",
    "_parse_process_keys",
    "_ordered_unique_process_keys",
    "_build_process_const_schema",
    "_build_enum_action_item",
    "_normalize_unambiguous_provider_type_mismatch",
    "_build_schema_envelope",
    "_action_schema",
    "_step_narrowed_schema",
    "extract_invocation_arg_properties",
]

# LM Studio grammar explosion guard: per-process and total ceilings on
# the number of argument properties in step-narrowed output schemas.
_MAX_PLUGIN_ARG_PROPERTIES_IN_OUTPUT_SCHEMA: int = 8
_MAX_TOTAL_ARG_PROPERTIES_IN_OUTPUT_SCHEMA: int = 10

_CANONICAL_ARG_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "minLength": 1},
        "query": {"type": "string"},
        "top_k": {"type": "integer"},
        "session_id": {"type": "string"},
        "message": {"type": "string"},
        "attachments": {
            "type": "array",
            "items": {"type": "string"},
        },
        "job_result_ref": {"type": "string"},
    },
    "additionalProperties": False,
}

# All known argument property definitions — superset of canonical.
# Step-narrowed schemas draw from this pool; the canonical schema
# only includes the builtin-process properties above.
_ALL_ARG_PROPERTIES: dict[str, object] = {
    "content": {"type": "string", "minLength": 1},
    "query": {"type": "string"},
    "top_k": {"type": "integer"},
    "knowledge_layers": {
        "type": "array",
        "items": {"type": "integer", "minimum": 1},
        "minItems": 1,
        "maxItems": 16,
    },
    "min_knowledge_layer": {"type": "integer", "minimum": 1},
    "max_knowledge_layer": {"type": "integer", "minimum": 1},
    "include_unlayered": {"type": "boolean"},
    "session_id": {"type": "string"},
    "message": {"type": "string"},
    "attachments": {"type": "array", "items": {"type": "string"}},
    "job_result_ref": {"type": "string"},
    "intake_id": {"type": "string"},
    "artifact_type": {"type": "string"},
    "artifact_id": {"type": "string"},
    "parent_id": {"type": "string"},
    "directive": {"type": "string", "minLength": 1},
    "wbs_id": {"type": "string"},
    "manifest_id": {"type": "string"},
    "phase_number": {"type": "integer"},
    "phase_name": {"type": "string"},
    "support_articles": {
        "type": "array",
        "items": {"type": "string"},
        "maxItems": 20,
    },
    "operation": {"type": "string"},
    "anchor_step_number": {"type": "integer"},
    "end_step_number": {"type": "integer"},
    "step_number": {"type": "integer"},
    "status": {"type": "string"},
    "state_summary": {"type": "string"},
    "output_artifacts": {"type": "array", "items": {"type": "string"}},
    "outcome_summary": {"type": "string"},
    "next_phase_instruction": {"type": "string"},
    "approved_artifacts": {"type": "array", "items": {"type": "string"}},
    "segment": {"type": "string", "minLength": 1},
    "movement_type": {"type": "string"},
    "joseki_key": {"type": "string"},
    "work_item_id": {"type": "string"},
    "packet_content": {"type": "string", "minLength": 1},
    "ledger_content": {"type": "string", "minLength": 1},
}

# Argument properties needed by each function_name.
_FUNCTION_ARG_PROPERTIES: dict[str, tuple[str, ...]] = {
    "upsert_plan": ("content",),
    "search": (
        "query",
        "top_k",
        "knowledge_layers",
        "min_knowledge_layer",
        "max_knowledge_layer",
        "include_unlayered",
    ),
    "recall": ("query", "top_k"),
    "post_message": ("message", "attachments", "job_result_ref"),
    "create_resolved_intake_state": ("intake_id", "content"),
    "create_work_manifest": ("content",),
    "patch_work_manifest": ("manifest_id", "content"),
    "patch_work_breakdown_structure": ("wbs_id", "content"),
    "graft_work_breakdown_structure_segment": (
        "wbs_id",
        "anchor_step_number",
    ),
    "record_work_breakdown_structure_step_state": (
        "wbs_id",
        "step_number",
        "status",
        "state_summary",
        "output_artifacts",
    ),
    "record_work_manifest_phase_state": (
        "manifest_id",
        "phase_number",
        "status",
        "outcome_summary",
        "approved_artifacts",
        "next_phase_instruction",
    ),
    "create_movement_design": (
        "manifest_id",
        "movement_type",
        "packet_content",
        "ledger_content",
    ),
    "create_authored_artifact": (
        "artifact_type",
        "content",
    ),
}

# Required arguments per function_name.  IO processes (post_message)
# require ``message`` at grammar level so constrained decoding can
# close the JSON string cleanly.  Without ``required``, the grammar
# has no termination anchor and the model enters a repetition loop.
# The AQP injects session_id post-inference for plugins that need it;
# session_id is NOT required at grammar level because the AQP handles
# it from flow context.
_FUNCTION_REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "post_message": ("message",),
    "upsert_plan": ("content",),
    "search": ("query",),
    "recall": ("query",),
    "create_resolved_intake_state": ("content",),
    "create_work_manifest": ("content",),
    "patch_work_manifest": ("manifest_id", "content"),
    "patch_work_breakdown_structure": ("wbs_id", "content"),
    "graft_work_breakdown_structure_segment": (
        "wbs_id",
        "anchor_step_number",
    ),
    "record_work_breakdown_structure_step_state": (
        "wbs_id",
        "step_number",
        "status",
    ),
    "record_work_manifest_phase_state": (
        "manifest_id",
        "phase_number",
        "status",
        "outcome_summary",
    ),
    "create_movement_design": (
        "manifest_id",
        "movement_type",
        "packet_content",
        "ledger_content",
    ),
    "create_authored_artifact": (
        "artifact_type",
        "content",
    ),
}


def _resolve_function_args(
    fn: str,
    registry_lookup: Callable[[str], dict[str, dict[str, object]]] | None,
    registry_required_lookup: Callable[[str], set[str]] | None,
) -> tuple[tuple[str, ...] | None, tuple[str, ...], dict[str, dict[str, object]]]:
    """Resolve argument names, required names, and dynamic props for one function.

    Returns (arg_names, required_names, dynamic_properties).
    arg_names is None when the function is unknown and has no registry fallback.
    """
    static = _FUNCTION_ARG_PROPERTIES.get(fn)
    if static is not None:
        req = _FUNCTION_REQUIRED_ARGS.get(fn, ())
        return static, req, {}

    if registry_lookup is None:
        return None, (), {}

    reg_props = registry_lookup(fn)
    if not reg_props:
        return None, (), {}

    req_set = registry_required_lookup(fn) if registry_required_lookup else set()
    return tuple(reg_props), tuple(req_set), reg_props


def _collect_arg_definitions(
    function_names: list[str],
    registry_lookup: Callable[[str], dict[str, dict[str, object]]] | None,
    registry_required_lookup: Callable[[str], set[str]] | None,
) -> tuple[set[str], list[str], dict[str, dict[str, object]]] | None:
    """Collect needed arg names, required list, and dynamic props for all functions.

    Returns None if any function is unresolvable.
    """
    needed: set[str] = set()
    required_sets: list[set[str]] = []
    dynamic_props: dict[str, dict[str, object]] = {}
    for fn in function_names:
        arg_names, req_names, dyn = _resolve_function_args(
            fn,
            registry_lookup,
            registry_required_lookup,
        )
        if arg_names is None:
            return None
        needed.update(arg_names)
        dynamic_props.update(dyn)
        required_sets.append(set(req_names))

    # Only require fields that ALL functions agree on.  When multiple
    # functions share one widened schema, per-function required fields
    # are enforced at AQP validation time — not in the grammar.
    if required_sets:
        common_required = required_sets[0]
        for s in required_sets[1:]:
            common_required &= s
        required = sorted(common_required)
    else:
        required = []
    return needed, required, dynamic_props


def _narrow_arg_schema(
    function_names: list[str],
    *,
    registry_lookup: (Callable[[str], dict[str, dict[str, object]]] | None) = None,
    registry_required_lookup: (Callable[[str], set[str]] | None) = None,
) -> dict[str, object]:
    """Build an argument schema containing only properties used by the given functions.

    Draws from ``_ALL_ARG_PROPERTIES`` for statically known functions.
    When ``registry_lookup`` is provided, unknown functions are resolved
    dynamically from the process registry — no hardcoding needed.
    """
    result = _collect_arg_definitions(
        function_names,
        registry_lookup,
        registry_required_lookup,
    )
    if result is None:
        return _CANONICAL_ARG_SCHEMA
    needed, required, dynamic_props = result

    narrowed_props: dict[str, object] = {
        k: v for k, v in _ALL_ARG_PROPERTIES.items() if k in needed
    }
    for k, v in dynamic_props.items():
        if k not in narrowed_props:
            narrowed_props[k] = v

    if not narrowed_props:
        return _CANONICAL_ARG_SCHEMA

    schema: dict[str, object] = {
        "type": "object",
        "properties": narrowed_props,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _narrow_arg_schema_with_all_required(
    process_keys: list[str],
) -> dict[str, object]:
    """Like _narrow_arg_schema but requires ALL properties.

    Used for the completion handoff step where state_summary and
    output_artifacts must be required per the Step 12 decode contract.

    Accepts full process keys (``provider_type::provider::function_name``)
    and extracts the short function name for property lookup.
    """
    needed: set[str] = set()
    for key in process_keys:
        fn = key.rsplit("::", 1)[-1] if "::" in key else key
        props = _FUNCTION_ARG_PROPERTIES.get(fn)
        if props is None:
            return _CANONICAL_ARG_SCHEMA
        needed.update(props)

    narrowed_props = {k: v for k, v in _ALL_ARG_PROPERTIES.items() if k in needed}
    if not narrowed_props:
        return _CANONICAL_ARG_SCHEMA

    return {
        "type": "object",
        "properties": narrowed_props,
        "additionalProperties": False,
        "required": sorted(narrowed_props.keys()),
    }


_CANONICAL_ACTION_ITEM: dict[str, object] = {
    "type": "object",
    "required": ["process", "reason", "arguments"],
    "properties": {
        "process": {
            "type": "object",
            "required": [
                "provider_type",
                "provider",
                "function_name",
            ],
            "properties": {
                "provider_type": {
                    "type": "string",
                    "enum": ["service_interface", "plugin"],
                },
                "provider": {
                    "type": "string",
                    "enum": [
                        "thinking_service",
                        "memory_service",
                        "knowledge_service",
                        "discord_plugin",
                        "signal_plugin",
                    ],
                },
                "function_name": {
                    "type": "string",
                    "enum": [
                        "upsert_plan",
                        "recall",
                        "search",
                        "post_message",
                    ],
                },
            },
            "additionalProperties": False,
        },
        "reason": {"type": "string"},
        "arguments": _CANONICAL_ARG_SCHEMA,
    },
    "additionalProperties": False,
}

# Pre-warm values: the distinct minItems values used across all plan steps.
_PREWARM_MIN_ITEMS = (1, 2, 3)

# Cache of schema variants keyed by minItems value (canonical schemas).
_ACTION_SCHEMA_CACHE: dict[int, dict[str, object]] = {}

# Cache of step-narrowed schemas keyed by (ordered unique process keys, min_items).
_NARROWED_SCHEMA_CACHE: dict[tuple[tuple[str, ...], int], dict[str, object]] = {}


def _parse_process_keys(
    process_keys: list[str],
) -> tuple[list[str], list[str], list[str]]:
    """Split resolved process keys into (provider_types, providers, function_names).

    Each key has format ``provider_type::provider::function_name``.
    Returns deduplicated lists suitable for JSON schema enums.

    Ordering matters for constrained decoding — LM Studio biases toward
    earlier enum values.  ``service_interface`` sorts before ``plugin``
    and service providers sort before plugin providers so that the model
    favors the correct provider_type on multi-provider steps like graft
    + post_message.
    """
    provider_types: set[str] = set()
    providers: set[str] = set()
    function_names: set[str] = set()
    for key in process_keys:
        parts = key.split("::")
        if len(parts) == 3:
            provider_types.add(parts[0])
            providers.add(parts[1])
            function_names.add(parts[2])
    return (
        _semantic_enum_order(provider_types),
        _semantic_enum_order(providers),
        sorted(function_names),
    )


def _ordered_unique_process_keys(process_keys: list[str]) -> tuple[str, ...]:
    """Return process keys with first-seen ordering preserved."""
    return tuple(dict.fromkeys(process_keys))


def _build_process_const_schema(process_key: str) -> dict[str, object]:
    """Build a process schema that fixes all three process-key components."""
    provider_type, provider, function_name = process_key.split("::", 2)
    return {
        "type": "object",
        "required": ["provider_type", "provider", "function_name"],
        "properties": {
            "provider_type": {"type": "string", "const": provider_type},
            "provider": {"type": "string", "const": provider},
            "function_name": {"type": "string", "const": function_name},
        },
        "additionalProperties": False,
    }


def _normalize_unambiguous_provider_type_mismatch(
    process_key: str,
    declared_keys: list[str],
) -> str:
    """Repair a provider_type-only mismatch when the declared match is unique."""
    parts = process_key.split("::")
    if len(parts) != 3:
        return process_key

    _, provider, function_name = parts
    candidates = [
        declared
        for declared in declared_keys
        if declared != process_key and declared.split("::")[1:] == [provider, function_name]
    ]
    if len(candidates) == 1:
        return candidates[0]
    return process_key


# Preferred enum ordering: service_interface providers first, then
# plugin providers.  Within each group, alphabetical.  This ensures
# the constrained decoder sees ``service_interface`` before ``plugin``
# on mixed steps (e.g. graft + post_message).
_PROVIDER_TYPE_ORDER = {"service_interface": 0, "plugin": 1}
_PROVIDER_ORDER = {
    "thinking_service": 0,
    "memory_service": 1,
    "knowledge_service": 2,
}


def _semantic_enum_order(values: set[str]) -> list[str]:
    """Sort enum values with service-interface providers first."""

    def _key(v: str) -> tuple[int, str]:
        rank = _PROVIDER_TYPE_ORDER.get(v, _PROVIDER_ORDER.get(v, 10))
        return (rank, v)

    return sorted(values, key=_key)


def _build_schema_envelope(
    action_item: dict[str, object],
    min_items: int,
) -> dict[str, object]:
    """Wrap an action item definition in the standard response schema."""
    actions: dict[str, object] = {
        "type": "array",
        "items": action_item,
    }
    if min_items > 0:
        actions["minItems"] = min_items
        actions["maxItems"] = min_items

    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["step_summary", "actions"],
        "properties": {
            "step_summary": {
                "type": "string",
                "description": (
                    "Internal: single short sentence summarizing what you "
                    "will do in this step. Not delivered to any IO interface."
                ),
            },
            "actions": actions,
        },
        "additionalProperties": False,
    }


def _action_schema(min_items: int = 0) -> dict[str, object]:
    """Return a canonical action schema with the given ``minItems`` constraint.

    Schemas are cached so each unique ``minItems`` value produces the same
    dict instance — important for LM Studio grammar caching which keys on
    schema identity.
    """
    cached = _ACTION_SCHEMA_CACHE.get(min_items)
    if cached is not None:
        return cached

    schema = _build_schema_envelope(_CANONICAL_ACTION_ITEM, min_items)
    _ACTION_SCHEMA_CACHE[min_items] = schema
    return schema


def _build_enum_action_item(
    ordered_keys: tuple[str, ...],
    arg_schema: dict[str, object] | None,
    function_names: list[str],
) -> dict[str, object]:
    """Build a single action item schema using enum constraints."""
    provider_types, providers, _ = _parse_process_keys(list(ordered_keys))
    effective_args = arg_schema if arg_schema is not None else _narrow_arg_schema(function_names)
    return {
        "type": "object",
        "required": ["process", "reason", "arguments"],
        "properties": {
            "process": {
                "type": "object",
                "required": ["provider_type", "provider", "function_name"],
                "properties": {
                    "provider_type": {"type": "string", "enum": provider_types},
                    "provider": {"type": "string", "enum": providers},
                    "function_name": {"type": "string", "enum": function_names},
                },
                "additionalProperties": False,
            },
            "reason": {"type": "string"},
            "arguments": effective_args,
        },
        "additionalProperties": False,
    }


def _step_narrowed_schema(
    process_keys: list[str],
    *,
    min_items: int | None = None,
    arg_schema: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return a schema narrowed to the step's declared actions.

    Always uses the enum-based format (single object type with enum
    arrays for provider_type/provider/function_name and a merged
    arguments schema).  This is the only format LM Studio's constrained
    decoder can compile efficiently.

    Cached by ``(ordered_unique_process_keys, min_items)`` so identical
    step shapes share a single compiled grammar.  Custom arg schemas are
    not cached.
    """
    effective_min = min_items if min_items is not None else len(process_keys)
    ordered_keys = _ordered_unique_process_keys(process_keys)
    is_canonical = arg_schema is None

    if is_canonical:
        cached = _NARROWED_SCHEMA_CACHE.get((ordered_keys, effective_min))
        if cached is not None:
            return cached

    _, _, function_names = _parse_process_keys(list(ordered_keys))
    action_item = _build_enum_action_item(ordered_keys, arg_schema, function_names)
    schema = _build_schema_envelope(action_item, effective_min)

    if is_canonical:
        _NARROWED_SCHEMA_CACHE[(ordered_keys, effective_min)] = schema
    return schema


# ── Invocation schema extraction ──


def _convert_property_to_type_def(
    prop: dict[str, object],
) -> dict[str, object]:
    """Convert a schema property to a simplified type definition."""
    prop_type = prop.get("type", "string")
    entry: dict[str, object] = {"type": prop_type}
    for constraint in ("minimum", "maximum", "enum"):
        if constraint in prop:
            entry[constraint] = prop[constraint]
    if prop_type == "array" and "items" in prop:
        items_def = prop["items"]
        if isinstance(items_def, dict) and items_def.get("type") == "object":
            pass  # Leave array unconstrained in output schema
        else:
            entry["items"] = items_def
    return entry


def _trim_to_max_properties(
    result: dict[str, dict[str, object]],
    required_names: list[str],
    max_properties: int,
) -> dict[str, dict[str, object]]:
    """Keep at most *max_properties* entries, required-first."""
    required_present = [n for n in required_names if n in result]
    max_properties = max(max_properties, len(required_present))
    required_ordered = list(dict.fromkeys(n for n in required_names if n in result))
    optional_ordered = [n for n in result if n not in set(required_ordered)]
    candidates = required_ordered + optional_ordered
    return {name: result[name] for name in candidates[:max_properties]}


def _navigate_to_args_schema(
    process_data: dict[str, object],
) -> dict[str, Any] | None:
    """Navigate invocation_schema envelope to the inner arguments schema."""
    schema = process_data.get("invocation_schema")
    if not isinstance(schema, dict):
        return None
    outer_props = schema.get("properties")
    if not isinstance(outer_props, dict):
        return None
    args_schema = outer_props.get("arguments")
    return args_schema if isinstance(args_schema, dict) else None


def extract_invocation_arg_properties(
    process_data: dict[str, object],
    *,
    max_properties: int | None = None,
) -> dict[str, dict[str, object]]:
    """Extract argument property definitions from invocation_schema.

    The invocation_schema is an envelope with ``process``, ``reason``,
    and ``arguments`` at the top level.  The actual tool parameters
    live at ``invocation_schema.properties.arguments.properties``.
    """
    args_schema = _navigate_to_args_schema(process_data)
    if args_schema is None:
        return {}

    arg_properties = args_schema.get("properties")
    if not isinstance(arg_properties, dict):
        return {}

    required_raw = args_schema.get("required")
    required_names = [str(r) for r in required_raw] if isinstance(required_raw, list) else []

    result: dict[str, dict[str, object]] = {}
    for name, prop in arg_properties.items():
        if not isinstance(prop, dict):
            continue
        result[name] = _convert_property_to_type_def(prop)

    if max_properties is None or len(result) <= max_properties:
        return result

    return _trim_to_max_properties(result, required_names, max_properties)
