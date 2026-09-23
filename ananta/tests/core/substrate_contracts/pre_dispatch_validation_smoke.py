#!/usr/bin/env python3
"""Phase 0 freeze — invalid keys / bound args fail BEFORE dispatch (no pytest).

Protects contract (4) of the Phase 0 "freeze current contracts" work
(``workbench/2026-07-01_claude_coding_agent_substrate_architecture_and_planning_v2.md``
PART VI): an action naming an undeclared process key, or a step whose bound
argument cannot be resolved from a closed-world source, is rejected during
validation — before the action is ever dispatched to the queue.

Two production surfaces:

* ``core.plans.contracts.action_contract.validate_step_contract`` — post-decode
  step-contract enforcement, called from
  ``inference_service.inference_transaction._validate_step_contract`` (line 206)
  on the model's emitted actions before they are returned for submission.
  Rejects an undeclared key, a missing required key, and an excess duplicate.
* ``core.result_processing.contracts.validate_deterministic_continuation`` — the
  bound-argument half: a required next-step argument with no WBS-bound /
  composed / runtime / slot / result-field source is refused (the deterministic
  path validates before it submits the next action).

Offline: pure validators + constructed inputs; no live solet / LM Studio / Postgres.

Run:
    .venv/bin/python3 \\
      ananta/tests/core/substrate_contracts/pre_dispatch_validation_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.core.plans.contracts.action_contract import (  # noqa: E402
    validate_step_contract,
)
from ananta.core.plans.parser import parse  # noqa: E402
from ananta.core.result_processing.contracts import (  # noqa: E402
    ResultContractViolationError,
    validate_deterministic_continuation,
)
from substrate_contract_fixtures import (  # noqa: E402
    CONTRACT_WBS,
    GRAFT_KEY,
    Checker,
    build_continuation_input,
)

# A REAL registered verb that is simply not declared by the step under test.
# ``validate_step_contract`` checks emitted keys against the STEP's declared
# keys (not the registry), so a real-but-undeclared key exercises the
# rejection faithfully — and resolves for the whole-tree integration gate.
_UNDECLARED = "service_interface::thinking_service::upsert_plan"


def test_undeclared_process_key_rejected(c: Checker) -> None:
    c.expect_raises(
        RuntimeError,
        "an emitted action with an undeclared process key is rejected pre-dispatch",
        lambda: validate_step_contract([{"process_key": _UNDECLARED}], [GRAFT_KEY]),
    )


def test_missing_declared_key_rejected(c: Checker) -> None:
    c.expect_raises(
        RuntimeError,
        "omitting a declared step key is rejected pre-dispatch",
        lambda: validate_step_contract([], [GRAFT_KEY]),
    )


def test_excess_duplicate_rejected(c: Checker) -> None:
    c.expect_raises(
        RuntimeError,
        "emitting a declared key more times than declared is rejected pre-dispatch",
        lambda: validate_step_contract(
            [{"process_key": GRAFT_KEY}, {"process_key": GRAFT_KEY}],
            [GRAFT_KEY],
        ),
    )


def test_valid_contract_passes(c: Checker) -> None:
    actions = [{"process_key": GRAFT_KEY}]
    try:
        validate_step_contract(actions, [GRAFT_KEY])
        c.check(True, "an exactly-matching action set passes the step contract")
    except RuntimeError as exc:
        c.check(False, f"valid contract unexpectedly rejected: {exc}")


def test_invalid_bound_arg_fails_before_dispatch(c: Checker) -> None:
    """A required bound arg with no mechanical source is refused before submission."""
    payload = build_continuation_input(
        parse(CONTRACT_WBS), required_args=frozenset({"unmapped_target"}),
    )
    try:
        validate_deterministic_continuation(payload)
        c.check(False, "an unresolvable bound arg was NOT rejected")
    except ResultContractViolationError as exc:
        invariant = exc.details.get("invariant")
        c.check(
            invariant == "arguments_not_mechanically_derivable",
            f"an unresolvable bound arg is rejected pre-dispatch (got {invariant!r})",
        )


def main() -> int:
    c = Checker("Invalid keys / bound args fail before dispatch (Phase 0 contract 4)")
    print(f"=== {c.title} ===")
    test_undeclared_process_key_rejected(c)
    test_missing_declared_key_rejected(c)
    test_excess_duplicate_rejected(c)
    test_valid_contract_passes(c)
    test_invalid_bound_arg_fails_before_dispatch(c)
    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
