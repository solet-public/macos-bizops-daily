"""Hydration-time export-root validation and persistence (07-29 ruling §3).

The 2026-07-29 data-boundary ruling requires hydration to ask two mutually
exclusive questions: where the solet itself lives (``app_home``,
unchanged) and where the operator keeps the projects they work in (the
export root — "parent-of-jobs", stable and singular). The ruling's own
words for the second question's guard are "the wizard validates prompt 2's
answer rather than merely recording it... a few lines in the wizard" — but
no scripted, interactive wizard exists on the seed-born path: genesis
(``steps.py``) is a deterministic, non-interactive step machine by design,
and the only actual interactive CLI wizard (``initialization/solets/
wizard.py``) is absent from ``seed_manifest.yaml``'s ``copy:`` allowlist and
never reaches a seed-born instance. So this module supplies the "few lines"
as a real, callable, testable function; the QUESTION itself is asked by the
driving coding agent as a hydration-runbook conversational step (see
``01_hydration_runbook.md``), which calls this module before persisting an
answer.

Deliberately an OWN COPY for this plugin, matching the platform's
per-plugin-copy direction already documented in every connector's own
``export_containment.py`` ("deliberate duplication over a shared module").
What is SHARED is the algorithm/constant-set, not an import: the same
realpath + ``os.path.commonpath`` containment technique
``assert_export_path_allowed`` uses, applied to a different predicate.
``assert_export_path_allowed`` asks "is this destination inside an allowed
root" (one-directional). This module asks "is this proposed root mutually
non-overlapping with app_home" (both directions checked, since a root that
CONTAINS app_home is exactly as dangerous as one contained BY it — either
way an export destination could land inside the platform's own tree).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Final

ERROR_EXPORT_ROOT_REJECTED: Final[str] = "github_midwife.export_root_rejected"
CONFIG_KEY_EXPORT_ALLOWED_ROOTS: Final[str] = "export_allowed_roots"

# The full connector roster this migration touches (07-29 ruling + the
# 2026-08-02 limits/data-export design doc §7, Tier 1 + Tier 2). Not every
# plugin in this tuple is present in every clone — `configure_export_root`
# only writes config for the ones actually installed under `<clone>/plugins/`,
# so an uninstalled connector's config is never touched (inert-by-absence,
# not inert-by-unread-key).
BUSINESS_CONNECTOR_PLUGINS: Final[tuple[str, ...]] = (
    "external_postgres_plugin",
    "snowflake_plugin",
    "salesforce_plugin",
    "jira_plugin",
    "g_suite_plugin",
    "zuora_plugin",
    "marketo_plugin",
)


class ExportRootRejectedError(RuntimeError):
    """Raised when a proposed export root fails the mutual-containment check."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = ERROR_EXPORT_ROOT_REJECTED


def _resolve_absolute(label: str, raw: str) -> str:
    """Expand ``~``/env vars and require the result to be absolute.

    Relative or blank input would resolve against the SERVER process cwd —
    the same implicit-cwd containment hole the connectors' own gate
    prohibits (2026-07-15 ruling).
    """
    expanded = os.path.expandvars(os.path.expanduser(raw))
    if not expanded or not os.path.isabs(expanded):
        raise ExportRootRejectedError(
            f"{label} must be an ABSOLUTE directory path after ~/$VAR "
            f"expansion (got {raw!r}); a relative or blank path would "
            "resolve against the server process cwd",
        )
    return expanded


def assert_export_root_valid(proposed_root: str, app_home: str) -> str:
    """Validate a proposed export root against ``app_home``.

    Rejects a root that is, is contained by, or contains ``app_home`` — an
    export destination must never fall inside a platform-managed tree, in
    either direction (07-29 ruling: "Configuring app_home as an export root
    would be worse than useless... a direct path into the blob and ledger
    surfaces"). Returns the realpath-resolved root on success; raises
    ``ExportRootRejectedError`` naming which direction failed otherwise.
    """
    resolved_root = _resolve_absolute("the export root", proposed_root)
    resolved_home = _resolve_absolute("app_home", app_home)

    root_real = os.path.realpath(resolved_root)
    home_real = os.path.realpath(resolved_home)

    if root_real == home_real or os.path.commonpath([home_real, root_real]) == root_real:
        raise ExportRootRejectedError(
            f"export root {proposed_root!r} contains (or equals) the "
            f"solet home {app_home!r} — an export destination must "
            "never admit the platform's own tree; choose a directory "
            "outside app_home, such as a parent-of-jobs workspace folder",
        )
    if os.path.commonpath([root_real, home_real]) == home_real:
        raise ExportRootRejectedError(
            f"export root {proposed_root!r} is INSIDE the solet home "
            f"{app_home!r} — exports would land inside the platform's own "
            "managed tree; choose a directory outside app_home",
        )
    return root_real


def configure_export_root(
    clone_root: Path,
    app_home: str,
    proposed_root: str,
    *,
    connector_plugins: tuple[str, ...] = BUSINESS_CONNECTOR_PLUGINS,
) -> dict[str, list[str]]:
    """Validate ``proposed_root`` and persist it into every installed connector's config.

    Idempotent and additive: an already-present root is not duplicated, and
    any other operator-added root already in a connector's
    ``export_allowed_roots`` is preserved, never clobbered. Only connectors
    actually installed under ``<clone_root>/plugins/<name>/`` are touched —
    an uninstalled connector's config is left alone entirely (there is
    nothing there to configure). Returns ``{plugin_name: updated_roots}``
    for every connector actually written, so the caller can report exactly
    what changed.
    """
    resolved_root = assert_export_root_valid(proposed_root, app_home)

    config_dir = Path(app_home) / "config" / "plugins"
    config_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, list[str]] = {}
    for plugin_name in connector_plugins:
        if not (clone_root / "plugins" / plugin_name).is_dir():
            continue
        config_path = config_dir / f"{plugin_name}.json"
        if config_path.is_file():
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        else:
            existing = {}
        roots = list(existing.get(CONFIG_KEY_EXPORT_ALLOWED_ROOTS, []))
        if resolved_root not in roots:
            roots.append(resolved_root)
        existing[CONFIG_KEY_EXPORT_ALLOWED_ROOTS] = roots
        config_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        written[plugin_name] = roots
    return written
