"""Packet-3 smoke — `kb_symlinks.materialize_kb_symlinks`: the genesis step that
derives + idempotently repairs the `knowledge_bases/` discovery symlinks for a
seed-born clone (whose MINT `assemble` shipped none).

The FIRST check is the data-loss regression guard (a real content directory at a
target name must survive untouched) — deliberately first because the primary
MINT/seed path never exercises the safety-wall branch (a pruned clone has no real
content dirs), so a MINT E2E alone would let a data-loss bug ship green. Then the
other three repair branches, the dangling-symlink case, the manifest-name (NOT
dir-name) discriminator (thinking-plugin trio), the ananta KBs, idempotency, and
the pruned-MINT-tree end-to-end.

Offline: pure filesystem ops under a tmp tree; no live Postgres, no MCP.

Run directly: ``SOLET_NAME=<name> .venv/bin/python3
plugins/github_midwife_plugin/tests/kb_symlinks_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import os
import sys
import tempfile
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from github_midwife_plugin.kb_symlinks import KbSymlinkError, materialize_kb_symlinks

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _write_kb_dir(target: Path, rel_dir: str, manifest_name: str) -> Path:
    """Create a KB source dir at `target/<rel_dir>` with a `manifest.yaml` whose
    `name:` is `manifest_name`, plus one content file. Returns the dir."""
    kb_dir = target / rel_dir
    kb_dir.mkdir(parents=True, exist_ok=True)
    (kb_dir / "manifest.yaml").write_text(f"name: {manifest_name}\ndescription: fixture\n")
    (kb_dir / "01_article.md").write_text("# fixture article\n")
    return kb_dir


# ── data-loss safety wall (FIRST) ────────────────────────────────────


def _check_real_dir_with_sentinel_never_touched(target: Path) -> None:
    """SAFETY WALL: a REAL directory (with a sentinel file) at a derived target
    name is NEVER touched -- not converted to a symlink, not emptied."""
    _write_kb_dir(target, "plugins/foo_plugin/knowledge_base", "foo_kb")
    real = target / "knowledge_bases" / "foo_kb"
    real.mkdir(parents=True)
    sentinel = real / "IRREPLACEABLE.txt"
    sentinel.write_text("real content — must survive")

    report = materialize_kb_symlinks(target)

    _check("safety wall: the real dir is still a real directory (not a symlink)",
           real.is_dir() and not real.is_symlink(), f"{real} was altered")
    _check("safety wall: the sentinel file inside the real dir is intact",
           sentinel.is_file() and sentinel.read_text() == "real content — must survive",
           "sentinel lost/altered")
    _check("safety wall: the name is reported as protected, never created/rewritten",
           "foo_kb" in report["protected"]
           and "foo_kb" not in report["created"] and "foo_kb" not in report["rewritten"],
           str(report))


# ── path-traversal name rejection (Dawn R2 — RED-FIRST) ──────────────


def _check_path_traversal_name_rejected(target: Path) -> None:
    """R2: a manifest `name:` that is not a bare path segment must raise
    KbSymlinkError (fail-loud) and materialize NOTHING. RED-FIRST: pre-validation
    code would escape the tree (`../escapee` links at kb_root's parent; `/abs/x`
    links at an absolute path) or crash with a bare OSError (`foo/bar`, missing
    parent) — never a clean KbSymlinkError."""
    for i, shape in enumerate(("../escapee", "/abs/escapee", "foo/bar")):
        root = target / f"case_{i}"
        root.mkdir()
        _write_kb_dir(root, "plugins/evil_plugin/knowledge_base", shape)
        raised = False
        try:
            materialize_kb_symlinks(root)
        except KbSymlinkError:
            raised = True
        _check(f"path-traversal name {shape!r} raises KbSymlinkError (fail-loud, not OSError)",
               raised, f"{shape!r} did not raise KbSymlinkError")
        _check(f"path-traversal name {shape!r} materialized no symlink under knowledge_bases/",
               not any(p.is_symlink() for p in (root / "knowledge_bases").iterdir()),
               f"{shape!r} left a symlink under knowledge_bases/")
        _check(f"path-traversal name {shape!r} created no escaping symlink above kb_root",
               not (root / "escapee").is_symlink() and not (root / "escapee").exists(),
               f"{shape!r} escaped to {root / 'escapee'}")


# ── the three repair branches + dangling ─────────────────────────────


def _check_absent_creates_relative_symlink(target: Path) -> None:
    _write_kb_dir(target, "plugins/bar_plugin/knowledge_base", "bar_kb")
    report = materialize_kb_symlinks(target)
    link = target / "knowledge_bases" / "bar_kb"
    _check("absent -> a symlink is created", link.is_symlink(), f"{link} not a symlink")
    _check("the created symlink target is RELATIVE and exact (no trailing slash)",
           os.readlink(link) == "../plugins/bar_plugin/knowledge_base", os.readlink(link))
    _check("the created symlink resolves to the real KB dir",
           (link / "manifest.yaml").is_file(), "symlink does not resolve")
    _check("the name is reported as created", "bar_kb" in report["created"], str(report))


def _check_correct_symlink_skipped(target: Path) -> None:
    _write_kb_dir(target, "plugins/baz_plugin/knowledge_base", "baz_kb")
    kb_root = target / "knowledge_bases"
    kb_root.mkdir(parents=True, exist_ok=True)
    (kb_root / "baz_kb").symlink_to("../plugins/baz_plugin/knowledge_base")
    report = materialize_kb_symlinks(target)
    _check("a correct pre-existing symlink is SKIPPED (idempotent)",
           "baz_kb" in report["skipped"] and "baz_kb" not in report["rewritten"], str(report))
    _check("the skipped symlink is unchanged",
           os.readlink(kb_root / "baz_kb") == "../plugins/baz_plugin/knowledge_base",
           os.readlink(kb_root / "baz_kb"))


def _check_wrong_target_symlink_rewritten(target: Path) -> None:
    _write_kb_dir(target, "plugins/qux_plugin/knowledge_base", "qux_kb")
    kb_root = target / "knowledge_bases"
    kb_root.mkdir(parents=True, exist_ok=True)
    (kb_root / "qux_kb").symlink_to("../some/wrong/target")
    report = materialize_kb_symlinks(target)
    _check("a wrong-target symlink is REWRITTEN", "qux_kb" in report["rewritten"], str(report))
    _check("the rewritten symlink now points at the correct relative target",
           os.readlink(kb_root / "qux_kb") == "../plugins/qux_plugin/knowledge_base",
           os.readlink(kb_root / "qux_kb"))


def _check_dangling_symlink_rewritten(target: Path) -> None:
    """A DANGLING symlink (target absent) must be classified via is_symlink()
    FIRST (exists() would read it as absent) and rewritten."""
    _write_kb_dir(target, "plugins/dang_plugin/knowledge_base", "dang_kb")
    kb_root = target / "knowledge_bases"
    kb_root.mkdir(parents=True, exist_ok=True)
    (kb_root / "dang_kb").symlink_to("../does/not/exist")
    _check("fixture is genuinely dangling (a symlink whose target is absent)",
           (kb_root / "dang_kb").is_symlink() and not (kb_root / "dang_kb").exists(),
           "fixture not dangling")
    report = materialize_kb_symlinks(target)
    _check("a dangling symlink is REWRITTEN (not misread as absent-and-recreated-wrong)",
           "dang_kb" in report["rewritten"], str(report))
    _check("the rewritten dangling symlink resolves to the real KB dir",
           (kb_root / "dang_kb" / "manifest.yaml").is_file(), "did not resolve after rewrite")


# ── manifest-name discriminator (Dawn rider 1) ───────────────────────


def _check_symlink_name_is_manifest_name_not_dirname(target: Path) -> None:
    """DISCRIMINATOR: the thinking-plugin trio ships three KB dirs whose manifest
    names differ from the dir names. A dir-name-derived implementation goes RED."""
    _write_kb_dir(target, "plugins/default_thinking_plugin/knowledge_base", "thinking_plans")
    _write_kb_dir(target, "plugins/default_thinking_plugin/knowledge_base_joseki", "authored_joseki")
    _write_kb_dir(
        target, "plugins/default_thinking_plugin/knowledge_base_plan_templates", "plan_templates"
    )
    report = materialize_kb_symlinks(target)
    kb_root = target / "knowledge_bases"
    for manifest_name in ("thinking_plans", "authored_joseki", "plan_templates"):
        _check(f"symlink is named by manifest name {manifest_name!r}",
               (kb_root / manifest_name).is_symlink() and manifest_name in report["created"],
               str(report))
    for dir_name in ("default_thinking_plugin", "knowledge_base_joseki", "knowledge_base_plan_templates"):
        _check(f"NO symlink named by the dir/plugin name {dir_name!r} (dir-name impl would create it)",
               not (kb_root / dir_name).exists() and not (kb_root / dir_name).is_symlink(),
               f"{dir_name} wrongly created")
    _check("the knowledge_base_joseki symlink targets the joseki dir (glob picks up knowledge_base*)",
           os.readlink(kb_root / "authored_joseki")
           == "../plugins/default_thinking_plugin/knowledge_base_joseki",
           os.readlink(kb_root / "authored_joseki"))


def _check_ananta_kbs_and_service(target: Path) -> None:
    _write_kb_dir(target, "ananta/knowledge_bases/ananta_platform", "ananta_platform")
    _write_kb_dir(target, "ananta/knowledge_base", "ananta_service")
    materialize_kb_symlinks(target)
    kb_root = target / "knowledge_bases"
    _check("ananta/knowledge_bases/<x> is symlinked with a relative target",
           os.readlink(kb_root / "ananta_platform") == "../ananta/knowledge_bases/ananta_platform",
           os.readlink(kb_root / "ananta_platform"))
    _check("ananta/knowledge_base (ananta_service) is symlinked with a relative target",
           os.readlink(kb_root / "ananta_service") == "../ananta/knowledge_base",
           os.readlink(kb_root / "ananta_service"))


# ── idempotency + pruned-MINT-tree E2E ───────────────────────────────


def _build_mint_tree(target: Path) -> set[str]:
    """A pruned MINT-shaped clone: several plugins' KB dirs + ananta KBs, and a
    knowledge_bases/ with ZERO symlinks (as a seed ships it). Returns the expected
    symlink-name set."""
    _write_kb_dir(target, "plugins/actr_memory_plugin/knowledge_base", "actr_memory_plugin")
    _write_kb_dir(target, "plugins/agent_messaging_plugin/knowledge_base", "agent_messaging_plugin")
    _write_kb_dir(target, "plugins/default_thinking_plugin/knowledge_base", "thinking_plans")
    _write_kb_dir(target, "plugins/default_thinking_plugin/knowledge_base_joseki", "authored_joseki")
    _write_kb_dir(target, "ananta/knowledge_bases/ananta_platform", "ananta_platform")
    _write_kb_dir(target, "ananta/knowledge_base", "ananta_service")
    return {
        "actr_memory_plugin", "agent_messaging_plugin", "thinking_plans",
        "authored_joseki", "ananta_platform", "ananta_service",
    }


def _check_idempotent_second_run_is_noop(target: Path) -> None:
    _build_mint_tree(target)
    materialize_kb_symlinks(target)
    report2 = materialize_kb_symlinks(target)
    _check("second run creates nothing (pure idempotent no-op)",
           report2["created"] == [] and report2["rewritten"] == [], str(report2))
    _check("second run skips every derived symlink",
           len(report2["skipped"]) == 6 and report2["protected"] == [], str(report2))


def _check_mint_tree_e2e(target: Path) -> None:
    """Zero symlinks in -> the full discoverable KB symlink set out (all relative,
    all resolving, none dangling)."""
    expected = _build_mint_tree(target)
    kb_root = target / "knowledge_bases"
    kb_root.mkdir(parents=True, exist_ok=True)
    _check("MINT tree starts with ZERO symlinks in knowledge_bases/",
           not any(p.is_symlink() for p in kb_root.iterdir()), "unexpected pre-existing symlinks")
    report = materialize_kb_symlinks(target)
    created = set(report["created"])
    _check("every expected KB name is created", created == expected,
           f"created={sorted(created)} expected={sorted(expected)}")
    for name in expected:
        link = kb_root / name
        _check(f"{name}: a relative symlink that resolves to a real KB dir",
               link.is_symlink() and not os.path.isabs(os.readlink(link))
               and (link / "manifest.yaml").is_file(),
               f"{name} -> {os.readlink(link)!r}")


def main() -> int:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _check_real_dir_with_sentinel_never_touched(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_path_traversal_name_rejected(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_absent_creates_relative_symlink(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_correct_symlink_skipped(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_wrong_target_symlink_rewritten(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_dangling_symlink_rewritten(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_symlink_name_is_manifest_name_not_dirname(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_ananta_kbs_and_service(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_idempotent_second_run_is_noop(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_mint_tree_e2e(Path(tmp))
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"kb_symlinks_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
