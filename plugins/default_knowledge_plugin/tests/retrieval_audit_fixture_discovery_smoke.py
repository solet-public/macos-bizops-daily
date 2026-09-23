#!/usr/bin/env python3
"""Regression smoke for the retrieval-audit fixture-discovery repair (2026-07-26).

``audit_retrieval_corpus`` at its default ``corpus_root`` used to discover only
10 of the real ``*retrieval_test.yaml`` fixtures — three independent defects:
symlink non-traversal (``knowledge_bases/`` is a symlink-aggregation dir and
``Path.rglob`` does not traverse symlinks by default), a dot-vs-underscore
glob mismatch, and an ``article_path`` dialect (real repo path vs.
symlink-relative path) that aborted the whole run. Fixed in
``retrieval_audit.discover_test_files`` and ``retrieval_test.parse_article_path``.

Offline against the real corpus on disk (read-only). No DB, no plugin
instance — exercises the same pure functions ``audit_retrieval_corpus`` calls.

Run:
    .venv/bin/python3 plugins/default_knowledge_plugin/tests/retrieval_audit_fixture_discovery_smoke.py
"""

from __future__ import annotations

import fnmatch
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"))

from default_knowledge_plugin.retrieval_audit import (  # noqa: E402
    discover_test_files,
)
from default_knowledge_plugin.retrieval_test import parse_article_path  # noqa: E402


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

    def report(self) -> bool:
        total = self.passed + len(self.failed)
        print(f"\n=== {self.title} ===")
        print(f"passed {self.passed}/{total}")
        for f in self.failed:
            print(f"  FAIL: {f}")
        return not self.failed


# The one known-incompatible file under the real corpus: a whole-KB batch
# query smoke (``tests: [{query, expect}]``) for a different tool (the KB
# Authoring Rubric's per-KB gate), not a per-article retrieval_test.yaml
# companion. Its bare filename (no ``.``/``_`` separator before
# "retrieval_test.yaml") is correctly never matched by either glob.
_OUT_OF_SCOPE_SUFFIX = "code_vetting_plugin/knowledge_base/retrieval_test.yaml"


def _expected_source_fixtures() -> set[Path]:
    """Enumerate the real source roots independently of the symlink aggregator."""
    source_roots = [
        REPO_ROOT / "ananta" / "knowledge_base",
        REPO_ROOT / "ananta" / "knowledge_bases",
        # `knowledge_base*`, not `knowledge_base`: discovery reaches the SUFFIXED
        # knowledge bases (knowledge_base_joseki, _plan_templates, _playbooks) through
        # the `knowledge_bases/` symlinks, so a bare-name enumeration here drifts from
        # what discovery actually walks. The comparison downstream is an EQUALITY, so
        # the drift surfaces as the first fixture added under a suffixed KB being
        # reported "unexpected". `is_dir()` is load-bearing: the glob also matches
        # regular files, and every result is handed to `.rglob()`.
        *sorted(
            path
            for path in (REPO_ROOT / "plugins").glob("*/knowledge_base*")
            if path.is_dir()
        ),
        *sorted(
            path
            for path in (REPO_ROOT / "knowledge_bases").iterdir()
            if path.is_dir() and not path.is_symlink()
        ),
    ]
    fixtures: set[Path] = set()
    for root in source_roots:
        for path in root.rglob("*retrieval_test.yaml"):
            if fnmatch.fnmatch(path.name, "*.retrieval_test.yaml") or fnmatch.fnmatch(
                path.name,
                "*_retrieval_test.yaml",
            ):
                fixtures.add(path.resolve())
    return fixtures


def _compare_discovery_inventory(files: list[Path]) -> tuple[bool, str]:
    discovered = {path.resolve() for path in files}
    expected = _expected_source_fixtures()
    missing = sorted(str(path) for path in expected - discovered)
    unexpected = sorted(str(path) for path in discovered - expected)
    detail = (
        "discovery matches every independently enumerated source fixture "
        f"(missing={missing!r}, unexpected={unexpected!r})"
    )
    return discovered == expected, detail


