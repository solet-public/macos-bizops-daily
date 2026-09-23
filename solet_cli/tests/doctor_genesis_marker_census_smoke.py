"""The two genesis marker records are actually compared.

Detection coverage for ``iss_3de66b3c`` (D-8.F2) and ``iss_c010206a``
(D-8-LATENT).

A genesis run writes its history twice.  ``_finalize_marker`` writes the RICHER
record (``profile/data/github_midwife/attempt.json``) from ``steps + phases``;
``_write_genesis_marker`` writes the POORER one (``.solet/genesis.json``) from
``result["steps"]`` -- the spine alone, with every phase record dropped.  The
manager's ``genesis_artifacts_valid`` probe and the installation doctor both key
on the poorer file, and nothing anywhere compared the two.

The defect has teeth because post-spine phase failures are CAUGHT and appended as
``{"status": "failed"}`` rather than raised, and ``_finalize_marker("success")``
then runs unconditionally.  So a target whose router install failed carries an
attempt record reading ``status="success"`` WITH a failed ``install_router``
phase, and a completion marker that mentions no such phase at all.  Every probe
downstream sees a clean install.  That silence is the defect; this smoke pins the
comparator that ends it.

The discriminator is deliberately not "the check went warn".  A check that warned
on every target would pass a warn-only assertion while being useless, so each red
asserts its NAMED ``reason_code``, and the divergence shapes are required to
produce DIFFERENT names:

* a failed phase the completion marker omits -> failed_phase_hidden_from_completion_marker
* the completion marker missing entirely     -> genesis_marker_missing_from_pair
* the attempt record missing entirely        -> attempt_marker_missing_from_pair
* the two records describing different installs -> genesis_marker_identity_disagreement
* an attempt record that never reported success -> completion_marker_contradicts_attempt_status
* a spine step that is not completed          -> genesis_marker_step_incomplete

Offline: constructed marker files under a temporary directory only.  Nothing here
reads a real target, a network, or the midwife plugin.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.doctor_genesis_marker_census import (  # noqa: E402
    collect_genesis_marker_advisories,
)

_PAIR_ID = "doctor::genesis_marker_pair_agreement_v1"
_VINTAGE_ID = "doctor::genesis_marker_step_vintage_v1"

_SPINE = (
    "preflight",
    "clone",
    "profile",
    "venv",
    "knowledge_base",
    "spine_complete",
)

_CHECKS = 0


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


class _Record:
    """The two fields the genesis-marker census reads off a registry record."""

    name = "census"
    target = "/nonexistent/census-target"


def _spine_steps(status: str = "completed") -> list[dict[str, str]]:
    return [{"step_name": name, "status": status} for name in _SPINE]


def _write_genesis(
    root: Path, *, steps: list[dict[str, str]], name: str = "census", profile: str = "default"
) -> None:
    marker = root / ".solet"
    marker.mkdir(parents=True, exist_ok=True)
    (marker / "genesis.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "solet_name": name,
                "profile": profile,
                "completed_at": "2026-09-05T00:00:00+00:00",
                "steps": steps,
            },
            indent=1,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_attempt(
    root: Path,
    *,
    steps: list[dict[str, str]],
    status: str = "success",
    name: str = "census",
    profile_name: str = "default",
) -> None:
    marker = root / "profile" / "data" / "github_midwife"
    marker.mkdir(parents=True, exist_ok=True)
    (marker / "attempt.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "solet_name": name,
                "profile_name": profile_name,
                "status": status,
                "steps": steps,
                "written_at": "2026-09-05T00:00:00+00:00",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _advisories(root: Path) -> dict[str, dict[str, object]]:
    """Index every emitted advisory by its check_id.

    Selecting by id rather than by list position means adding a check can never
    silently re-point an existing assertion at a different check.
    """

    results = collect_genesis_marker_advisories(_Record(), target_root=root)
    by_id: dict[str, dict[str, object]] = {}
    for entry in results:
        assert isinstance(entry, dict)
        check_id = str(entry["check_id"])
        _check(check_id not in by_id, f"duplicate advisory id emitted: {check_id}")
        by_id[check_id] = entry
    return by_id


def _pair(root: Path) -> dict[str, object]:
    return _advisories(root)[_PAIR_ID]


def _vintage(root: Path) -> dict[str, object]:
    return _advisories(root)[_VINTAGE_ID]


def _assert_agreeing_pair_is_green() -> None:
    """A target whose two records agree must not be warned about."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_genesis(root, steps=_spine_steps())
        _write_attempt(
            root,
            steps=_spine_steps() + [{"step_name": "install_router", "status": "completed"}],
        )
        advisory = _pair(root)
        _check(
            advisory["status"] == "verified",
            f"an agreeing marker pair was not verified: {advisory['status']}",
        )
        _check(
            advisory["blocking"] is False,
            "a genesis-marker advisory must never block a doctor result",
        )


