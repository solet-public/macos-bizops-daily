"""Regression coverage for deferred optional-account stage boundaries."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.flow import initial_stage_probe_statuses  # noqa: E402
from solet_manager.models import CheckpointStatus  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import Transaction  # noqa: E402

_CONTRACTS = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "github_midwife_plugin"
    / "knowledge_base"
)


def _answers(*, timing: str, connectors: list[str] | None) -> dict[str, object]:
    decisions: dict[str, object] = {"connector_configuration_timing": timing}
    if connectors is not None:
        decisions["connectors_to_configure"] = connectors
    return {"decisions": decisions}


def main() -> int:
    bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
    stage = bundle.stages["optional_accounts"]
    if stage.get("required_when") != {
        "decision_ref": "connector_configuration_timing",
        "operator": "equals",
        "value": "configure_now",
    }:
        raise AssertionError("red: run optional-account stage after connector deferral")
    jira = bundle.probes["jira_connection_valid"]
    if jira.get("required_when") != {
        "decision_ref": "connectors_to_configure",
        "operator": "contains",
        "value": "jira",
    }:
        raise AssertionError("red: leave Jira connection validation unconditional")
    deferred = initial_stage_probe_statuses(
        bundle, _answers(timing="first_use", connectors=None)
    )
    if not all(
        status is CheckpointStatus.NOT_APPLICABLE
        for status in deferred["optional_accounts"]["exit"].values()
    ):
        raise AssertionError("red: invoke deferred optional-account boundary probes")
    selected = initial_stage_probe_statuses(
        bundle, _answers(timing="configure_now", connectors=["jira"])
    )
    exits = selected["optional_accounts"]["exit"]
    if (
        exits["jira_connection_valid"] is not CheckpointStatus.PENDING
        or exits["salesforce_connection_valid"] is not CheckpointStatus.NOT_APPLICABLE
    ):
        raise AssertionError("red: activate connector checks outside the selected set")
    transaction = Transaction.create(
        name="deferred-accounts",
        target=Path("/tmp/deferred-accounts"),
        input_fingerprint="a" * 64,
        answers=_answers(timing="first_use", connectors=None),
        seed=SeedLock("owner/repo", "v1.0.0", "a" * 40, "b" * 40, None, "default"),
        flow_id=bundle.flow_id,
        flow_source_revision="a" * 40,
        flow_contract_digest=bundle.contract_digest,
        stage_ids=tuple(bundle.stages),
        completion_probe_ids=bundle.completion_probe_ids,
        stage_probe_statuses=deferred,
    )
    if transaction.stages["optional_accounts"] is not CheckpointStatus.NOT_APPLICABLE:
        raise AssertionError("red: persist deferred optional-account stage as pending")
    Transaction.from_dict(transaction.to_dict())
    print("optional_accounts_boundary_smoke OK: 6 checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
