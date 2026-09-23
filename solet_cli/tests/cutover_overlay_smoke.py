#!/usr/bin/env python3
"""T2's overlay contribution — one receipt-authorized entry, through the API.

The seam contract makes this side a CONSUMER: the entry exists only under a
terminal receipt's authority, and only :mod:`solet_manager.managed_tree` gets to
decide that.  So the sharpest check here is the one this module does NOT
implement — an append with no receipt must be refused by the API, and it must
still be refused when the contribution helper is the caller.

Legs, and the mutation each one reds on:

===============================  ===================================================
Leg                              Red mutation it catches
===============================  ===================================================
receipt authorizes the entry     bypass ManagedTreeStore.append and write the ledger
unreceipted append refused       resolve the receipt leniently
deletion recorded explicitly     drop absent paths from the entry
creation recorded as null        substitute a zero hash for a missing before-image
ordinal is derived               hardcode ordinal 0 on every contribution
files ordered by path            emit in caller iteration order
symlink refused, named           digest the link target as if it were a file
===============================  ===================================================
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "solet_cli" / "src"))
sys.path.insert(0, str(_ROOT / "ananta" / "src"))

from solet_manager.cutover_overlay import (  # noqa: E402
    contribute_cutover_overlay,
    file_sha256,
    overlay_files_for_refresh,
)
from solet_manager.managed_tree import ManagedTreeStore  # noqa: E402

_BASELINE = "a" * 40


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _private_dirs(target: Path) -> None:
    """State dirs must be no broader than 0700, as the real writer requires.

    A fixture that left these world-readable would prove the append works in
    conditions the production path refuses to run in.
    """
    walked = target / "profile" / "data" / "reconciliations"
    for path in (target / "profile", target / "profile" / "data", walked):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)


def _write_receipt(target: Path, reconciliation_id: str, *, status: str) -> None:
    receipt = (
        target / "profile" / "data" / "reconciliations" / "adapter" / "receipts"
        / f"{reconciliation_id}.json"
    )
    _private_dirs(target)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.parent.chmod(0o700)
    (receipt.parent.parent).chmod(0o700)
    receipt.write_text(
        json.dumps({"kind": "cutover_terminal_receipt", "terminal": {"status": status}}),
        encoding="utf-8",
    )
    # The API validates state files are exactly 0600; a fixture that skipped
    # this would be testing a receipt the real reader would refuse to open.
    receipt.chmod(0o600)


def _sha(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _checks(root: Path) -> list[tuple[bool, str]]:
    checks: list[tuple[bool, str]] = []
    target = root / "target"
    target.mkdir(parents=True, exist_ok=True)
    _private_dirs(target)
    plugin_src = target / "plugins" / "example" / "src"
    _write(plugin_src / "kept.py", "after\n")
    _write(plugin_src / "created.py", "new\n")
    removed = "plugins/example/src/removed.py"
    before = {
        "plugins/example/src/kept.py": _sha("before\n"),
        removed: _sha("gone\n"),
    }
    changed = ["plugins/example/src/created.py", "plugins/example/src/kept.py", removed]

    files = overlay_files_for_refresh(
        target=target, before_sha256_by_path=before, changed_paths=changed,
    )
    by_path = {item.path: item for item in files}
    checks.extend([
        (
            [item.path for item in files] == sorted(item.path for item in files),
            "overlay files are ordered by path ascending",
        ),
        (
            by_path["plugins/example/src/kept.py"].before_sha256 == _sha("before\n")
            and by_path["plugins/example/src/kept.py"].after_sha256 == file_sha256(plugin_src / "kept.py"),
            "a replaced file carries both before and after identities",
        ),
        (
            by_path["plugins/example/src/created.py"].before_sha256 is None,
            "a created file records a null before, not a fabricated hash",
        ),
        (
            by_path[removed].after_sha256 is None and by_path[removed].before_sha256 is not None,
            "a deleted file is recorded explicitly, not omitted",
        ),
    ])

    # Unreceipted append must be refused BY THE API, not by this module.
    unreceipted = "rec_no_receipt"
    try:
        contribute_cutover_overlay(
            target=target, reconciliation_id=unreceipted, baseline_digest=_BASELINE,
            before_sha256_by_path=before, changed_paths=changed,
        )
        refused = False
    except Exception:  # noqa: BLE001 — any refusal class is a pass; silence is the failure
        refused = True
    checks.append((refused, "an append with no terminal receipt is refused"))
    checks.append(
        (not ManagedTreeStore(target).path.exists(), "a refused append writes no ledger"),
    )

    # Receipted append succeeds and lands one entry.
    first = "rec_cutover_one"
    _write_receipt(target, first, status="reconciled")
    ledger = contribute_cutover_overlay(
        target=target, reconciliation_id=first, baseline_digest=_BASELINE,
        before_sha256_by_path=before, changed_paths=changed,
    )
    checks.extend([
        (len(ledger.entries) == 1, "a receipted contribution appends exactly one entry"),
        (ledger.entries[0].ordinal == 0, "the first entry takes ordinal 0"),
        (
            ledger.entries[0].reconciliation_id == first,
            "the entry names the authorizing reconciliation",
        ),
        (ManagedTreeStore(target).path.exists(), "the ledger is persisted at the target"),
    ])

    # A second reconciliation derives ordinal 1 rather than reusing 0.
    second = "rec_cutover_two"
    _write_receipt(target, second, status="already_reconciled")
    _write(plugin_src / "kept.py", "after-two\n")
    ledger2 = contribute_cutover_overlay(
        target=target, reconciliation_id=second, baseline_digest=_BASELINE,
        before_sha256_by_path={"plugins/example/src/kept.py": _sha("after\n")},
        changed_paths=["plugins/example/src/kept.py"],
    )
    checks.extend([
        (len(ledger2.entries) == 2, "a second contribution appends, never replaces"),
        (ledger2.entries[1].ordinal == 1, "the ordinal is derived from the ledger, not hardcoded"),
    ])

    # A non-successful terminal status is not authority to append.
    third = "rec_cutover_failed"
    _write_receipt(target, third, status="needs_intervention")
    try:
        contribute_cutover_overlay(
            target=target, reconciliation_id=third, baseline_digest=_BASELINE,
            before_sha256_by_path={}, changed_paths=["plugins/example/src/created.py"],
        )
        unsuccessful_refused = False
    except Exception:  # noqa: BLE001
        unsuccessful_refused = True
    checks.append(
        (unsuccessful_refused, "a receipt whose terminal status is not successful cannot authorize"),
    )

    # Symlinks are a named v1 limit, refused rather than silently digested.
    link = plugin_src / "linked.py"
    link.symlink_to(plugin_src / "kept.py")
    try:
        overlay_files_for_refresh(
            target=target, before_sha256_by_path={},
            changed_paths=["plugins/example/src/linked.py"],
        )
        symlink_refused = False
    except ValueError:
        symlink_refused = True
    checks.append((symlink_refused, "a symlink is refused as the named v1 limit"))

    try:
        overlay_files_for_refresh(target=target, before_sha256_by_path={}, changed_paths=[])
        empty_refused = False
    except ValueError:
        empty_refused = True
    checks.append((empty_refused, "an entry covering no paths is refused"))
    return checks


def main() -> int:
    root = Path("~/.ananta/releases").expanduser() / f"cutover-overlay-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    try:
        checks = _checks(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    for passed, label in checks:
        print(f"{'PASS' if passed else 'FAIL'} {label}")
    return 0 if all(passed for passed, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
