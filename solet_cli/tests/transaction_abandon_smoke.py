#!/usr/bin/env python3
"""Focused decision-frontier checks for retained transaction recovery."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.config import CreateConfig  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.errors import StateConflictError  # noqa: E402
from solet_manager.resume_rules import assert_decision_revision_allowed  # noqa: E402
from solet_manager.transaction import assert_resume_identity  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_CONTRACTS = _REPO / "plugins" / "github_midwife_plugin" / "knowledge_base"
_CHECKS = 0


def _check(condition: bool, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _transaction(
    attempted_stage: str,
    *,
    decision_id: str = "autostart",
    operation_stages: dict[str, str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        answers={"decisions": {decision_id: "enabled"}},
        operation_attempts=({"stage_id": attempted_stage},),
        stage_probe_attempts=(),
        operation_stages=(
            {
                "run_genesis": "genesis",
                "install_launchagent": "models",
            }
            if operation_stages is None
            else operation_stages
        ),
    )


def _decision_change_refused(
    transaction: SimpleNamespace,
    bundle: ContractBundle,
    decision_id: str = "autostart",
) -> bool:
    try:
        assert_decision_revision_allowed(
            transaction,
            {"decisions": {decision_id: "disabled"}},
            bundle,
        )
    except StateConflictError:
        return True
    return False


def _target_change_refused() -> bool:
    retained = SimpleNamespace(
        name="fixture",
        target="/fixture",
        input_fingerprint="sha256:" + "a" * 64,
    )
    try:
        assert_resume_identity(
            retained,
            name="fixture",
            target=Path("/other"),
            input_fingerprint="sha256:" + "a" * 64,
        )
    except StateConflictError:
        return True
    return False


def main() -> int:
    enabled = CreateConfig("fixture", Path("/fixture"), True)
    disabled = CreateConfig("fixture", Path("/fixture"), False)
    _check(
        enabled.to_identity_dict() == disabled.to_identity_dict(),
        "autostart is a decision, not immutable transaction identity",
    )
    _check(
        CreateConfig("fixture", Path("/other"), True).to_identity_dict()
        != enabled.to_identity_dict(),
        "target remains immutable transaction identity",
    )
    bundle = ContractBundle.load(source_revision="fixture", directory=_CONTRACTS)
    _check(
        not _decision_change_refused(_transaction("system_dependencies"), bundle),
        "safe-window control: stage 20 has not consumed autostart",
    )
    _check(
        _decision_change_refused(_transaction("genesis"), bundle),
        "frontier control: stage 30 consumed autostart and still refuses a flip",
    )
    _check(
        not _decision_change_refused(
            _transaction("genesis", decision_id="coding_agents"),
            bundle,
            "coding_agents",
        ),
        "never-consumed decision control remains mutable after an unrelated stage",
    )
    _check(
        _decision_change_refused(
            _transaction("genesis", operation_stages={"missing": "genesis"}),
            bundle,
        ),
        "retained operation without a declared contract binding fails closed",
    )
    _check(
        _decision_change_refused(
            _transaction("genesis", operation_stages={"run_genesis": "missing"}),
            bundle,
        ),
        "retained operation bound to an unknown stage fails closed",
    )
    _check(
        _target_change_refused(),
        "identity control: a target change remains refused after autostart projection changes",
    )
    print(f"transaction_abandon_smoke: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
