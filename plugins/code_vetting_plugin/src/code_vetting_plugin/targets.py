"""Target inventory for a vetting run.

The scan target is the *tracked* working tree, enumerated with read-only
``git ls-files`` (a permitted git verb) so gitignored noise — ``.venv``, blob
stores, the scratchpad — never enters the scan. Scanners draw the view they
need from here rather than each re-walking the filesystem.

Scope model (RB-SCOPE): the platform quality surface is ``ananta/src/``,
``plugins/<X>/src/``, ``plugins/<X>/tests/``, and ``quality_gates/``. Operator
tooling (``workbench/``, ``deployment/``, ``plugins/<X>/{research,tools,
migrations,parity_tests}/``) is out of gate scope. Leak/safety scanners
(secrets, identity, hidden-unicode) still sweep the whole tracked tree — those
are not gate-style nits.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .toolrun import run

_MODEL_FACING_SUFFIXES = frozenset({".md", ".json", ".yaml", ".yml", ".toml", ".py", ".txt", ".cfg"})

# Stack-detection surfaces (R7-1) — module-level, no magic strings. ``.d.ts`` resolves to
# a ``.ts`` suffix via ``Path.suffix`` so declaration files count as TypeScript.
_TS_SUFFIXES: frozenset[str] = frozenset({".ts", ".tsx"})
_JS_SUFFIXES: frozenset[str] = frozenset({".js", ".jsx", ".mjs", ".cjs"})
_NPM_LOCKFILE_NAMES: frozenset[str] = frozenset({"package-lock.json", "yarn.lock", "pnpm-lock.yaml"})

# FT-1 (foreign-target ruling A.3) — the CURATED STRUCTURAL exclude set for the
# non-git walk fallback. Locally-materialized junk (VCS dirs, JS/Python deps +
# caches, build/dist outputs) only; this is a bounded, KNOWN family. Deliberately
# NOT a .gitignore emulation: parsing gitignore semantics (negations, nesting,
# precedence) half-faithfully produces silently-wrong scan surfaces, and a fetched
# tarball carries only tracked content anyway. If a target's junk isn't covered,
# the fix is EXTENDING this set (a visible, reviewed change), never per-target
# gitignore interpretation.
WALK_EXCLUDE_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn",                                          # VCS
    "node_modules",                                                 # JS deps
    ".venv", "venv",                                                # Python venvs
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".cache",  # caches
    "dist", "build", ".next", ".expo", ".turbo", ".dart_tool",      # build outputs
    ".idea", ".vscode",                                             # IDE
})

# Operator-tooling path fragments excluded from the platform quality surface.
_OPERATOR_TOOLING = re.compile(
    r"(^|/)(workbench|deployment)/"
    r"|(^|/)plugins/[^/]+/(research|tools|migrations|parity_tests)/"
)
# Platform quality surface membership (RB-SCOPE).
_QUALITY_SURFACE = re.compile(
    r"^ananta/src/"
    r"|^plugins/[^/]+/src/"
    r"|^plugins/[^/]+/tests/"
    r"|^quality_gates/"
)


def _is_quality_surface(path: str) -> bool:
    return bool(_QUALITY_SURFACE.match(path)) and not _OPERATOR_TOOLING.search(path)


# Files that declare or pin python dependencies. Used to tell a foreign target with
# real python deps from one with none (FT-1.1 defect 1): pip-audit's environment-audit
# mode is self-vet-only and must never attribute THIS engine's venv to a foreign target.
_PY_DEP_MANIFEST_NAMES: frozenset[str] = frozenset({
    "pyproject.toml", "setup.py", "setup.cfg", "poetry.lock", "pipfile", "pipfile.lock", "uv.lock",
})


def _is_py_dep_manifest(name_lower: str) -> bool:
    return name_lower in _PY_DEP_MANIFEST_NAMES or (
        name_lower.startswith("requirements") and name_lower.endswith(".txt")
    )


@dataclass(slots=True)
class TargetTree:
    """Enumerated views over a scan target tree.

    ``enumeration`` records HOW the file view was built (``git`` = ``git ls-files``,
    honoring the target's own gitignore; ``walk`` = the read-only structural walk
    fallback for a non-git target). ``foreign`` is the DERIVED target-class (FT-1
    ruling B.1): False for a self-vet of the platform's own worktree, True for any other
    target — it is NOT a caller-settable axis, the plugin derives it by comparing
    the resolved target to the own worktree.
    """

    root: Path
    tracked: tuple[str, ...]
    enumeration: str = "git"
    foreign: bool = False

    @classmethod
    def from_git(cls, root: Path, *, foreign: bool = False) -> TargetTree:
        """Build from ``git ls-files`` at ``root`` (read-only). ``foreign`` marks a
        target OUTSIDE the own worktree (a foreign repo that happens to carry a
        ``.git``); the self-vet default leaves it False."""
        outcome = run(["git", "-C", str(root), "ls-files"], timeout_s=120)
        if outcome.returncode != 0:
            raise RuntimeError(f"git ls-files failed at {root}: {outcome.stderr.strip()}")
        tracked = tuple(sorted(line for line in outcome.stdout.splitlines() if line))
        return cls(root=root, tracked=tracked, enumeration="git", foreign=foreign)

    @classmethod
    def from_walk(cls, root: Path) -> TargetTree:
        """Enumerate a NON-git target by a deterministic, READ-ONLY filesystem walk.

        The fallback when the target has no ``.git`` (a fetched tarball / a plain
        source tree). Prunes the CURATED STRUCTURAL exclude set (``WALK_EXCLUDE_DIRS``)
        of locally-materialized junk; does NOT emulate ``.gitignore`` (ruling A.3).
        Never writes into the target and never follows a symlink out of it
        (``os.walk`` does not descend symlinked dirs; symlinked files are skipped)
        — the read-only invariant. A walk-mode target is always ``foreign`` (a
        self-vet always has ``.git``).
        """
        resolved = root.resolve()
        files: list[str] = []
        for dirpath, dirnames, filenames in os.walk(resolved):
            dirnames[:] = sorted(name for name in dirnames if name not in WALK_EXCLUDE_DIRS)
            base = Path(dirpath)
            for name in filenames:
                candidate = base / name
                if candidate.is_symlink():
                    continue
                files.append(candidate.relative_to(resolved).as_posix())
        return cls(root=resolved, tracked=tuple(sorted(files)), enumeration="walk", foreign=True)

    def all_files(self) -> tuple[str, ...]:
        """Every tracked path (repo-relative)."""
        return self.tracked

    def model_facing(self) -> tuple[str, ...]:
        """Text files an LLM will read — the hidden-unicode scan surface."""
        return tuple(p for p in self.tracked if Path(p).suffix.lower() in _MODEL_FACING_SUFFIXES)

    def python_files(self) -> tuple[str, ...]:
        return tuple(p for p in self.tracked if p.endswith(".py"))

    def quality_surface(self) -> tuple[str, ...]:
        """Platform-quality-surface files only (RB-SCOPE) — gate-wrapper target."""
        return tuple(p for p in self.tracked if _is_quality_surface(p))

    def quality_surface_python(self) -> tuple[str, ...]:
        return tuple(p for p in self.quality_surface() if p.endswith(".py"))

    def pyproject_files(self) -> tuple[str, ...]:
        return tuple(p for p in self.tracked if Path(p).name == "pyproject.toml")

    def python_dependency_manifests(self) -> tuple[str, ...]:
        """Every file that declares/pins python dependencies (pyproject/setup, lockfiles,
        ``requirements*.txt``). Distinguishes a foreign target with real python deps from
        one with none — FT-1.1: pip-audit's environment-audit mode is self-vet-only."""
        return tuple(p for p in self.tracked if _is_py_dep_manifest(Path(p).name.lower()))

    def typescript_files(self) -> tuple[str, ...]:
        """Enumerated TypeScript sources (``.ts``/``.tsx``; ``.d.ts`` counts) — R7-1 stack surface."""
        return tuple(p for p in self.tracked if Path(p).suffix in _TS_SUFFIXES)

    def javascript_files(self) -> tuple[str, ...]:
        """Enumerated JavaScript sources (``.js``/``.jsx``/``.mjs``/``.cjs``) — R7-1 stack surface."""
        return tuple(p for p in self.tracked if Path(p).suffix in _JS_SUFFIXES)

    def npm_lockfiles(self) -> tuple[str, ...]:
        """Enumerated npm lockfiles (``package-lock.json``/``yarn.lock``/``pnpm-lock.yaml``) — the osv SCA surface (R7-3)."""
        return tuple(p for p in self.tracked if Path(p).name in _NPM_LOCKFILE_NAMES)

    def process_json(self) -> tuple[str, ...]:
        """Plugin KB process-overlay JSON files."""
        return tuple(
            p
            for p in self.tracked
            if "/knowledge_base/processes/" in p and p.endswith(".json")
        )

    def abspath(self, rel: str) -> Path:
        return self.root / rel