def _assert_hidden_failed_phase_is_named() -> None:
    """The headline: a failed phase the completion marker omits entirely.

    This is the exact residue of ``iss_3de66b3c`` -- attempt says success, a
    phase inside it failed, and the file every probe reads does not mention it.
    """

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_genesis(root, steps=_spine_steps())
        _write_attempt(
            root,
            status="success",
            steps=_spine_steps()
            + [
                {"step_name": "credential_seed", "status": "completed"},
                {"step_name": "install_router", "status": "failed"},
            ],
        )
        advisory = _pair(root)
        _check(
            advisory["reason_code"] == "failed_phase_hidden_from_completion_marker",
            f"a hidden failed phase was not named: {advisory['reason_code']}",
        )
        observed = advisory["observed"]
        assert isinstance(observed, dict)
        _check(
            observed["failed_records_hidden_from_probe"] == ["install_router"],
            f"the hidden phase was not identified: {observed['failed_records_hidden_from_probe']}",
        )
        _check(
            observed["attempt_status"] == "success",
            "the advisory must record that the attempt claimed success while hiding a failure",
        )


def _assert_missing_completion_marker_is_a_different_name() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_attempt(root, steps=_spine_steps())
        advisory = _pair(root)
        _check(
            advisory["reason_code"] == "genesis_marker_missing_from_pair",
            f"an absent completion marker was not named: {advisory['reason_code']}",
        )


def _assert_missing_attempt_record_is_a_different_name() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_genesis(root, steps=_spine_steps())
        advisory = _pair(root)
        _check(
            advisory["reason_code"] == "attempt_marker_missing_from_pair",
            f"an absent attempt record was not named: {advisory['reason_code']}",
        )


def _assert_identity_disagreement_is_named() -> None:
    """Two records describing different installs must not be graded as agreeing."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_genesis(root, steps=_spine_steps(), profile="default")
        _write_attempt(root, steps=_spine_steps(), profile_name="other-profile")
        advisory = _pair(root)
        _check(
            advisory["reason_code"] == "genesis_marker_identity_disagreement",
            f"a profile disagreement was not named: {advisory['reason_code']}",
        )


def _assert_unsuccessful_attempt_is_named() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_genesis(root, steps=_spine_steps())
        _write_attempt(root, steps=_spine_steps(), status="failed")
        advisory = _pair(root)
        _check(
            advisory["reason_code"] == "completion_marker_contradicts_attempt_status",
            f"a non-success attempt status was not named: {advisory['reason_code']}",
        )


def _assert_skipped_phase_is_not_a_divergence() -> None:
    """``skipped`` is a real benign outcome and must not read as a failure."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_genesis(root, steps=_spine_steps())
        _write_attempt(
            root,
            steps=_spine_steps() + [{"step_name": "install_autostart", "status": "skipped"}],
        )
        advisory = _pair(root)
        _check(
            advisory["status"] == "verified",
            f"a skipped phase was misread as a divergence: {advisory['reason_code']}",
        )


def _assert_unrecognised_status_is_not_green() -> None:
    """An ungraded status must never be folded into the passing bucket."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_genesis(root, steps=_spine_steps())
        _write_attempt(
            root,
            steps=_spine_steps() + [{"step_name": "install_router", "status": "partial"}],
        )
        advisory = _pair(root)
        _check(
            advisory["reason_code"] == "unrecognised_genesis_step_status",
            f"an unrecognised status was not named: {advisory['reason_code']}",
        )


def _assert_absent_pair_is_not_a_divergence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        advisory = _pair(root)
        _check(
            advisory["status"] == "verified",
            f"a target with no markers at all was warned about: {advisory['reason_code']}",
        )


def _assert_unreadable_marker_is_unknown_not_green() -> None:
    """An unreadable record is not a passing record."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        marker = root / ".solet"
        marker.mkdir(parents=True, exist_ok=True)
        (marker / "genesis.json").write_text("{not json", encoding="utf-8")
        _write_attempt(root, steps=_spine_steps())
        advisory = _pair(root)
        _check(
            advisory["status"] == "unknown",
            f"an unparseable marker did not read as unknown: {advisory['status']}",
        )
        _check(
            advisory["reason_code"] == "genesis_marker_unreadable",
            f"an unparseable marker was not named: {advisory['reason_code']}",
        )


