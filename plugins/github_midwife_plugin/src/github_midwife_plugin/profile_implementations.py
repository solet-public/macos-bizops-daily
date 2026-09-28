"""The fresh roster follows the setup's implementation decisions (iss_3a2a74ea).

A profile template may declare ``implementation_plugins``: for an
implementation decision (``embeddings_implementation``,
``inference_implementation``) the service it binds and a closed table from each
option to the plugin that serves it.  The template's own ``plugins`` list and
``service_bindings`` name the default choice; :func:`resolve_implementations`
swaps that plugin in place for the selected option's plugin, rebinds the
service, and drops ``plugin_config_overrides`` for a plugin the roster no longer
carries.  Genesis applies it once to the raw template, and both the pip
allowlist and the materialized manifest and bindings read the one resolved
mapping, so the installed roster and the booted roster cannot disagree.

A decision the template does not declare is ignored (profiles without the
table, and decisions not given, keep the template as written); a declared
decision whose selected option has no row refuses loud.  The table must equal
the setup flow's ``plugins.*.enabled_when``, which a smoke pins.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, cast

__all__ = [
    "IMPLEMENTATION_DECISIONS",
    "ProfileImplementationError",
    "implementation_table",
    "resolve_implementations",
]

#: The implementation decisions genesis receives (as SOLET_<NAME> environment variables).
IMPLEMENTATION_DECISIONS: tuple[str, ...] = ("embeddings_implementation", "inference_implementation")
_TABLE_KEY = "implementation_plugins"
_ROW_KEYS = frozenset({"service", "options"})


class ProfileImplementationError(ValueError):
    """The profile template's implementation table is malformed or has no row for a selected option."""


def implementation_table(profile: Mapping[str, Any]) -> dict[str, tuple[str, dict[str, str]]]:
    """``{decision: (service, {option: plugin})}``; an absent table declares nothing."""
    raw = profile.get(_TABLE_KEY, {})
    if not isinstance(raw, dict):
        raise ProfileImplementationError(f"{_TABLE_KEY} must be a mapping")
    return {decision: _table_row(decision, row) for decision, row in cast(dict[str, object], raw).items()}


def _table_row(decision: str, row: object) -> tuple[str, dict[str, str]]:
    if decision not in IMPLEMENTATION_DECISIONS:
        raise ProfileImplementationError(f"{_TABLE_KEY} names unknown decision {decision!r}")
    if not isinstance(row, dict) or frozenset(cast(dict[str, object], row)) != _ROW_KEYS:
        raise ProfileImplementationError(f"{_TABLE_KEY}.{decision} must declare exactly service and options")
    values = cast(dict[str, object], row)
    service = values["service"]
    if not isinstance(service, str) or not service:
        raise ProfileImplementationError(f"{_TABLE_KEY}.{decision} needs a service name")
    return service, _options(decision, values["options"])


def _options(decision: str, options: object) -> dict[str, str]:
    if not isinstance(options, dict) or not options or not all(
        isinstance(key, str) and isinstance(value, str) and value
        for key, value in cast(dict[object, object], options).items()
    ):
        raise ProfileImplementationError(f"{_TABLE_KEY}.{decision}.options must map option ids to plugin names")
    return cast(dict[str, str], options)


def resolve_implementations(profile: Mapping[str, Any], implementations: Mapping[str, str]) -> dict[str, Any]:
    """A copy of ``profile`` whose roster, bindings and overrides follow ``implementations``."""
    resolved: dict[str, Any] = copy.deepcopy(dict(profile))
    table = implementation_table(profile)
    plugins = resolved.get("plugins")
    bindings = resolved.get("service_bindings")
    if not table or not implementations:
        return resolved
    if not isinstance(plugins, list) or not isinstance(bindings, dict):
        raise ProfileImplementationError("a template with implementation_plugins needs plugins and service_bindings")
    roster = cast(list[str], plugins)
    for decision, (service, options) in table.items():
        selected = implementations.get(decision)
        if selected is not None:
            _swap(resolved, roster, cast(dict[str, str], bindings), (decision, selected), (service, options))
    if len(set(roster)) != len(roster):
        raise ProfileImplementationError(f"the resolved roster repeats a plugin: {roster}")
    return resolved


def _swap(
    resolved: dict[str, Any],
    roster: list[str],
    services: dict[str, str],
    choice: tuple[str, str],
    row: tuple[str, dict[str, str]],
) -> None:
    """Replace the service's bound plugin in place with the selected option's plugin."""
    (decision, selected), (service, options) = choice, row
    if selected not in options:
        raise ProfileImplementationError(
            f"profile {resolved.get('profile_name')!r} has no plugin for {decision}={selected!r}; "
            f"declared options are {sorted(options)}"
        )
    current = services.get(service)
    if current is None or current not in roster:
        raise ProfileImplementationError(f"the template binds {service} to {current!r}, which its plugins list does not carry")
    chosen = options[selected]
    if chosen != current:
        roster[roster.index(current)] = chosen
        services[service] = chosen
        _drop_overrides(resolved, current)


def _drop_overrides(profile: dict[str, Any], plugin: str) -> None:
    overrides = profile.get("plugin_config_overrides")
    if isinstance(overrides, dict):
        cast(dict[str, object], overrides).pop(plugin, None)
