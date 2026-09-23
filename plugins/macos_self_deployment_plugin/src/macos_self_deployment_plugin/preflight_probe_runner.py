"""GTE-06 L2 fresh-source preflight probe — the blue-side spawning harness.

Runs the release-side probe entrypoint
(``ananta.services.lifecycle_management_service.preflight_probe``) as a
clean subprocess under the CANDIDATE release's own interpreter, feeds it
the on-disk manifest (the source of truth per the
``restart_with_manifest`` ABC) on stdin, and classifies the result.

Design contract (``workbench/2026-07-06_gte06_fresh_source_preflight_probe_design.md`` §5):

* **A5a — the harness NEVER raises.** Every failure mode is a classified
  :class:`ProbeOutcome`. An exception escaping this module would be
  classified core-side as ``plugin_raised``, which routes to the
  leave-the-committed-bytes envelope — bypassing the manifest rollback
  exactly when the probe is least trustworthy. The public entrypoint
  wraps everything.
* **A5b — GREEN is a conjunction:** exit code 0 AND stdout parses as one
  JSON object AND ``probe_version == 1`` AND ``ok is True``. A RED
  preflight verdict additionally requires exit 3, ``ok is False``, and a
  NON-EMPTY well-formed ``failures`` list (validated BEFORE indexing —
  the ``failures[0]`` IndexError vector). Every other combination is a
  RED ``ProbeEnvelopeError`` / ``ProbeHarnessError`` / ``ProbeTimeout``.
* Fail-LOUD: there is no warn-only classification; a broken probe blocks
  the swap.

Env/cwd mirror the green spawn contract (:mod:`child_spawn`): inherited
environment + ``SOLET_NAME`` (module-level vault-name resolution in
agent_messaging fast-fails without it — the GTE-03 class) +
``SOLET_RELEASE_ID``; cwd = the out-of-tree runtime dir. Full child
output is appended to a per-probe log file so a red probe always leaves
a complete diagnostic even when the envelope tail is truncated.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TypeGuard

from ananta.core.plugins.profile_manifest import load_manifest_plugin_set

from macos_self_deployment_plugin.constants import (
    ENV_SOLET_NAME,
    resolve_project_root,
)
from macos_self_deployment_plugin.release_manager import CandidatePaths

PROBE_MODULE: Final[str] = (
    "ananta.services.lifecycle_management_service.preflight_probe"
)
EXPECTED_PROBE_VERSION: Final[int] = 1
PROBE_EXIT_OK: Final[int] = 0
PROBE_EXIT_PREFLIGHT_FAILURES: Final[int] = 3

PROBE_ERROR_TIMEOUT: Final[str] = "ProbeTimeout"
PROBE_ERROR_HARNESS: Final[str] = "ProbeHarnessError"
PROBE_ERROR_ENVELOPE: Final[str] = "ProbeEnvelopeError"

_ENV_RELEASE_ID: Final[str] = "SOLET_RELEASE_ID"
_FAILING_STEP_HARNESS: Final[str] = "harness"
_STDOUT_CAP_BYTES: Final[int] = 262_144
_DETAIL_TAIL_CHARS: Final[int] = 2_000

#: §46.1 — the root-manifest gate now runs INSIDE the probe, under the
#: candidate's interpreter. These mirror ``preflight_probe``'s tokens; they
#: are duplicated rather than imported because this module runs in the
#: OUTGOING process, whose ``ananta`` import is exactly the stale anchor the
#: fix exists to stop trusting.
CHECK_ROOT_MANIFEST: Final[str] = "root_manifest"
_STDIN_KEY_REPO_ROOT: Final[str] = "repo_root"
_ENVELOPE_KEY_CAPABILITIES: Final[str] = "capabilities"
_ENVELOPE_KEY_CHECKS_RUN: Final[str] = "checks_run"
PROBE_ERROR_CHECK_SKIPPED: Final[str] = "ProbeCheckSkipped"


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """Classified probe result.

    ``ok=True`` — probe GREEN; ``payload`` is the Q5 success evidence
    (``{ok, duration_ms, release_id}``) destined for the applied
    envelope. ``ok=False`` — probe RED; ``payload`` is the
    ``RestartResult.probe`` rejection detail
    (``failing_step`` / ``error_class`` / ``detail`` / ``failures`` /
    ``release_id`` / ``duration_ms``).
    """

    ok: bool
    payload: dict[str, Any]


def run_preflight_probe(
    *,
    candidate: CandidatePaths,
    app_home: Path,
    solet_name: str,
    cwd: Path,
    log_path: Path,
    timeout_seconds: float,
    logger: logging.Logger,
) -> ProbeOutcome:
    """Spawn + classify the L2 probe for ``candidate``. NEVER raises (A5a)."""
    try:
        return _run_probe(
            candidate=candidate,
            app_home=app_home,
            solet_name=solet_name,
            cwd=cwd,
            log_path=log_path,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )
    except Exception as exc:  # noqa: BLE001 — A5a containment is the contract
        logger.exception(
            "preflight probe harness raised — contained to a RED outcome",
        )
        return _error_outcome(
            error_class=PROBE_ERROR_HARNESS,
            detail=f"harness raised {type(exc).__name__}: {exc}",
            release_id=candidate.release_id,
        )


def _run_probe(
    *,
    candidate: CandidatePaths,
    app_home: Path,
    solet_name: str,
    cwd: Path,
    log_path: Path,
    timeout_seconds: float,
    logger: logging.Logger,
) -> ProbeOutcome:
    manifest = _manifest_from_disk(app_home)
    env = os.environ.copy()
    env[ENV_SOLET_NAME] = solet_name
    env[_ENV_RELEASE_ID] = candidate.release_id
    proc = subprocess.Popen(
        [str(candidate.venv_python), "-m", PROBE_MODULE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd),
        env=env,
        start_new_session=True,
    )
    try:
        stdout_bytes, stderr_bytes = proc.communicate(
            json.dumps(manifest).encode("utf-8"), timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        stdout_bytes, stderr_bytes = proc.communicate()
        _append_probe_log(
            log_path, exit_code=proc.returncode,
            stdout_bytes=stdout_bytes, stderr_bytes=stderr_bytes,
        )
        logger.error(
            "preflight probe timed out after %.1fs — process group SIGKILLed",
            timeout_seconds,
        )
        return _error_outcome(
            error_class=PROBE_ERROR_TIMEOUT,
            detail=(
                f"probe exceeded {timeout_seconds:.1f}s; process group "
                "SIGKILLed. See the probe log for partial output."
            ),
            release_id=candidate.release_id,
        )
    exit_code = -1 if proc.returncode is None else proc.returncode
    _append_probe_log(
        log_path, exit_code=exit_code,
        stdout_bytes=stdout_bytes, stderr_bytes=stderr_bytes,
    )
    outcome = _classify(
        exit_code=exit_code,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        release_id=candidate.release_id,
    )
    if outcome.payload.get(CHECK_ROOT_MANIFEST) == ROOT_CHECK_UNSUPPORTED:
        logger.warning(
            "DEGRADED: candidate release %s predates the root-manifest probe "
            "contract — the F1 gate did NOT run for this cutover. Permitted "
            "(an older target cannot echo a capability it does not have); the "
            "gate resumes on the next cutover whose target carries it.",
            candidate.release_id,
        )
    return outcome


def _manifest_from_disk(app_home: Path) -> dict[str, Any]:
    """Build the probe's stdin payload from the on-disk source of truth.

    ``repo_root`` rides along so the candidate's interpreter classifies the
    LIVE deployment root (§46.1). Additive and safe across a version-mixed
    cutover in both directions: an older probe's ``_extract_plugin_names``
    reads only ``plugins`` and ignores every other key.
    """
    plugin_set = load_manifest_plugin_set(app_home)
    plugins: Iterable[str] = sorted(plugin_set) if plugin_set is not None else ()
    return {
        "plugins": list(plugins),
        _STDIN_KEY_REPO_ROOT: str(resolve_project_root(app_home).resolve()),
    }


def _kill_process_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the probe's whole process group (it was its own session)."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def _append_probe_log(
    log_path: Path, *, exit_code: int | None,
    stdout_bytes: bytes, stderr_bytes: bytes,
) -> None:
    """Append the full child output — the complete diagnostic surface."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_fh:
        log_fh.write(f"=== preflight probe exit={exit_code} ===\n".encode())
        log_fh.write(b"--- stdout ---\n")
        log_fh.write(stdout_bytes)
        log_fh.write(b"\n--- stderr ---\n")
        log_fh.write(stderr_bytes)
        log_fh.write(b"\n")


