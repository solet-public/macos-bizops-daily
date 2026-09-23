"""Passive genesis-marker advisories over a target's two durable marker records.

Detection coverage for the genesis-marker rows of the 2026-09-02 doctor-detection
audit.  Every check here is REPORT-ONLY, for the reason the vintage census gives:
a marker divergence names a record-keeping fault, not a failed completion
contract, so it must be reported loudly without retroactively refusing an
installation whose own contract verified.

Why there are two records at all
--------------------------------
A genesis run writes its history TWICE, to two different files, with two
different shapes:

* ``profile/data/github_midwife/attempt.json`` -- the RICHER record.  Written by
  ``_finalize_marker`` from ``steps + phases``, so it carries the post-spine
  phase records (``credential_seed``, ``install_router``, ``install_autostart``,
  ``install_command_launcher``, ``git_init``, ``vault_passphrase``,
  ``vault_stale_check``) alongside a top-level ``status``.
* ``.solet/genesis.json`` -- the POORER record.  Written by
  ``_write_genesis_marker`` from ``result["steps"]`` alone, which is the spine
  ONLY.  Every phase record is dropped.

The manager's ``genesis_artifacts_valid`` probe and the installation doctor both
key on ``.solet/genesis.json`` -- the poorer one.  Nothing anywhere compared the
two.  This module is that missing comparator.

Rows detected
-------------
``iss_3de66b3c`` (D-8.F2) -- two genesis marker writers, no comparator.  The
divergence is not hypothetical and it is not merely cosmetic: a post-spine phase
failure is CAUGHT and appended as ``{"status": "failed"}`` rather than raised,
and ``_finalize_marker("success")`` then runs unconditionally.  So the richer
record can read ``status="success"`` while carrying failed phase records, and the
poorer record -- the one every probe reads -- omits those phases entirely.  A
target whose router or credential seeding failed presents as a clean completion
to everything downstream.  :func:`_marker_pair_agreement_advisory` names it.

``iss_c010206a`` (D-8-LATENT) -- ``_marker_steps_valid`` grades a durable target
marker by pinning EXACT COUNT and POSITIONAL ORDER against the manager's current
``_GENESIS_COMPLETED_STEPS``, and the marker carries no step-list vintage of its
own.  A step-list change therefore invalidates every pre-existing marker at once,
and the grader cannot tell "the manager's step list moved" from "this install
broke".  :func:`_marker_step_vintage_advisory` records the discriminator the
artifact itself is missing.

What this module does NOT claim
-------------------------------
It does not compare a target's marker against the manager's *current* step list,
which is what would let it call ``iss_c010206a`` fired rather than armed.  That
list is ``GENESIS_STEP_RUNNERS`` inside the midwife plugin, and the manager never
imports the plugin -- it reaches it only by path into the TARGET's own checkout
(``adapters.py``), so the only copy reachable from here is the target's, not the
grading manager's.  Reading it would also mean scanning source for a behavioural
property, which measures the wrong thing.  The honest target-readable check is
the one implemented: record the spine this marker actually claims, and say
plainly that no vintage field exists to distinguish the two failure causes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .doctor_advisory_result import advisory_unknown, advisory_verified, advisory_warn
from .models import InstanceRecord, JsonValue

# Both paths are owned by the midwife plugin, which the manager does not import
# (see the module docstring).  They are spelled here as literals for the same
# reason ``doctor_blue_green_census`` spells the release-ledger filenames: the
# plugin's ``MANIFEST_MARKER_PATH`` and ``_write_genesis_marker`` are the sources
# of truth, and a drift between them and these constants is what the smoke pins.
_GENESIS_MARKER_RELPATH = Path(".solet") / "genesis.json"
_ATTEMPT_MARKER_RELPATH = Path("profile") / "data" / "github_midwife" / "attempt.json"

_PAIR_CHECK_ID = "doctor::genesis_marker_pair_agreement_v1"
_VINTAGE_CHECK_ID = "doctor::genesis_marker_step_vintage_v1"

# ``skipped`` is a real, benign outcome (``install_autostart`` records it when
# autostart was not requested).  Only ``failed`` is a divergence; anything else
# is reported as unrecognised rather than silently folded into either bucket.
_BENIGN_STEP_STATUSES = frozenset({"completed", "skipped"})
_FAILED_STEP_STATUS = "failed"

_MARKER_SUCCESS_STATUS = "success"


def collect_genesis_marker_advisories(
    record: InstanceRecord,
    *,
    target_root: Path | None = None,
) -> list[JsonValue]:
    """Return report-only checks over the target's pair of genesis markers."""

    root = Path(record.target) if target_root is None else target_root
    genesis_path = root / _GENESIS_MARKER_RELPATH
    attempt_path = root / _ATTEMPT_MARKER_RELPATH
    return [
        _marker_pair_agreement_advisory(record, genesis_path, attempt_path),
        _marker_step_vintage_advisory(genesis_path),
    ]


