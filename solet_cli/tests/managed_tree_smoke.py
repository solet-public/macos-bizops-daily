#!/usr/bin/env python3
"""Hermetic controls for the frozen managed-tree-v1 overlay seam."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.errors import StateConflictError  # noqa: E402
from solet_manager.managed_tree import (  # noqa: E402
    ManagedTreeLedger,
    ManagedTreeStore,
    OverlayEntry,
    OverlayFile,
    verify_managed_tree,
)
from solet_manager.seed_tree_verifier import GitQueryResult  # noqa: E402
from solet_manager.state_io import atomic_write_json  # noqa: E402

_CHECKS = 0
_SEED_TREE = "a" * 40


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {label}")


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


class FakeGit:
    def __init__(self, paths: tuple[str, ...]) -> None:
        self._paths = paths

    def __call__(self, command: Sequence[str], _timeout: int) -> GitQueryResult:
        if command[-2:] == ("rev-parse", "HEAD^{tree}"):
            return GitQueryResult(0, f"{_SEED_TREE}\n", "")
        if command[-3:] == ("diff", "--name-status", "-z"):
            return GitQueryResult(0, "".join(f"M\0{path}\0" for path in self._paths), "")
        if command[-4:] == ("diff", "--cached", "--name-status", "-z"):
            return GitQueryResult(0, "", "")
        raise AssertionError(f"unexpected Git query: {command!r}")


def _write_receipt(target: Path, reconciliation_id: str) -> None:
    path = (
        target
        / "profile"
        / "data"
        / "reconciliations"
        / "adapter"
        / "receipts"
        / f"{reconciliation_id}.json"
    )
    atomic_write_json(
        path,
        {
            "kind": "cutover_terminal_receipt",
            "terminal": {"status": "reconciled"},
        },
    )


def _write_target_file(target: Path, path: str, content: bytes) -> OverlayFile:
    destination = target / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return OverlayFile(path, None, _digest(content))


def _assert_two_producer_shapes_verify() -> None:
    with tempfile.TemporaryDirectory(prefix="managed-tree-") as temporary:
        target = Path(temporary) / "target"
        target.mkdir()
        reconciliations = target / "profile" / "data" / "reconciliations"
        reconciliations.mkdir(parents=True, mode=0o700)
        reconciliations.chmod(0o700)
        _write_receipt(target, "rec_cutover_fixture")
        _write_receipt(target, "rec_contract_fixture")
        cutover_file = _write_target_file(target, "plugins/example.py", b"cutover bytes")
        contract_files = tuple(
            _write_target_file(target, f"contracts/{index}.json", f"contract {index}".encode())
            for index in range(5)
        )
        store = ManagedTreeStore(target)
        baseline = _SEED_TREE
        first = store.append(
            baseline_digest=baseline,
            entry=OverlayEntry(0, "rec_cutover_fixture", (cutover_file,)),
        )
        ledger = store.append(
            baseline_digest=baseline,
            entry=OverlayEntry(1, "rec_contract_fixture", contract_files),
        )
        _check(first.entries[0].ordinal == 0, "cutover-shaped fake did not append first")
        _check(len(ledger.entries[1].files) == 5, "contract-shaped fake did not carry five paths")
        _check(
            ledger.to_dict()["schema"] == "managed-tree-v1",
            "ledger schema changed",
        )
        verified = verify_managed_tree(
            target,
            seed_tree_hash=_SEED_TREE,
            expected_digest=ledger.digest,
            runner=FakeGit(tuple(item.path for item in (*first.entries[0].files, *contract_files))),
        )
        _check(verified.entry_count == 2, "verified overlay count is wrong")
        _check(verified.digest == ledger.digest, "verification did not recompute ledger digest")
        (target / cutover_file.path).write_bytes(b"mutated")
        try:
            verify_managed_tree(
                target,
                seed_tree_hash=_SEED_TREE,
                expected_digest=ledger.digest,
                runner=FakeGit(tuple(item.path for item in (*first.entries[0].files, *contract_files))),
            )
        except StateConflictError as exc:
            _check("content is wrong" in str(exc), "content mutation refusal is imprecise")
        else:
            _check(False, "declared content mutation did not refuse")


def _assert_closed_shape_refusals() -> None:
    for path in ("../escape", "/absolute", ".git/config", "profile/data/state.json"):
        try:
            OverlayFile(path, None, _digest(b"value"))
        except StateConflictError:
            _check(True, f"unsafe overlay path {path!r} refused")
        else:
            _check(False, f"unsafe overlay path {path!r} accepted")
    try:
        OverlayEntry(0, "rec_fixture", (OverlayFile("z", None, _digest(b"z")), OverlayFile("a", None, _digest(b"a"))))
    except StateConflictError:
        _check(True, "unordered overlay files refused")
    else:
        _check(False, "unordered overlay files accepted")
    try:
        ManagedTreeLedger(_SEED_TREE, (OverlayEntry(1, "rec_fixture", (OverlayFile("a", None, _digest(b"a")),)),))
    except StateConflictError:
        _check(True, "sparse ordinal refused")
    else:
        _check(False, "sparse ordinal accepted")


def _assert_ordered_path_replay() -> None:
    with tempfile.TemporaryDirectory(prefix="managed-tree-replay-") as temporary:
        target = Path(temporary) / "target"
        target.mkdir()
        reconciliations = target / "profile" / "data" / "reconciliations"
        reconciliations.mkdir(parents=True, mode=0o700)
        reconciliations.chmod(0o700)
        _write_receipt(target, "rec_replay_one")
        _write_receipt(target, "rec_replay_two")
        first = _digest(b"first")
        second = _digest(b"second")
        ordered_path = "plugins/ordered.py"
        _write_target_file(target, ordered_path, b"second")
        store = ManagedTreeStore(target)
        first_entry = OverlayEntry(0, "rec_replay_one", (OverlayFile(ordered_path, None, first),))
        second_entry = OverlayEntry(1, "rec_replay_two", (OverlayFile(ordered_path, first, second),))
        store.append(baseline_digest=_SEED_TREE, entry=first_entry)
        ledger = store.append(baseline_digest=_SEED_TREE, entry=second_entry)
        verified = verify_managed_tree(
            target,
            seed_tree_hash=_SEED_TREE,
            expected_digest=ledger.digest,
            runner=FakeGit((ordered_path,)),
        )
        _check(verified.entry_count == 2, "ordered replacement ledger did not verify")
        _check(ledger.effective_files == (OverlayFile(ordered_path, None, second),), "ordered replacement did not collapse to final delta")
        wrong_baseline = ManagedTreeLedger("b" * 40, ledger.entries)
        atomic_write_json(store.path, wrong_baseline.to_dict())
        try:
            verify_managed_tree(
                target,
                seed_tree_hash=_SEED_TREE,
                expected_digest=wrong_baseline.digest,
                runner=FakeGit((ordered_path,)),
            )
        except StateConflictError as exc:
            _check("does not match" in str(exc), "wrong baseline digest refusal is imprecise")
        else:
            _check(False, "wrong baseline digest was accepted")
        try:
            ManagedTreeLedger(
                _SEED_TREE,
                (
                    OverlayEntry(0, "rec_replay_one", (OverlayFile(ordered_path, None, first),)),
                    OverlayEntry(1, "rec_replay_two", (OverlayFile(ordered_path, _digest(b"wrong"), second),)),
                ),
            )
        except StateConflictError as exc:
            _check("discontinuous" in str(exc), "receipt-chain discontinuity refusal is imprecise")
        else:
            _check(False, "receipt-chain discontinuity accepted")
        deletion_target = Path(temporary) / "deletion-target"
        deletion_target.mkdir()
        deletion_reconciliations = deletion_target / "profile" / "data" / "reconciliations"
        deletion_reconciliations.mkdir(parents=True, mode=0o700)
        deletion_reconciliations.chmod(0o700)
        _write_receipt(deletion_target, "rec_replay_one")
        _write_receipt(deletion_target, "rec_replay_two")
        deletion_store = ManagedTreeStore(deletion_target)
        deletion_store.append(
            baseline_digest=_SEED_TREE,
            entry=OverlayEntry(0, "rec_replay_one", (OverlayFile("plugins/transient.py", None, first),)),
        )
        deletion_ledger = deletion_store.append(
            baseline_digest=_SEED_TREE,
            entry=OverlayEntry(1, "rec_replay_two", (OverlayFile("plugins/transient.py", first, None),)),
        )
        _check(deletion_ledger.effective_files == (), "created-then-deleted path did not return to baseline")
        verified_deletion = verify_managed_tree(
            deletion_target,
            seed_tree_hash=_SEED_TREE,
            expected_digest=deletion_ledger.digest,
            runner=FakeGit(()),
        )
        _check(verified_deletion.entry_count == 2, "created-then-deleted path did not verify")


def main() -> int:
    _assert_two_producer_shapes_verify()
    _assert_closed_shape_refusals()
    _assert_ordered_path_replay()
    print(f"managed_tree_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