def _classify(
    *, exit_code: int, stdout_bytes: bytes, stderr_bytes: bytes,
    release_id: str,
) -> ProbeOutcome:
    """A5b conjunction classification. Validates shape BEFORE indexing."""
    if len(stdout_bytes) > _STDOUT_CAP_BYTES:
        return _error_outcome(
            error_class=PROBE_ERROR_ENVELOPE,
            detail=(
                f"probe stdout exceeded the {_STDOUT_CAP_BYTES}-byte cap "
                "(truncated); full output is in the probe log."
            ),
            release_id=release_id,
            truncated=True,
        )
    if exit_code not in (PROBE_EXIT_OK, PROBE_EXIT_PREFLIGHT_FAILURES):
        stderr_tail = stderr_bytes.decode("utf-8", errors="replace")
        return _error_outcome(
            error_class=PROBE_ERROR_HARNESS,
            detail=f"unexpected probe exit code {exit_code}: {stderr_tail}",
            release_id=release_id,
        )
    envelope = _parse_envelope(stdout_bytes)
    if envelope is None or envelope.get("probe_version") != EXPECTED_PROBE_VERSION:
        return _error_outcome(
            error_class=PROBE_ERROR_ENVELOPE,
            detail=(
                "probe stdout is not a version-"
                f"{EXPECTED_PROBE_VERSION} JSON envelope: "
                f"{stdout_bytes.decode('utf-8', errors='replace')}"
            ),
            release_id=release_id,
        )
    return _classify_envelope(
        exit_code=exit_code, envelope=envelope, release_id=release_id,
    )


