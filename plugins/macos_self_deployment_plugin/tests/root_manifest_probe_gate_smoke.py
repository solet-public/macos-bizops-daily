#!/usr/bin/env python3
"""§46.1 — the root-manifest gate validates under the CANDIDATE's interpreter.

The defect: ``_preflight_root_manifest`` ran in the OUTGOING process and
validated with whatever that process imported at its own last start. It
therefore refused demonstrably valid manifests immediately after an update —
self-defeating exactly when the zero-downtime path is used.

WHAT REDS EACH GREEN (the mutation, not a restatement of the assertion) —
each of these was RUN against this file, not merely asserted:

* [1-4] revert the check to the outgoing process's imports. A stale anchor
  refuses the manifest that [3] proves valid, so [3] flips.
* [5-7] collapse the discriminator to "is ``root_manifest`` in
  ``checks_run``". Absence cannot separate a version gap from a skipped
  check, so [6] (predates ⇒ permitted) flips.
* [8-9] let a root-manifest failure flatten into the probe's own
  classification. ``status`` becomes ``probe_failed`` — the token core
  actually routes on — so [8] flips and manifest rollback silently switches
  on for root-manifest refusals.
* [13] drop ``repo_root`` from the runner's stdin payload. Note [10] does
  NOT catch this: it builds its own payload, so every probe-side assertion
  stays green while the check silently never runs. [13] exercises the real
  builder and is what reds.

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/root_manifest_probe_gate_smoke.py
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_PLUGIN_SRC = Path(__file__).resolve().parents[1] / "src"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ANANTA_SRC = _REPO_ROOT / "ananta" / "src"
for _p in (str(_PLUGIN_SRC), str(_ANANTA_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ananta.services.lifecycle_management_service.preflight_probe import (  # noqa: E402
    CHECK_ROOT_MANIFEST,
    ERROR_CLASS_ROOT_MANIFEST_DRIFT,
    PROBE_CAPABILITIES,
    STDIN_KEY_REPO_ROOT,
    build_probe_envelope,
    run_root_manifest_check,
)
from macos_self_deployment_plugin.constants import RestartReasonCode  # noqa: E402
from macos_self_deployment_plugin.preflight_probe_runner import (  # noqa: E402
    ROOT_CHECK_RAN,
    ROOT_CHECK_SKIPPED,
    ROOT_CHECK_UNSUPPORTED,
    ProbeOutcome,
    _manifest_from_disk,
    _root_manifest_check_state,
)
from macos_self_deployment_plugin.swap_orchestrator import (  # noqa: E402
    _probe_failed_restart_result,
)

_PROBE_ENTRY = "ananta.services.lifecycle_management_service.preflight_probe"

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
    else:
        _failed.append(label)


_VALID_MANIFEST = """\
schema_version: 1
solet_name: example-solet
universal:
  files: ["pyproject.toml", "root_manifest.yaml"]
  directories: ["ananta"]
platform_managed:
  directories: ["profile"]
sanctioned: []
overrides: []
diagnostic:
  report_categories: ["unknown_root_entries", "missing_universal_entries"]
  ignore_patterns: [".*"]
"""


def _make_root(tmp: Path, *, drifted: bool) -> Path:
    """A solet root whose ONLY difference is an undeclared top-level entry."""
    root = tmp / ("drifted" if drifted else "clean")
    (root / "ananta").mkdir(parents=True)
    (root / "profile").mkdir()
    (root / "pyproject.toml").touch()
    (root / "root_manifest.yaml").write_text(_VALID_MANIFEST, encoding="utf-8")
    if drifted:
        (root / "undeclared_dir").mkdir()
    return root


def _logger() -> logging.Logger:
    log = logging.getLogger("root_manifest_probe_gate_smoke")
    log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


def _drift_outcome() -> ProbeOutcome:
    message = "ROOT MANIFEST CHECK — BLOCKING\n..."
    return ProbeOutcome(ok=False, payload={
        "failing_step": CHECK_ROOT_MANIFEST,
        "error_class": ERROR_CLASS_ROOT_MANIFEST_DRIFT,
        "detail": message,
        "failures": [{
            "check": CHECK_ROOT_MANIFEST,
            "plugin": None,
            "message": message,
            "error_class": ERROR_CLASS_ROOT_MANIFEST_DRIFT,
        }],
        "release_id": "rel-test",
    })


def _probe_subprocess(
    payload: dict[str, Any], cwd: Path
) -> tuple[int, dict[str, Any]]:
    """Run the probe as the harness does: its own process, stdin JSON."""
    proc = subprocess.run(
        [sys.executable, "-m", _PROBE_ENTRY],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        env={"PYTHONPATH": str(_ANANTA_SRC), "PATH": "/usr/bin:/bin"},
        cwd=str(cwd),
    )
    envelope: dict[str, Any] = json.loads(proc.stdout) if proc.stdout else {}
    return proc.returncode, envelope


def _test_check_itself(clean_root: Path, drifted_root: Path) -> None:
    """[1-4] the check, run by the interpreter that would run the new code."""
    clean_failures = run_root_manifest_check(clean_root)
    _check(clean_failures == [], "[1] clean root ⇒ no failures")

    drift_failures = run_root_manifest_check(drifted_root)
    _check(len(drift_failures) == 1, "[2] drifted root ⇒ exactly one failure")

    # The manifest a stale anchor would refuse is ACCEPTED here.
    _check(
        not any(f.check == CHECK_ROOT_MANIFEST for f in clean_failures),
        "[3] the valid manifest is ACCEPTED by the validating interpreter",
    )

    if not drift_failures:
        _check(False, "[4] failure shape — no failure produced")
        return
    failure = drift_failures[0]
    _check(
        failure.check == CHECK_ROOT_MANIFEST
        and failure.plugin is None
        and failure.error_class == ERROR_CLASS_ROOT_MANIFEST_DRIFT,
        f"[4] failure shape: check/plugin=None/error_class (got {failure!r})",
    )


def _test_discriminator() -> None:
    """[5-7] three states, keyed on the probe's POSITIVE self-assertion."""
    _check(
        _root_manifest_check_state({
            "capabilities": list(PROBE_CAPABILITIES),
            "checks_run": ["plugin_manifest", CHECK_ROOT_MANIFEST],
        }) == ROOT_CHECK_RAN,
        "[5] advertised + run ⇒ RAN",
    )
    # Predates the contract: no capabilities key at all.
    _check(
        _root_manifest_check_state({"ok": True}) == ROOT_CHECK_UNSUPPORTED,
        "[6] predates the contract ⇒ UNSUPPORTED (permitted, not refused)",
    )
    # Advertised but not run — a version gap CANNOT produce this.
    _check(
        _root_manifest_check_state({
            "capabilities": list(PROBE_CAPABILITIES),
            "checks_run": ["plugin_manifest"],
        }) == ROOT_CHECK_SKIPPED,
        "[7] advertised + not run ⇒ SKIPPED (refused)",
    )


