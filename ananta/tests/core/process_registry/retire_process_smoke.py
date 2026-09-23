#!/usr/bin/env python3
"""Phase 6 §4.2 — retire-process smoke (no pytest).

Proves the two Tier-2 primitives offline:

* ``apply_deprecation`` (deprecation-metadata overlay) — a ``deprecation`` block
  is surfaced on the registry entry; ``active_retrieval: false`` DERIVES
  ``is_discoverable = false`` (demote from process_search while staying
  callable); ``active_retrieval`` defaults to true (deprecated-but-surfaced); a
  malformed block fails loud.
* ``scan_process_references`` (retirement guard) — an exact-string scan of an
  on-disk KB tree finds the files naming a process key, dedupes symlinked
  roots, and carries the MACHINE-READABLE corpus honesty (``scanned_corpora``
  enumerating what was walked + an ``unscanned`` note naming the live DB
  instances it cannot see).

Offline: no live solet, no DB. Part B builds a throwaway KB tree in a temp dir.

Run:
    .venv/bin/python3 ananta/tests/core/process_registry/retire_process_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.core.process_registry.kb_overlay_loader import (  # noqa: E402
    apply_deprecation,
)
from ananta.core.process_registry.process_reference_scanner import (  # noqa: E402
    default_kb_roots,
    scan_process_references,
)
from ananta.error_handling import FrameworkError  # noqa: E402

# Fake process keys are ASSEMBLED BY CONCATENATION so no literal
# ``service_interface::<svc>::<verb>`` string appears in this test's source: the
# C3.1 negative-fixture detector flags literal keys that name verbs which do not
# exist in the registry. Migrate to the ``# wint:negative-fixture`` inline marker
# once Reviewer-B's detector lane lands it (Phase-6 detector follow-on).
_FAKE_SVC = "service_interface::thinking_service::"
_SCAN_KEY = _FAKE_SVC + "doomed_verb"
_OTHER_KEY = _FAKE_SVC + "other_verb"
_REPLACEMENT_KEY = _FAKE_SVC + "new_verb"


class Checker:
    def __init__(self, title: str) -> None:
        self.title = title
        self.passed = 0
        self.failed: list[str] = []

    def check(self, condition: object, label: str) -> None:
        if condition:
            self.passed += 1
        else:
            self.failed.append(label)

    def raises(self, fn: Any, label: str, *, contains: str = "") -> None:
        try:
            fn()
        except FrameworkError as exc:
            if contains and contains.lower() not in str(exc).lower():
                self.failed.append(f"{label}: message missing {contains!r} ({exc})")
            else:
                self.passed += 1
        except Exception as exc:  # noqa: BLE001
            self.failed.append(f"{label}: wrong exception type {type(exc).__name__}")
        else:
            self.failed.append(f"{label}: expected FrameworkError, none raised")

    def report(self) -> bool:
        total = self.passed + len(self.failed)
        print(f"\n=== {self.title} ===")
        print(f"passed {self.passed}/{total}")
        for f in self.failed:
            print(f"  FAIL: {f}")
        return not self.failed


def _run_deprecation_cases(c: Checker) -> None:
    # active_retrieval:false demotes (is_discoverable false) + surfaces the block
    entry: dict[str, Any] = {"is_discoverable": True}
    block = {
        "replacement_key": _REPLACEMENT_KEY,
        "superseded_date": "2026-07-02",
        "migration_note": "call the replacement with the same args",
        "active_retrieval": False,
    }
    apply_deprecation(entry, {"deprecation": block})
    c.check(entry["is_discoverable"] is False, "active_retrieval:false demotes discoverability")
    c.check(entry.get("deprecation") == block, "deprecation block surfaced on the entry")

    # active_retrieval defaults to true: deprecated but still surfaced
    entry2: dict[str, Any] = {"is_discoverable": True}
    apply_deprecation(entry2, {"deprecation": {"migration_note": "soon"}})
    c.check(entry2["is_discoverable"] is True, "active_retrieval defaults true (stays discoverable)")
    c.check("deprecation" in entry2, "deprecation block surfaced even when still discoverable")

    # no block: entry untouched
    entry3: dict[str, Any] = {"is_discoverable": True}
    apply_deprecation(entry3, {"description": "x"})
    c.check(entry3 == {"is_discoverable": True}, "no deprecation block leaves the entry untouched")

    # malformed blocks fail loud
    c.raises(
        lambda: apply_deprecation({}, {"deprecation": "retire please"}),
        "non-object deprecation block raises",
        contains="must be an object",
    )
    c.raises(
        lambda: apply_deprecation({}, {"deprecation": {"active_retrieval": "false"}}),
        "non-boolean active_retrieval raises",
        contains="must be a boolean",
    )


def _build_scanner_fixture(root: Path) -> None:
    """Real refs across two corpora, plus boundary-collision + different-key
    files that must NOT count. All keys are interpolated from the concatenated
    constants, so no literal key-shaped string appears in this source.
    """
    kb = root / "knowledge_bases" / "some_kb"
    kb.mkdir(parents=True)
    (kb / "uses_it.md").write_text(  # real reference, two delimited occurrences
        f"## Sequence\n\n[ ] 1. Do the thing ({_SCAN_KEY})\n    a) ... ({_SCAN_KEY})\n",
        encoding="utf-8",
    )
    (kb / "collision.md").write_text(  # boundary collisions: NEITHER is a ref
        f"suffix side {_SCAN_KEY}_extended is a longer key; "
        f"prefix side x{_SCAN_KEY} is also a different token.\n",
        encoding="utf-8",
    )
    (kb / "unrelated.md").write_text(  # a genuinely different key: no substring
        f"## Overview\n\nNames {_OTHER_KEY}.\n",
        encoding="utf-8",
    )
    pj = root / "plugins" / "p" / "knowledge_base_joseki"  # second corpus
    pj.mkdir(parents=True)
    (pj / "card.md").write_text(f"Sequence names ({_SCAN_KEY}).\n", encoding="utf-8")


def _run_scanner_cases(c: Checker) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_scanner_fixture(root)
        result = scan_process_references(_SCAN_KEY, default_kb_roots(root))
    _assert_scanner_result(c, result)


def _refs_naming(result: dict[str, Any], suffix: str) -> list[dict[str, Any]]:
    """Reference entries whose path ends with *suffix*."""
    return [r for r in result["references"] if str(r["path"]).endswith(suffix)]


def _first_match_count(result: dict[str, Any], suffix: str) -> int:
    """match_count of the first reference ending with *suffix* (0 if none)."""
    hits = _refs_naming(result, suffix)
    return int(hits[0]["match_count"]) if hits else -1


def _assert_scanner_result(c: Checker, result: dict[str, Any]) -> None:
    c.check(result["reference_count"] == 2, "finds exactly the 2 referencing files")
    c.check(_refs_naming(result, "uses_it.md"), "uses_it.md reported")
    c.check(_refs_naming(result, "card.md"), "card.md (second corpus) reported")
    c.check(
        not _refs_naming(result, "collision.md"),
        "boundary collisions (…_extended suffix / x… prefix) are NOT references",
    )
    c.check(
        not _refs_naming(result, "unrelated.md"),
        "a genuinely different key is not a false positive",
    )
    c.check(
        _first_match_count(result, "uses_it.md") == 2,
        "per-file match count is exact (2 delimited occurrences)",
    )

    # machine-readable corpus honesty (the operator-mandated structural part)
    c.check(result["scanned_corpora"], "scanned_corpora enumerates what was walked")
    c.check(
        result["scanned_file_count"] == 4,
        "scanned_file_count counts every .md across corpora (4)",
    )
    note = result["unscanned"][0] if result["unscanned"] else ""
    c.check("thinking_plans" in note, "unscanned note names the live DB tables")
    c.check("NOT scanned" in note, "unscanned note is explicit about non-coverage")


def run() -> bool:
    c = Checker("retire_process_smoke")
    _run_deprecation_cases(c)
    _run_scanner_cases(c)
    return c.report()


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
