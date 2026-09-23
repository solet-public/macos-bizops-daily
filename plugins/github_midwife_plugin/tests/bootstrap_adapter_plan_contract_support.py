"""Plan-level proof that the Homebrew stop is reachable before dependent work."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[3]
_SOLET_MANAGER_ROOT = _ROOT / "solet_cli" / "src"
if str(_SOLET_MANAGER_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOLET_MANAGER_ROOT))

from solet_manager.config import CreateConfig  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.flow import build_setup_plan  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402

_CONTRACTS = _ROOT / "plugins" / "github_midwife_plugin" / "knowledge_base"


def _seed() -> SeedLock:
    return SeedLock(
        "https://github.com/solet-public/macos-bizops.git",
        "release-2026-08-20",
        "a" * 40,
        "b" * 40,
        "c" * 64,
        "macos-bizops",
    )


def _plan_operation_ids(root: Path, contracts: Path) -> list[str]:
    seed = _seed()
    paths = ManagerPaths.resolve(explicit_home=root / "manager", home=root)
    config = CreateConfig(
        name="bootstrap-plan",
        target=root / "target",
        autostart=True,
        decisions={"inference_implementation": "lm_studio"},
        decision_sources={"inference_implementation": "config"},
    )
    bundle = ContractBundle.load(source_revision=seed.commit, directory=contracts)
    plan = build_setup_plan(
        bundle=bundle,
        config=config,
        seed=seed,
        journal_path=paths.transaction_path(config.name),
    )
    return [operation.operation_id for operation in plan.operations]


def check_homebrew_operation_plan(*, root: Path, check: Any) -> None:
    """The declared operation must be selected before any Homebrew dependency."""

    operation_ids = _plan_operation_ids(root, _CONTRACTS)
    homebrew_index = operation_ids.index("request_homebrew_install")
    python_index = operation_ids.index("install_python_runtime")
    environment_index = operation_ids.index("build_instance_environment")
    postgresql_index = operation_ids.index("install_postgresql")
    check(
        homebrew_index < python_index < environment_index < postgresql_index,
        "Homebrew stop-and-ask is planned before Python and Homebrew-dependent installs",
    )
    changed_contracts = root / "bootstrap-order-contracts"
    shutil.copytree(_CONTRACTS, changed_contracts)
    changed_flow_path = changed_contracts / "macos_setup_flow.json"
    changed_flow = json.loads(changed_flow_path.read_text(encoding="utf-8"))
    changed_flow["stages"]["system_dependencies"]["operation_refs"].remove(
        "request_homebrew_install"
    )
    changed_flow_path.write_text(json.dumps(changed_flow, indent=2), encoding="utf-8")
    changed_ids = _plan_operation_ids(root, changed_contracts)
    check(
        "request_homebrew_install" not in changed_ids
        and "install_postgresql" in changed_ids,
        "removing the stage reference makes the Homebrew stop disappear from the real plan",
    )
