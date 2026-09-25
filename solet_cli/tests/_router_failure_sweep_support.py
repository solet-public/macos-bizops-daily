"""Shared body of the Step-6 F-RT-2 router candidate failure sweep slices (design sections 6.1-6.2, M5).

The reference run's controller reports ``failed_prior_serving``; the journal goes terminal ``failed
runtime_candidate_failed`` with the runtime axis at the baseline, ``needs_attention`` published and the text
naming the router previous as code-only.  The sweep injects a crash after every journal write and after
``invoke_reconciliation`` returns (including the crash between the controller recording the failure and the
journal) and asserts that every crash point resumes to the same terminal with at most one ``rec_`` apply and one
recover -- never a second apply.  ``existing_install_router_failure_sweep_{1..10}_smoke.py`` each sweep one tenth
of the write boundaries and ``existing_install_router_failure_sweep_applies_{1,2}_smoke.py`` each sweep one half
of the apply boundaries, so every file holds at most five crash points (iss_d1a9649b).
"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _step5_support import Fixture, advance_to_source_advanced, db_spy, expect, runtime_fingerprint  # noqa: E402
from _step6_support import CrashSweep, last_update_journal, sweep_slice  # noqa: E402
from existing_install_router_cutover_smoke import BASELINE, FakeSeedController, _attestation, _router_fixture  # noqa: E402
from solet_manager.errors import UpdateFailedError  # noqa: E402
from solet_manager.update_execution import apply_update  # noqa: E402


class _Checks:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self, condition: object, label: str) -> None:
        self.count += 1
        if not condition:
            raise AssertionError(label)


def run_slice(name: str, mode: Literal["write", "apply"], part: int, parts: int) -> int:
    """Sweep slice ``part`` of ``parts`` of the ``mode`` boundaries of the router candidate failure run and print the covered range."""
    check = _Checks()
    with db_spy(), TemporaryDirectory() as temporary:
        indices, total = _assert_router_candidate_failure_sweep(Path(temporary).resolve() / "rt2", check, mode, part, parts)
    print(f"{name} OK: {check.count} checks passed; {mode} crash points {indices[0]}..{indices[-1]} of {total}")
    return 0


def _assert_router_candidate_failure_sweep(root: Path, check: _Checks, mode: Literal["write", "apply"], part: int, parts: int) -> tuple[range, int]:
    controllers: list[FakeSeedController] = []

    def build(path: Path) -> Fixture:
        fixture, controller = _router_fixture(path, controller_outcome="failed_prior_serving", attestations=[_attestation(BASELINE)])
        controllers.append(controller)
        return fixture

    sweep = CrashSweep(build=build, scenario=lambda fixture: _candidate_failure_scenario(fixture, check), root=root, expected_terminal="failed")
    _, writes, applies = sweep.reference()
    check((writes >= 10, applies >= 1) == (True, True), f"reference failure run crossed {writes} writes and {applies} applies")
    total = writes if mode == "write" else applies
    if mode == "write":
        sweep.sweep_writes(total, part, parts)
    else:
        sweep.sweep_applies(total, part, parts)
    indices = sweep_slice(total, part, parts)
    check(len(sweep.outcomes) == len(indices), "one outcome per crash point in this slice")
    check([outcome.index for outcome in sweep.outcomes] == list(indices), "the slice covered exactly its crash indices")
    for index, controller in enumerate(controllers):
        _assert_one_dispatch(controller, "reference" if index == 0 else f"{sweep.outcomes[index - 1].mode}:{sweep.outcomes[index - 1].boundary}", check)
    check(all(outcome.resumed_status == "failed" for outcome in sweep.outcomes), "F-RT-2: every crash point resumed to the reference terminal failed")
    reference_row = cast(dict[str, Any], sweep.reference_row)
    shape = (reference_row["runtime_release"], reference_row["management_state"], "runtime_candidate_failed" in reference_row["update_eligibility"]["reason_codes"])
    check(shape == (None, "needs_attention", True), "runtime axis at baseline; needs_attention published")
    return indices, total


def _candidate_failure_scenario(fixture: Fixture, check: _Checks) -> str:
    advance_to_source_advanced(fixture)
    fingerprint = runtime_fingerprint(fixture)
    exc = expect(UpdateFailedError, lambda: apply_update(fixture.request, fingerprint), "candidate failure accepted")
    rendered = f"{exc} {cast(UpdateFailedError, exc).repair}"
    check(("code-only" in rendered, "database rollback" in rendered) == (True, False), "the failure text names the router previous as code-only")
    return cast(str, last_update_journal(fixture)["status"])


def _assert_one_dispatch(controller: FakeSeedController, boundary: str, check: _Checks) -> None:
    """A crash between the journaled rec_ id and the dispatch resumes with a recover the fake controller answers
    from its canned outcome (0 applies, 1 recover); every other point is one apply and at most one recover.
    Never two applies."""
    applies_seen = sum(1 for e in controller.envelopes if e["phase"] == "apply")
    recovers = sum(1 for e in controller.envelopes if e["phase"] == "recover")
    check((applies_seen <= 1, recovers <= 1, applies_seen + recovers >= 1) == (True, True, True), f"F-RT-2 {boundary}: at most one rec_ apply and one recover per run, never a second apply ({applies_seen}, {recovers})")
