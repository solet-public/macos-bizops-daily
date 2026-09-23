#!/usr/bin/env python3
"""S1 — GTE-06 probe HARNESS smoke: real subprocesses, A5a/A5b pins (no pytest).

Exercises :func:`preflight_probe_runner.run_preflight_probe` against:

* the REAL probe module under the real interpreter — green (no manifest)
  and red (manifest naming a missing entry point, typed
  ``EntryPointMissingError`` with ``failing_step=L1.1_import``);
* SHIM interpreters (tiny executables standing in for a broken candidate
  venv python) for every fold-added classification case: timeout ⇒
  group-kill ⇒ ``ProbeTimeout``; garbage stdout ⇒ ``ProbeEnvelopeError``;
  ``ok=false`` with EMPTY ``failures[]`` (the IndexError vector) ⇒
  ``ProbeEnvelopeError``; exit 0 paired with ``ok=false`` ⇒
  ``ProbeEnvelopeError``; ``probe_version`` mismatch ⇒
  ``ProbeEnvelopeError``; unexpected exit code ⇒ ``ProbeHarnessError``;
  oversized stdout ⇒ ``ProbeEnvelopeError`` + ``truncated``; a missing
  interpreter (Popen raises) ⇒ ``ProbeHarnessError`` — and in every case
  the harness RETURNS, never raises (A5a).
* the probe log file: exists and carries the child output.

Run:
    SOLET_NAME=<name> .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/preflight_probe_harness_smoke.py
"""

from __future__ import annotations

import logging
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

