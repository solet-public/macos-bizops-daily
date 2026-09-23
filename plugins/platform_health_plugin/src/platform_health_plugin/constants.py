"""Constants for the platform_health_plugin registry sweep.

Read-shape prefix set per Architect's Q1 ruling: only methods whose function
name begins with ``list_`` or ``get_`` (which subsumes ``list_active_``) are
considered safe to execute against the live state_service. Everything else
is treated as write-shape and skipped unless the caller explicitly opts in
with ``write_enabled=True``.
"""

from __future__ import annotations

from typing import Final

PLUGIN_NAME: Final[str] = "platform_health_plugin"
SWEEP_PROCESS_NAME: Final[str] = "execute_registry_sweep"
SELF_PROCESS_KEY: Final[str] = f"plugin::{PLUGIN_NAME}::{SWEEP_PROCESS_NAME}"

# Per Architect Q1: list_*, get_*, list_active_* are read-shape. list_active_
# is a subset of list_.
READ_SHAPE_PREFIXES: Final[tuple[str, ...]] = ("list_", "get_")

# Per-type sentinel for required-but-not-provided parameters. The sweep only
# populates required params; optional params are omitted so default values
# (if any) take effect. Sentinels are deliberately bland so they cannot match
# real data — "smoke_sentinel" is unlikely to collide with a real id.
SENTINEL_STRING: Final[str] = "smoke_sentinel"
SENTINEL_INTEGER: Final[int] = 0
SENTINEL_NUMBER: Final[float] = 0.0
SENTINEL_BOOLEAN: Final[bool] = False

# ResultPayload status tokens.
STATUS_OK: Final[str] = "ok"
STATUS_FAILED: Final[str] = "failed"
STATUS_SKIPPED_WRITE: Final[str] = "skipped_write"
STATUS_SKIPPED_SELF: Final[str] = "skipped_self"
STATUS_SKIPPED_UNRESOLVED: Final[str] = "skipped_unresolved"
STATUS_SKIPPED_SCOPE: Final[str] = "skipped_scope"
STATUS_DRY_RUN: Final[str] = "dry_run"

# This is declared safety data, not a heuristic. These provider names were
# measured from the live registry/plugin roster and their manifests declare an
# external credential, endpoint, or third-party integration. A plugin provider
# outside this table remains fail-closed: it is never in the default live scope.
OUTWARD_FACING_PLUGIN_NAMESPACES: Final[frozenset[str]] = frozenset({
    "discord_plugin",
    "g_suite_plugin",
    "jira_plugin",
    "marketo_plugin",
    "salesforce_plugin",
    "schwab_market_data_plugin",
    "signal_plugin",
    "snowflake_plugin",
    "soundcloud_artist_studio_plugin",
    "zuora_plugin",
})
