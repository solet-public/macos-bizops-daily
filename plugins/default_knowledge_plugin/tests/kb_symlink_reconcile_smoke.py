#!/usr/bin/env python3
"""The KB-symlink reconcile's ADDITION side (no services, no DB).

THE BUG. ``auto_install_knowledge_bases`` ran its two directions off two
different sources of truth: removal from the LIVE MANIFEST, addition from
``sorted(kb_root.iterdir())`` — the genesis symlink set, written once at birth
and never revisited. A plugin added post-birth therefore had KB content on disk,
no symlink, and no index, and was not a candidate the loop rejected but not a
candidate at all. Measured on a real instance: 197 chunks invisible across 9
connectors. The severity is the point — retrieval does not degrade gracefully
when a corpus is missing, it answers CONFIDENTLY from whatever IS indexed
("how do I query Salesforce opportunities" returned Marketo docs top-3).

These tests need no services at all, which is the reason the materialisation
step was split out of the service-taking entry point: the safety rule that most
wants a test is the one that used to be hardest to reach.

Run:
    .venv/bin/python3 plugins/default_knowledge_plugin/tests/kb_symlink_reconcile_smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"))

from default_knowledge_plugin.kb_lifecycle import (  # noqa: E402
    materialize_missing_plugin_kb_symlinks,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


class _Tree:
    """A throwaway repo-shaped tree: <root>/plugins/... and <root>/knowledge_bases."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.kb_root = self.root / "knowledge_bases"
        self.kb_root.mkdir()
        (self.root / "plugins").mkdir()

    def plugin(self, name: str, *, content: bool = True, kb_dirname: str = "knowledge_base") -> Path:
        kb_dir = self.root / "plugins" / name / kb_dirname
        kb_dir.mkdir(parents=True)
        if content:
            (kb_dir / "01_overview.md").write_text(f"# {name}\n")
        return kb_dir

    def link(self, entry_name: str, target: Path) -> None:
        (self.kb_root / entry_name).symlink_to(
            Path(os.path.relpath(target, self.kb_root)),
        )

    def entries(self) -> set[str]:
        return {p.name for p in self.kb_root.iterdir()}

    def close(self) -> None:
        self._tmp.cleanup()


def test_materialises_a_manifest_enabled_plugin_with_no_entry() -> None:
    """The Part-23 case: enabled, ships content, no symlink -> gets one."""
    t = _Tree()
    try:
        t.plugin("platform_health_plugin")
        created = materialize_missing_plugin_kb_symlinks(t.kb_root, {"platform_health_plugin"})
        _check(created == ["platform_health_plugin"], f"the missing plugin is materialised (got {created})")
        link = t.kb_root / "platform_health_plugin"
        _check(link.is_symlink(), "the entry is a SYMLINK, not a copied directory")
        _check(
            link.resolve() == (t.root / "plugins" / "platform_health_plugin" / "knowledge_base").resolve(),
            "the symlink resolves to the plugin's knowledge_base/",
        )
        _check(
            not os.path.isabs(os.readlink(link)),
            "the link target is RELATIVE (an absolute one would not survive a clone or a release copy)",
        )
    finally:
        t.close()


def test_keys_on_resolved_target_not_entry_name() -> None:
    """CONDITION 1, and the whole safety argument.

    ``default_thinking_plugin`` ships four DISTINCT corpora indexed under
    descriptive names; ``knowledge_bases/default_thinking_plugin`` does not
    exist. A NAME-keyed implementation sees no entry for the plugin and creates
    a fifth link duplicating the corpus ``thinking_plans`` already indexes. A
    TARGET-keyed one sees the directory is already reachable and does nothing.
    """
    t = _Tree()
    try:
        kb_dir = t.plugin("default_thinking_plugin")
        t.link("thinking_plans", kb_dir)  # already indexed under a different NAME
        before = t.entries()
        created = materialize_missing_plugin_kb_symlinks(t.kb_root, {"default_thinking_plugin"})
        _check(
            created == [],
            f"a corpus already reachable under a DIFFERENT entry name is NOT re-linked (got {created})",
        )
        _check(
            "default_thinking_plugin" not in t.entries(),
            "no duplicate name-keyed entry is created (the fifth-link bug)",
        )
        _check(t.entries() == before, "the entry set is untouched")
    finally:
        t.close()


def test_none_manifest_materialises_nothing() -> None:
    """CONDITION 2 — fail closed.

    ``None`` means "skip manifest filtering". Harmless for removal (it does
    less) but it INVERTS for addition: a manifest-unaware caller would link and
    index every plugin on disk, including ones deliberately excluded. For
    addition, "no manifest" must mean "do nothing".
    """
    t = _Tree()
    try:
        t.plugin("marketo_plugin")
        t.plugin("snowflake_plugin")
        created = materialize_missing_plugin_kb_symlinks(t.kb_root, None)
        _check(created == [], f"manifest_plugin_set=None creates NOTHING (got {created})")
        _check(t.entries() == set(), "kb_root is left empty — no link-everything-on-disk")
    finally:
        t.close()


