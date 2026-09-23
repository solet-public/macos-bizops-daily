"""Regression for a failed Genesis apply carrying its bounded stderr diagnostic."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import cast

_ROOT = Path(__file__).resolve().parents[2]
for _path in (
    _ROOT / "plugins/github_midwife_plugin/src",
    _ROOT / "solet_cli/src",
):
    sys.path.insert(0, str(_path))

from github_midwife_plugin.setup_adapter_contract import AdapterRequest  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome  # noqa: E402
from github_midwife_plugin.setup_operations import _genesis  # noqa: E402
from solet_manager.adapters import OperationRequest, OperationResult  # noqa: E402
from solet_manager.errors import AdapterProtocolError  # noqa: E402
from solet_manager.flow import PlannedOperation  # noqa: E402
from solet_manager.models import CheckpointStatus  # noqa: E402
from solet_manager.operation_executor import _record_operation_result  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import Transaction, load_transaction  # noqa: E402


class _FailedGenesisRuntime:
    """Hermetic runtime matching repro5's nonzero Genesis subprocess result."""

    def __init__(self, home: Path) -> None:
        self.home = home

    def run(self, _command: tuple[str, ...], **_kwargs: object) -> CommandOutcome:
        return CommandOutcome(
            returncode=1,
            timed_out=False,
            duration_ms=41750,
            stdout="",
            stderr="repro5 fixture Genesis subprocess failed",
        )


def main() -> int:
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        target = root / "target"
        python = target / ".venv/bin/python3"
        python.parent.mkdir(parents=True)
        python.touch()
        request = _apply_request(target)
        adapter_request = AdapterRequest.from_dict(
            cast(dict[str, object], request.to_dict())
        )

        raw_result = _genesis(adapter_request, _FailedGenesisRuntime(root / "home"))
        result = OperationResult.from_dict(raw_result, request)
        _require(
            result.checkpoint_status is CheckpointStatus.FAILED,
            "failed Genesis apply parses through the manager adapter boundary",
        )
        _require(
            result.reason is not None
            and set(result.reason)
            == {
                "outcome_class",
                "exit_code",
                "duration_ms",
                "timed_out",
                "stdout_bytes",
                "stderr_bytes",
                "stdout_truncated",
                "stderr_truncated",
                "stderr_diagnostic",
                "stderr_diagnostic_truncated",
            },
            "failed Genesis apply preserves only the closed augmented reason shape",
        )
        malformed_reason = dict(cast(dict[str, object], raw_result["reason"]))
        del malformed_reason["stderr_diagnostic_truncated"]
        _raises_protocol_error(
            lambda: OperationResult.from_dict(
                {**raw_result, "reason": malformed_reason}, request
            ),
            "a partial augmented reason remains outside the closed union",
        )

        transaction = _transaction(target).bind_operations({"run_genesis": "genesis"})
        paths = ManagerPaths(root / "config", root / "state", root / "cache")
        paths.transactions_dir.mkdir(parents=True)
        paths.transactions_dir.chmod(0o700)
        recorded = _record_operation_result(
            transaction,
            _genesis_operation(),
            result,
            phase="apply",
            attempt=1,
            paths=paths,
        )
        reloaded = load_transaction(paths.transaction_path(recorded.name))
        _require(reloaded is not None, "failed Genesis apply journal reloads")
        _require(
            reloaded.operation_statuses["run_genesis"] is CheckpointStatus.FAILED,
            "failed Genesis apply does not leave run_genesis applying",
        )
        _require(
            reloaded.operation_attempts[-1]["phase"] == "apply"
            and reloaded.operation_attempts[-1]["checkpoint_status"] == "failed",
            "failed Genesis apply attempt is durably journaled before reload",
        )
    print("genesis_failed_apply_reason_smoke OK")
    return 0


def _apply_request(target: Path) -> OperationRequest:
    return OperationRequest(
        request_id="123e4567-e89b-12d3-a456-426614174000",
        operation_id="run_genesis",
        operation_ref="genesis::solet.run",
        phase="apply",
        probe_purpose=None,
        attempt=1,
        name="genesis-fixture",
        target=str(target),
        flow_id="macos.repository_setup",
        flow_source_revision="a" * 40,
        answers_fingerprint="sha256:" + "b" * 64,
        approval_fingerprint="sha256:" + "c" * 64,
        dry_run=False,
        timeout_seconds=300,
        public_inputs={"autostart": "disabled", "setup_profile": "macos-bizops"},
    )


def _transaction(target: Path) -> Transaction:
    return Transaction.create(
        name="genesis-fixture",
        target=target,
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
        flow_id="macos.repository_setup",
        flow_source_revision="a" * 40,
        flow_contract_digest="sha256:" + "e" * 64,
        stage_ids=("genesis",),
        completion_probe_ids=(),
    )


def _genesis_operation() -> PlannedOperation:
    return PlannedOperation(
        stage_id="genesis",
        operation_id="run_genesis",
        operation_ref="genesis::solet.run",
        runner="genesis",
        risk="high",
        requires_confirmation=True,
        precondition_probe_ids=("genesis_artifacts_valid",),
        postcondition_probe_ids=("genesis_artifacts_valid",),
        public_inputs={"autostart": "disabled", "setup_profile": "macos-bizops"},
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _raises_protocol_error(callback: object, message: str) -> None:
    try:
        callback()  # type: ignore[operator]
    except AdapterProtocolError:
        return
    raise AssertionError(message)


if __name__ == "__main__":
    raise SystemExit(main())