def _check_dialect_glob_width(c: Checker) -> None:
    """Assert the discovery glob's WIDTH as a property of the function.

    This replaces a corpus CENSUS ("at least one underscore-separated fixture exists"),
    which was a population assertion: it asserted something about the shipped roster
    rather than about the code, so it failed wherever the corpus happens to carry only
    one dialect — exactly what a pruned seed bundle looks like. The property actually
    under test is that ``discover_test_files`` matches BOTH dialects and skips the bare
    filename, which holds in the origin checkout and in any bundle alike.

    A synthetic tree is used rather than the live corpus so the assertion cannot be
    satisfied or broken by which fixtures happen to ship.
    """
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        dotted = root / "article.retrieval_test.yaml"
        underscored = root / "article_retrieval_test.yaml"
        bare = root / "retrieval_test.yaml"
        for path in (dotted, underscored, bare):
            path.write_text("cases: []\n", encoding="utf-8")
        found = {path.name for path in discover_test_files(root)}

    c.check(dotted.name in found, "discovery matches the dot-separated dialect")
    c.check(
        underscored.name in found,
        "discovery matches the underscore-separated dialect",
    )
    c.check(
        bare.name not in found,
        "discovery skips a bare retrieval_test.yaml (no article stem)",
    )


def main() -> int:
    c = Checker("retrieval-audit fixture discovery repair")

    # --- Symlink traversal + dot/underscore glob width, against the real corpus ---
    files = discover_test_files(REPO_ROOT / "knowledge_bases")
    inventory_matches, inventory_detail = _compare_discovery_inventory(files)
    c.check(inventory_matches, inventory_detail)
    c.check(
        not any(str(f).endswith(_OUT_OF_SCOPE_SUFFIX) for f in files),
        "the out-of-scope code_vetting batch-smoke file is not matched",
    )
    c.check(
        any("ananta_platform" in str(f) for f in files),
        "reaches ananta_platform fixtures (symlink traversal)",
    )
    c.check(
        any("github_midwife_plugin" in str(f) for f in files),
        "reaches github_midwife_plugin fixtures (symlink traversal)",
    )
    _check_dialect_glob_width(c)

    # --- parse_article_path dialect normalization ---
    symlink_style = parse_article_path(
        "knowledge_bases/ananta_service/coordinator_dispatch_discipline.md"
    )
    c.check(
        symlink_style.knowledge_base == "ananta_service",
        "symlink-relative dialect still parses",
    )

    ananta_real = parse_article_path(
        "ananta/knowledge_bases/ananta_platform/21_scheduling_service/"
        "01_template_flow_record_lifecycle.md"
    )
    c.check(
        ananta_real.knowledge_base == "ananta_platform",
        "ananta/knowledge_bases/<kb>/... real-path dialect normalizes to kb name",
    )
    c.check(
        ananta_real.relative_path
        == "21_scheduling_service/01_template_flow_record_lifecycle.md",
        "ananta real-path dialect preserves the KB-relative path",
    )

    plugin_real = parse_article_path(
        "plugins/audio_processing_plugin/knowledge_base/going_beyond_the_local_ffmpeg_kb.md"
    )
    c.check(
        plugin_real.knowledge_base == "audio_processing_plugin",
        "plugins/<plugin>/knowledge_base/... real-path dialect normalizes to plugin name",
    )
    c.check(
        plugin_real.relative_path == "going_beyond_the_local_ffmpeg_kb.md",
        "plugin real-path dialect preserves the KB-relative path",
    )

    try:
        parse_article_path("workbench/some_doc.md")
    except ValueError:
        c.check(True, "an unrecognized dialect still raises ValueError")
    else:
        c.check(False, "an unrecognized dialect still raises ValueError")

    return 0 if c.report() else 1


if __name__ == "__main__":
    raise SystemExit(main())