def _read_marker(path: Path) -> dict[str, Any] | None:
    """Return the parsed marker, or ``None`` when the file is absent.

    A present-but-unparseable marker raises rather than reading as absent: an
    unreadable record is not a missing record, and collapsing the two is the
    false-green shape this campaign exists to remove.
    """

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"marker at {path} is {type(value).__name__}, not an object")
    return value


def _step_pairs(marker: dict[str, Any]) -> list[tuple[str, str]]:
    """Return ``(step_name, status)`` for each well-formed step record."""

    steps = marker.get("steps")
    if not isinstance(steps, list):
        return []
    pairs: list[tuple[str, str]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        name = step.get("step_name")
        status = step.get("status")
        if isinstance(name, str) and isinstance(status, str):
            pairs.append((name, status))
    return pairs


def _marker_pair_agreement_advisory(
    record: InstanceRecord, genesis_path: Path, attempt_path: Path
) -> dict[str, JsonValue]:
    """Compare the poorer record every probe reads against the richer one."""

    source = f"{genesis_path} + {attempt_path}"
    expected: dict[str, JsonValue] = {
        "solet_name": record.name,
        "attempt_status": _MARKER_SUCCESS_STATUS,
        "phases_absent_from_genesis_marker": [],
    }
    observed: dict[str, JsonValue] = {
        "genesis_marker_present": False,
        "attempt_marker_present": False,
        "attempt_status": None,
        "failed_records_hidden_from_probe": [],
    }

    try:
        genesis = _read_marker(genesis_path)
        attempt = _read_marker(attempt_path)
    except (OSError, ValueError) as exc:
        return advisory_unknown(
            _PAIR_CHECK_ID,
            "A genesis marker could not be read, so the two records cannot be compared.",
            expected,
            observed,
            source,
            "genesis_marker_unreadable",
            str(exc),
        )

    observed["genesis_marker_present"] = genesis is not None
    observed["attempt_marker_present"] = attempt is not None

    if genesis is None and attempt is None:
        return advisory_verified(
            _PAIR_CHECK_ID,
            "The target carries neither genesis marker, so there is no pair to disagree.",
            expected,
            observed,
            source,
        )

    if genesis is None:
        return advisory_warn(
            _PAIR_CHECK_ID,
            "The attempt record exists but .solet/genesis.json does not, so every probe "
            "that keys on the completion marker cannot verify a genesis that ran.",
            expected,
            observed,
            source,
            "genesis_marker_missing_from_pair",
            "Re-run genesis, or write .solet/genesis.json from the attempt record; the "
            "probe and the installation doctor both key on the file that is absent.",
        )

    if attempt is None:
        return advisory_warn(
            _PAIR_CHECK_ID,
            "Only the poorer record exists: .solet/genesis.json is present with no attempt "
            "record, so no post-spine phase outcome was retained anywhere on this target.",
            expected,
            observed,
            source,
            "attempt_marker_missing_from_pair",
            "Treat post-spine phase state (router, credential seeding, autostart, git init) "
            "as unverified for this target; the record that would carry it was never written.",
        )

    return _compare_present_pair(record, genesis, attempt, expected, observed, source)


def _compare_present_pair(
    record: InstanceRecord,
    genesis: dict[str, Any],
    attempt: dict[str, Any],
    expected: dict[str, JsonValue],
    observed: dict[str, JsonValue],
    source: str,
) -> dict[str, JsonValue]:
    """Grade a target that carries BOTH records, in order of severity.

    Split out of :func:`_marker_pair_agreement_advisory` to keep each function
    inside the radon-cc gate's A/B band; the gate's allowlist is tracked debt,
    not a skip path, so new code earns its rank rather than registering for an
    exemption.
    """

    attempt_status = attempt.get("status")
    observed["attempt_status"] = attempt_status if isinstance(attempt_status, str) else None

    hidden_failures, unrecognised = _scan_attempt_steps(genesis, attempt)
    identity_mismatches = _identity_mismatches(record, genesis, attempt)
    observed["failed_records_hidden_from_probe"] = hidden_failures
    observed["unrecognised_step_statuses"] = unrecognised
    observed["identity_mismatches"] = identity_mismatches

    # Ordered by severity: the first matching divergence is the one reported.
    # Expressed as data so adding a shape cannot quietly raise this function's
    # complexity rank past the radon-cc gate.
    divergences: tuple[tuple[bool, str, str, str], ...] = (
        (
            bool(identity_mismatches),
            "genesis_marker_identity_disagreement",
            "The two genesis markers disagree about which solet or profile they describe.",
            "Do not trust either record for this target until the disagreement is resolved; "
            "one of them was written for a different install.",
        ),
        (
            bool(hidden_failures),
            "failed_phase_hidden_from_completion_marker",
            "The attempt record names failed genesis phases that .solet/genesis.json omits "
            "entirely, so the marker every probe reads presents this install as clean.",
            "Treat the named phases as not installed and repair them specifically; a genesis "
            "re-run is not required to learn which ones failed -- the attempt record has them.",
        ),
        (
            attempt_status != _MARKER_SUCCESS_STATUS,
            "completion_marker_contradicts_attempt_status",
            "The completion marker exists while the attempt record does not report success.",
            "Re-check the attempt record's final status before treating this target as "
            "genesis-complete; the file the probe reads carries no status of its own.",
        ),
        (
            bool(unrecognised),
            "unrecognised_genesis_step_status",
            "The attempt record carries step statuses this check does not recognise, so the "
            "pair cannot be graded as agreeing.",
            "Extend the recognised status vocabulary, or correct the writer; an ungraded "
            "status must not be read as a passing one.",
        ),
    )

    for matched, reason_code, summary, repair in divergences:
        if matched:
            return advisory_warn(
                _PAIR_CHECK_ID, summary, expected, observed, source, reason_code, repair
            )

    return advisory_verified(
        _PAIR_CHECK_ID,
        "Both genesis markers are present, agree on identity, and the richer record names no "
        "failed phase that the completion marker hides.",
        expected,
        observed,
        source,
    )


def _scan_attempt_steps(
    genesis: dict[str, Any], attempt: dict[str, Any]
) -> tuple[list[JsonValue], list[JsonValue]]:
    """Return the attempt's failed-but-hidden phases and its ungraded statuses.

    A failure the completion marker ALSO carries is not hidden -- the vintage
    check names that one -- so only names absent from the poorer record count.
    """

    genesis_names = {name for name, _status in _step_pairs(genesis)}
    hidden_failures: list[JsonValue] = []
    unrecognised: list[JsonValue] = []
    for name, status in _step_pairs(attempt):
        if status == _FAILED_STEP_STATUS and name not in genesis_names:
            hidden_failures.append(name)
        elif status != _FAILED_STEP_STATUS and status not in _BENIGN_STEP_STATUSES:
            unrecognised.append(f"{name}={status}")
    return hidden_failures, unrecognised


def _identity_mismatches(
    record: InstanceRecord, genesis: dict[str, Any], attempt: dict[str, Any]
) -> list[JsonValue]:
    """Name every identity field on which the records disagree.

    The two writers spell the profile differently -- ``profile`` in
    ``.solet/genesis.json``, ``profile_name`` in the attempt record -- which is
    itself part of why nothing compared them.
    """

    mismatches: list[JsonValue] = []
    genesis_name = genesis.get("solet_name")
    attempt_name = attempt.get("solet_name")
    if genesis_name != attempt_name:
        mismatches.append(f"solet_name: genesis={genesis_name!r} attempt={attempt_name!r}")
    elif genesis_name != record.name:
        mismatches.append(f"solet_name: markers={genesis_name!r} registry={record.name!r}")
    genesis_profile = genesis.get("profile")
    attempt_profile = attempt.get("profile_name")
    if genesis_profile != attempt_profile:
        mismatches.append(f"profile: genesis={genesis_profile!r} attempt={attempt_profile!r}")
    return mismatches


def _marker_step_vintage_advisory(genesis_path: Path) -> dict[str, JsonValue]:
    """Record the step spine this marker claims, and the vintage field it lacks."""

    source = str(genesis_path)
    expected: dict[str, JsonValue] = {
        "step_list_vintage_field": "absent by design of the current marker schema",
        "all_steps_completed": True,
    }
    observed: dict[str, JsonValue] = {
        "recorded_steps": [],
        "recorded_step_count": 0,
        "incomplete_steps": [],
    }

    try:
        genesis = _read_marker(genesis_path)
    except (OSError, ValueError) as exc:
        return advisory_unknown(
            _VINTAGE_CHECK_ID,
            "The genesis completion marker could not be read, so its step spine is unknown.",
            expected,
            observed,
            source,
            "genesis_marker_unreadable",
            str(exc),
        )

    if genesis is None:
        return advisory_verified(
            _VINTAGE_CHECK_ID,
            "The target carries no genesis completion marker, so no marker can be invalidated "
            "by a step-list change.",
            expected,
            observed,
            source,
        )

    pairs = _step_pairs(genesis)
    recorded: list[JsonValue] = [name for name, _status in pairs]
    incomplete: list[JsonValue] = [
        f"{name}={status}" for name, status in pairs if status != "completed"
    ]
    observed["recorded_steps"] = recorded
    observed["recorded_step_count"] = len(pairs)
    observed["incomplete_steps"] = incomplete

    if incomplete:
        return advisory_warn(
            _VINTAGE_CHECK_ID,
            "The completion marker records steps that are not completed, so the validator "
            "will reject it -- and here the cause is visible in the marker itself.",
            expected,
            observed,
            source,
            "genesis_marker_step_incomplete",
            "Repair the named steps; this rejection is a genuinely incomplete install, NOT "
            "the stale-step-list case the validator cannot otherwise distinguish.",
        )

    return advisory_verified(
        _VINTAGE_CHECK_ID,
        "The completion marker records a fully completed step spine; it is retained here so a "
        "later step-list change can be diffed against the spine this target actually claims.",
        expected,
        observed,
        source,
    )
