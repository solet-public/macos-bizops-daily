"""Typed IO helpers for per-plugin and platform-level JSON config.

Used by the lifecycle management service for the Track D verbs
(``set_plugin_enabled``, ``set_plugin_priority``, ``reload_plugin_config``,
``update_platform_config``). Centralising the file IO here keeps
:mod:`service.py` focused on lifecycle orchestration and ensures every verb
that mutates a config goes through the same load / merge / write path.

Path layout (rooted at the ``ConfigManager`` instance):

* per-plugin config: ``<config_dir>/plugins/<plugin_name>.json``
* platform-level config: ``<config_dir>/platform.json``

The helpers never swallow IO errors -- callers are expected to surface
failures through the lifecycle service's ``_error_result`` envelope.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ananta.core.config.config_manager import ConfigManager

PLATFORM_CONFIG_FILENAME = "platform.json"

# Allowlist of platform config (scope, key) pairs that ``update_platform_config``
# is willing to write. Keep deliberately small; any new entry must be paired
# with a code path that consumes it (see service._apply_platform_config_change).
PLATFORM_CONFIG_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "logging": ("log_level",),
}


def plugin_config_path(config_manager: ConfigManager, plugin_name: str) -> Path:
    """Return the canonical on-disk path for a per-plugin JSON config."""
    return Path(config_manager.plugins_config_dir) / f"{plugin_name}.json"


def platform_config_path(config_manager: ConfigManager) -> Path:
    """Return the canonical on-disk path for the platform-level JSON config."""
    return Path(config_manager.config_dir) / PLATFORM_CONFIG_FILENAME


def read_plugin_config_file(
    config_manager: ConfigManager, plugin_name: str
) -> dict[str, Any]:
    """Read the on-disk per-plugin config, returning ``{}`` if no file exists."""
    path = plugin_config_path(config_manager, plugin_name)
    if not path.exists():
        return {}
    return _read_json(path)


def write_plugin_config_file(
    config_manager: ConfigManager,
    plugin_name: str,
    config: dict[str, Any],
) -> Path:
    """Write a per-plugin config to disk and refresh the in-memory cache."""
    path = plugin_config_path(config_manager, plugin_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, config)
    # Keep the ConfigManager's cached view in lock-step so subsequent
    # ``get_plugin_config`` calls reflect the new state without a restart.
    config_manager._plugin_configs[plugin_name] = dict(config)
    return path


def read_platform_config_file(config_manager: ConfigManager) -> dict[str, Any]:
    """Read ``platform.json`` from the active profile, returning ``{}`` if absent."""
    path = platform_config_path(config_manager)
    if not path.exists():
        return {}
    return _read_json(path)


def write_platform_config_file(
    config_manager: ConfigManager, config: dict[str, Any]
) -> Path:
    """Write the platform-level config to disk."""
    path = platform_config_path(config_manager)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, config)
    return path


def diff_config_keys(
    previous: dict[str, Any], current: dict[str, Any]
) -> list[str]:
    """Return the keys whose values differ (or appeared / disappeared) between two configs."""
    all_keys = set(previous) | set(current)
    return sorted(
        key
        for key in all_keys
        if previous.get(key) != current.get(key)
    )


def is_scope_key_allowlisted(scope: str, key: str) -> bool:
    """Return True if ``(scope, key)`` appears in :data:`PLATFORM_CONFIG_ALLOWLIST`."""
    allowed_keys = PLATFORM_CONFIG_ALLOWLIST.get(scope)
    if allowed_keys is None:
        return False
    return key in allowed_keys


def merge_platform_scope(
    document: dict[str, Any], scope: str, key: str, value: Any
) -> Any:
    """Apply ``document[scope][key] = value`` in place; return the previous value."""
    scope_section = document.setdefault(scope, {})
    if not isinstance(scope_section, dict):
        raise TypeError(
            f"platform config scope '{scope}' must be a JSON object; "
            f"found {type(scope_section).__name__}"
        )
    previous_value = scope_section.get(key)
    scope_section[key] = value
    return previous_value


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise TypeError(
            f"config file '{path}' must contain a JSON object at the top level; "
            f"found {type(payload).__name__}"
        )
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=4, sort_keys=True)
        handle.write("\n")