def _parse_envelope(stdout_bytes: bytes) -> dict[str, Any] | None:
    try:
        parsed: Any = json.loads(stdout_bytes)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


ROOT_CHECK_RAN: Final[str] = "ran"
ROOT_CHECK_UNSUPPORTED: Final[str] = "unsupported_by_probe"
ROOT_CHECK_SKIPPED: Final[str] = "skipped"


def _root_manifest_check_state(envelope: dict[str, Any]) -> str:
    """Which of the three root-manifest outcomes this envelope reports.

    Discriminates on the probe's POSITIVE self-assertion (``capabilities``),
    never on the absence of ``checks_run`` — absence cannot tell a version
    gap from a skipped check, and a bound that cannot tell those apart is a
    bound in name only.

    * ``UNSUPPORTED`` — the probe does not advertise the check. Expected
      whenever the cutover target predates this contract; permitted, and
      recorded rather than swallowed.
    * ``SKIPPED`` — the probe advertises the check and did not run it. A
      version gap cannot produce this; it is an inconsistency and the caller
      refuses on it.
    * ``RAN`` — advertised and executed.
    """
    capabilities = envelope.get(_ENVELOPE_KEY_CAPABILITIES)
    if not isinstance(capabilities, list) or CHECK_ROOT_MANIFEST not in capabilities:
        return ROOT_CHECK_UNSUPPORTED
    checks_run = envelope.get(_ENVELOPE_KEY_CHECKS_RUN)
    if not isinstance(checks_run, list) or CHECK_ROOT_MANIFEST not in checks_run:
        return ROOT_CHECK_SKIPPED
    return ROOT_CHECK_RAN


