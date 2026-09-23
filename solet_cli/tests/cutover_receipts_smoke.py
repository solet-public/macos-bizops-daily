#!/usr/bin/env python3
"""Focused M1 controls for durable target-local cutover receipts."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.adapter_reconciliation import recover_adapter_reconciliation  # noqa: E402
from solet_manager.cutover_receipts import (  # noqa: E402
    CutoverJournal,
    CutoverReceiptStore,
    CutoverRuntimeObservation,
    CutoverTerms,
    SnapshotReceipt,
    new_reconciliation_id,
)
from solet_manager.errors import StateConflictError  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402

_CHECKS = 0


def _check(condition: bool, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _terms() -> CutoverTerms:
    return CutoverTerms(
        current_release_id="rel_prior",
        active_color="blue",
        active_instance_id="inst_prior",
        active_start_token="start_prior",
        manifest_etag="manifest_prior",
        launch_topology="materialized_supervisor",
        launchagent_label="local.solet.fixture",
        launchagent_plist_sha256="sha256:" + "1" * 64,
        adapter_module_sha256="sha256:" + "2" * 64,
        adapter_module_replaced=True,
        source_surface_sha256="sha256:" + "3" * 64,
        release_surface_sha256="sha256:" + "4" * 64,
        verification_modules=(
            "github_midwife_plugin.setup_adapter",
            "macos_self_deployment_plugin.plugin",
        ),
    )


def _journal(target: Path) -> CutoverJournal:
    return CutoverJournal.prepared(
        reconciliation_id=new_reconciliation_id(),
        name="fixture",
        target=target,
        fingerprint="sha256:" + "5" * 64,
        terms=_terms(),
        files=(),
    )


def _snapshot_file(path: Path, before: bytes, after: bytes) -> dict[str, object]:
    return {
        "path": str(path),
        "mode": 0o644,
        "before_sha256": hashlib.sha256(before).hexdigest(),
        "before_base64": base64.b64encode(before).decode("ascii"),
        "after_sha256": hashlib.sha256(after).hexdigest(),
        "after_base64": base64.b64encode(after).decode("ascii"),
    }


def _raises(function: object, label: str) -> None:
    try:
        function()  # type: ignore[operator]
    except StateConflictError:
        return
    raise AssertionError(label)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cutover-receipts-") as temporary:
        target = Path(temporary) / "target"
        target.mkdir()
        store = CutoverReceiptStore(target)
        journal = _journal(target)
        store.write_active(journal)
        _check(store.active_path.is_file(), "prepared journal is target-local and durable")
        _check(
            "profile/data/reconciliations/adapter" in str(store.active_path),
            "journal uses target profile path",
        )
        _raises(lambda: journal.advance("prepared"), "stage machine refuses non-forward move")
        requested = journal.advance("bytes_applied").advance("cutover_requested")
        store.write_active(requested)
        recovered, terminal = store.recover_requested(
            lambda _journal: CutoverRuntimeObservation(
                reachable=True,
                release_id="rel_candidate",
                active_instance_id="inst_candidate",
                source_surface_sha256=_terms().source_surface_sha256,
                release_surface_sha256=_terms().release_surface_sha256,
            )
        )
        _check(
            recovered.stage == "runtime_verified",
            "orphaned requested cutover is reconciled by observation",
        )
        _check(terminal == "reconciled", "desired observed state becomes reconciled")
        receipt_path = store.finalize(recovered, "reconciled")
        _check(receipt_path.is_file(), "terminal receipt is immutable per reconciliation id")
        _check(not store.active_path.exists(), "terminal receipt retires active journal")
        _raises(
            lambda: store.finalize(recovered, "reconciled"),
            "finalize refuses an existing immutable receipt path",
        )

        unresolved = _journal(target).advance("bytes_applied").advance("cutover_requested")
        store.write_active(unresolved)
        same, terminal = store.recover_requested(
            lambda _journal: CutoverRuntimeObservation(False, None, None, None, None)
        )
        _check(same.stage == "cutover_requested", "unreadable observation does not invent a stage")
        _check(
            terminal == "needs_intervention",
            "unreadable observation is intervention, not rollback",
        )
        _check(
            new_reconciliation_id() != new_reconciliation_id(),
            "receipt ids are opaque per attempt",
        )

        legacy_target = Path(temporary) / "legacy-target"
        first = legacy_target / "plugins" / "first.py"
        second = legacy_target / "plugins" / "second.py"
        first.parent.mkdir(parents=True)
        before_first, after_first = b"before-first\\n", b"after-first\\n"
        before_second, after_second = b"before-second\\n", b"after-second\\n"
        first.write_bytes(after_first)
        second.write_bytes(before_second)
        paths = ManagerPaths(
            Path(temporary) / "manager" / "config",
            Path(temporary) / "manager" / "state",
            Path(temporary) / "manager" / "cache",
        )
        legacy_path = paths.adapter_reconciliations_dir / "legacy.json"
        legacy_path.parent.mkdir(parents=True)
        legacy_path.parent.chmod(0o700)
        legacy_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "legacy",
                    "target": str(legacy_target),
                    "seed": {"commit": "a" * 40},
                    "preview_fingerprint": "sha256:" + "6" * 64,
                    "state": "prepared",
                    "files": [
                        _snapshot_file(first, before_first, after_first),
                        _snapshot_file(second, before_second, after_second),
                    ],
                }
            ),
            encoding="utf-8",
        )
        legacy_store = CutoverReceiptStore(legacy_target, legacy_snapshot_path=legacy_path)
        legacy = legacy_store.load_active()
        _check(isinstance(legacy, SnapshotReceipt), "v1 receipt loads as snapshot mode")
        _check(
            isinstance(legacy, SnapshotReceipt) and legacy.state == "prepared",
            "v1 snapshot state remains readable without conversion",
        )
        _check(
            recover_adapter_reconciliation(paths, "legacy"),
            "v1 snapshot receipt still drives authenticated recovery",
        )
        _check(
            first.read_bytes() == before_first and second.read_bytes() == before_second,
            "v1 partial application recovers every before-image",
        )
    print(f"cutover_receipts_smoke: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
