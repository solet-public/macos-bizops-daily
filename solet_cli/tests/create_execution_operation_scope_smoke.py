#!/usr/bin/env python3
"""Create approval must reconstruct bindings from the whole current plan (1.40).

Fixture-3 captures the b15 convergence failure: a historical
``open_files_permissions_settings=awaiting_user`` attempt remains in the
journal although that operation is no longer in the destination's selected
plan.  Approval must retain that attempt as history, but it must rebuild the
live operation map from the whole selected plan so the orphan cannot keep the
``session_sources`` stage open.  An in-plan verified operation must survive.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.adapters import AdapterRegistry
from solet_manager.config import CreateConfig
from solet_manager.contracts import ContractBundle, contract_filenames
from solet_manager.create_execution import _approve_frontier, _load_or_create_transaction
from solet_manager.errors import StateConflictError
from solet_manager.flow import initial_probe_activations
from solet_manager.models import CheckpointStatus, JsonValue
from solet_manager.paths import ManagerPaths
from solet_manager.transaction import Transaction, canonical_sha256, write_transaction

_FIXTURES_ROOT = Path(__file__).parent / "fixtures"
_ROOT = _FIXTURES_ROOT / "reconciliation_identity"
_FIXTURE = _ROOT / "bizopsb15_postcutover_convergefail"
_RETIRED = "open_files_permissions_settings"
_RETAINED = "run_genesis"
_CHECKS = 0
_REQUIRED_CONTRACT_FILENAMES = frozenset(contract_filenames())


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


def _empty_boundaries(**kwargs: object) -> tuple[Transaction, dict[str, JsonValue], list[object]]:
    transaction = kwargs["transaction"]
    if not isinstance(transaction, Transaction):
        raise AssertionError("boundary stub did not receive a transaction")
    return transaction, {}, []


def _fixture_contract_bundle_directories(root: Path) -> tuple[Path, ...]:
    """Discover fixture bundles without duplicating their owning fixture names."""

    return tuple(sorted(path for path in root.rglob("contract_bundle") if path.is_dir()))


def _check_fixture_contract_bundle_completeness(root: Path) -> tuple[Path, ...]:
    bundles = _fixture_contract_bundle_directories(root)
    _check(bool(bundles), "fixture tree contains contract bundles")
    for bundle in bundles:
        actual = {path.name for path in bundle.iterdir() if path.is_file()}
        _check(
            actual == _REQUIRED_CONTRACT_FILENAMES,
            f"{bundle.relative_to(root)} exactly matches the required-at-load contract set",
        )
    return bundles


def _check_fixture_completeness_mutations_are_red() -> None:
    with tempfile.TemporaryDirectory() as raw:
        copied_root = Path(raw) / "fixtures"
        shutil.copytree(_FIXTURES_ROOT, copied_root)
        bundle = _fixture_contract_bundle_directories(copied_root)[0]
        (bundle / "macos_setup_flow.json").unlink()
        try:
            _check_fixture_contract_bundle_completeness(copied_root)
        except AssertionError:
            _check(True, "removing a fixture contract file is red")
        else:
            raise AssertionError("red: missing fixture contract file was accepted")

    with tempfile.TemporaryDirectory() as raw:
        copied_root = Path(raw) / "fixtures"
        shutil.copytree(_FIXTURES_ROOT, copied_root)
        bundle = _fixture_contract_bundle_directories(copied_root)[0]
        (bundle / "stray-contract.json").write_text("{}\n", encoding="utf-8")
        try:
            _check_fixture_contract_bundle_completeness(copied_root)
        except AssertionError:
            _check(True, "adding a stray fixture contract file is red")
        else:
            raise AssertionError("red: stray fixture contract file was accepted")


def _awaiting_in_plan_transaction(source: Transaction) -> Transaction:
    prior_attempt = next(
        attempt for attempt in source.operation_attempts if attempt["operation_id"] == _RETAINED
    )
    awaiting_attempt = dict(prior_attempt)
    awaiting_attempt["attempt"] = 99
    awaiting_attempt["checkpoint_status"] = CheckpointStatus.AWAITING_USER.value
    return source.with_operation_status(
        _RETAINED,
        CheckpointStatus.AWAITING_USER,
        attempt=awaiting_attempt,
    )


def _check_fresh_transaction_receives_activation_carrier(
    bundle: ContractBundle,
    source: Transaction,
) -> None:
    """Fresh creation must persist the composite carrier, not only derive it later."""

    expected = initial_probe_activations(bundle, source.answers)
    registry = MagicMock()
    registry.get.return_value = None
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        paths = ManagerPaths.resolve(explicit_home=root / "manager-home")
        config = CreateConfig(name="fresh-activation", target=root / "target", autostart=True)
        with (
            patch("solet_manager.create_execution.load_transaction", return_value=None),
            patch("solet_manager.create_execution.load_seed_lock", return_value=source.seed),
            patch("solet_manager.create_execution.ContractBundle.load", return_value=bundle),
            patch(
                "solet_manager.create_execution.build_setup_plan",
                return_value=SimpleNamespace(answers=source.answers),
            ),
            patch(
                "solet_manager.create_execution.Transaction.create", return_value=source
            ) as create,
            patch("solet_manager.create_execution.write_transaction"),
            patch("solet_manager.create_execution._provisional_record"),
        ):
            _load_or_create_transaction(
                paths=paths,
                contract_directory=_FIXTURE / "contract_bundle",
                seed_lock_path=root / "seed-lock.json",
                registry=registry,
                config=config,
                approved_fingerprint="sha256:" + "f" * 64,
                selections={},
                decision_source="flag",
                sources={},
            )
    _check(
        create.call_args.kwargs["probe_activations"] == expected,
        "fresh creation did not persist the composite probe activation carrier",
    )


def _check_seed_lock_resume_identity(source: Transaction) -> None:
    """Resume compares the identity persisted by the transaction journal."""

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        paths = ManagerPaths.resolve(explicit_home=root / "manager-home")
        paths.transactions_dir.mkdir(parents=True)
        paths.transactions_dir.chmod(0o700)
        config = CreateConfig(name="seed-lock-round-trip", target=root / "target", autostart=True)
        retained = replace(
            source,
            name=config.name,
            target=str(config.target),
            input_fingerprint=canonical_sha256(config.to_identity_dict()),
        )
        installed_seed = replace(
            retained.seed,
            channel_id="production",
            provenance={"seed_id": "seed-fixture", "source_date": "2026-09-17"},
            existing_install_contract={"flow_id": retained.flow_id},
            allowed_repository_migrations=(
                {
                    "from_repository": retained.seed.repository,
                    "to_repository": retained.seed.repository,
                },
            ),
        )
        write_transaction(
            paths.transaction_path(config.name),
            replace(retained, seed=installed_seed),
        )

        with patch("solet_manager.create_execution.load_seed_lock", return_value=installed_seed):
            resumed = _load_or_create_transaction(
                paths=paths,
                contract_directory=None,
                seed_lock_path=root / "installed-seed.lock.json",
                registry=MagicMock(),
                config=config,
                approved_fingerprint="sha256:" + "f" * 64,
                selections={},
                decision_source="flag",
                sources={},
            )

        _check(
            resumed.seed.identity_dict() == installed_seed.identity_dict(),
            "journal round-trip resumes when installed seed matches persisted identity",
        )
        _check(
            resumed.seed.channel_id is None
            and resumed.seed.provenance is None
            and resumed.seed.existing_install_contract is None
            and resumed.seed.allowed_repository_migrations == (),
            "journal round-trip omits non-identity seed-lock fields",
        )

        mismatched_seed = replace(
            installed_seed,
            commit="f" * 40,
            tree_hash="e" * 40,
            archive_sha256="d" * 64,
        )
        with patch(
            "solet_manager.create_execution.load_seed_lock", return_value=mismatched_seed
        ):
            try:
                _load_or_create_transaction(
                    paths=paths,
                    contract_directory=None,
                    seed_lock_path=root / "installed-seed.lock.json",
                    registry=MagicMock(),
                    config=config,
                    approved_fingerprint="sha256:" + "f" * 64,
                    selections={},
                    decision_source="flag",
                    sources={},
                )
            except StateConflictError as exc:
                message = str(exc)
                repair = exc.repair
            else:
                raise AssertionError("red: stale transaction silently resumed under new seed")
    _check(retained.seed.commit in message, "conflict names the retained transaction seed")
    _check(mismatched_seed.commit in message, "conflict names the installed seed lock")
    _check(
        repair is not None and "cannot reuse" in repair,
        "conflict gives stale-transaction repair guidance",
    )


def main() -> int:
    _check_fixture_contract_bundle_completeness(_FIXTURES_ROOT)
    _check_fixture_completeness_mutations_are_red()
    bundle = ContractBundle.load(
        source_revision="a" * 40,
        directory=_FIXTURE / "contract_bundle",
    )
    source = Transaction.from_dict(
        json.loads((_FIXTURE / "transaction_bizopsb15.json").read_text(encoding="utf-8"))
    )
    _check(
        source.operation_statuses[_RETIRED] is CheckpointStatus.AWAITING_USER,
        "fixture-3 lacks the out-of-plan awaiting_user blocker",
    )
    _check(
        source.operation_statuses[_RETAINED] is CheckpointStatus.VERIFIED,
        "fixture-3 lacks an in-plan verified operation to preserve",
    )
    _check_fresh_transaction_receives_activation_carrier(bundle, source)
    _check_seed_lock_resume_identity(source)

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        paths = ManagerPaths.resolve(explicit_home=root / "manager-home")
        paths.transactions_dir.mkdir(parents=True)
        paths.transactions_dir.chmod(0o700)
        config = CreateConfig(name=source.name, target=Path(source.target), autostart=True)
        in_plan_awaiting = _awaiting_in_plan_transaction(source)
        with patch(
            "solet_manager.create_execution.run_stage_boundaries",
            side_effect=_empty_boundaries,
        ):
            _plan, approved, stopped = _approve_frontier(
                bundle=bundle,
                transaction=source,
                approved_frontier=("session_sources",),
                whole_plan_operation_stage_ids=set(bundle.stages),
                approved_fingerprint="sha256:" + "d" * 64,
                adapter_registry=cast(AdapterRegistry, object()),
                config=config,
                paths=paths,
                selections={},
                decision_source="flag",
                sources={},
            )
            _in_plan, in_plan_approved, in_plan_stopped = _approve_frontier(
                bundle=bundle,
                transaction=in_plan_awaiting,
                approved_frontier=("session_sources",),
                whole_plan_operation_stage_ids=set(bundle.stages),
                approved_fingerprint="sha256:" + "e" * 64,
                adapter_registry=cast(AdapterRegistry, object()),
                config=config,
                paths=paths,
                selections={},
                decision_source="flag",
                sources={},
            )

    _check(stopped is None, "fixture replay unexpectedly stopped at a boundary")
    _check(
        _RETIRED not in approved.operation_stages,
        "out-of-plan awaiting_user operation survived approval bindings",
    )
    _check(
        approved.stages["session_sources"] is not CheckpointStatus.AWAITING_USER,
        "out-of-plan awaiting_user still blocks fixture-3 convergence",
    )
    _check(
        approved.operation_statuses.get(_RETAINED) is CheckpointStatus.VERIFIED,
        "in-plan verified operation was not preserved during reconstruction",
    )
    _check(
        any(attempt.get("operation_id") == _RETIRED for attempt in approved.operation_attempts),
        "whole-plan reconstruction discarded retained operation history",
    )
    reloaded = Transaction.from_dict(approved.to_dict())
    _check(
        reloaded.operation_stages == approved.operation_stages,
        "reconstructed transaction fails its own derived-state validation",
    )
    _check(in_plan_stopped is None, "in-plan control unexpectedly stopped at a boundary")
    _check(
        in_plan_approved.operation_statuses[_RETAINED] is CheckpointStatus.AWAITING_USER,
        "in-plan awaiting_user operation was incorrectly dropped during reconstruction",
    )
    _check(
        in_plan_approved.stages["genesis"] is CheckpointStatus.AWAITING_USER,
        "in-plan awaiting_user operation no longer blocks its declared stage",
    )
    print(f"create_execution_operation_scope_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
