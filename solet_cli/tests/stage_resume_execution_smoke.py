"""Regression checks for stage-limited execution after a named resume."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import solet_manager.operation_executor as executor  # noqa: E402
from solet_manager.models import CheckpointStatus, CommandResult, ExitCode  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.registry import InstanceRegistry  # noqa: E402

_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


class _Transaction:
    name = "fixture"
    target = "/private/tmp/stage-resume-target"
    stages = {"models": CheckpointStatus.VERIFIED}

    def with_result_kind(self, _result_kind: str) -> _Transaction:
        return self

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "target": self.target}


def main() -> int:
    transaction = _Transaction()
    bundle = SimpleNamespace(stages={"models": {}})
    plan = SimpleNamespace(operations=())
    with tempfile.TemporaryDirectory() as temporary:
        paths = ManagerPaths(
            Path(temporary) / "config",
            Path(temporary) / "state",
            Path(temporary) / "cache",
        )
        _limited_run(bundle, plan, transaction, paths)
        _ordinary_run(bundle, plan, transaction, paths)
    print(f"stage_resume_execution_smoke OK: {_CHECKS} checks passed")
    return 0


def _limited_run(
    bundle: object,
    plan: object,
    transaction: _Transaction,
    paths: ManagerPaths,
) -> None:
    with (
        patch.object(
            executor,
            "_finish_operation_stages",
            return_value=executor.OperationOutcome(transaction, None),
        ) as finish,
        patch.object(executor, "_advance_read_only_frontiers") as advance,
        patch.object(executor, "_complete_create") as complete,
        patch.object(executor, "write_transaction") as write,
    ):
        result = executor.run_operations(
            bundle=bundle,  # type: ignore[arg-type]
            plan=plan,  # type: ignore[arg-type]
            transaction=transaction,  # type: ignore[arg-type]
            approved_actions={},
            config=SimpleNamespace(),  # type: ignore[arg-type]
            paths=paths,
            instance_registry=InstanceRegistry(paths.registry_path),
            refresh_preview=lambda: CommandResult("fixture", "fixture", "fixture", ExitCode.OK),
            stop_after_stage="models",
        )
    _check(result.status == "stage_completed", "named resume reports one completed stage")
    _check(
        finish.call_args.kwargs["stage_ids"] == ("models",),
        "named resume runs the selected stage exit boundary even with no operations",
    )
    _check(
        not advance.called and not complete.called,
        "named resume never advances or completes successors",
    )
    _check(write.called, "named resume persists its stage-limited result")


def _ordinary_run(
    bundle: object,
    plan: object,
    transaction: _Transaction,
    paths: ManagerPaths,
) -> None:
    complete_result = CommandResult("create", "verified", "fixture", ExitCode.OK)
    with (
        patch.object(
            executor,
            "_finish_operation_stages",
            return_value=executor.OperationOutcome(transaction, None),
        ) as finish,
        patch.object(
            executor,
            "_advance_read_only_frontiers",
            return_value=executor.OperationOutcome(transaction, None),
        ) as advance,
        patch.object(executor, "_complete_create", return_value=complete_result) as complete,
    ):
        result = executor.run_operations(
            bundle=bundle,  # type: ignore[arg-type]
            plan=plan,  # type: ignore[arg-type]
            transaction=transaction,  # type: ignore[arg-type]
            approved_actions={},
            config=SimpleNamespace(),  # type: ignore[arg-type]
            paths=paths,
            instance_registry=InstanceRegistry(paths.registry_path),
            refresh_preview=lambda: CommandResult("fixture", "fixture", "fixture", ExitCode.OK),
        )
    _check(result is complete_result, "ordinary create retains full-ladder completion behavior")
    _check(
        finish.call_args.kwargs["stage_ids"] is None,
        "ordinary create retains operation-derived exits",
    )
    _check(
        advance.called and complete.called,
        "ordinary create still advances and completes normally",
    )


if __name__ == "__main__":
    raise SystemExit(main())
