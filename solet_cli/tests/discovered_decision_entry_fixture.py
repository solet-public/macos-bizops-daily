"""Local contract fixture for the generic failed-stage-entry invariant."""

from __future__ import annotations

import json
from pathlib import Path

from discovered_decision_support import _prepare
from solet_manager.config import CreateConfig
from solet_manager.contracts import ContractBundle, target_contract_directory
from solet_manager.create import CreateManager
from solet_manager.flow import build_setup_plan, initial_stage_probe_statuses
from solet_manager.paths import ManagerPaths
from solet_manager.transaction import Transaction, canonical_sha256, write_transaction


def prepare_with_decision_review_entry_probe(
    root: Path,
    *,
    name: str,
    probe_id: str,
) -> tuple[ManagerPaths, CreateConfig, CreateManager, Transaction]:
    """Install one test-local entry probe and rebuild its pinned transaction."""

    paths, config, manager, transaction = _prepare(root, name=name)
    contract_directory = target_contract_directory(config.target)
    flow_path = contract_directory / "macos_setup_flow.json"
    flow = json.loads(flow_path.read_text(encoding="utf-8"))
    flow["stages"]["decision_review"]["entry_probe_refs"] = [probe_id]
    flow_path.write_text(json.dumps(flow), encoding="utf-8")
    bundle = ContractBundle.load(
        source_revision=transaction.flow_source_revision,
        directory=contract_directory,
    )
    plan = build_setup_plan(
        bundle=bundle,
        config=config,
        seed=transaction.seed,
        journal_path=paths.transaction_path(config.name),
        decision_selections=config.decisions,
        decision_sources=config.decision_sources,
    )
    pinned = Transaction.create(
        name=config.name,
        target=config.target,
        input_fingerprint=canonical_sha256(config.to_identity_dict()),
        answers=plan.answers,
        seed=transaction.seed,
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        flow_contract_digest=bundle.contract_digest,
        stage_ids=tuple(bundle.stages),
        completion_probe_ids=bundle.completion_probe_ids,
        stage_probe_statuses=initial_stage_probe_statuses(bundle, plan.answers),
    )
    write_transaction(paths.transaction_path(config.name), pinned)
    return paths, config, manager, pinned
