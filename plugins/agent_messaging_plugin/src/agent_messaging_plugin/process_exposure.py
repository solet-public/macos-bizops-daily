"""Process export policy + registry helpers for the MCP bridge surface.

Pure functions and a small dataclass — no FastAPI, no plugin instance state.

Ported from the now-deleted ``claude_code_channel_plugin/process_exposure.py`` (2026-05-16)
during the bridge-consolidation work — see
``workbench/2026-05-16_codex_mcp_channel_and_inter_agent_outstanding_work.md``.

The allow/deny pattern *values* are not hardcoded here; ``plugin.yaml``
supplies them at runtime and the merged plugin's policy includes its
own ``plugin::agent_messaging_plugin::*`` deny entry to prevent the
bridge surface from invoking its own private processes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

_PROCESS_KEY_RE = re.compile(r"^[A-Za-z0-9_]+::[A-Za-z0-9_]+::[A-Za-z0-9_]+$")
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")


@dataclass(frozen=True)
class ProcessExportPolicy:
    """Allow/deny/promote patterns governing direct process exposure."""

    enabled: bool = True
    allow_patterns: tuple[str, ...] = ()
    deny_patterns: tuple[str, ...] = ()
    promote_patterns: tuple[str, ...] = ()
    max_promoted_tools: int = 40

    def is_allowed(self, process_key: str) -> bool:
        if not self.enabled:
            return False
        if not is_valid_process_key_shape(process_key):
            return False
        if any(fnmatchcase(process_key, p) for p in self.deny_patterns):
            return False
        return any(fnmatchcase(process_key, p) for p in self.allow_patterns)

    def matches_promotion(self, process_key: str) -> bool:
        return any(fnmatchcase(process_key, p) for p in self.promote_patterns)


@dataclass
class PromotedTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    process_key: str

    def to_descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "process_key": self.process_key,
        }


@dataclass
class _PromotedNameAllocator:
    """Allocates unique tool names, falling back to sha1 suffix on collision."""

    used: set[str] = field(default_factory=set)

    def allocate(self, base_name: str, process_key: str) -> str:
        if base_name not in self.used:
            self.used.add(base_name)
            return base_name
        suffix = hashlib.sha1(process_key.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
        suffixed = f"{base_name}__{suffix}"
        self.used.add(suffixed)
        return suffixed


def is_valid_process_key_shape(process_key: str) -> bool:
    """True if process_key looks like ``provider_type::provider::function_name``."""
    return bool(_PROCESS_KEY_RE.match(process_key))


def iter_registry_processes(
    process_registry: dict[str, object] | None,
) -> Iterable[tuple[str, dict[str, Any]]]:
    """Yield (process_key, metadata) pairs from the live registry."""
    if not process_registry:
        return
    processes_obj = process_registry.get("processes", {})
    if not isinstance(processes_obj, dict):
        return
    for key, value in processes_obj.items():
        if isinstance(key, str) and isinstance(value, dict):
            yield key, value


def get_process_metadata(
    process_registry: dict[str, object] | None, process_key: str,
) -> dict[str, Any] | None:
    if not process_registry:
        return None
    processes_obj = process_registry.get("processes", {})
    if not isinstance(processes_obj, dict):
        return None
    metadata = processes_obj.get(process_key)
    if isinstance(metadata, dict):
        return metadata
    return None


def filter_discovery_payload(
    payload: dict[str, Any], policy: ProcessExportPolicy,
) -> dict[str, Any]:
    """Strip denied processes/keys from a discovery query result."""
    filtered = dict(payload)

    raw_processes = payload.get("processes", [])
    kept_processes: list[dict[str, Any]] = []
    if isinstance(raw_processes, list):
        for entry in raw_processes:
            if not isinstance(entry, dict):
                continue
            key = entry.get("process_key")
            if isinstance(key, str) and policy.is_allowed(key):
                kept_processes.append(entry)
    filtered["processes"] = kept_processes
    filtered["process_keys"] = [
        entry["process_key"] for entry in kept_processes
        if isinstance(entry.get("process_key"), str)
    ]
    filtered["process_count"] = len(kept_processes)
    return filtered


def promoted_tool_name_for(process_key: str) -> str:
    return "process__" + _NON_ALNUM_RE.sub("_", process_key).strip("_")


def build_promoted_tool(
    process_key: str,
    metadata: dict[str, Any],
    name_allocator: _PromotedNameAllocator,
) -> PromotedTool | None:
    """Build a promoted MCP tool descriptor from registry metadata.

    Returns None if the process has no usable arguments schema.
    """
    invocation_schema = metadata.get("invocation_schema")
    if not isinstance(invocation_schema, dict):
        return None
    properties = invocation_schema.get("properties")
    if not isinstance(properties, dict):
        return None
    arguments_schema = properties.get("arguments")
    if not isinstance(arguments_schema, dict):
        return None

    input_schema = _build_promoted_input_schema(arguments_schema)
    description = str(metadata.get("description") or process_key)
    base_name = promoted_tool_name_for(process_key)
    name = name_allocator.allocate(base_name, process_key)

    return PromotedTool(
        name=name,
        description=description,
        input_schema=input_schema,
        process_key=process_key,
    )


def build_promoted_tools(
    process_registry: dict[str, object] | None,
    policy: ProcessExportPolicy,
) -> list[PromotedTool]:
    """Build the full list of promoted tools subject to policy + cap."""
    allocator = _PromotedNameAllocator()
    tools: list[PromotedTool] = []
    for process_key, metadata in iter_registry_processes(process_registry):
        if not policy.is_allowed(process_key):
            continue
        if not policy.matches_promotion(process_key):
            continue
        tool = build_promoted_tool(process_key, metadata, allocator)
        if tool is None:
            continue
        tools.append(tool)
        if len(tools) >= policy.max_promoted_tools:
            break
    return tools


def _build_promoted_input_schema(arguments_schema: dict[str, Any]) -> dict[str, Any]:
    """Wrap the process arguments schema with reserved properties.

    The reserved property ``_reason`` only appears if it does not collide with a
    real argument name.
    """
    raw_arg_props = arguments_schema.get("properties")
    arg_props: dict[str, Any] = (
        dict(raw_arg_props) if isinstance(raw_arg_props, dict) else {}
    )
    raw_required = arguments_schema.get("required", [])
    required_list: list[str] = (
        [r for r in raw_required if isinstance(r, str)]
        if isinstance(raw_required, list)
        else []
    )

    properties: dict[str, Any] = dict(arg_props)
    if "_reason" not in properties:
        properties["_reason"] = {
            "type": "string",
            "description": "Optional human-readable reason for invoking the process.",
        }

    return {
        "type": "object",
        "properties": properties,
        "required": required_list,
        "additionalProperties": False,
    }


def split_promoted_arguments(
    raw_arguments: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Split promoted-tool args into (process_args, reason)."""
    process_args: dict[str, Any] = {}
    reason: str | None = None
    for key, value in raw_arguments.items():
        if key == "_reason" and isinstance(value, str):
            reason = value
        else:
            process_args[key] = value
    return process_args, reason


__all__ = [
    "ProcessExportPolicy",
    "PromotedTool",
    "build_promoted_tool",
    "build_promoted_tools",
    "filter_discovery_payload",
    "get_process_metadata",
    "is_valid_process_key_shape",
    "iter_registry_processes",
    "promoted_tool_name_for",
    "split_promoted_arguments",
]
