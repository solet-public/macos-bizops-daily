#!/usr/bin/env python3
"""W-INT Cycle 2 driver-import gate (W-WINT2-EARLY) — warn mode.

Detects Postgres-driver imports outside the two state_service plugins.
Per master plan §1.7, only `postgres_state_management_plugin` (local) and
`rds_postgres_state_management_plugin` (cloud) may import psycopg /
psycopg_pool / asyncpg / pg8000 / sqlalchemy in the runtime code base;
every other plugin uses the state_service surface verbs.

Cycle 2 early-ratchet ships in WARN mode: invoke with `--warn-only` and
the gate always exits 0 — findings still print so the gate stays honest,
but no commit is blocked. Initial allowlist captures the current bypass
sites from the postgres-drift-inventory design record §1 (dev-checkout
workbench — not part of the shipped tree) + the
three retained sites per master plan §1.7 (midwife `postgres_init.py`
role-DDL caller; state-plugin tests; the sqlalchemy code-generation site
at `ananta/src/ananta/platform/code_generator.py`). Each subsequent
cleanup workstream removes its allowlist entry as work lands. Mode flips
warn→fail at W-WINT2-FINAL (Tier 7, post-W-DB-LOCKDOWN).

Scope (mirrors the KB "Peer Pre-Completion Gate Procedure"):
  - ananta/src/
  - plugins/*/src/
  - plugins/*/tests/

Operator-tooling (research / tools / migrations / parity_tests under any
plugin) is excluded — the same platform-quality-surface rule the radon /
god-class / W-INT-Cycle-1 gates use.

Detection covers BOTH module-level and nested (inside-function /
inside-class / inside-method) imports — drift can hide in lazy-import
patterns. Both `import X` and `from X import Y` shapes are detected for
each driver root.

Exit codes (mirror radon_*_check.py + W-INT Cycle 1):
  0  — clean, all findings allowlisted, OR `--warn-only` mode active
  1  — non-allowlisted findings present AND `--warn-only` NOT passed
  2  — harness error
  64 — usage error (argparse)

Allowlist format mirrors the Cycle 1 register:

  <check_id>::<scope_qualifier>::<specifier>
    check_id        — "D1.1" (driver-import outside state plugins)
    scope_qualifier — repo-relative POSIX path to the offending file
    specifier       — "<lineno>::<module-root>" OR "*" wildcard

Allowlisted findings are STILL printed (prefixed `[allowlisted]`) so the
gate stays honest; they do NOT contribute to the exit-1 verdict. Per
the KB "Gate Allowlist Conventions", adding entries without operator
approval defeats the gate's purpose. Removing an entry is the unit of
remediation progress.

Reference: the state-service consolidation master plan (dev-checkout
workbench — not part of the shipped tree) §1.7, §3.6, §4 W-WINT2-EARLY.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from quality_gates.allowlist_schema import load_allowlist as _load_tagged_allowlist  # noqa: E402

CHECK_ID = "D1.1"

# Driver-root module names — top dotted segment only. The gate matches
# both bare `import psycopg` AND `from psycopg.rows import dict_row` to
# the same root `psycopg`.
_DRIVER_ROOTS = frozenset({
    "psycopg",
    "psycopg_pool",
    "asyncpg",
    "pg8000",
    "sqlalchemy",
})

_SCAN_ROOTS = (
    REPO_ROOT / "ananta" / "src",
    REPO_ROOT / "plugins",
)

# `plugins/<X>/{research,tools,migrations,parity_tests}/...` segments
# are operator-tooling per the KB "Peer Pre-Completion Gate Procedure" — skipped.
_OPERATOR_TOOLING_PLUGIN_SEGMENTS = frozenset({
    "research", "tools", "migrations", "parity_tests",
})

# Bundled venv segments (.venv, .venv_cosyvoice, ...) ship vendored
# third-party deps whose own driver imports are not platform code.
_BUNDLED_VENV_PREFIX = ".venv"

# Pruned cache directories — same as Cycle 1.
_PRUNE_DIRS = frozenset({"__pycache__", ".mypy_cache", ".pytest_cache"})

# Inside `plugins/<X>/`, only `src/` and `tests/` are in the platform
# quality surface. Anything else (`knowledge_base/`, `policies/`, etc.)
# is content or per-plugin assets — not code under gate scope.
_PLUGIN_SCOPE_SEGMENTS = frozenset({"src", "tests"})


def _rel(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------------------
# Data classes (shape mirrors quality_gates/whole_tree_integration_gate.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    check_id: str
    scope_qualifier: str
    specifier: str
    message: str
    file_path: str = ""
    lineno: int = 0


@dataclass(frozen=True)
class AllowlistEntry:
    check_id: str
    scope_qualifier: str
    specifier: str


@dataclass
class Allowlist:
    entries: frozenset[AllowlistEntry] = field(default_factory=frozenset)

    def covers(self, finding: Finding) -> bool:
        """True iff some entry matches; `*` wildcards the specifier."""
        for entry in self.entries:
            if entry.check_id != finding.check_id:
                continue
            if entry.scope_qualifier != finding.scope_qualifier:
                continue
            if entry.specifier in ("*", finding.specifier):
                return True
        return False


def load_allowlist(path: Path) -> Allowlist:
    """Load the mandatory owner/reason/expires tracked-debt register."""

    if not path.exists():
        return Allowlist()
    entries: set[AllowlistEntry] = set()
    for key in _load_tagged_allowlist(path):
        parts = key.split("::", 2)
        if len(parts) < 3:
            print(
                f"WARN: malformed allowlist line (need <check>::<scope>::<spec>): {key!r}",
                file=sys.stderr,
            )
            continue
        entries.add(AllowlistEntry(parts[0].strip(), parts[1].strip(), parts[2].strip()))
    return Allowlist(frozenset(entries))


# ---------------------------------------------------------------------------
# Path filtering
# ---------------------------------------------------------------------------


def _is_in_scope(path: Path) -> bool:
    """True iff `path` lives under the gate's scope and not operator-tooling."""
    parts = path.parts
    if any(p in _PRUNE_DIRS for p in parts):
        return False
    if any(p.startswith(_BUNDLED_VENV_PREFIX) for p in parts):
        return False
    if "plugins" in parts:
        plugins_idx = parts.index("plugins")
        if plugins_idx + 2 >= len(parts):
            return False
        scope_segment = parts[plugins_idx + 2]
        if scope_segment not in _PLUGIN_SCOPE_SEGMENTS:
            return False
        # Belt-and-suspenders: catch nested operator-tooling under src/
        # or tests/. Currently none, but cheap to defend.
        remaining = parts[plugins_idx + 3:]
        if any(seg in _OPERATOR_TOOLING_PLUGIN_SEGMENTS for seg in remaining):
            return False
    return True