_PLUGIN_SRC = Path(__file__).resolve().parents[1] / "src"
_REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(_PLUGIN_SRC), str(_REPO_ROOT / "ananta" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from macos_self_deployment_plugin.preflight_probe_runner import (  # noqa: E402
    PROBE_ERROR_ENVELOPE,
    PROBE_ERROR_HARNESS,
    PROBE_ERROR_TIMEOUT,
    ProbeOutcome,
    run_preflight_probe,
)
from macos_self_deployment_plugin.release_manager import CandidatePaths  # noqa: E402

_passed = 0
_failed: list[str] = []

_logger = logging.getLogger("preflight_probe_harness_smoke")


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _candidate_for(interpreter: Path) -> CandidatePaths:
    base = interpreter.parent
    return CandidatePaths(
        release_id="rel-harness-smoke",
        release_dir=base,
        code_root=base / "code",
        venv_python=interpreter,
        version_file=base / "VERSION",
        missing_pth_targets=(),
        schema_snapshot=None,
    )


def _write_shim(shim_dir: Path, name: str, body: str) -> Path:
    """Write an executable /bin/sh shim standing in for a candidate python."""
    shim = shim_dir / name
    shim.write_text(f"#!/bin/sh\n{body}\n")
    shim.chmod(
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP,
    )
    return shim


def _run(
    *, interpreter: Path, app_home: Path, tmp: Path,
    timeout_seconds: float = 300.0, log_name: str = "probe.log",
) -> tuple[ProbeOutcome, Path]:
    log_path = tmp / "logs" / log_name
    outcome = run_preflight_probe(
        candidate=_candidate_for(interpreter),
        app_home=app_home,
        solet_name=os.environ["SOLET_NAME"],
        cwd=tmp,
        log_path=log_path,
        timeout_seconds=timeout_seconds,
        logger=_logger,
    )
    return outcome, log_path


def _envelope_shim_body(
    *, version: int, ok: str, failures: str, exit_code: int,
) -> str:
    envelope = (
        f'{{"probe_version": {version}, "ok": {ok}, "failures": {failures}, '
        f'"release_id": "x", "interpreter": "x", "duration_ms": 1}}'
    )
    return f"echo '{envelope}'\nexit {exit_code}"


def _real_probe_cases(
    *, real_python: Path, app_home_green: Path, app_home_red: Path, tmp: Path,
) -> None:
    """[1-3] the REAL probe module: green evidence, log file, typed red."""
    # [1] real probe, GREEN.
    outcome, log_path = _run(
        interpreter=real_python, app_home=app_home_green, tmp=tmp,
        log_name="green.log",
    )
    _check(
        outcome.ok and outcome.payload.get("ok") is True
        and outcome.payload.get("release_id") == "rel-harness-smoke",
        f"[1] real probe GREEN with success evidence ({outcome.payload})",
    )
    _check(
        log_path.is_file() and b"--- stdout ---" in log_path.read_bytes(),
        "[2] probe log file written with child output",
    )

    # [3] real probe, RED — typed preflight failure.
    outcome, _ = _run(
        interpreter=real_python, app_home=app_home_red, tmp=tmp,
        log_name="red.log",
    )
    _check(
        not outcome.ok
        and outcome.payload.get("failing_step") == "L1.1_import"
        and outcome.payload.get("error_class") == "EntryPointMissingError"
        and outcome.payload.get("failures"),
        f"[3] real probe RED: typed EntryPointMissingError rejection "
        f"({outcome.payload.get('error_class')})",
    )


def _timeout_case(*, shims: Path, app_home_green: Path, tmp: Path) -> None:
    """[4] timeout ⇒ process-group SIGKILL ⇒ ProbeTimeout."""
    # [4] timeout → group-kill → ProbeTimeout.
    sleeper = _write_shim(shims, "sleeper", "sleep 60")
    started = time.monotonic()
    outcome, _ = _run(
        interpreter=sleeper, app_home=app_home_green, tmp=tmp,
        timeout_seconds=1.0, log_name="timeout.log",
    )
    elapsed = time.monotonic() - started
    _check(
        not outcome.ok
        and outcome.payload.get("error_class") == PROBE_ERROR_TIMEOUT
        and elapsed < 30.0,
        f"[4] timeout ⇒ group-SIGKILL ⇒ ProbeTimeout in {elapsed:.1f}s "
        f"({outcome.payload.get('error_class')})",
    )


def _envelope_shim_cases(*, shims: Path, app_home_green: Path, tmp: Path) -> None:
    """[5-8,10] A5b conjunction: every envelope violation classifies RED."""
    # [5] garbage stdout ⇒ ProbeEnvelopeError.
    garbage = _write_shim(shims, "garbage", "echo notjson\nexit 0")
    outcome, _ = _run(interpreter=garbage, app_home=app_home_green, tmp=tmp)
    _check(
        not outcome.ok
        and outcome.payload.get("error_class") == PROBE_ERROR_ENVELOPE,
        f"[5] garbage stdout ⇒ ProbeEnvelopeError ({outcome.payload.get('error_class')})",
    )

    # [6] ok=false with EMPTY failures (the IndexError vector) ⇒ envelope RED.
    empty_failures = _write_shim(
        shims, "empty_failures",
        _envelope_shim_body(version=1, ok="false", failures="[]", exit_code=3),
    )
    outcome, _ = _run(interpreter=empty_failures, app_home=app_home_green, tmp=tmp)
    _check(
        not outcome.ok
        and outcome.payload.get("error_class") == PROBE_ERROR_ENVELOPE,
        f"[6] ok=false + EMPTY failures ⇒ ProbeEnvelopeError, no raise "
        f"({outcome.payload.get('error_class')})",
    )

    # [7] exit 0 paired with ok=false ⇒ envelope RED (conjunction).
    disagree = _write_shim(
        shims, "disagree",
        _envelope_shim_body(
            version=1, ok="false",
            failures='[{"check": "c", "plugin": null, "message": "m", "error_class": "E"}]',
            exit_code=0,
        ),
    )
    outcome, _ = _run(interpreter=disagree, app_home=app_home_green, tmp=tmp)
    _check(
        not outcome.ok
        and outcome.payload.get("error_class") == PROBE_ERROR_ENVELOPE,
        f"[7] exit 0 + ok=false disagreement ⇒ ProbeEnvelopeError "
        f"({outcome.payload.get('error_class')})",
    )

    # [8] probe_version mismatch ⇒ envelope RED.
    wrong_version = _write_shim(
        shims, "wrong_version",
        _envelope_shim_body(version=2, ok="true", failures="[]", exit_code=0),
    )
    outcome, _ = _run(interpreter=wrong_version, app_home=app_home_green, tmp=tmp)
    _check(
        not outcome.ok
        and outcome.payload.get("error_class") == PROBE_ERROR_ENVELOPE,
        f"[8] probe_version mismatch ⇒ ProbeEnvelopeError "
        f"({outcome.payload.get('error_class')})",
    )

    # [10] oversized stdout ⇒ envelope RED + truncated flag.
    flood = _write_shim(
        shims, "flood", "head -c 400000 /dev/zero | tr '\\0' 'a'\nexit 0",
    )
    outcome, _ = _run(interpreter=flood, app_home=app_home_green, tmp=tmp)
    _check(
        not outcome.ok
        and outcome.payload.get("error_class") == PROBE_ERROR_ENVELOPE
        and outcome.payload.get("truncated") is True,
        f"[10] oversized stdout ⇒ ProbeEnvelopeError + truncated "
        f"({outcome.payload.get('error_class')})",
    )


def _harness_error_cases(*, shims: Path, app_home_green: Path, tmp: Path) -> None:
    """[9,11] harness-class errors: weird exit code; Popen failure contained."""
    # [9] unexpected exit code ⇒ ProbeHarnessError.
    weird_exit = _write_shim(shims, "weird_exit", "echo boom 1>&2\nexit 7")
    outcome, _ = _run(interpreter=weird_exit, app_home=app_home_green, tmp=tmp)
    _check(
        not outcome.ok
        and outcome.payload.get("error_class") == PROBE_ERROR_HARNESS,
        f"[9] unexpected exit code ⇒ ProbeHarnessError "
        f"({outcome.payload.get('error_class')})",
    )

    # [11] missing interpreter (Popen raises) ⇒ contained ProbeHarnessError.
    outcome, _ = _run(
        interpreter=tmp / "nonexistent" / "python3",
        app_home=app_home_green, tmp=tmp,
    )
    _check(
        not outcome.ok
        and outcome.payload.get("error_class") == PROBE_ERROR_HARNESS,
        f"[11] Popen failure contained: harness RETURNS ProbeHarnessError, "
        f"never raises ({outcome.payload.get('error_class')})",
    )



def run_smoke() -> int:
    print("=== preflight_probe_harness_smoke (S1: A5a never-raise + A5b conjunction) ===")
    real_python = Path(sys.executable)
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        app_home_green = tmp / "app_home_green"  # no manifest → plugins: []
        app_home_green.mkdir()
        app_home_red = tmp / "app_home_red"
        (app_home_red / "config").mkdir(parents=True)
        (app_home_red / "config" / "manifest.yaml").write_text(
            "profile_name: local\nplugins:\n- gte06_smoke_missing_plugin\n",
        )
        shims = tmp / "shims"
        shims.mkdir()
        _real_probe_cases(
            real_python=real_python, app_home_green=app_home_green,
            app_home_red=app_home_red, tmp=tmp,
        )
        _timeout_case(shims=shims, app_home_green=app_home_green, tmp=tmp)
        _envelope_shim_cases(shims=shims, app_home_green=app_home_green, tmp=tmp)
        _harness_error_cases(shims=shims, app_home_green=app_home_green, tmp=tmp)

    print(f"\npreflight_probe_harness_smoke: {_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(run_smoke())
