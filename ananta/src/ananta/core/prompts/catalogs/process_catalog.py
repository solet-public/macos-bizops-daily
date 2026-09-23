"""Process catalog builder — platform-owned catalog construction.

Builds the system prompt process catalog from discovery service data.
Replaces the inference plugin's ``_get_builtin_processes_block`` and
``_build_discovered_schema_text`` callbacks.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from ananta.core.prompts.catalogs.discovered_schema import find_first_plugin_key
from ananta.core.prompts.catalogs.io_catalog import (
    format_io_args_summary,
    navigate_to_args_schema,
)

logger = logging.getLogger(__name__)


class CatalogDataSource(Protocol):
    """Narrow protocol for process catalog data access."""

    def get_system_prompt_processes(self) -> list[dict[str, object]]: ...
    def get_all_io_processes(self) -> list[dict[str, object]]: ...
    def get_process_by_key(self, process_key: str) -> dict[str, object] | None: ...


# ── Preamble constant ─────────────────────────────────────────────

EXECUTION_PLANS_PREAMBLE = (
    "## Execution Plans\n\n"
    "This platform relies upon a react loop where 'Execution Plans', "
    "not atomic prompts, are the atomic work structure.\n\n"
    "There is always an Execution Plan. Use "
    "service_interface::thinking_service::upsert_plan only when you need "
    "to amend or replace the plan text itself. Routine step advancement "
    "is handled automatically by the platform after a successful step. "
    "When there are no more steps to be completed in an Execution Plan, "
    "that Execution Plan is considered completed and this message will "
    "again be presented to begin a new plan.\n\n"
    "*Plan Step Status Indicators*\n"
    "[X] = Step has completed --> [>] = Current step --> "
    "[ ] = Step has not been completed --> [-] = Skip this step"
)


# ── Builtin process catalog ──────────────────────────────────────

def build_process_catalog(source: CatalogDataSource) -> str:
    """Build the full process catalog for system prompt injection.

    Returns the formatted "## Core Processes" + "## IO Processes"
    catalog string.

    Raises:
        RuntimeError: If no processes are available.
    """
    processes = source.get_system_prompt_processes()
    if not processes:
        raise RuntimeError(
            "No built-in processes found — process registry may not be loaded"
        )

    core_block = _format_core_processes(processes)

    io_processes = source.get_all_io_processes()
    if io_processes:
        io_block = _format_io_processes(io_processes)
        return f"{core_block}\n\n{io_block}"

    return core_block


def _format_core_processes(processes: list[dict[str, object]]) -> str:
    """Format core processes as the "## Core Processes" section."""
    from ananta.core.process_registry.constants import SYSTEM_PROMPT_PROCESS_ORDER

    order_index = {key: i for i, key in enumerate(SYSTEM_PROMPT_PROCESS_ORDER)}
    sorted_processes = sorted(
        processes,
        key=lambda p: order_index.get(
            str(p.get("process_key", "")), len(order_index),
        ),
    )

    lines: list[str] = ["## Core Processes"]

    for proc in sorted_processes:
        process_key = str(proc.get("process_key", ""))
        description = str(proc.get("description", ""))
        compact_args = _compact_arguments_schema(
            proc.get("invocation_schema", {}),
            max_optional=_core_process_optional_arg_limit(process_key),
        )

        lines.append("")
        lines.append(f"- {process_key}")
        lines.append(f"  Purpose: {description}")
        lines.append(f"  arguments schema: {compact_args}")

        if process_key.endswith("::upsert_plan"):
            lines.append(_upsert_plan_notes())

    return "\n".join(lines)


def _core_process_optional_arg_limit(process_key: str) -> int:
    """Return the compact-schema optional argument budget for a core process."""
    if process_key == "service_interface::knowledge_service::search":
        return 7
    return 2


def _upsert_plan_notes() -> str:
    """Return the encoding notes for upsert_plan."""
    return (
        "  Notes: `content` is the plan as plain text steps. "
        "Write steps in the format shown in the guidance article. "
        "Use \u2424 (U+2424) as the line separator "
        '\u2014 e.g. "[>] 1. First step\u2424'
        "    a) Sub-step one\u2424\u2424"
        '[ ] 2. Next step". '
        "Use one \u2424 between lines within the same step, "
        "and two \u2424 between steps. "
        "Do not use `upsert_plan` for routine progress "
        "bookkeeping; the platform advances step markers "
        "automatically."
    )


def _format_io_processes(io_processes: list[dict[str, object]]) -> str:
    """Format IO processes as the "## IO Processes" section."""
    process_keys = [
        str(proc.get("process_key", "")) for proc in io_processes
    ]
    enum_values = " | ".join(f'"{pk}"' for pk in process_keys)

    lines: list[str] = [
        "## IO Processes",
        "",
        "IO is terminal communication"
        " (does not execute tools, does not create plan artifacts).",
        "Persisted messages include a JSON metadata trailer"
        " appended by the platform."
        " Do NOT include any metadata trailer in the message content.",
    ]

    for proc in io_processes:
        process_key = str(proc.get("process_key", ""))
        compact_args = _compact_arguments_schema(
            proc.get("invocation_schema", {}), max_optional=2,
        )
        lines.append("")
        lines.append(f"- {process_key}")
        lines.append(f"  arguments schema: {compact_args}")

    lines.append("")
    lines.append("## IO Routing")
    lines.append("")
    lines.append(f"POST_MESSAGE = {enum_values}")

    return "\n".join(lines)


def _navigate_to_arg_props(
    invocation_schema: object,
) -> tuple[dict[str, object], set[str]] | None:
    """Navigate invocation_schema to (arg_properties, required_set)."""
    if not isinstance(invocation_schema, dict):
        return None
    outer_props = invocation_schema.get("properties", {})
    if not isinstance(outer_props, dict):
        return None
    args_schema = outer_props.get("arguments", {})
    if not isinstance(args_schema, dict):
        return None
    arg_props = args_schema.get("properties", {})
    if not isinstance(arg_props, dict):
        return None
    required = set(args_schema.get("required", []))
    return arg_props, required


def _compact_arguments_schema(
    invocation_schema: object,
    *,
    max_optional: int = 1,
) -> str:
    """Render invocation schema arguments as compact JSON.

    Produces ``{"arg":"type","optional_arg":"type?"}`` format.
    Required args show bare type; optional args append ``?``.
    """
    nav = _navigate_to_arg_props(invocation_schema)
    if nav is None:
        return "{}"
    arg_props, required = nav

    compact: dict[str, str] = {}
    optional_count = 0
    for name, spec in arg_props.items():
        if not isinstance(spec, dict):
            continue
        is_required = name in required
        if not is_required:
            if optional_count >= max_optional:
                continue
            optional_count += 1
        json_type = str(spec.get("type", "string"))
        if json_type == "array":
            items = spec.get("items", {})
            item_type = (
                items.get("type", "string")
                if isinstance(items, dict)
                else "string"
            )
            json_type = f"{item_type}[]"
        suffix = "" if is_required else "?"
        compact[name] = f"{json_type}{suffix}"

    return json.dumps(compact, separators=(",", ":"))


# ── Discovered schema formatting ─────────────────────────────────

def build_discovered_schema_text(
    raw_observation_dict: dict[str, Any] | None,
    source: CatalogDataSource,
) -> str | None:
    """Build discovered schema text from observation and registry.

    Extracts the first plugin process from discovery results, looks
    up its full schema, and formats a compact summary.
    """
    if not raw_observation_dict:
        return None
    action_result = raw_observation_dict.get("action_result")
    if not isinstance(action_result, dict):
        return None

    processes = _extract_processes_from_result(action_result)
    plugin_key = find_first_plugin_key(processes)
    if not plugin_key:
        return None

    process_data = source.get_process_by_key(plugin_key)
    if process_data is None:
        return None

    return _format_discovered_schema(plugin_key, process_data)


def _extract_processes_from_result(
    action_result: dict[str, object],
) -> list[object]:
    """Extract processes list from action_result dict."""
    raw = action_result.get("processes")
    if not isinstance(raw, list):
        data = action_result.get("data")
        if isinstance(data, dict):
            raw = data.get("processes")
    return raw if isinstance(raw, list) else []


def _format_discovered_schema(
    process_key: str,
    process_data: dict[str, object],
) -> str:
    """Format a discovered process as a compact schema summary."""
    description = process_data.get("description", "")
    args_schema = navigate_to_args_schema(process_data)
    args_summary = (
        format_io_args_summary(process_data) if args_schema else ""
    )

    parts = [
        "Discovered process schema",
        f"process: {process_key}",
    ]
    if description:
        parts.append(f"description: {description}")
    if args_summary:
        parts.append(f"arguments: {args_summary}")

    return "\n".join(parts)