def test_manifest_absent_plugin_is_not_materialised() -> None:
    """A plugin on disk but NOT enabled stays unlinked — the 12 false positives."""
    t = _Tree()
    try:
        t.plugin("enabled_plugin")
        t.plugin("disabled_plugin")
        created = materialize_missing_plugin_kb_symlinks(t.kb_root, {"enabled_plugin"})
        _check(created == ["enabled_plugin"], f"only the manifest-enabled plugin is linked (got {created})")
        _check("disabled_plugin" not in t.entries(), "a manifest-absent plugin is NOT linked")
    finally:
        t.close()


def test_empty_knowledge_base_is_not_materialised() -> None:
    """"Non-empty" means real content: an empty dir has nothing to index, and
    linking it would create tracked repo churn for no retrieval benefit."""
    t = _Tree()
    try:
        t.plugin("hollow_plugin", content=False)
        created = materialize_missing_plugin_kb_symlinks(t.kb_root, {"hollow_plugin"})
        _check(created == [], f"an empty knowledge_base/ is NOT linked (got {created})")
    finally:
        t.close()


def test_plugin_without_a_knowledge_base_dir_is_skipped() -> None:
    """Most plugins ship no KB at all; they must not trip the sweep."""
    t = _Tree()
    try:
        (t.root / "plugins" / "codeless_plugin").mkdir(parents=True)
        created = materialize_missing_plugin_kb_symlinks(t.kb_root, {"codeless_plugin"})
        _check(created == [], f"a plugin with no knowledge_base/ is skipped (got {created})")
    finally:
        t.close()


def test_existing_name_pointing_elsewhere_is_never_clobbered() -> None:
    """If the name is taken by an entry resolving somewhere else, leave it alone.

    The operator's mapping outranks this repair, and silently repointing an
    indexed corpus would be a far worse failure than the gap being repaired.
    """
    t = _Tree()
    try:
        real = t.plugin("alpha_plugin")
        other = t.plugin("beta_plugin")
        t.link("alpha_plugin", other)  # name says alpha, target is beta
        created = materialize_missing_plugin_kb_symlinks(t.kb_root, {"alpha_plugin", "beta_plugin"})
        _check(
            (t.kb_root / "alpha_plugin").resolve() == other.resolve(),
            "the pre-existing entry still resolves to its ORIGINAL target (not clobbered)",
        )
        _check("alpha_plugin" not in created, "the taken name is reported as skipped, not created")
        _check(real.exists(), "the real alpha knowledge_base/ is untouched on disk")
    finally:
        t.close()


def test_idempotent_second_run_creates_nothing() -> None:
    """RATIFIED Condition 3(b) — asserted EXPLICITLY, not left to follow from
    the target-keying test.

    "Follows from" is exactly how this class of property silently stops holding
    under a later refactor, and the failure is invisible until it dirties
    someone else's gate run: the reconcile fires on every boot, and kb_root is
    inside a tracked git checkout, so a non-idempotent sweep means unexplained
    working-tree churn that nobody can attribute.
    """
    t = _Tree()
    try:
        t.plugin("first_plugin")
        t.plugin("second_plugin")
        manifest = {"first_plugin", "second_plugin"}
        first = materialize_missing_plugin_kb_symlinks(t.kb_root, manifest)
        _check(sorted(first) == ["first_plugin", "second_plugin"], f"first run creates both (got {first})")
        snapshot = {p.name: os.readlink(p) for p in t.kb_root.iterdir()}
        second = materialize_missing_plugin_kb_symlinks(t.kb_root, manifest)
        _check(second == [], f"SECOND run creates NOTHING (got {second})")
        _check(
            {p.name: os.readlink(p) for p in t.kb_root.iterdir()} == snapshot,
            "the entry set and every link target are byte-identical after the second run",
        )
        third = materialize_missing_plugin_kb_symlinks(t.kb_root, manifest)
        _check(third == [], "a third run is also a no-op (stable, not merely alternating)")
    finally:
        t.close()


def test_returns_names_so_the_caller_can_report_what_changed() -> None:
    """The return value is the attribution surface a pre-commit harness reports,
    and what makes 'expect exactly this derived set' checkable by NAME rather
    than by a count that goes stale the moment the manifest moves."""
    t = _Tree()
    try:
        for name in ("c_plugin", "a_plugin", "b_plugin"):
            t.plugin(name)
        created = materialize_missing_plugin_kb_symlinks(
            t.kb_root, {"a_plugin", "b_plugin", "c_plugin"},
        )
        _check(created == ["a_plugin", "b_plugin", "c_plugin"], f"names returned, sorted + deterministic (got {created})")
    finally:
        t.close()


def main() -> int:
    print("=== KB-symlink reconcile (addition side) ===")
    test_materialises_a_manifest_enabled_plugin_with_no_entry()
    test_keys_on_resolved_target_not_entry_name()
    test_none_manifest_materialises_nothing()
    test_manifest_absent_plugin_is_not_materialised()
    test_empty_knowledge_base_is_not_materialised()
    test_plugin_without_a_knowledge_base_dir_is_skipped()
    test_existing_name_pointing_elsewhere_is_never_clobbered()
    test_idempotent_second_run_creates_nothing()
    test_returns_names_so_the_caller_can_report_what_changed()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
