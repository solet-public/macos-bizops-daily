#!/usr/bin/env python3
"""Completed empty frontiers must reach normal create finalization."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager import operation_executor
from solet_manager.config import CreateConfig
from solet_manager.flow import SetupPlan
from solet_manager.models import CheckpointStatus, ExitCode
from solet_manager.paths import ManagerPaths
from solet_manager.preview_engine import (
    PreviewRound,
    _preview_is_blocked,
    _setup_preview_result,
)
from solet_manager.release_lock import SeedLock
from solet_manager.transaction import Transaction

_CHECKS = 0


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


def _transaction(stages: dict[str, CheckpointStatus]) -> Transaction:
    transaction = Transaction.create(
        name="empty-frontier",
        target=Path("/tmp/empty-frontier"),
        input_fingerprint="sha256:" + "d" * 64,
        answers={},
        seed=SeedLock(
            "https://example.invalid/seed.git",
            "fixture",
            "a" * 40,
            "b" * 40,
            "c" * 64,
            "fixture",
        ),
        flow_id="fixture.flow",
        flow_source_revision="a" * 40,
        flow_contract_digest="sha256:" + "e" * 64,
        stage_ids=tuple(stages),
        completion_probe_ids=("completion",),
    )
    return transaction.with_statuses(
        stages=stages,
        completion={"completion": CheckpointStatus.PENDING},
    )


def _completed_empty_frontier(transaction: Transaction) -> bool:
    return all(
        status in {CheckpointStatus.VERIFIED, CheckpointStatus.NOT_APPLICABLE}
        for status in transaction.stages.values()
    )


def _preview_result(
    transaction: Transaction,
    plan: SetupPlan,
) -> tuple[bool, object]:
    evaluated = PreviewRound(
        transaction=transaction,
        answers={},
        frontier=(),
        plan=plan,
        stage_observations={},
        decision_observations={},
        decision_prompts=[],
        decision_errors=[],
        unresolved_actions=[],
        operation_results={},
        terminal=True,
    )
    blocking = _preview_is_blocked(
        plan,
        evaluated.decision_errors,
        evaluated.unresolved_actions,
        {},
        completed=_completed_empty_frontier(transaction),
    )
    result = _setup_preview_result(
        config=CreateConfig("empty-frontier", Path("/tmp/empty-frontier"), False),
        evaluated=evaluated,
        plan=plan,
        blocking=blocking,
        data={},
    )
    return blocking, result


def _completed_empty_frontier_finalizes() -> None:
    transaction = _transaction(
        {
            "required": CheckpointStatus.VERIFIED,
            "optional": CheckpointStatus.NOT_APPLICABLE,
        }
    )
    plan = SetupPlan({}, (), (), ())
    blocking, preview = _preview_result(transaction, plan)
    _check(not blocking, "completed empty frontier is not unresolved")
    _check(
        preview.status == "preview_ready"
        and preview.exit_code is ExitCode.OK
        and preview.error_kind is None,
        "completed empty frontier is approval-ready",
    )
    finalized = transaction.with_statuses(
        completion={"completion": CheckpointStatus.VERIFIED}
    )
    with tempfile.TemporaryDirectory() as raw:
        paths = ManagerPaths(Path(raw) / "config", Path(raw) / "state", Path(raw) / "cache")
        paths.transactions_dir.mkdir(parents=True)
        paths.transactions_dir.chmod(0o700)
        with (
            patch.object(
                operation_executor,
                "run_completion_probes",
                return_value=(finalized, [{"id": "completion", "status": "verified"}]),
            ),
            patch.object(operation_executor, "ensure_registry") as ensure_registry,
        ):
            result = operation_executor._complete_create(
                bundle=MagicMock(),
                transaction=transaction,
                registry=MagicMock(),
                paths=paths,
                instance_registry=MagicMock(),
            )
    _check(
        result.status == "verified" and result.exit_code is ExitCode.OK,
        "normal completion finalization promotes the transaction to verified",
    )
    _check(
        result.data["transaction"]["status"] == "verified",
        "finalization records the verified transaction",
    )
    _check(ensure_registry.call_count == 1, "verified finalization registers the instance")


def _pending_consent_stays_unresolved() -> None:
    transaction = _transaction({"required": CheckpointStatus.VERIFIED})
    plan = SetupPlan({}, (), (), ("consent",))
    blocking, result = _preview_result(transaction, plan)
    _check(blocking, "pending consent remains blocking")
    _check(
        result.exit_code is ExitCode.HUMAN_ACTION
        and result.error_kind == "setup_preview_unresolved",
        "pending consent preserves exit 3 setup_preview_unresolved",
    )


def _nonfinal_stage_stays_unresolved() -> None:
    transaction = _transaction({"required": CheckpointStatus.PENDING})
    plan = SetupPlan({}, (), (), ())
    blocking, result = _preview_result(transaction, plan)
    _check(blocking, "nonfinal stage does not qualify an empty plan")
    _check(
        result.exit_code is ExitCode.HUMAN_ACTION
        and result.error_kind == "setup_preview_unresolved",
        "nonfinal stage preserves exit 3 setup_preview_unresolved",
    )


def main() -> int:
    _completed_empty_frontier_finalizes()
    _pending_consent_stays_unresolved()
    _nonfinal_stage_stays_unresolved()
    print(f"finalization_empty_frontier_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