def _assert_unopenable_marker_is_unknown_not_green() -> None:
    """A marker that cannot be OPENED is a separate path from one that cannot be PARSED.

    ``_read_marker`` swallows only ``FileNotFoundError``; every other ``OSError``
    -- a permission-denied marker being the realistic one -- must surface as
    ``unknown``.  Without this fixture that success path is never exercised, and
    widening the swallowed exceptions to ``OSError`` would silently turn an
    unreadable marker into an absent one, which reads as green.
    """

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_genesis(root, steps=_spine_steps())
        _write_attempt(root, steps=_spine_steps())
        marker = root / ".solet" / "genesis.json"
        marker.chmod(0o000)
        try:
            if marker.read_text(encoding="utf-8"):  # pragma: no cover - root ignores chmod
                return
        except PermissionError:
            pass
        try:
            advisory = _pair(root)
            _check(
                advisory["status"] == "unknown",
                f"an unopenable marker did not read as unknown: {advisory['status']}",
            )
            _check(
                advisory["reason_code"] == "genesis_marker_unreadable",
                f"an unopenable marker was not named: {advisory['reason_code']}",
            )
        finally:
            marker.chmod(0o600)


def _assert_completed_spine_is_recorded_for_diffing() -> None:
    """The vintage check retains the spine this target actually claims."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_genesis(root, steps=_spine_steps())
        advisory = _vintage(root)
        _check(
            advisory["status"] == "verified",
            f"a fully completed spine was not verified: {advisory['status']}",
        )
        observed = advisory["observed"]
        assert isinstance(observed, dict)
        _check(
            observed["recorded_steps"] == list(_SPINE),
            f"the recorded spine was not retained: {observed['recorded_steps']}",
        )
        _check(
            observed["recorded_step_count"] == len(_SPINE),
            f"the recorded step count is wrong: {observed['recorded_step_count']}",
        )


def _assert_incomplete_spine_step_is_named() -> None:
    """A visibly incomplete step is the case the validator CAN distinguish."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        steps = _spine_steps()
        steps[3] = {"step_name": _SPINE[3], "status": "failed"}
        _write_genesis(root, steps=steps)
        advisory = _vintage(root)
        _check(
            advisory["reason_code"] == "genesis_marker_step_incomplete",
            f"an incomplete spine step was not named: {advisory['reason_code']}",
        )
        observed = advisory["observed"]
        assert isinstance(observed, dict)
        _check(
            observed["incomplete_steps"] == [f"{_SPINE[3]}=failed"],
            f"the incomplete step was not identified: {observed['incomplete_steps']}",
        )


def _assert_failed_spine_step_is_not_double_reported() -> None:
    """A failure the completion marker DOES carry is not a hidden one.

    Guards the comparator against over-reporting: the pair check stays quiet
    because nothing is hidden, and the vintage check is the one that names it.
    """

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        steps = _spine_steps()
        steps[1] = {"step_name": _SPINE[1], "status": "failed"}
        _write_genesis(root, steps=steps)
        _write_attempt(root, steps=steps, status="success")
        pair = _pair(root)
        observed = pair["observed"]
        assert isinstance(observed, dict)
        hidden = observed["failed_records_hidden_from_probe"]
        _check(hidden == [], f"a visible failure was reported as hidden: {hidden}")
        _check(
            _vintage(root)["reason_code"] == "genesis_marker_step_incomplete",
            "the vintage check must be the one that names a visible spine failure",
        )


def _assert_both_checks_are_always_emitted() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_genesis(root, steps=_spine_steps())
        _write_attempt(root, steps=_spine_steps())
        ids = set(_advisories(root))
        _check(
            ids == {_PAIR_ID, _VINTAGE_ID},
            f"the census stopped emitting one of its checks: {sorted(ids)}",
        )


def main() -> int:
    _assert_agreeing_pair_is_green()
    _assert_hidden_failed_phase_is_named()
    _assert_missing_completion_marker_is_a_different_name()
    _assert_missing_attempt_record_is_a_different_name()
    _assert_identity_disagreement_is_named()
    _assert_unsuccessful_attempt_is_named()
    _assert_skipped_phase_is_not_a_divergence()
    _assert_unrecognised_status_is_not_green()
    _assert_absent_pair_is_not_a_divergence()
    _assert_unreadable_marker_is_unknown_not_green()
    _assert_unopenable_marker_is_unknown_not_green()
    _assert_completed_spine_is_recorded_for_diffing()
    _assert_incomplete_spine_step_is_named()
    _assert_failed_spine_step_is_not_double_reported()
    _assert_both_checks_are_always_emitted()
    print(f"doctor_genesis_marker_census_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
