#!/usr/bin/env python3
"""Smoke — knowledge_service `ingest` verb logic (kb_lifecycle.ingest_kb).

`ingest` is the content-hash-gated idempotent (re)ingest verb: a single KB is skipped
when current else reindexed (fail-loud); name="all" scans the KB root and applies the
same per-KB gate, collecting per-KB failures (status="partial"). The NEW responsibility
this slice adds is ingest_kb's ORCHESTRATION — the has_valid_install gate, the all-mode
scan + loop, the result assembly, and the partial-status-on-failure. `has_valid_install`
and `install_kb` are pre-existing, separately-exercised leaves, so they are faked here
(the fake-source / real-logic unit boundary). The live end-to-end (real embed + state)
is verified post-restart via process_call per the create-process skill Step 5; this is
the restart-free slice-ready proof.

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:
    .venv/bin/python3 plugins/default_knowledge_plugin/tests/ingest_kb_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"))

import default_knowledge_plugin.kb_lifecycle as kb  # noqa: E402

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


_SVC: Any = object()  # opaque service stand-in; the faked leaves ignore it


def _install_fakes(current: set[str], fail: set[str], chunks: int = 3) -> None:
    """Replace the leaf functions ingest_kb composes — the fake-source boundary."""

    def fake_has_valid(name: str, state: Any, memory: Any, kb_root: Any) -> bool:
        del state, memory, kb_root
        return name in current

    def fake_install(
        name: str, source: Any, kb_root: Any, state: Any, memory: Any, ab: Any,
    ) -> dict[str, Any]:
        del source, kb_root, state, memory, ab
        if name in fail:
            raise RuntimeError(f"index failure: {name}")
        return {"status": "success", "name": name, "chunk_count": chunks}

    kb.has_valid_install = fake_has_valid  # type: ignore[assignment]
    kb.install_kb = fake_install  # type: ignore[assignment]


def _ingest(name: str, kb_root: Path) -> dict[str, Any]:
    return kb.ingest_kb(name, kb_root, _SVC, _SVC, _SVC)


def _make_kb_root(names: list[str]) -> tempfile.TemporaryDirectory[str]:
    tmp = tempfile.TemporaryDirectory(prefix="ingest_smoke_")
    root = Path(tmp.name)
    for n in names:
        (root / n).mkdir()
    (root / ".hidden").mkdir()  # dotdir → skipped by the scan
    (root / "loose.txt").write_text("x")  # file → skipped (not is_dir)
    return tmp


def test_single_unchanged() -> None:
    _install_fakes(current={"alpha"}, fail=set())
    res = _ingest("alpha", REPO_ROOT)
    _check(res["mode"] == "single", "single: mode=single")
    _check(
        res["unchanged"] == ["alpha"] and res["ingested"] == [],
        "single current → unchanged, not reindexed",
    )
    _check(res["status"] == "success" and res["total_chunks"] == 0, "single unchanged → success, 0 chunks")


def test_single_stale_reindexes() -> None:
    _install_fakes(current=set(), fail=set(), chunks=5)
    res = _ingest("beta", REPO_ROOT)
    _check(res["ingested"] == ["beta"] and res["unchanged"] == [], "single stale → reindexed")
    _check(res["total_chunks"] == 5, "single reindex → chunk count from install_kb")
    _check(res["status"] == "success" and res["failed"] == [], "single reindex → success, no failures")


def test_single_fails_loud() -> None:
    _install_fakes(current=set(), fail={"gamma"})
    raised = False
    try:
        _ingest("gamma", REPO_ROOT)
    except RuntimeError:
        raised = True
    _check(raised, "single-name install failure PROPAGATES (fail-loud, not collected)")


def test_all_mixed() -> None:
    tmp = _make_kb_root(["kb_a", "kb_b", "kb_c"])
    try:
        _install_fakes(current={"kb_a"}, fail=set(), chunks=2)
        res = _ingest("all", Path(tmp.name))
        _check(res["mode"] == "all", "all: mode=all")
        _check(res["unchanged"] == ["kb_a"], "all: current KB skipped (unchanged)")
        _check(sorted(res["ingested"]) == ["kb_b", "kb_c"], "all: stale KBs reindexed")
        _check(res["total_chunks"] == 4, "all: total_chunks summed across reindexed (2+2)")
        _check(res["status"] == "success", "all: success when none failed")
        seen = set(res["ingested"]) | set(res["unchanged"])
        _check(".hidden" not in seen and "loose.txt" not in seen, "all: dotdirs + non-dir entries skipped")
    finally:
        tmp.cleanup()


def test_all_partial_on_failure() -> None:
    tmp = _make_kb_root(["kb_a", "kb_b", "kb_c"])
    try:
        _install_fakes(current={"kb_a"}, fail={"kb_b"}, chunks=3)
        res = _ingest("all", Path(tmp.name))
        _check(res["status"] == "partial", "all: status='partial' when a KB fails (birth path can detect)")
        _check([f["name"] for f in res["failed"]] == ["kb_b"], "all: failed lists the failing KB")
        _check("index failure" in res["failed"][0]["error"], "all: failure error surfaced, not swallowed")
        _check(res["ingested"] == ["kb_c"], "all: batch CONTINUES past failure (kb_c still reindexed)")
        _check(res["unchanged"] == ["kb_a"], "all: current KB still skipped despite sibling failure")
    finally:
        tmp.cleanup()


def test_all_empty_root() -> None:
    _install_fakes(current=set(), fail=set())
    res = _ingest("all", REPO_ROOT / "does_not_exist_xyz")
    _check(
        res["ingested"] == [] and res["unchanged"] == [] and res["failed"] == [],
        "all: missing root → empty result",
    )
    _check(res["status"] == "success", "all: missing root → success (nothing to do)")


def main() -> int:
    print("knowledge_service ingest verb (kb_lifecycle.ingest_kb) smoke")
    print("===========================================================")
    test_single_unchanged()
    test_single_stale_reindexes()
    test_single_fails_loud()
    test_all_mixed()
    test_all_partial_on_failure()
    test_all_empty_root()
    print(f"\nPASSED: {_passed}\nFAILED: {len(_failed)}")
    if _failed:
        for label in _failed:
            print(f"  - {label}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
