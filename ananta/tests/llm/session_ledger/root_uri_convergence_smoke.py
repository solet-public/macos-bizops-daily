#!/usr/bin/env python3
"""N4 preflight — root_uri convergence regression (pure, no DB).

A ``session_ledger__source`` row is keyed by ``(source_kind, root_uri)``. The
historical duplicate-source bug had two causes, both of which this smoke pins:

* **cause #1 (spelling drift):** ``~/.codex/history.jsonl`` (operator-config),
  ``/Users/alice/.codex/history.jsonl`` (bare abs) and
  ``file:///Users/alice/.codex/history.jsonl`` (boot-normalized) all named the same
  file but produced THREE rows. :func:`normalize_root_uri` must collapse them.
* **cause #2 (symlink alias):** two symlink spellings of the same EXISTING
  directory must converge under :func:`canonicalize_root_uri_for_storage`'s
  realpath pass.

Plus the contract that non-filesystem ``root_uri`` values (``pushed:*`` /
``local:*`` / blob ids) pass through UNCHANGED and that
:func:`root_uri_to_path` is the inverse for fs forms and raises for the rest.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/root_uri_convergence_smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.root_uri import (  # noqa: E402
    canonicalize_root_uri_for_storage,
    normalize_root_uri,
    root_uri_to_path,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def test_spelling_drift_collapses_to_one_canonical() -> None:
    """cause #1: bare abs / ``file://`` / ``~`` spellings → one ``file:///<abs>``."""
    home = os.path.expanduser("~")
    abs_path = f"{home}/.codex/history.jsonl"
    spellings = [
        abs_path,
        f"file://{abs_path}",
        "~/.codex/history.jsonl",
    ]
    normalized = {normalize_root_uri(s) for s in spellings}
    _check(
        normalized == {f"file://{abs_path}"},
        f"3 spellings collapse to one file:///<abs> (got {normalized})",
    )


def test_normalize_is_idempotent_fixed_point() -> None:
    once = normalize_root_uri("~/.codex/history.jsonl")
    twice = normalize_root_uri(once)
    _check(once == twice, f"normalize is an idempotent fixed point ({once!r})")


def test_symlink_alias_converges_under_storage_canonicalize() -> None:
    """cause #2: two symlink spellings of the same existing dir → one realpath."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        real_dir = root / "real_sessions"
        real_dir.mkdir()
        link_dir = root / "link_sessions"
        link_dir.symlink_to(real_dir, target_is_directory=True)
        via_real = canonicalize_root_uri_for_storage(str(real_dir))
        via_link = canonicalize_root_uri_for_storage(str(link_dir))
        _check(
            via_real == via_link,
            f"symlink + real spellings converge to one row ({via_real} vs {via_link})",
        )
        _check(
            via_real == f"file://{os.path.realpath(real_dir)}",
            "convergence target is the realpath of the real directory",
        )


def test_storage_canonicalize_keeps_lexical_when_missing() -> None:
    """A path that does not exist keeps the lexical (no-realpath) form."""
    missing = "/__nonexistent_ledger_root__/x"
    _check(
        canonicalize_root_uri_for_storage(missing) == f"file://{missing}",
        "non-existent path keeps the lexical file:///<abs> form (no realpath)",
    )


def test_non_filesystem_root_uris_pass_through_unchanged() -> None:
    for value in (
        "pushed:codex_pushed",
        "local:agent_messaging",
        "session-ledger-export-sha256-deadbeef",
        "bmd-0001",
    ):
        _check(
            normalize_root_uri(value) == value
            and canonicalize_root_uri_for_storage(value) == value,
            f"non-fs root_uri {value!r} passes through unchanged",
        )


def test_root_uri_to_path_inverts_fs_forms() -> None:
    home = os.path.expanduser("~")
    _check(
        root_uri_to_path(f"file://{home}/x") == Path(f"{home}/x"),
        "root_uri_to_path inverts the file:///<abs> form",
    )
    _check(
        root_uri_to_path("~/x") == Path(f"{home}/x"),
        "root_uri_to_path expands ~ to an absolute path",
    )


def test_root_uri_to_path_raises_for_non_filesystem() -> None:
    for value in ("pushed:codex_pushed", "local:agent_messaging", "bmd-0001"):
        try:
            root_uri_to_path(value)
        except ValueError:
            _check(True, f"root_uri_to_path raises ValueError for {value!r}")
        else:
            _check(False, f"expected ValueError for non-fs root_uri {value!r}")


def main() -> int:
    print("=== root_uri_convergence_smoke (N4 preflight) ===")
    test_spelling_drift_collapses_to_one_canonical()
    test_normalize_is_idempotent_fixed_point()
    test_symlink_alias_converges_under_storage_canonicalize()
    test_storage_canonicalize_keeps_lexical_when_missing()
    test_non_filesystem_root_uris_pass_through_unchanged()
    test_root_uri_to_path_inverts_fs_forms()
    test_root_uri_to_path_raises_for_non_filesystem()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
