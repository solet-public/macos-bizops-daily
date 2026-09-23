"""Doctor's acceptance verdict is over the checks it ran (rollup-cluster 1.43).

``_doctor_result`` derived its pass/fail from ``transaction.status``, which
``roll_up_transaction`` folds from the whole retained setup-stage map *and* the
completion set.  So a target whose every completion probe verified, but which
retained one non-final setup stage, was told:

    Installation doctor did not verify every required check for 'x'.
    Repair the listed required checks and rerun doctor.

while every listed check was in fact verified.  The instruction is unfollowable
-- there is nothing to repair in that list -- and the real blocker is an
unnamed setup stage.  That misdirection is what makes the 1.40/1.42 rollup
defects present as a mystery instead of a named blocker.

An acceptance oracle must be over its own criteria.  The completion verdict now
reflects the completion probes; a setup-stage blocker is reported separately
and by name.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.doctor import _doctor_result  # noqa: E402
from solet_manager.models import CheckpointStatus, JsonValue  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import Transaction, canonical_sha256  # noqa: E402

_BLOCKED_STAGE = "preflight"
_CLEAN_STAGE = "install"
_CHECKS = 0


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


def _verified_checks() -> list[JsonValue]:
    return [
        {
            "id": "router_ready",
            "status": "verified",
            "declared_expectation_advisory": None,
            "repair": None,
        },
        {
            "id": "tmux_available",
            "status": "verified",
            "declared_expectation_advisory": None,
            "repair": None,
        },
    ]


def _transaction(*, stage_status: CheckpointStatus) -> Transaction:
    """A target whose completion set verified but whose setup stage is not final."""

    seed = SeedLock(
        "https://github.com/solet-public/macos-bizops.git",
        "release-2026-08-20",
        "a" * 40,
        "b" * 40,
        "c" * 64,
        "macos-bizops",
    )
    answers: dict[str, JsonValue] = {"decisions": {}, "consents": {}}
    created = Transaction.create(
        name="verdict",
        target=Path("/tmp/verdict"),
        input_fingerprint=canonical_sha256({"name": "verdict"}),
        answers=answers,
        seed=seed,
        flow_id="flow",
        flow_source_revision="a" * 40,
        flow_contract_digest="sha256:" + "b" * 64,
        stage_ids=(_BLOCKED_STAGE, _CLEAN_STAGE),
        completion_probe_ids=("router_ready", "tmux_available"),
    )
    return created.with_statuses(
        stages={
            _BLOCKED_STAGE: stage_status,
            _CLEAN_STAGE: CheckpointStatus.VERIFIED,
        },
        completion={
            "router_ready": CheckpointStatus.VERIFIED,
            "tmux_available": CheckpointStatus.VERIFIED,
        },
    )


def _blockers(result_data: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    raw = result_data.get("setup_stage_blockers")
    if not isinstance(raw, list):
        raise AssertionError("red: doctor reports no setup_stage_blockers field at all")
    return [item for item in raw if isinstance(item, dict)]


def _assert_names_the_blocker_not_the_checks() -> None:
    """The load-bearing case: verified checks, one non-final setup stage."""

    transaction = _transaction(stage_status=CheckpointStatus.AWAITING_USER)
    result = _doctor_result("verdict", transaction, _verified_checks())
    data = result.data

    _check(
        result.repair is None or "listed required checks" not in result.repair,
        "doctor still says to repair the listed required checks while every "
        "listed check is verified",
    )
    _check(
        "did not verify every required check" not in result.message,
        "doctor still reports unverified checks while every check verified",
    )
    blockers = _blockers(data)
    named = {str(item.get("stage_id")) for item in blockers}
    _check(
        named == {_BLOCKED_STAGE},
        f"doctor did not name the blocking setup stage; named={sorted(named)}",
    )
    _check(
        str(blockers[0].get("status")) == CheckpointStatus.AWAITING_USER.value,
        "doctor named the blocker without its status",
    )
    _check(
        _BLOCKED_STAGE in (result.repair or ""),
        "doctor's repair does not name the setup stage actually blocking",
    )
    _check(
        data.get("verified_count") == 2 and data.get("required_count") == 2,
        "doctor's completion counts no longer reflect the checks it ran",
    )
    _check(
        data.get("completion_verified") is True,
        "doctor folded a setup-stage blocker into its completion verdict",
    )
    _check(
        data.get("transaction_status") == CheckpointStatus.AWAITING_USER.value,
        "doctor dropped the transaction status it still needs to report",
    )


def _assert_unverified_checks_still_fail() -> None:
    """The opposite direction: a real completion failure must still fail."""

    transaction = _transaction(stage_status=CheckpointStatus.VERIFIED)
    checks = _verified_checks()
    failing = dict(checks[0])
    failing["status"] = "failed"
    failing["repair"] = "Restart the router."
    result = _doctor_result("verdict", transaction, [failing, checks[1]])
    _check(
        result.data.get("completion_verified") is False,
        "doctor called the completion set verified with a failed check in it",
    )
    _check(
        "listed required checks" in (result.repair or ""),
        "doctor stopped pointing at the checks when a check genuinely failed",
    )
    _check(
        not _blockers(result.data),
        "doctor invented a setup-stage blocker where every stage is final",
    )


def _assert_clean_target_passes() -> None:
    """No blockers, all checks verified -- the ordinary green."""

    transaction = _transaction(stage_status=CheckpointStatus.VERIFIED)
    result = _doctor_result("verdict", transaction, _verified_checks())
    _check(
        result.data.get("completion_verified") is True and not _blockers(result.data),
        "a fully verified target did not read as verified",
    )
    _check(
        result.error_kind is None,
        "a fully verified target reported an error kind",
    )


def _assert_not_applicable_is_not_a_blocker() -> None:
    """A deliberately inactive stage is final, not an unfinished one."""

    transaction = _transaction(stage_status=CheckpointStatus.NOT_APPLICABLE)
    result = _doctor_result("verdict", transaction, _verified_checks())
    _check(
        not _blockers(result.data),
        "doctor reported a not-applicable stage as a setup blocker",
    )


def main() -> int:
    _assert_names_the_blocker_not_the_checks()
    _assert_unverified_checks_still_fail()
    _assert_clean_target_passes()
    _assert_not_applicable_is_not_a_blocker()
    print(f"doctor_verdict_scope_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
