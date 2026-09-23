"""L2 fresh-source manifest-preflight probe — subprocess entrypoint (GTE-06).

Executed by the deployment plugin's probe harness as::

    <candidate>/venv/bin/python3 -m ananta.services.lifecycle_management_service.preflight_probe

with the manifest JSON on stdin. Because the module ships INSIDE every
materialized release's ``code/`` copy and runs under the candidate's own
interpreter, the imports it performs resolve against the RELEASE source
with a fresh ``sys.modules`` and fresh ``importlib.metadata`` — closing
the in-process preflight's cache-poisoning limit (see the
:mod:`manifest_preflight` module docstring) and the entry-point-staleness
false-reject (the 2026-07-06 "L1.1 boot-stale" specimen).

Contract (design ``workbench/2026-07-06_gte06_fresh_source_preflight_probe_design.md`` §5):

* stdin — one JSON object: the effective manifest (``{"plugins": [...]}``),
  optionally carrying ``"repo_root"`` (see ROOT-MANIFEST CHECK below).
* stdout — exactly one JSON envelope::

      {"probe_version": 1, "release_id": "...", "interpreter": "...",
       "capabilities": ["plugin_manifest", "root_manifest"],
       "checks_run": ["plugin_manifest", ...],
       "ok": bool, "failures": [{check, plugin, message, error_class}, ...],
       "duration_ms": int}

ROOT-MANIFEST CHECK (§46.1)
---------------------------
The F1 root-manifest gate used to run **in the outgoing process**, which
validated it against whatever that process imported at ITS OWN last start
— so it refused valid manifests immediately after an update, exactly when
its imports were most stale. It runs here instead, for the same reason the
plugin-manifest preflight does: this module executes under the CANDIDATE
release's interpreter, so the code doing the validating is the code about
to be activated.

Two axes that must not be collapsed:

* the **code** doing the validating is the candidate's (state 4);
* the **subject** validated is the LIVE deployment root, passed in as
  ``repo_root``. ``root_manifest.yaml`` is drift discipline over the live
  solet root's top-level entries; classifying the candidate's own copy
  would check the wrong tree and make the gate meaningless while still
  passing.

``capabilities`` is a POSITIVE self-assertion, and it exists so a runner can
tell "this probe predates the root-manifest contract" (no ``capabilities``
key — expected when an older release is the cutover target) apart from
"this probe supports the check but did not run it" (an inconsistency).
Absence of ``checks_run`` cannot separate those two, and a bound that
cannot tell a version gap from a skipped check is a bound in name only.

* exit code — ``0`` (ran, ``ok=true``), ``3`` (ran, preflight failures
  listed), anything else = harness/environment error (traceback on
  stderr; the harness classifies it RED).

The probe deliberately reuses :func:`run_manifest_preflight` verbatim —
one validation function, two execution contexts, zero drift surface.
Unlike the L1 call site, the probe context NEVER defers
``EntryPointMissingError`` (design §4/A2): a fresh interpreter's
entry-point scan is authoritative, so a missing entry point here is a
real rejection.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Final

from ananta.core.root_manifest import MANIFEST_FILENAME, classify_root_entries
from ananta.core.root_manifest.report import format_report
from ananta.services.lifecycle_management_service.manifest_preflight import (
    PreflightFailure,
    run_manifest_preflight,
)

PROBE_VERSION: Final[int] = 1
EXIT_OK: Final[int] = 0
EXIT_PREFLIGHT_FAILURES: Final[int] = 3

CHECK_PLUGIN_MANIFEST: Final[str] = "plugin_manifest"
CHECK_ROOT_MANIFEST: Final[str] = "root_manifest"
#: Every check this probe build knows how to run — the positive assertion a
#: runner discriminates on. Extend when a new check lands here.
PROBE_CAPABILITIES: Final[tuple[str, ...]] = (
    CHECK_PLUGIN_MANIFEST,
    CHECK_ROOT_MANIFEST,
)
ERROR_CLASS_ROOT_MANIFEST_DRIFT: Final[str] = "RootManifestDrift"

STDIN_KEY_REPO_ROOT: Final[str] = "repo_root"

_ENV_RELEASE_ID: Final[str] = "SOLET_RELEASE_ID"


def run_root_manifest_check(repo_root: Path) -> list[PreflightFailure]:
    """Classify root-manifest drift for ``repo_root`` UNDER THIS INTERPRETER.

    No-op when ``root_manifest.yaml`` is absent — the early-cycle bootstrap
    window, preserved verbatim from the gate's previous in-process home so
    this change alters WHERE the check runs and nothing about when it
    applies.

    ``plugin=None`` because a root-manifest failure is not attributable to
    any plugin. Null is the field's declared shape
    (:class:`PreflightFailure.plugin` is ``str | None``), so every existing
    consumer already handles it; a string sentinel would be the novel thing,
    and the one that eventually gets matched against real plugin names.
    """
    manifest_path = repo_root / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return []
    classification = classify_root_entries(manifest_path, repo_root)
    if not classification.has_blocking_violations:
        return []
    return [
        PreflightFailure(
            check=CHECK_ROOT_MANIFEST,
            plugin=None,
            message=format_report(classification, severity="BLOCKING"),
            error_class=ERROR_CLASS_ROOT_MANIFEST_DRIFT,
        )
    ]


def build_probe_envelope(
    manifest: dict[str, Any], *, release_id: str, repo_root: Path | None = None
) -> tuple[int, dict[str, Any]]:
    """Run the preflight in THIS interpreter; return ``(exit_code, envelope)``.

    ``repo_root`` is ``None`` when the spawning runner predates the
    root-manifest contract and therefore did not ask for the check. That is
    a version gap, not a skip: the check is then absent from ``checks_run``
    while ``capabilities`` still advertises it, which is exactly the pair a
    runner needs to tell the two apart.
    """
    started = time.monotonic()
    result = run_manifest_preflight(manifest)
    checks_run: list[str] = [CHECK_PLUGIN_MANIFEST]
    if repo_root is not None:
        result.failures.extend(run_root_manifest_check(repo_root))
        checks_run.append(CHECK_ROOT_MANIFEST)
    envelope: dict[str, Any] = {
        "probe_version": PROBE_VERSION,
        "release_id": release_id,
        "interpreter": sys.executable,
        "capabilities": list(PROBE_CAPABILITIES),
        "checks_run": checks_run,
        "ok": result.ok,
        "failures": [
            {
                "check": failure.check,
                "plugin": failure.plugin,
                "message": failure.message,
                "error_class": failure.error_class,
            }
            for failure in result.failures
        ],
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    return (EXIT_OK if result.ok else EXIT_PREFLIGHT_FAILURES), envelope


def _repo_root_from_stdin(payload: dict[str, Any]) -> Path | None:
    """Extract ``repo_root``; ``None`` iff the key is absent.

    Absent means the runner predates the contract — permitted, and the
    ``checks_run`` / ``capabilities`` pair records it. Present-but-malformed
    is a runner bug and fails LOUD: degrading it to ``None`` would silently
    skip the check while the runner believed it had asked for it, which is
    the failure this whole change exists to remove.
    """
    if STDIN_KEY_REPO_ROOT not in payload:
        return None
    raw = payload[STDIN_KEY_REPO_ROOT]
    if not isinstance(raw, str) or not raw:
        msg = (
            f"{STDIN_KEY_REPO_ROOT!r} on stdin must be a non-empty string; "
            f"got {raw!r}"
        )
        raise TypeError(msg)
    return Path(raw)


def main() -> int:
    """Read manifest JSON from stdin, emit the envelope on stdout.

    Any exception (unparseable stdin, non-object manifest, an unexpected
    crash inside the preflight machinery) propagates: traceback on
    stderr + a non-{0,3} exit code, which the harness classifies as a
    RED ``ProbeHarnessError`` — fail-LOUD, never degrade to warn.
    """
    manifest_raw: Any = json.loads(sys.stdin.read())
    if not isinstance(manifest_raw, dict):
        raise TypeError(
            "manifest on stdin must be a JSON object; "
            f"got {type(manifest_raw).__name__}"
        )
    exit_code, envelope = build_probe_envelope(
        manifest_raw,
        release_id=os.environ.get(_ENV_RELEASE_ID, ""),
        repo_root=_repo_root_from_stdin(manifest_raw),
    )
    sys.stdout.write(json.dumps(envelope))
    sys.stdout.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
