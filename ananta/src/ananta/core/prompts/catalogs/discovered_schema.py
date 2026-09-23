"""Discovered schema helpers — pure functions for discovery result formatting.

Extracts and formats process schemas from discovery results for injection
into the model context.  No plugin or service dependencies.
"""

from __future__ import annotations

from typing import Any

from ananta.core.prompts.context import ACTIVE_PLAN_MARKER


def find_first_plugin_key(processes: list[object]) -> str | None:
    """Find the first plugin-type process key in a discovery result list."""
    for proc in processes:
        if not isinstance(proc, dict):
            continue
        pkey = proc.get("process_key", "")
        if isinstance(pkey, str) and pkey.startswith("plugin::"):
            return pkey
    return None


def find_discovered_schema_insertion_index(
    messages: list[dict[str, Any]],
) -> int:
    """Find the index to insert discovered schema — after the last observation."""
    last_obs_idx = -1
    for i, msg in enumerate(messages):
        content = msg.get("content", "")
        if isinstance(content, str) and ACTIVE_PLAN_MARKER in content:
            continue
        if msg.get("role") == "assistant":
            last_obs_idx = i
    return last_obs_idx + 1 if last_obs_idx >= 0 else len(messages)


def is_discovery_no_matches(observation: str | None) -> bool:
    """Check if a discovery observation indicates zero matches."""
    if not observation:
        return False
    return "0 processes matched" in observation or "no processes matched" in observation.lower()


def extract_discovery_processes(
    raw_observation: dict[str, Any] | None,
) -> list[object]:
    """Extract the process list from a discovery action result."""
    if not raw_observation:
        return []
    # Try top-level processes key first
    processes = raw_observation.get("processes")
    if isinstance(processes, list):
        return processes
    # Try nested data.processes (template-wrapped)
    data = raw_observation.get("data")
    if isinstance(data, dict):
        processes = data.get("processes")
        if isinstance(processes, list):
            return processes
    return []
