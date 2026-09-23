"""foreign_target_smoke.py — FT-1 foreign-target vetting (Architect ruling A + C).

Pins the target de-anchor + scanner-applicability invariants that make
``vet_codebase(target_path=…)`` safe on an EXTERNAL repo:

  * ``_resolve_target``: self-vet default (no target_path → own worktree, foreign=False,
    BYTE-COMPATIBLE), and the fail-loud refusals — relative path, non-existent,
    non-directory, and a path resolving INSIDE/around our own worktree (red-first).
  * ``TargetTree.from_walk``: the READ-ONLY structural walk fallback prunes the curated
    junk set (node_modules/.venv/dist/… — NOT gitignore emulation), skips symlinks,
    never writes into the target, and marks enumeration='walk' + foreign=True.
  * ``_build_tree``: git-preferred when the target carries .git (ref = HEAD), walk-fallback
    otherwise (ref = '' — no invented provenance).
  * ``run_all`` applicability (ruling C): on a FOREIGN tree the self_only scanners
    (code_quality/sql_access/orphan_kb/prior_pass + rulebook_sync) do NOT execute but stay in the ledger
    as CoverageRecord{ran=False, gap_reason='not_applicable: …'} — a DISTINCT reason class
    from a tool-missing gap — and scanners_total stays the full roster (roster never forks).

Run directly: ``.venv/bin/python3 plugins/code_vetting_plugin/tests/foreign_target_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_vetting_plugin.plugin import CodeVettingPlugin, TargetValidationError
from code_vetting_plugin.runner import SCANNERS, Applicability, run_all
from code_vetting_plugin.targets import TargetTree

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _plugin_with_worktree(worktree: Path) -> CodeVettingPlugin:
    plugin = CodeVettingPlugin()
    plugin._worktree_root = worktree  # noqa: SLF001 — inject the anchored root for the hermetic check
    return plugin


def _expect_refusal(label: str, plugin: CodeVettingPlugin, target_path: str) -> None:
    _CHECKS_RUN.append(label)
    try:
        plugin._resolve_target(target_path)  # noqa: SLF001
    except TargetValidationError:
        return
    raise SmokeFailureError(f"{label}: expected TargetValidationError, none raised")


def _check_resolve_target(worktree: Path, external: Path) -> None:
    plugin = _plugin_with_worktree(worktree)

    root, foreign = plugin._resolve_target(None)  # noqa: SLF001
    _check("self-vet default: None -> own worktree, foreign=False", root == worktree and foreign is False, f"{root} {foreign}")

    root, foreign = plugin._resolve_target(str(external))  # noqa: SLF001
    _check("external absolute dir -> foreign=True", root == external.resolve() and foreign is True, f"{root} {foreign}")

    _expect_refusal("refuse: relative path", plugin, "relative/path")
    _expect_refusal("refuse: non-existent path", plugin, str(external / "nope-does-not-exist"))
    _expect_refusal("refuse: a file, not a dir", plugin, str(external / "afile.txt"))
    _expect_refusal("refuse: inside our own worktree", plugin, str(worktree / "subdir"))
    _expect_refusal("refuse: our own worktree itself", plugin, str(worktree))
    _expect_refusal("refuse: an ancestor that contains our worktree", plugin, str(worktree.parent))


def _check_from_walk(root: Path) -> None:
    # Build a target tree with real source + curated-junk dirs + a symlink.
    root.mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "app.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (root / "package.json").write_text("{}\n", encoding="utf-8")
    for junk in ("node_modules", ".venv", "dist", "__pycache__", ".expo"):
        (root / junk).mkdir()
        (root / junk / "junk.js").write_text("junk\n", encoding="utf-8")
    (root / "node_modules" / "dep").mkdir()
    (root / "node_modules" / "dep" / "index.js").write_text("nested junk\n", encoding="utf-8")
    os.symlink(root / "src" / "app.ts", root / "link.ts")

    tree = TargetTree.from_walk(root)
    _check("from_walk marks enumeration='walk'", tree.enumeration == "walk", tree.enumeration)
    _check("from_walk marks foreign=True", tree.foreign is True, str(tree.foreign))
    files = set(tree.all_files())
    _check("from_walk keeps real source", {"src/app.ts", "package.json"} <= files, str(sorted(files)))
    _check("from_walk prunes ALL curated junk dirs", not any("/" in f and f.split("/")[0] in {"node_modules", ".venv", "dist", "__pycache__", ".expo"} for f in files), str(sorted(files)))
    _check("from_walk skips symlinks (read-only safety)", "link.ts" not in files, str(sorted(files)))


def _run_git(repo: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env)


def _check_build_tree(scratch: Path) -> None:
    # Walk-mode: a non-git target.
    walk_target = scratch / "walk_target"
    walk_target.mkdir()
    (walk_target / "a.py").write_text("A = 1\n", encoding="utf-8")
    tree, ref = CodeVettingPlugin._build_tree(walk_target, foreign=True)  # noqa: SLF001
    _check("build_tree (no .git) -> walk enumeration", tree.enumeration == "walk", tree.enumeration)
    _check("build_tree walk-mode ref is empty (no invented provenance)", ref == "", repr(ref))

    # Git-mode: a target carrying its own .git.
    git_target = scratch / "git_target"
    git_target.mkdir()
    (git_target / "b.py").write_text("B = 2\n", encoding="utf-8")
    _run_git(git_target, "-c", "init.defaultBranch=main", "init", "-q")
    _run_git(git_target, "add", "-A")
    _run_git(git_target, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init")
    tree, ref = CodeVettingPlugin._build_tree(git_target, foreign=True)  # noqa: SLF001
    _check("build_tree (.git present) -> git enumeration", tree.enumeration == "git", tree.enumeration)
    _check("build_tree git-mode carries the target's own HEAD ref (hex sha)", bool(ref) and all(c in "0123456789abcdef" for c in ref), repr(ref))
    _check("build_tree threads foreign=True", tree.foreign is True, str(tree.foreign))


def _check_applicability(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "app.ts").write_text("export const y = 2;\n", encoding="utf-8")
    tree = TargetTree.from_walk(root)
    _, coverage, _structural = run_all(tree, "vr-ft1-foreign")
    by_scanner = {record.scanner: record for record in coverage}
    _check("run_all keeps the FULL roster on a foreign target (roster never forks)", len(coverage) == len(SCANNERS), f"{len(coverage)} != {len(SCANNERS)}")
    self_only = [spec.name for spec in SCANNERS if spec.applicability is Applicability.SELF_ONLY]
    _check("the roster declares the 5 self_only scanners (4 self-canon gates + rulebook_sync integrity)", len(self_only) == 5, str(self_only))
    for name in self_only:
        record = by_scanner.get(name)
        _check(f"self_only scanner {name!r} did NOT execute on the foreign target", record is not None and record.ran is False, str(record))
        _check(f"self_only scanner {name!r} recorded a distinct not_applicable reason", record is not None and (record.gap_reason or "").startswith("not_applicable:"), str(record))


def main() -> int:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            worktree = base / "example_worktree"
            (worktree / "subdir").mkdir(parents=True)
            external = base / "external_target"
            external.mkdir()
            (external / "afile.txt").write_text("x\n", encoding="utf-8")
            _check_resolve_target(worktree, external)
        with tempfile.TemporaryDirectory() as tmp:
            _check_from_walk(Path(tmp) / "walk")
        with tempfile.TemporaryDirectory() as tmp:
            _check_build_tree(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_applicability(Path(tmp) / "app")
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"foreign_target_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
