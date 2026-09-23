#!/usr/bin/env python3
"""Regression coverage for the blocking embedding-description bound gate."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from quality_gates import embedding_description_bound_gate as gate  # noqa: E402

_FAILURES: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    if not condition:
        _FAILURES.append(name)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fixture(root: Path, *, low: int = 10, high: int = 20) -> Path:
    validator = root / gate._VALIDATOR_REL
    _write(
        validator,
        f"EMBEDDING_DESCRIPTION_MIN_LENGTH = {low}\n"
        f"EMBEDDING_DESCRIPTION_MAX_LENGTH = {high}\n",
    )
    process = root / "plugins/example_plugin/processes/example.json"
    _write(
        process,
        '{"process_key": "example::verb", "embedding_description": "abcdefghij"}',
    )
    return process


def test_bound_is_read_from_validator_ast() -> None:
    with tempfile.TemporaryDirectory(prefix="embedding_bound_") as tmp:
        root = Path(tmp)
        _fixture(root, low=10, high=20)
        check("reads the validator lower bound", gate._read_bound(root) == (10, 20))
        _write(
            root / gate._VALIDATOR_REL,
            "EMBEDDING_DESCRIPTION_MIN_LENGTH = 11\nEMBEDDING_DESCRIPTION_MAX_LENGTH = 12\n",
        )
        check("a changed validator bound is observed", gate._read_bound(root) == (11, 12))


def test_out_of_range_description_is_a_blocking_finding() -> None:
    with tempfile.TemporaryDirectory(prefix="embedding_bound_") as tmp:
        root = Path(tmp)
        _fixture(root)
        files = gate._iter_process_json(gate._scan_roots(root))
        checked, findings, used = gate._scan(files, root, 11, 20, frozenset(), False)
        check("one discoverable process is checked", checked == 1)
        check("too-short description is found", len(findings) == 1 and not findings[0].allowlisted)
        check("no exclusion is spuriously used", used == set())
        check("finding carries the process key", findings[0].process_key == "example::verb")


def test_tagged_allowlist_suppresses_only_the_named_finding() -> None:
    with tempfile.TemporaryDirectory(prefix="embedding_bound_") as tmp:
        root = Path(tmp)
        _fixture(root)
        allowlist = root / "allowlist.txt"
        _write(
            allowlist,
            "plugins/example_plugin/processes/example.json::example::verb  # "
            "owner: iss_4f2cea3c reason: fixture suppression expires: 2026-12-31\n",
        )
        files = gate._iter_process_json(gate._scan_roots(root))
        _, findings, used = gate._scan(files, root, 11, 20, gate._load_allowlist(allowlist), False)
        check("named tagged entry suppresses its finding", len(findings) == 1 and findings[0].allowlisted)
        check("the matching tagged entry is recorded", len(used) == 1)


def main() -> int:
    print("Embedding-description bound gate smoke\n")
    test_bound_is_read_from_validator_ast()
    test_out_of_range_description_is_a_blocking_finding()
    test_tagged_allowlist_suppresses_only_the_named_finding()
    if _FAILURES:
        print(f"\nFAILED: {_FAILURES}")
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