def _walk_python_files() -> Iterator[Path]:
    seen: set[Path] = set()
    for root in _SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if path in seen:
                continue
            seen.add(path)
            if not _is_in_scope(path):
                continue
            yield path


# ---------------------------------------------------------------------------
# AST scanning
# ---------------------------------------------------------------------------


def _module_root(name: str) -> str:
    """Return the top dotted segment of an import name."""
    return name.split(".", 1)[0]


def _scan_module_for_drivers(module: ast.Module) -> list[tuple[int, str]]:
    """Return (lineno, module_root) for every banned import in `module`.

    `ast.walk` traverses unconditionally, so `import psycopg` inside a
    method body is caught the same as one at module scope. Drift can hide
    in lazy-import patterns — this gate refuses to give it a free pass.
    """
    hits: list[tuple[int, str]] = []
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = _module_root(alias.name)
                if root in _DRIVER_ROOTS:
                    hits.append((node.lineno, root))
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` -> module=None; skipped by the membership check.
            module_name = node.module or ""
            root = _module_root(module_name)
            if root in _DRIVER_ROOTS:
                hits.append((node.lineno, root))
    return hits


def _parse_safely(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        print(f"WARN: cannot parse {path}: {exc}", file=sys.stderr)
        return None


def _findings_from_module(module: ast.Module, path: Path) -> list[Finding]:
    rel = _rel(path)
    findings: list[Finding] = []
    for lineno, root in _scan_module_for_drivers(module):
        findings.append(Finding(
            check_id=CHECK_ID,
            scope_qualifier=rel,
            specifier=f"{lineno}::{root}",
            message=(
                f"{rel}:{lineno} imports '{root}' — only the two "
                "state_service plugins (postgres_state_management_plugin, "
                "rds_postgres_state_management_plugin) may import Postgres "
                "drivers in the runtime code base. See master plan §1.7."
            ),
            file_path=rel,
            lineno=lineno,
        ))
    return findings


def collect_findings() -> list[Finding]:
    out: list[Finding] = []
    for path in _walk_python_files():
        module = _parse_safely(path)
        if module is None:
            continue
        out.extend(_findings_from_module(module, path))
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _emit_human(findings: list[Finding], allowlist: Allowlist) -> tuple[int, int]:
    blocking = 0
    allowlisted = 0
    for finding in findings:
        is_allow = allowlist.covers(finding)
        marker = " [allowlisted]" if is_allow else ""
        print(f"{finding.check_id}::{finding.scope_qualifier}::{finding.specifier}{marker}")
        print(f"   {finding.message}")
        if is_allow:
            allowlisted += 1
        else:
            blocking += 1
    return blocking, allowlisted


def _emit_json(findings: list[Finding], allowlist: Allowlist) -> tuple[int, int]:
    blocking = 0
    allowlisted = 0
    payload_findings: list[dict[str, object]] = []
    for finding in findings:
        is_allow = allowlist.covers(finding)
        if is_allow:
            allowlisted += 1
        else:
            blocking += 1
        payload_findings.append({
            "check_id": finding.check_id,
            "scope_qualifier": finding.scope_qualifier,
            "specifier": finding.specifier,
            "message": finding.message,
            "file_path": finding.file_path,
            "lineno": finding.lineno,
            "allowlisted": is_allow,
        })
    print(json.dumps({
        "blocking": blocking,
        "allowlisted": allowlisted,
        "findings": payload_findings,
    }, indent=2))
    return blocking, allowlisted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allowlist", type=Path, default=None,
                        help="Path to tracked-debt allowlist file.")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON instead of human text.")
    parser.add_argument("--warn-only", action="store_true",
                        help=("Always exit 0; print findings but do NOT "
                              "contribute to the blocking verdict. This is "
                              "the W-WINT2-EARLY (Tier 0) mode. W-WINT2-FINAL "
                              "(Tier 7) drops this flag to flip to fail-mode."))
    return parser


def main(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    allowlist = load_allowlist(args.allowlist) if args.allowlist else Allowlist()

    try:
        findings = collect_findings()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: harness failure: {exc}", file=sys.stderr)
        return 2

    findings.sort(key=lambda f: (f.check_id, f.scope_qualifier, f.specifier))

    if args.json:
        blocking, allowlisted = _emit_json(findings, allowlist)
    else:
        blocking, allowlisted = _emit_human(findings, allowlist)

    if not args.json:
        if blocking == 0:
            if not findings:
                print("OK: 0 findings; W-WINT2 driver-import gate clean.")
            else:
                print(
                    f"OK: {len(findings)} finding(s) — all allowlisted; "
                    "W-WINT2 driver-import gate clean."
                )
        else:
            mode = "warn" if args.warn_only else "fail"
            print(
                f"\n{len(findings)} W-WINT2 driver-import finding(s) "
                f"({allowlisted} allowlisted; {blocking} non-allowlisted; "
                f"mode={mode}).",
                file=sys.stderr,
            )
    if args.warn_only:
        return 0
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
