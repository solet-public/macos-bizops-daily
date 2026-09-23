"""Workspace-root containment for memory export/import file paths.

Unified-memory-passthrough Slice 1(d) (2026-07-16). The hydrate/drain loop
reads and writes bulk memory snapshots as files — never through the MCP bridge
(the 2026-06-28 bulk-read clog incident) — so ``export_memories`` and
``import_memories`` admit a ``file_path`` only when its realpath lies under one
operator-configured ``export_allowed_roots`` entry (``commonpath`` equality,
NOT string-prefix). An empty ``export_allowed_roots`` is the secure default:
every export and import is refused until the operator opts workspace roots in
via the plugin config, and every refusal names that config key.

Own-copy per plugin, mirroring ``salesforce_plugin`` / ``external_postgres_plugin``
``export_containment`` and the ``ledger_allowed_roots`` gate. Deliberately NO
``Path.cwd()`` and NO session-cwd plumbing — a client-reported cwd is
caller-supplied by the same principal that supplies the path, so it is not
containment, and internal EDGE dispatch has no bridge session at all (Architect
ruling 2026-07-15, ``workbench/2026-07-15_architect_connector_rollout_rulings.md``).
"""

from __future__ import annotations

import os

from ananta.error_handling import FrameworkError

from .constants import ERROR_EXPORT_PATH_REFUSED


def assert_path_within_allowed_roots(
    file_path: str,
    allowed_roots: list[str],
    *,
    config_key: str,
    plugin_name: str,
) -> str:
    """Admit a path contained under one allowed root; return its realpath.

    The empty-``allowed_roots`` refuse-all default is checked FIRST because it is
    the headline security property: nothing is exportable until the operator opts
    a root in, so path shape is moot when no root exists. Refusals name the
    config key so the operator knows exactly which knob opts a workspace root in.

    Raises ``FrameworkError(error_code=ERROR_EXPORT_PATH_REFUSED)`` on any refusal.
    """
    if not allowed_roots:
        raise FrameworkError(
            message=(
                f"no roots are configured — {config_key} in the {plugin_name} "
                "config is empty, which refuses every memory export/import by "
                "default. Add the workspace directory (or directories) memory "
                f"snapshots may land in to {config_key}, then re-run."
            ),
            error_code=ERROR_EXPORT_PATH_REFUSED,
        )
    expanded = os.path.expandvars(os.path.expanduser(file_path))
    if not os.path.isabs(expanded):
        raise FrameworkError(
            message=(
                f"file_path must resolve to an ABSOLUTE path (got {file_path!r}); "
                f"pass a path under one of the operator-configured roots "
                f"({config_key} in the {plugin_name} config)"
            ),
            error_code=ERROR_EXPORT_PATH_REFUSED,
        )
    candidate = os.path.realpath(expanded)
    for allowed in allowed_roots:
        allowed_expanded = os.path.expandvars(os.path.expanduser(allowed))
        if not allowed_expanded or not os.path.isabs(allowed_expanded):
            # A relative or blank root would realpath against the SERVER process
            # cwd — exactly the implicit-cwd containment hole the 2026-07-15
            # ruling prohibits. Config fault: fail loud.
            raise FrameworkError(
                message=(
                    f"misconfigured {config_key} entry {allowed!r} in the "
                    f"{plugin_name} config: every root must be an ABSOLUTE "
                    "directory path (after ~/$VAR expansion); relative or blank "
                    "roots would resolve against the server process cwd"
                ),
                error_code=ERROR_EXPORT_PATH_REFUSED,
            )
        allowed_real = os.path.realpath(allowed_expanded)
        if os.path.commonpath([allowed_real, candidate]) == allowed_real:
            return candidate
    raise FrameworkError(
        message=(
            f"file_path {file_path!r} is not contained in any operator-configured "
            f"root ({config_key} in the {plugin_name} config)"
        ),
        error_code=ERROR_EXPORT_PATH_REFUSED,
    )
