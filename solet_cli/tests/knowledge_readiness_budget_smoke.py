"""Dedicated knowledge budget validation and Manager request-routing regressions."""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager import stage_boundaries  # noqa: E402
from solet_manager.adapters import AdapterRegistry, OperationRequest  # noqa: E402
from solet_manager.contracts import (  # noqa: E402
    ContractBundle,
    knowledge_readiness_budget,
    startup_readiness_budget,
)
from solet_manager.errors import ContractError  # noqa: E402
from solet_manager.models import JsonValue  # noqa: E402
from solet_manager.transaction import Transaction  # noqa: E402

_CONTRACTS = Path(__file__).resolve().parents[2] / "plugins/github_midwife_plugin/knowledge_base"


def _request(bundle: ContractBundle, probe: str, boundary: str) -> OperationRequest:
    transaction = cast(Transaction, SimpleNamespace(
        name="knowledge-fixture", target="/tmp/knowledge-fixture",
        answers_fingerprint="a" * 64,
    ))
    with (
        patch.object(stage_boundaries, "invoke_adapter", return_value=object()) as invoke,
        patch.object(stage_boundaries, "probe_public_inputs", return_value={}),
    ):
        stage_boundaries._invoke_boundary_probe(
            bundle=bundle, transaction=transaction,
            registry=AdapterRegistry(target=Path(transaction.target)),
            stage_id="completion", boundary=boundary, probe_id=probe,
            answers={}, attempt=1,
        )
    return cast(OperationRequest, invoke.call_args.kwargs["request"])


def _invalid_budgets(bundle: ContractBundle) -> None:
    executor = cast(dict[str, JsonValue], bundle.flow["executor_contracts"])
    original = deepcopy(cast(dict[str, JsonValue], executor["knowledge_readiness"]))
    mutations: list[tuple[str, JsonValue]] = [
        ("timeout_seconds", value) for value in (True, 0, 5, 901, 120.5, "120", None)
    ]
    mutations.extend([
        ("launch_result_reserve_seconds", 0),
        ("consumer_probe_purposes", ["completion"]),
        ("consumer_probe_refs", ["embedding_request_succeeds"]),
        ("release_signal", "all_kbs_hydrated"),
    ])
    for field, value in mutations:
        modified = deepcopy(original)
        modified[field] = value
        executor["knowledge_readiness"] = modified
        try:
            knowledge_readiness_budget(bundle)
        except ContractError:
            pass
        else:
            raise AssertionError(f"invalid knowledge budget accepted: {field}={value}")
    executor["knowledge_readiness"] = original


def _schema_generation_checks(bundle: ContractBundle) -> None:
    executor = cast(dict[str, JsonValue], bundle.flow["executor_contracts"])
    policy = executor.pop("knowledge_readiness")
    try:
        for check in (bundle.validate, lambda: knowledge_readiness_budget(bundle)):
            try:
                check()
            except ContractError:
                pass
            else:
                raise AssertionError("current schema accepted missing knowledge policy")
    finally:
        executor["knowledge_readiness"] = policy
    historical = ContractBundle.load(
        source_revision="5ae81ea4c453849f971512e5ea7b2eb762f90379",
        directory=Path(__file__).parent / "fixtures/reconciliation_identity/bizopsb15_postcutover_convergefail/contract_bundle",
        expected_digest="sha256:572552bdd63cf06c48b76c79dbcbd0d7eae6e8f15ed85646acf84d36e9e5c054",
    )
    if knowledge_readiness_budget(historical) != 30:
        raise AssertionError("historical schema lost its ordinary boundary budget")
    if _request(historical, "knowledge_retrieval_succeeds", "exit").timeout_seconds != 30:
        raise AssertionError("historical stage exit did not retain its pinned policy generation")
    historical_executor = cast(dict[str, JsonValue], historical.flow["executor_contracts"])
    historical_executor["knowledge_readiness"] = None
    for check in (historical.validate, lambda: knowledge_readiness_budget(historical)):
        try:
            check()
        except ContractError:
            pass
        else:
            raise AssertionError("historical schema bypassed malformed explicit knowledge policy")


def main() -> None:
    bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
    if knowledge_readiness_budget(bundle) != 120:
        raise AssertionError("knowledge parent policy must initially be 120 seconds")
    startup = startup_readiness_budget(bundle)
    for probe, boundary, expected in (
        ("knowledge_retrieval_succeeds", "exit", 120),
        ("knowledge_retrieval_succeeds", "entry", 30),
        ("router_ready", "exit", 30),
        ("embedding_request_succeeds", "exit", startup.parent_budget_seconds),
    ):
        request = _request(bundle, probe, boundary)
        if request.timeout_seconds != expected:
            raise AssertionError(f"wrong parent budget: {probe}/{boundary}")
        if probe == "embedding_request_succeeds":
            if request.public_inputs.get("startup_readiness_release_signal") != startup.release_signal:
                raise AssertionError("embedding startup-health lineage changed")
    _invalid_budgets(bundle)
    _schema_generation_checks(bundle)
    # Changing knowledge policy must not change embedding authority or ordinary boundaries.
    executor = cast(dict[str, JsonValue], bundle.flow["executor_contracts"])
    readiness = cast(dict[str, JsonValue], executor["knowledge_readiness"])
    readiness["timeout_seconds"] = 180
    if _request(bundle, "knowledge_retrieval_succeeds", "exit").timeout_seconds != 180:
        raise AssertionError("Manager ignored declared knowledge policy")
    if startup_readiness_budget(bundle) != startup:
        raise AssertionError("knowledge policy changed embedding startup-health semantics")
    if _request(bundle, "knowledge_retrieval_succeeds", "entry").timeout_seconds != 30:
        raise AssertionError("knowledge entry consumed the exit-only budget")
    print("PASS: knowledge budget validation, stage-exit routing, embedding isolation")


if __name__ == "__main__":
    main()