def _classify_envelope(
    *, exit_code: int, envelope: dict[str, Any], release_id: str,
) -> ProbeOutcome:
    """Exit code and envelope must AGREE (A5b); disagreement is envelope error."""
    root_check = _root_manifest_check_state(envelope)
    if root_check == ROOT_CHECK_SKIPPED:
        return _error_outcome(
            error_class=PROBE_ERROR_CHECK_SKIPPED,
            detail=(
                f"probe advertises the {CHECK_ROOT_MANIFEST!r} capability but "
                f"did not report it in {_ENVELOPE_KEY_CHECKS_RUN!r} "
                f"({envelope.get(_ENVELOPE_KEY_CHECKS_RUN)!r}) — refusing to "
                "treat an unrun gate as a passed one."
            ),
            release_id=release_id,
        )
    ok_flag = envelope.get("ok")
    if exit_code == PROBE_EXIT_OK and ok_flag is True:
        return ProbeOutcome(ok=True, payload={
            "ok": True,
            "duration_ms": envelope.get("duration_ms"),
            "release_id": release_id,
            CHECK_ROOT_MANIFEST: root_check,
        })
    failures = envelope.get("failures")
    if (
        exit_code == PROBE_EXIT_PREFLIGHT_FAILURES
        and ok_flag is False
        and _failures_well_formed(failures)
    ):
        first: dict[str, Any] = failures[0]
        return ProbeOutcome(ok=False, payload={
            "failing_step": first.get("check"),
            "error_class": first.get("error_class"),
            "detail": first.get("message"),
            "failures": failures,
            "release_id": release_id,
            "duration_ms": envelope.get("duration_ms"),
            CHECK_ROOT_MANIFEST: root_check,
        })
    return _error_outcome(
        error_class=PROBE_ERROR_ENVELOPE,
        detail=(
            f"exit code {exit_code} and envelope disagree "
            f"(ok={ok_flag!r}, failures={envelope.get('failures')!r}) — "
            "refusing to trust the verdict."
        ),
        release_id=release_id,
    )


def _failures_well_formed(failures: object) -> TypeGuard[list[dict[str, Any]]]:
    """NON-EMPTY list of dicts — validated BEFORE ``failures[0]`` (A5a)."""
    return (
        isinstance(failures, list)
        and len(failures) > 0
        and all(isinstance(entry, dict) for entry in failures)
    )


def _error_outcome(
    *, error_class: str, detail: str, release_id: str, truncated: bool = False,
) -> ProbeOutcome:
    payload: dict[str, Any] = {
        "failing_step": _FAILING_STEP_HARNESS,
        "error_class": error_class,
        "detail": detail[-_DETAIL_TAIL_CHARS:],
        "failures": [],
        "release_id": release_id,
    }
    if truncated:
        payload["truncated"] = True
    return ProbeOutcome(ok=False, payload=payload)


__all__ = [
    "CHECK_ROOT_MANIFEST",
    "EXPECTED_PROBE_VERSION",
    "PROBE_ERROR_CHECK_SKIPPED",
    "PROBE_ERROR_ENVELOPE",
    "PROBE_ERROR_HARNESS",
    "PROBE_ERROR_TIMEOUT",
    "PROBE_EXIT_OK",
    "PROBE_EXIT_PREFLIGHT_FAILURES",
    "PROBE_MODULE",
    "ROOT_CHECK_RAN",
    "ROOT_CHECK_SKIPPED",
    "ROOT_CHECK_UNSUPPORTED",
    "ProbeOutcome",
    "run_preflight_probe",
]
