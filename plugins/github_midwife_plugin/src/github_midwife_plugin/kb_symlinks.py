"""Genesis step — materialize the `knowledge_bases/` discovery symlinks (Slice D+).

A full `git clone` of the platform ships the git-tracked `knowledge_bases/`
symlinks, so genesis historically treated them as N/A. But the MINT seed's
`assemble` step BANS symlinks from the published seed (they do not survive
`git archive` + the export-ignore/prune), so a SEED-BORN solet's clone has
NONE -- and `default_knowledge_plugin`'s auto-installer, which registers every
`knowledge_bases/<dir>` that carries a `manifest.yaml`, finds an empty tree: a
dead knowledge base. This step mechanically DERIVES and idempotently REPAIRS
those symlinks in the newborn's clone, pre-first-boot.

Derivation (derive-don't-declare -- there is NO manifest of the symlink set):
for every knowledge-base directory in the clone -- each
`plugins/<x>/knowledge_base*/` (the `*` picks up e.g. `knowledge_base_joseki`),
each `ananta/knowledge_bases/<x>/`, and `ananta/knowledge_base/` -- that carries a
`manifest.yaml`, create `knowledge_bases/<manifest-name>` as a RELATIVE symlink to
it. The symlink NAME is the KB's OWN manifest `name:`, NOT the plugin/dir name:
the thinking plugin ships THREE KBs whose manifest names differ from their dirs
(`knowledge_base` -> `thinking_plans`, `knowledge_base_joseki` -> `authored_joseki`,
`knowledge_base_plan_templates` -> `plan_templates`). The plugin-name == symlink
matches seen elsewhere (`agent_messaging_plugin`, ...) are coincidences of
manifest-name == plugin-name.

RELATIVE targets are mandatory: `knowledge_bases/` resolves in both the local
layout and the container/cloud bind-mount, and an absolute path breaks in the
container.

Idempotent-repair, four branches, classified by `is_symlink()` FIRST -- both
`exists()` and `is_dir()` FOLLOW the link, so testing them first would
misclassify a symlink-to-dir (reads as a dir) and a DANGLING symlink (reads as
absent):
  * a symlink whose stored target already equals the expected relative target
    -> SKIP (idempotent second run is a pure no-op);
  * a symlink with a WRONG or DANGLING target -> REWRITE (`unlink` the symlink,
    then relink -- never removes the target it pointed at);
  * a REAL directory or file (not a symlink) -> NEVER TOUCH. This is the SAFETY
    WALL: `knowledge_bases/` also holds REAL content directories (`.archive`,
    `compositions`, `neuro_ambient`, `planning_architecture`, ...) that must
    never be clobbered. A rewrite here would destroy real content, so the branch
    only ever `unlink`s a *symlink*, never a directory.
  * absent -> CREATE.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import yaml

_KB_DIR_GLOB = "knowledge_base*"
_MANIFEST = "manifest.yaml"


class KbSymlinkError(RuntimeError):
    """Raised when the `knowledge_bases/` symlink materialization cannot proceed safely."""


def _kb_dirs_under(parent: Path, glob: str) -> Iterator[Path]:
    """Directories directly under `parent` matching `glob` that carry a
    `manifest.yaml`. Silently yields nothing if `parent` is not a directory."""
    if not parent.is_dir():
        return
    for kb_dir in sorted(parent.glob(glob)):
        if kb_dir.is_dir() and (kb_dir / _MANIFEST).is_file():
            yield kb_dir


def _kb_source_dirs(target: Path) -> Iterator[Path]:
    """Yield every knowledge-base source directory in the clone that carries a
    `manifest.yaml`: `plugins/<x>/knowledge_base*/`, `ananta/knowledge_bases/<x>/`,
    and `ananta/knowledge_base/`."""
    plugins_dir = target / "plugins"
    if plugins_dir.is_dir():
        for plugin_dir in sorted(plugins_dir.iterdir()):
            yield from _kb_dirs_under(plugin_dir, _KB_DIR_GLOB)
    yield from _kb_dirs_under(target / "ananta" / "knowledge_bases", "*")
    ananta_kb = target / "ananta" / "knowledge_base"
    if ananta_kb.is_dir() and (ananta_kb / _MANIFEST).is_file():
        yield ananta_kb


def _manifest_name(kb_dir: Path) -> str:
    """The KB's canonical name from its OWN `manifest.yaml` `name:` -- the derivation
    key (see module docstring; NOT the plugin/dir name)."""
    try:
        data = yaml.safe_load((kb_dir / _MANIFEST).read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise KbSymlinkError(f"could not read {kb_dir / _MANIFEST}: {exc}") from exc
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise KbSymlinkError(f"{kb_dir / _MANIFEST} has no non-empty `name:` field")
    stripped = name.strip()
    # The name becomes a symlink basename directly under `knowledge_bases/`. Any
    # value that is not a bare path segment (`../escapee`, `/abs/x`, `foo/bar`)
    # would place or escape the link outside that directory -- reject fail-loud
    # here rather than crash on the resulting OSError at `symlink_to` time.
    if stripped != Path(stripped).name:
        raise KbSymlinkError(
            f"{kb_dir / _MANIFEST} `name:` {stripped!r} is not a bare path segment"
        )
    return stripped


def materialize_kb_symlinks(target: Path) -> dict[str, list[str]]:
    """Derive and idempotently repair the `knowledge_bases/` discovery symlinks in
    the clone rooted at `target`.

    Returns a report mapping `created`/`skipped`/`rewritten`/`protected` to the
    sorted symlink names in each class. NEVER touches a real directory or file at a
    target name (the safety wall).
    """
    kb_root = target / "knowledge_bases"
    kb_root.mkdir(parents=True, exist_ok=True)
    report: dict[str, list[str]] = {"created": [], "skipped": [], "rewritten": [], "protected": []}
    for kb_dir in _kb_source_dirs(target):
        name = _manifest_name(kb_dir)
        link = kb_root / name
        expected = os.path.relpath(kb_dir, kb_root)  # relative, no trailing slash
        # is_symlink() MUST be tested before exists()/is_dir() -- those follow the
        # link and would misclassify a symlink-to-dir or a dangling symlink.
        if link.is_symlink():
            if os.readlink(link) == expected:
                report["skipped"].append(name)
            else:
                link.unlink()  # removes the SYMLINK only, never its target
                link.symlink_to(expected)
                report["rewritten"].append(name)
        elif link.exists():
            # A real directory or file lives here -- the safety wall. Never touch.
            report["protected"].append(name)
        else:
            link.symlink_to(expected)
            report["created"].append(name)
    for names in report.values():
        names.sort()
    return report


__all__ = ["KbSymlinkError", "materialize_kb_symlinks"]
