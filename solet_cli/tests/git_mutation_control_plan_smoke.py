"""Planner regression smoke for Git-mutation-control input propagation."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.config import CreateConfig  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.flow import build_setup_plan  # noqa: E402
from solet_manager.models import JsonValue  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402

_CONTRACTS = Path(__file__).resolve().parents[2] / "plugins" / "github_midwife_plugin" / "knowledge_base"
_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _shell_inputs(git_mutation_control: str) -> dict[str, JsonValue]:
    seed = SeedLock(
        "https://github.com/solet-public/macos-bizops.git",
        "release-2026-08-20",
        "a" * 40,
        "b" * 40,
        "c" * 64,
        "macos-bizops",
    )
    bundle = ContractBundle.load(source_revision=seed.commit, directory=_CONTRACTS)
    config = CreateConfig(
        name="git-mutation-control",
        target=Path("/tmp/git-mutation-control"),
        autostart=True,
        decisions={
            "inference_implementation": "lm_studio",
            "execution_topology": "solo",
            "git_mutation_control": git_mutation_control,
        },
        decision_sources={
            "inference_implementation": "config",
            "execution_topology": "config",
            "git_mutation_control": "config",
        },
    )
    plan = build_setup_plan(
        bundle=bundle,
        config=config,
        seed=seed,
        journal_path=Path("/tmp/git-mutation-control.json"),
        decision_selections=config.decisions,
        decision_sources=config.decision_sources,
    )
    return next(
        operation.public_inputs
        for operation in plan.operations
        if operation.operation_id == "install_shell_integration"
    )


def main() -> int:
    _check(
        _shell_inputs("designated_controller")
        == {"git_controller_name": "Git-Controller"},
        "designated Git controller reaches the shell-integration plan step",
    )
    _check(
        _shell_inputs("single_session") == {},
        "single-session Git control leaves the shell-integration gate unarmed",
    )
    print(f"git_mutation_control_plan_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