def _test_classification_preserved() -> None:
    """[8-9] ``status`` is what core routes on — it must NOT flatten.

    ``service.py`` branches on ``restart_status == "probe_failed"`` to roll
    the committed manifest bytes back. Root-manifest drift is a property of
    the deployment ROOT, not of those bytes, and must not reach that branch.
    """
    result = _probe_failed_restart_result(
        _drift_outcome(), reason="test", expected_etag="etag", logger=_logger(),
    )
    _check(
        result.status.value == "failed"
        and result.reason_code == RestartReasonCode.ROOT_MANIFEST_DRIFT,
        f"[8] root drift ⇒ FAILED + root_manifest_drift "
        f"(got {result.status.value}/{result.reason_code})",
    )
    _check(
        result.status.value != "probe_failed",
        "[8b] root drift does NOT enter the manifest-rollback branch",
    )

    # A non-root rejection keeps the probe's own classification — guards
    # against having flattened everything the other way.
    other = ProbeOutcome(ok=False, payload={
        "failing_step": "plugin_manifest",
        "error_class": "ImportError",
        "detail": "boom",
        "failures": [{
            "check": "plugin_manifest", "plugin": "p",
            "message": "boom", "error_class": "ImportError",
        }],
        "release_id": "rel-test",
    })
    other_result = _probe_failed_restart_result(
        other, reason="test", expected_etag="etag", logger=_logger(),
    )
    _check(
        other_result.status.value == "probe_failed"
        and other_result.reason_code == RestartReasonCode.PROBE_REJECTED,
        f"[9] non-root rejection still PROBE_FAILED/probe_rejected "
        f"(got {other_result.status.value}/{other_result.reason_code})",
    )


def _test_subprocess_contract(tmp: Path, drifted_root: Path) -> None:
    """[10-12] the real subprocess envelope contract."""
    code, envelope = _probe_subprocess(
        {"plugins": [], STDIN_KEY_REPO_ROOT: str(drifted_root)}, tmp,
    )
    checks_run = envelope.get("checks_run", [])
    failures = envelope.get("failures", [])
    _check(
        code == 3
        and CHECK_ROOT_MANIFEST in checks_run
        and any(f.get("check") == CHECK_ROOT_MANIFEST for f in failures),
        f"[10] subprocess reports the drift (exit={code}, checks_run={checks_run})",
    )

    _, env2 = _probe_subprocess({"plugins": []}, tmp)
    _check(
        CHECK_ROOT_MANIFEST not in env2.get("checks_run", [])
        and CHECK_ROOT_MANIFEST in env2.get("capabilities", []),
        "[10b] no repo_root ⇒ not run, but still ADVERTISED "
        "(the pair separating a version gap from a skip)",
    )

    # Present-but-malformed must fail LOUD, never degrade to "not asked".
    code3, _ = _probe_subprocess({"plugins": [], STDIN_KEY_REPO_ROOT: ""}, tmp)
    _check(
        code3 not in (0, 3),
        f"[11] malformed repo_root ⇒ loud harness error (got exit {code3})",
    )

    _, direct = build_probe_envelope(
        {"plugins": []}, release_id="r", repo_root=None,
    )
    _check(
        CHECK_ROOT_MANIFEST in direct.get("capabilities", []),
        "[12] capabilities advertise root_manifest regardless of the ask",
    )


def _test_runner_sends_repo_root(clean_root: Path) -> None:
    """[13] the PRODUCTION payload builder actually carries ``repo_root``.

    [10] feeds the probe a payload it builds itself, so it cannot see a
    runner that stops SENDING repo_root — the gate would silently never run
    while every probe-side assertion stayed green. This is the assertion that
    reds on that mutation.
    """
    built = _manifest_from_disk(clean_root / "profile")
    _check(
        built.get(STDIN_KEY_REPO_ROOT) == str(clean_root.resolve()),
        f"[13] runner's payload carries repo_root = the resolved solet root "
        f"(got {built.get(STDIN_KEY_REPO_ROOT)!r})",
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        clean_root = _make_root(tmp, drifted=False)
        drifted_root = _make_root(tmp, drifted=True)

        _test_check_itself(clean_root, drifted_root)
        _test_discriminator()
        _test_classification_preserved()
        _test_subprocess_contract(tmp, drifted_root)
        _test_runner_sends_repo_root(clean_root)

    print(f"root_manifest_probe_gate_smoke: {_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAIL  {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
