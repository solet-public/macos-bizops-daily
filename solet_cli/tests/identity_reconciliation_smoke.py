#!/usr/bin/env python3
"""Fixture-only controls for receipt-bound in-field identity reconciliation."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager import identity_reconciliation as identity_module  # noqa: E402
from solet_manager.errors import StateConflictError  # noqa: E402
from solet_manager.identity_reconciliation import (  # noqa: E402
    IdentityReconciliationManager,
    TargetRevision,
)
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.transaction import canonical_sha256, load_transaction  # noqa: E402

_CHECKS = 0
_FIXTURE = Path(__file__).parent / "fixtures" / "reconciliation_identity" / "bizopsb15_postcutover_convergefail"
_NAME = "bizopsb15"


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must be a JSON object")
    return value


def _write(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _replace_target(value: object, old: str, new: str) -> object:
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [_replace_target(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _replace_target(item, old, new) for key, item in value.items()}
    return value


def _fixture(root: Path) -> tuple[ManagerPaths, Path, str]:
    target = root / "target"
    paths = ManagerPaths(root / "config", root / "state", root / "cache")
    original_target = "/Users/admin/Solets/bizopsb15"
    transaction = _replace_target(_load(_FIXTURE / "transaction_bizopsb15.json"), original_target, str(target))
    registry = _replace_target(_load(_FIXTURE / "registry_instances.json"), original_target, str(target))
    install_state = _replace_target(_load(_FIXTURE / "install_state.json"), original_target, str(target))
    if not isinstance(transaction, dict) or not isinstance(registry, dict) or not isinstance(install_state, dict):
        raise AssertionError("fixture rewrites must remain objects")
    answers = transaction.get("answers")
    if not isinstance(answers, dict):
        raise AssertionError("fixture transaction answers must be an object")
    transaction["answers_fingerprint"] = canonical_sha256(answers)
    _write(paths.transaction_path(_NAME), transaction)
    _write(paths.registry_path, registry)
    _write(target / ".solet" / "install-state.json", install_state)

    receipt = _load(_FIXTURE / "terminal_contract_reconciliation_receipt.json")
    receipt["target"] = str(target)
    files = receipt.get("files")
    if not isinstance(files, list):
        raise AssertionError("fixture receipt files must be a list")
    original_transaction = "/Users/admin/.local/state/solet/transactions/bizopsb15.json"
    for item in files:
        if isinstance(item, dict) and item.get("path") == original_transaction:
            item["path"] = str(paths.transaction_path(_NAME))
    _write(paths.reconciliations_dir / f"{_NAME}.json", receipt)
    seed = str(transaction["seed_commit"])
    return paths, target, seed


def _manager(paths: ManagerPaths, seed: str) -> IdentityReconciliationManager:
    return IdentityReconciliationManager(
        paths=paths,
        target_revision_probe=lambda _target: TargetRevision(head=seed, main=seed),
    )


def _rewrite_receipt_before_revision(paths: ManagerPaths, revision: str) -> None:
    receipt_path = paths.reconciliations_dir / f"{_NAME}.json"
    receipt = _load(receipt_path)
    files = receipt.get("files")
    if not isinstance(files, list):
        raise AssertionError("fixture receipt files must be a list")
    for item in files:
        if isinstance(item, dict) and item.get("path") == str(paths.transaction_path(_NAME)):
            before = json.loads(base64.b64decode(str(item["before_base64"]), validate=True))
            if not isinstance(before, dict):
                raise AssertionError("fixture receipt transaction before-image must be an object")
            before["flow_source_revision"] = revision
            encoded = json.dumps(before, indent=2, sort_keys=True).encode() + b"\n"
            item["before_base64"] = base64.b64encode(encoded).decode("ascii")
            item["before_sha256"] = hashlib.sha256(encoded).hexdigest()
            _write(receipt_path, receipt)
            return
    raise AssertionError("fixture receipt lacks the transaction before-image")


def _run_positive_and_idempotency() -> None:
    with tempfile.TemporaryDirectory(prefix="identity-reconciliation-") as raw:
        paths, target, seed = _fixture(Path(raw))
        manager = _manager(paths, seed)
        before = load_transaction(paths.transaction_path(_NAME))
        if before is None:
            raise AssertionError("fixture transaction missing")
        preview = manager.run(_NAME, dry_run=True, approved_fingerprint=None)
        fingerprint = preview.data.get("approval_fingerprint")
        _check(preview.status == "preview_ready", "fixture selector yields a preview")
        _check(preview.data.get("old_revision") != seed, "preview names the stale stored revision")
        _check(preview.data.get("new_revision") == seed, "preview names the revised identity")
        _check(preview.data.get("seed_commit") == seed, "preview names the seed identity")
        _check(preview.data.get("dry_run_writes") == 0, "preview writes no fixture bytes")
        _check(isinstance(fingerprint, str), "preview carries an approval fingerprint")

        applied = manager.run(_NAME, dry_run=False, approved_fingerprint=fingerprint)
        after = load_transaction(paths.transaction_path(_NAME))
        if after is None:
            raise AssertionError("reconciled transaction missing")
        _check(applied.status == "reconciled", "approved identity migration applies")
        _check(
            after.flow_source_revision == after.answers["flow_source_revision"] == seed,
            "journal and answers revision converge to seed",
        )
        _check(
            after.stages == before.stages and after.stage_probe_statuses == before.stage_probe_statuses and after.probe_activations == before.probe_activations and after.operation_statuses == before.operation_statuses and after.completion == before.completion,
            "identity migration leaves derived collections untouched",
        )
        registry = _load(paths.registry_path)
        instances = registry.get("instances")
        _check(
            isinstance(instances, dict) and isinstance(instances.get(_NAME), dict) and instances[_NAME].get("flow_source_revision") == seed,
            "registry revision converges to seed",
        )
        projection = _load(target / ".solet" / "install-state.json")
        _check(projection.get("flow_source_revision") == seed, "target projection converges to seed")
        _check(
            (paths.identity_reconciliations_dir / f"{_NAME}.json").is_file(),
            "identity migration writes its own recovery receipt",
        )
        state_before_retry = paths.transaction_path(_NAME).read_bytes()
        retry = manager.run(_NAME, dry_run=False, approved_fingerprint=None)
        _check(retry.status == "identity_current", "already-converged identity is idempotent")
        _check(
            paths.transaction_path(_NAME).read_bytes() == state_before_retry,
            "idempotent retry writes no transaction bytes",
        )


def _run_refusals() -> None:
    with tempfile.TemporaryDirectory(prefix="identity-reconciliation-refusal-") as raw:
        paths, _, seed = _fixture(Path(raw))
        (paths.reconciliations_dir / f"{_NAME}.json").unlink()
        try:
            _manager(paths, seed).run(_NAME, dry_run=True, approved_fingerprint=None)
        except StateConflictError as exc:
            _check("receipt" in str(exc), "missing historical receipt refuses the selector")
        else:
            _check(False, "missing historical receipt must refuse")

    with tempfile.TemporaryDirectory(prefix="identity-reconciliation-refusal-") as raw:
        paths, _, seed = _fixture(Path(raw))
        manager = IdentityReconciliationManager(
            paths=paths,
            target_revision_probe=lambda _target: TargetRevision(head="0" * 40, main=seed),
        )
        try:
            manager.run(_NAME, dry_run=True, approved_fingerprint=None)
        except StateConflictError as exc:
            _check("HEAD and main" in str(exc), "target revision disagreement refuses")
        else:
            _check(False, "target revision disagreement must refuse")

    with tempfile.TemporaryDirectory(prefix="identity-reconciliation-refusal-") as raw:
        paths, _, seed = _fixture(Path(raw))
        _rewrite_receipt_before_revision(paths, "f" * 40)
        try:
            _manager(paths, seed).run(_NAME, dry_run=True, approved_fingerprint=None)
        except StateConflictError as exc:
            _check("before-image" in str(exc), "receipt before-image disagreement refuses")
        else:
            _check(False, "receipt before-image disagreement must refuse")


def _run_recovery() -> None:
    with tempfile.TemporaryDirectory(prefix="identity-reconciliation-recovery-") as raw:
        paths, _, seed = _fixture(Path(raw))
        manager = _manager(paths, seed)
        preview = manager.run(_NAME, dry_run=True, approved_fingerprint=None)
        fingerprint = preview.data.get("approval_fingerprint")
        if not isinstance(fingerprint, str):
            raise AssertionError("fixture preview lacks an approval fingerprint")
        actual = identity_module.atomic_replace_bytes
        calls = 0

        def interrupted(path: Path, value: bytes, *, mode: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("fixture interruption")
            actual(path, value, mode=mode)

        try:
            with patch.object(identity_module, "atomic_replace_bytes", interrupted):
                manager.run(_NAME, dry_run=False, approved_fingerprint=fingerprint)
        except OSError as exc:
            _check("interruption" in str(exc), "fixture interruption leaves a prepared receipt")
        else:
            _check(False, "fixture interruption must interrupt promotion")
        recovered = manager.run(_NAME, dry_run=True, approved_fingerprint=None)
        _check(recovered.data.get("recovery_performed") is True, "next preview recovers authenticated bytes")


def main() -> None:
    _run_positive_and_idempotency()
    _run_refusals()
    _run_recovery()
    print(f"identity_reconciliation_smoke OK: {_CHECKS} checks passed")


if __name__ == "__main__":
    main()
