#!/usr/bin/env python3
"""Born-clone identity smoke for the materialized Git policy modules.

The canonical-source rule is documented in
``plugins/github_midwife_plugin/coordination_hooks_common/README.md`` and the
shipped hook security boundary in
``plugins/github_midwife_plugin/claude_plugin/coordination-hooks/SECURITY.md``.
This smoke keeps only the three-copy identity legs from the checkout-level
lineage smoke, so it is valid in a born clone as well as this repository.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.dont_write_bytecode = True

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_MODULE_NAMES = (
    "_git_policy.py",
    "_git_controller_walker.py",
    "_git_controller_lex.py",
)
_PASSED = 0
_FAILED: list[str] = []


def _check(condition: bool, label: str) -> None:
    global _PASSED
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if condition:
        _PASSED += 1
    else:
        _FAILED.append(label)


def _copies(name: str) -> tuple[Path, Path, Path]:
    return (
        _PLUGIN_ROOT / "coordination_hooks_common" / name,
        _PLUGIN_ROOT / "claude_plugin" / "coordination-hooks" / "hooks" / name,
        _PLUGIN_ROOT / "codex_plugin" / "coordination-hooks" / "hooks" / name,
    )


def _case_canonical_copies_are_present_and_identical() -> None:
    for name in _MODULE_NAMES:
        copies = _copies(name)
        present = all(path.is_file() for path in copies)
        _check(present, f"all three canonical {name} copies are present")
        if not present:
            continue
        digests = {hashlib.sha256(path.read_bytes()).hexdigest() for path in copies}
        _check(
            len(digests) == 1,
            f"{name} is byte-identical across common and both materialized copies",
        )


def main() -> int:
    print("Git policy shipped identity smoke (common plus two materialized copies)")
    print("=" * 72)
    _case_canonical_copies_are_present_and_identical()
    print("=" * 72)
    if _FAILED:
        print(f"FAIL: {_PASSED} passed, {len(_FAILED)} failed")
        return 1
    print(f"PASS: {_PASSED} cross-copy identity checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
