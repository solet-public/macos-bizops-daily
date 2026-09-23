"""Workspace-root containment for Bulk v2 ingest CSV inputs.

The read-side mirror of ``export_containment.py`` (2026-08-24 plan §4): a
Bulk v2 ingest job's ``csv_path`` must be realpath-contained under one
operator-configured ``import_allowed_roots`` entry (``commonpath`` equality,
NOT string-prefix). An empty ``import_allowed_roots`` is the secure default:
every ingest is refused until the operator opts workspace roots in via the
plugin config — same refuse-all posture as ``export_allowed_roots``.

Own-copy per connector plugin (deliberate duplication over a shared module,
per the platform's per-plugin-copy direction — see ``export_containment.py``
for the fuller rationale and the Architect ruling it cites).

Unlike an export destination, an ingest source must already exist: the
caller is pointing at a file `sf` will read, not a location it will create.
The gate therefore also confirms the resolved path is a real file — a clear
``sf.import_path_refused`` up front beats a dispatched async job that fails
minutes later with an opaque CLI error over a missing/renamed CSV.
"""

from __future__ import annotations

import os

from .constants import CSV_SUFFIX, ERROR_IMPORT_PATH_REFUSED


class ImportPathRefusedError(RuntimeError):
    """Raised when a bulk ingest csv_path fails workspace-root containment."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = ERROR_IMPORT_PATH_REFUSED


def assert_import_path_allowed(
    csv_path: str,
    allowed_roots: list[str],
    *,
    config_key: str,
    plugin_name: str,
) -> str:
    """Admit an absolute ``.csv`` path contained under one allowed root.

    Returns the realpath-resolved path to read. Refusals name the config key
    so the operator knows exactly which knob opts a workspace root in.
    """
    if not os.path.isabs(csv_path):
        raise ImportPathRefusedError(
            f"csv_path must be an ABSOLUTE path (got {csv_path!r}); pass a path "
            f"under one of the operator-configured workspace roots ({config_key} "
            f"in the {plugin_name} config)",
        )
    if not csv_path.endswith(CSV_SUFFIX):
        raise ImportPathRefusedError(
            f"csv_path must end in '{CSV_SUFFIX}' (got {csv_path!r})",
        )
    if not allowed_roots:
        raise ImportPathRefusedError(
            f"no workspace roots are configured — {config_key} in the "
            f"{plugin_name} config is empty, which refuses every bulk ingest by "
            "default. Add the workspace directory (or directories) ingest CSVs "
            f"may be read from to {config_key}, then re-run.",
        )
    candidate = os.path.realpath(csv_path)
    for allowed in allowed_roots:
        expanded = os.path.expandvars(os.path.expanduser(allowed))
        if not expanded or not os.path.isabs(expanded):
            # A relative or blank root would realpath against the SERVER
            # process cwd — exactly the implicit-cwd containment hole the
            # export-side gate already prohibits; the read side inherits it.
            raise ImportPathRefusedError(
                f"misconfigured {config_key} entry {allowed!r} in the "
                f"{plugin_name} config: every root must be an ABSOLUTE "
                "directory path (after ~/$VAR expansion); relative or empty "
                "roots would resolve against the server process cwd",
            )
        allowed_real = os.path.realpath(expanded)
        if os.path.commonpath([allowed_real, candidate]) == allowed_real:
            if not os.path.isfile(candidate):
                raise ImportPathRefusedError(
                    f"csv_path {csv_path!r} is contained under an allowed root "
                    "but does not exist as a file — create the CSV before "
                    "submitting the bulk ingest job",
                )
            return candidate
    raise ImportPathRefusedError(
        f"csv_path {csv_path!r} is not contained in any operator-configured "
        f"workspace root ({config_key} in the {plugin_name} config)",
    )
