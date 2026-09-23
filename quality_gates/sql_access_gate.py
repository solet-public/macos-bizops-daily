#!/usr/bin/env python3
"""SQL-access quality gate (#0 SQL lockdown) — WARN-phase v1.

Enforces the operator's 2026-06-19 rule: NO direct database interaction; ALL
persistence flows through the ``StateManagementInterface`` primitives. Only the
relational owner and the pgvector vector engine may touch SQL directly. The
existing driver-import gate (``wint2_driver_import_check.py``) misses the bulk,
because callers reach SQL through ``state_handle.execute_sql(...)`` /
``.transactional()`` WITHOUT importing a driver. This gate detects the FULL
non-owner SQL-access surface.

Design: the SQL-lockdown-gate design record v2 (dev-checkout workbench
— not part of the shipped tree). Category
registry — adding a rule = one Detector + one registry row; the runner,
allowlist matcher, reporter, and exit-code logic are category-agnostic.

Categories (initial — SQL lockdown):
  S0  driver-import   ``import psycopg/psycopg_pool/asyncpg/pg8000/sqlalchemy``
  S1  raw-SQL call    ``<recv>.execute_sql/.executemany/.fetch_one/.fetch_all``.
                        Two deliberate exclusions: bare ``.execute`` (collides
                        with PluginBase.execute / the prompt pipeline; its SQL
                        string is still caught by S2) and ``.transactional``
                        (typed-txn exemption — 2026-06-22 kb-cohort forks-ruling
                        §4: a transactional() block using only typed ops is
                        sanctioned house-style, so the gate flags the raw SQL
                        INSIDE a block [S2 + the cursor verbs], never the
                        wrapper. A bare ``.execute`` cursor call needs no flag —
                        its SQL string is caught by S2).
  S2  raw-SQL string  a NON-docstring string / f-string literal that either
                        starts with a SQL verb + space AND carries a structural
                        companion (FROM/INTO/SET/VALUES/WHERE/TABLE/INDEX/JOIN/
                        ON CONFLICT/RETURNING/AS/USING), or carries the standalone
                        ON CONFLICT marker. Docstrings are skipped and a bare verb
                        without a companion is ignored — both were prose FPs in
                        the WARN baseline (601→142 after tuning).
  S3  hand-DDL        the DDL subset of S2 (CREATE/ALTER/DROP TABLE, CREATE/DROP
                        INDEX) — bypasses the metadata-driven DDL lifecycle.

Owner exemption is STRUCTURAL (path-prefix), per category (so a future
cross-import / direct-process-call category can carry its own exemption). The
relational owner is the postgres/rds state PLUGINS **plus the state-service
core-impl layer in ananta/src** (the wrapper + strategy layer that FORWARD
execute_sql/transactional; they are permanent, so they are exempt like the
plugins, NOT allowlisted). The single inline ``StateServiceAdapter`` forwarder
that no dir-prefix can cleanly catch carries one line-level ``# false-positive:``
allowlist entry instead.

Scope (mirrors the KB "Peer Pre-Completion Gate Procedure" plus the
radon/god-class/wint2 gates): ``ananta/src/``, ``plugins/*/src/``,
``plugins/*/tests/``,
``quality_gates/``. Operator-tooling (research/tools/migrations/parity_tests,
workbench, deployment) is out of scope. The gate excludes its own source (its
keyword tables) and ``quality_gates/tests/`` (detector fixtures) from self-scan.

Exit codes (mirror wint2/radon):
  0  — clean / all-allowlisted / ``--warn-only``
  1  — non-allowlisted findings present (or a ``--require-clean`` subtree dirty)
  2  — harness error
  64 — usage error (argparse)

Allowlist format (category-keyed; one register serves every present + future
category):

  <category_id>::<posix_repo_relative_path>::<specifier>   [  # note: ...]
    category_id  — S0 | S1 | S2 | S3 | (future) X1 | X2
    path         — repo-relative POSIX path; matched by SUFFIX
    specifier    — "<lineno>" | "*" (whole-file) | "sql:<snippet>"
                   (content-anchored — matches the finding's normalized snippet
                   regardless of line, so unrelated edits stop re-breaking it. A
                   snippet containing "#" can't be anchored — the note delimiter
                   would truncate it — so keep "<lineno>" for those.)
    note         — optional. Two note families mark a PERMANENT park (real or
                   not, never migrates) that ``--require-clean`` ignores:
                   "# false-positive: ..." (not actually SQL / test fixture) and
                   "# sanctioned-exempt: ..." (a real, operator-ruled permanent
                   exception — e.g. the runtime lockdown-enforcement guard).
                   "# migrate-with: ..." marks real debt ``--require-clean``
                   still fails on (removing the entry is migration progress).

Allowlisted findings are ALWAYS printed (prefixed ``[allowlisted]``) so the gate
stays honest; they do NOT contribute to the exit-1 verdict. Adding entries
without operator approval defeats the gate (the KB "Gate Allowlist Conventions").
Removing an entry is the unit of remediation progress.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- category ids ---
CAT_DRIVER = "S0"
CAT_SQL_CALL = "S1"
CAT_RAW_SQL = "S2"
CAT_DDL = "S3"

# --- content-anchored allowlist specifier prefix (2026-07-02, line-agnostic
# pinning): "sql:<snippet>" matches a finding by its normalized snippet
# (``Finding.detail``) regardless of line, so unrelated edits above the site
# stop re-breaking the entry (the plugin.py ::2752→2763→2764→2786 drift, 4× in
# one day). File-scoped by the path suffix; a REWORDED string changes the
# snippet and correctly re-surfaces. ---
_CONTENT_ANCHOR_PREFIX = "sql:"

# --- S0: Postgres driver roots (top dotted segment) ---
_DRIVER_ROOTS = frozenset({
    "psycopg", "psycopg2", "psycopg_pool", "asyncpg", "pg8000", "sqlalchemy",
})

# --- S1: raw-SQL call method names. Two deliberate exclusions:
#   * bare ``execute`` (N1) — collides with PluginBase.execute / the prompt
#     pipeline; its SQL string is still caught by S2.
#   * ``transactional`` — typed-txn exemption per the 2026-06-22 kb-cohort
#     forks-ruling §4. With typed-txn blocks the sanctioned house-style, an
#     unconditional flag on the WRAPPER mismarks every migrated kb/ledger
#     typed-txn block as debt. The DEBT is raw SQL INSIDE a txn — the cursor
#     verbs below (fetch_one/fetch_all/executemany) + any S2 SQL string — which
#     still fire. So a block doing real raw SQL is still caught, while a block
#     using only typed ops (write_state/update_state/delete_records/query_state)
#     is correctly clean. ---
_SQL_CALL_METHODS = frozenset({
    "execute_sql", "executemany", "fetch_one", "fetch_all",
})

# --- S2/S3: SQL verb prefixes (matched as "<verb> ", so a bare keyword token
# never trips) + DDL prefixes (the S3 subset) + substring markers. ---
_SQL_VERBS = (
    "SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER",
    "DROP", "WITH", "TRUNCATE", "GRANT", "REVOKE",
)
_DDL_PREFIXES = (
    "CREATE TABLE", "ALTER TABLE", "DROP TABLE",
    "CREATE UNIQUE INDEX", "CREATE INDEX", "DROP INDEX",
)
# v1-WARN tuning (against the inventory's 222): a verb prefix alone over-counts
# ~3x on prose docstrings ("Create the directory", "Update the record"). A real
# SQL statement also carries a structural companion keyword — require one.
_SQL_COMPANIONS = (
    " FROM ", " INTO ", " SET ", " VALUES", " WHERE ", " TABLE ",
    " INDEX ", " JOIN ", " ON CONFLICT", " RETURNING", " AS ", " USING ",
)
# Standalone fragment markers (SQL-specific, low prose risk). " RETURNING" is
# intentionally NOT standalone (English "returning the value" trips it) — it
# only counts as a companion to a verb prefix; bare " JOIN " likewise.
_SUBSTR_MARKERS = (" ON CONFLICT",)

# --- S2 statement-shape (2026-07-02 detector refinement) ---
# A leading SQL verb + an incidental companion word elsewhere in prose
# over-counts: "Create the … document for … placeholders from the focused …"
# (verb CREATE + English "from"), "Delete temporary blobs from S3" (DELETE +
# "from"), "Create recurring scheduled jobs using cron …" (CREATE + "using").
# A REAL statement continues, right after the verb, with a structural token in
# that verb's canonical slot. For the CLOSED-follower verbs the token
# immediately after the verb is fixed (INSERT INTO, DELETE FROM, CREATE/ALTER/
# DROP <object>), so we REQUIRE it. This is a pure TIGHTENING — an extra
# conjunct on the existing verb-prefix + companion match; it can only ever
# REJECT a prose match, never add a new one (so it cannot turn the gate red on
# a previously-missed string). Open-follower verbs — SELECT (column list),
# UPDATE (table name), WITH (CTE name), TRUNCATE (bare table), GRANT/REVOKE
# (privilege list) — keep the companion-only rule: their next token is an open
# identifier a whitelist can't enumerate without risking false-negatives (a
# missed real violation is the dangerous direction for a FAIL-phase gate).
# The object keywords a CREATE/ALTER/DROP verb may be immediately followed by.
# Kept deliberately broad (incl. exotic single-token leaders FOREIGN/LANGUAGE/
# CAST/SERVER/EVENT) so the tightening never produces a false-NEGATIVE — the
# dangerous direction for a FAIL-phase gate. It targets real DDL shape, not an
# exhaustive Postgres grammar; a truly exotic object whose leader is absent
# would classify as None, but such statements are owner-only DBA operations that
# never appear in non-owner feature code (current real-violation count: 0).
_OBJECT_KEYWORDS = frozenset({
    "TABLE", "INDEX", "UNIQUE", "VIEW", "MATERIALIZED", "SEQUENCE", "SCHEMA",
    "EXTENSION", "FUNCTION", "PROCEDURE", "TRIGGER", "TYPE", "DOMAIN", "ROLE",
    "USER", "DATABASE", "AGGREGATE", "OPERATOR", "POLICY", "RULE", "TEMP",
    "TEMPORARY", "UNLOGGED", "COLUMN", "CONSTRAINT", "PARTITION", "COLLATION",
    "PUBLICATION", "SUBSCRIPTION", "STATISTICS", "FOREIGN", "LANGUAGE", "CAST",
    "SERVER", "EVENT", "OR", "IF",
})
_CLOSED_FOLLOWERS: dict[str, frozenset[str]] = {
    "INSERT": frozenset({"INTO"}),
    "DELETE": frozenset({"FROM"}),
    "CREATE": _OBJECT_KEYWORDS,
    "ALTER": _OBJECT_KEYWORDS,
    "DROP": _OBJECT_KEYWORDS,
}

# --- structural owner exemption: the relational owner = state/pgvector plugins
# + the state-service CORE IMPL layer in ananta/src (B1). Path-prefix match. ---
_OWNER_PREFIXES = (
    "plugins/postgres_state_management_plugin/src/",
    "plugins/rds_postgres_state_management_plugin/src/",
    "plugins/pgvector_service_plugin/src/",
    "plugins/rds_pgvector_service_plugin/src/",
    "ananta/src/ananta/services/state_service/",
    "ananta/src/ananta/services/database_operations/",
    # R-phase TODO (pending operator GAP-3): if the ledger's search backend is
    # ruled a sanctioned search-primitive owner, add its src/ prefix here. Do
    # NOT add it yet — until GAP-3 resolves, the ledger search SQL stays flagged.
)

_SCAN_ROOTS = (
    REPO_ROOT / "ananta" / "src",
    REPO_ROOT / "plugins",
    REPO_ROOT / "quality_gates",
)

# --- canonical bare-invocation defaults (2026-08-14, lane E) — the exact pair
# the Git-Controller commit skill's Step 7.6 passes explicitly. A FULLY bare
# run (no --allowlist AND no --require-clean at all) resolves to these so its
# verdict matches the canonical invocation instead of reporting every finding
# as non-allowlisted (a standing false-alarm generator: same tree, opposite
# verdicts). An explicit --allowlist (with or without --require-clean, the
# shape used by gate_registry.py / platform_gates.py) is left untouched —
# defaults apply ONLY when both flags are absent, so no existing caller's
# behavior changes by even one finding. Paths are REPO_ROOT-anchored (derived
# from this file's own location, not cwd) so a bare run from a subdirectory
# still resolves correctly. ---
_DEFAULT_ALLOWLIST = REPO_ROOT / "quality_gates" / "sql_access_allowlist.txt"
_DEFAULT_REQUIRE_CLEAN = (
    "ananta/src/ananta/llm/session_ledger",
    "ananta/src/ananta/llm/agent_messaging",
    "ananta/src/ananta/core/actions",
    "plugins/default_knowledge_plugin/src",
    "plugins/actr_memory_plugin/src",
    "plugins/default_thinking_plugin/src",
)
_PRUNE_DIRS = frozenset({"__pycache__", ".mypy_cache", ".pytest_cache"})
_OPERATOR_TOOLING_SEGMENTS = frozenset({"research", "tools", "migrations", "parity_tests"})
_PLUGIN_SCOPE_SEGMENTS = frozenset({"src", "tests"})
_BUNDLED_VENV_PREFIX = ".venv"
# The gate's own source carries the keyword tables; its tests dir holds detector
# fixtures. Both are detector *inputs*, not violations — excluded from self-scan.
_SELF_REL = "quality_gates/sql_access_gate.py"
_TESTS_PREFIX = "quality_gates/tests/"


def _rel(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    category_id: str
    path: str
    line: int
    detail: str


@dataclass(frozen=True)
class CategoryDef:
    category_id: str
    label: str
    severity: str  # "high" | "critical"
    detect: Callable[[ast.Module, str], Iterable[Finding]]
    is_exempt: Callable[[str], bool]


@dataclass(frozen=True)
class AllowEntry:
    category_id: str
    path_suffix: str
    specifier: str
    # Permanently parked (never migrates): a "# false-positive:" OR a
    # "# sanctioned-exempt:" note. ``--require-clean`` ignores these; only
    # "# migrate-with:" entries count as removable debt.
    is_permanent: bool


@dataclass
class Allowlist:
    entries: tuple[AllowEntry, ...] = field(default_factory=tuple)

    def match(self, finding: Finding) -> AllowEntry | None:
        for entry in self.entries:
            if entry.category_id != finding.category_id:
                continue
            if not finding.path.endswith(entry.path_suffix):
                continue
            if entry.specifier in ("*", str(finding.line)):
                return entry
            if (entry.specifier.startswith(_CONTENT_ANCHOR_PREFIX)
                    and entry.specifier[len(_CONTENT_ANCHOR_PREFIX):] == finding.detail):
                return entry
        return None


# ---------------------------------------------------------------------------
# Owner exemption (per-category predicate — N4)
# ---------------------------------------------------------------------------


def _owner_exempt(path: str) -> bool:
    """True iff ``path`` is owned (state/pgvector plugins + state-service impl)."""
    return any(path.startswith(prefix) for prefix in _OWNER_PREFIXES)


# ---------------------------------------------------------------------------
# Detectors (pure AST; module-level AND nested)
# ---------------------------------------------------------------------------


def detect_driver_import(module: ast.Module, path: str) -> Iterator[Finding]:
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] in _DRIVER_ROOTS:
                    yield Finding(CAT_DRIVER, path, node.lineno, f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if root in _DRIVER_ROOTS:
                yield Finding(CAT_DRIVER, path, node.lineno, f"from {node.module} import ...")


def detect_sql_call(module: ast.Module, path: str) -> Iterator[Finding]:
    for node in ast.walk(module):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _SQL_CALL_METHODS:
                yield Finding(CAT_SQL_CALL, path, node.lineno, f".{node.func.attr}()")


def _docstring_constant_ids(module: ast.Module) -> set[int]:
    """Node ids of module/class/function docstrings — prose, not SQL.

    Docstrings frequently begin with an imperative verb ("Create the table of
    contents", "Update the record") and were the dominant S2 false-positive
    source in the WARN baseline, so they are skipped.
    """
    ids: set[int] = set()
    for node in ast.walk(module):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = node.body[0] if node.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            ids.add(id(first.value))
    return ids


def _joinedstr_segment_ids(module: ast.Module) -> set[int]:
    """Node ids of ``Constant`` segments that live INSIDE a ``JoinedStr``.

    These are folded into one reconstructed template string by
    ``_iter_string_literals`` (so a verb + companion split across an f-string
    interpolation — ``f"SELECT {cols} FROM t"`` — is classified as ONE statement,
    not slipped through as unrelated segments), so they must NOT also be yielded
    standalone.
    """
    ids: set[int] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.Constant):
                    ids.add(id(value))
    return ids


def _reconstruct_joinedstr(node: ast.JoinedStr) -> str:
    """Flatten an f-string to one template string: literal parts verbatim, each
    ``{...}`` interpolation replaced by a single space so a verb and its
    structural companion that straddle a placeholder land in the same
    classifiable string (the S2 f-string false-negative fix)."""
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        else:
            parts.append(" ")
    return "".join(parts)


def _iter_string_literals(module: ast.Module) -> Iterator[tuple[int, str]]:
    """Yield (lineno, text) for every str literal except docstrings.

    ``JoinedStr`` (f-string) nodes are reconstructed into ONE template string —
    literal segments verbatim, each interpolation replaced by a single space — so
    dynamic SQL whose verb and companion straddle a ``{...}`` placeholder
    (``f"SELECT {cols} FROM t"``) is classified as a single statement. Before
    this, ``ast.walk`` yielded each segment as its own ``Constant`` and the
    ``SELECT `` part (no companion) and `` FROM ...`` part (no leading verb) both
    escaped S2 — a real false-negative Codex caught on the vendor SQLite readers.
    The inner ``Constant`` segments are skipped here (folded into the reconstruction).
    """
    docstrings = _docstring_constant_ids(module)
    joined_segments = _joinedstr_segment_ids(module)
    for node in ast.walk(module):
        if isinstance(node, ast.JoinedStr):
            yield node.lineno, _reconstruct_joinedstr(node)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings and id(node) not in joined_segments:
                yield node.lineno, node.value


def _leading_sql_verb(stripped_upper: str) -> str | None:
    """The SQL verb a stripped-upper string starts with (verb + space), else None.

    A verb is matched only when followed by a space, so a bare token (an enum
    value ``"SELECT"``) never trips the heuristic.
    """
    for verb in _SQL_VERBS:
        if stripped_upper.startswith(verb + " "):
            return verb
    return None


def _statement_shaped(verb: str, stripped_upper: str) -> bool:
    """True unless a CLOSED-follower verb is NOT followed by its canonical token.

    Kills verb-led PROSE ("Create the …", "Delete temporary …") whose only
    companion sits far downstream in an unrelated clause: a real INSERT / DELETE
    / CREATE / ALTER / DROP statement carries INTO / FROM / an object keyword as
    its VERY NEXT token. Open-follower verbs (not in ``_CLOSED_FOLLOWERS`` —
    SELECT / UPDATE / WITH / TRUNCATE / GRANT / REVOKE) return True: their next
    token is an open identifier a whitelist can't enumerate without risking a
    false-negative, so the companion check alone gates them.
    """
    followers = _CLOSED_FOLLOWERS.get(verb)
    if followers is None:
        return True
    tokens = stripped_upper.split()
    return len(tokens) >= 2 and tokens[1] in followers


def classify_sql(text: str) -> str | None:
    """Return CAT_DDL / CAT_RAW_SQL / None for a string literal.

    DDL is checked first (the higher-severity subset; ``CREATE TABLE`` etc. are
    inherently SQL-shaped, so a prefix match suffices). For DML, a verb prefix
    alone over-counts on prose, so S2 requires (a) a structural companion
    keyword AND (b) statement shape — the verb's canonical follower token in
    position, for the closed-follower verbs (see ``_statement_shaped``). A
    standalone SQL-specific marker (ON CONFLICT) is matched on its own. A verb is
    only matched when followed by a space, so a bare token (e.g. an enum value
    ``"SELECT"``) never trips the heuristic.
    """
    stripped = text.strip().upper()
    if not stripped:
        return None
    for ddl in _DDL_PREFIXES:
        if stripped.startswith(ddl + " "):
            return CAT_DDL
    upper = text.upper()
    verb = _leading_sql_verb(stripped)
    has_companion = any(companion in upper for companion in _SQL_COMPANIONS)
    if verb is not None and has_companion and _statement_shaped(verb, stripped):
        return CAT_RAW_SQL
    if any(marker in upper for marker in _SUBSTR_MARKERS):
        return CAT_RAW_SQL
    return None


def _snippet(text: str) -> str:
    flat = " ".join(text.split())
    return flat[:60] + ("…" if len(flat) > 60 else "")


def detect_raw_sql(module: ast.Module, path: str) -> Iterator[Finding]:
    for line, text in _iter_string_literals(module):
        if classify_sql(text) == CAT_RAW_SQL:
            yield Finding(CAT_RAW_SQL, path, line, _snippet(text))


def detect_hand_ddl(module: ast.Module, path: str) -> Iterator[Finding]:
    for line, text in _iter_string_literals(module):
        if classify_sql(text) == CAT_DDL:
            yield Finding(CAT_DDL, path, line, _snippet(text))


CATEGORIES: tuple[CategoryDef, ...] = (
    CategoryDef(CAT_DRIVER, "driver-import", "high", detect_driver_import, _owner_exempt),
    CategoryDef(CAT_SQL_CALL, "raw-SQL call", "high", detect_sql_call, _owner_exempt),
    CategoryDef(CAT_RAW_SQL, "raw-SQL string", "high", detect_raw_sql, _owner_exempt),
    CategoryDef(CAT_DDL, "hand-DDL", "critical", detect_hand_ddl, _owner_exempt),
)


# ---------------------------------------------------------------------------
# File discovery + scanning
# ---------------------------------------------------------------------------


def _plugin_in_scope(parts: tuple[str, ...]) -> bool:
    """For a ``plugins/<X>/...`` path, only ``src/`` and ``tests/`` (and not
    nested operator-tooling) are in the platform-quality surface."""
    idx = parts.index("plugins")
    if idx + 2 >= len(parts) or parts[idx + 2] not in _PLUGIN_SCOPE_SEGMENTS:
        return False
    return not any(seg in _OPERATOR_TOOLING_SEGMENTS for seg in parts[idx + 3:])


def _is_in_scope(path: Path) -> bool:
    parts = path.parts
    if any(p in _PRUNE_DIRS for p in parts):
        return False
    if any(p.startswith(_BUNDLED_VENV_PREFIX) for p in parts):
        return False
    rel = _rel(path)
    if rel == _SELF_REL or rel.startswith(_TESTS_PREFIX):
        return False
    if "plugins" in parts and not _plugin_in_scope(parts):
        return False
    return True


def _walk_python_files() -> Iterator[Path]:
    seen: set[Path] = set()
    for root in _SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if path in seen or not _is_in_scope(path):
                continue
            seen.add(path)
            yield path


def _parse_safely(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        print(f"WARN: cannot parse {path}: {exc}", file=sys.stderr)
        return None


def scan_module(module: ast.Module, path: str) -> list[Finding]:
    """Run every non-exempt category detector over one parsed module."""
    findings: list[Finding] = []
    for category in CATEGORIES:
        if category.is_exempt(path):
            continue
        findings.extend(category.detect(module, path))
    return findings


def collect_findings() -> list[Finding]:
    out: list[Finding] = []
    for path in _walk_python_files():
        module = _parse_safely(path)
        if module is None:
            continue
        out.extend(scan_module(module, _rel(path)))
    return out


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


def load_allowlist(path: Path) -> Allowlist:
    if not path.exists():
        return Allowlist()
    entries: list[AllowEntry] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        body, _, note = line.partition("#")
        # maxsplit=2 so a content-anchored specifier ("sql:<snippet>") that
        # itself contains "::" is captured verbatim as the third field.
        parts = body.strip().split("::", 2)
        if len(parts) < 3:
            print(f"WARN: malformed allowlist line: {line!r}", file=sys.stderr)
            continue
        entries.append(AllowEntry(
            category_id=parts[0].strip(),
            path_suffix=parts[1].strip(),
            specifier=parts[2].strip(),
            is_permanent=("false-positive" in note.lower()
                          or "sanctioned-exempt" in note.lower()),
        ))
    return Allowlist(tuple(entries))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _under_any_prefix(path: str, prefixes: list[str]) -> bool:
    return any(path.startswith(prefix) for prefix in prefixes)


def render_report(findings: list[Finding], allowlist: Allowlist) -> tuple[int, int]:
    """Print findings grouped by category; return (blocking, allowlisted)."""
    blocking = 0
    allowlisted = 0
    by_category: dict[str, list[Finding]] = {c.category_id: [] for c in CATEGORIES}
    for finding in findings:
        by_category.setdefault(finding.category_id, []).append(finding)

    label = {c.category_id: c.label for c in CATEGORIES}
    for category in CATEGORIES:
        group = by_category.get(category.category_id, [])
        if not group:
            continue
        print(f"\n=== {category.category_id} {label[category.category_id]} "
              f"({len(group)} finding(s)) ===")
        for finding in sorted(group, key=lambda f: (f.path, f.line)):
            entry = allowlist.match(finding)
            marker = " [allowlisted]" if entry else ""
            print(f"  {finding.path}:{finding.line}  {finding.detail}{marker}")
            if entry:
                allowlisted += 1
            else:
                blocking += 1
    return blocking, allowlisted


def _per_file_summary(findings: list[Finding], allowlist: Allowlist) -> None:
    blocking_by_file: dict[str, int] = {}
    for finding in findings:
        if allowlist.match(finding) is None:
            blocking_by_file[finding.path] = blocking_by_file.get(finding.path, 0) + 1
    if not blocking_by_file:
        return
    print("\n--- non-allowlisted findings per file (top 20) ---")
    ranked = sorted(blocking_by_file.items(), key=lambda kv: (-kv[1], kv[0]))
    for path, count in ranked[:20]:
        print(f"  {count:4d}  {path}")


def _require_clean_violations(
    findings: list[Finding], allowlist: Allowlist, prefixes: list[str],
) -> list[Finding]:
    """Findings under a --require-clean prefix that are NOT permanent FPs (N3)."""
    out: list[Finding] = []
    for finding in findings:
        if not _under_any_prefix(finding.path, prefixes):
            continue
        entry = allowlist.match(finding)
        if entry is not None and entry.is_permanent:
            continue
        out.append(finding)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SQL-access quality gate (#0 lockdown).")
    parser.add_argument("--allowlist", type=Path, default=None,
                        help="Path to the category-keyed tracked-debt allowlist.")
    parser.add_argument("--warn-only", action="store_true",
                        help="Always exit 0 (WARN phase); findings still print.")
    parser.add_argument("--require-clean", action="append", default=[], metavar="PREFIX",
                        help="Assert a migrated subtree has zero non-FP findings; "
                             "repeatable. Fails (exit 1) even under --warn-only.")
    return parser


def _resolve_bare_defaults(args: argparse.Namespace) -> None:
    """Mutate ``args`` in place: a FULLY bare invocation resolves to the
    canonical --allowlist + --require-clean pair (see ``_DEFAULT_ALLOWLIST``
    above). Any explicit --allowlist is left untouched."""
    if args.allowlist is None and not args.require_clean:
        args.allowlist = _DEFAULT_ALLOWLIST
        args.require_clean = list(_DEFAULT_REQUIRE_CLEAN)


def run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _resolve_bare_defaults(args)
    allowlist = load_allowlist(args.allowlist) if args.allowlist else Allowlist()

    try:
        findings = collect_findings()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: harness failure: {exc}", file=sys.stderr)
        return 2

    blocking, allowlisted = render_report(findings, allowlist)
    _per_file_summary(findings, allowlist)

    require_clean: list[str] = list(args.require_clean)
    rc_violations = (
        _require_clean_violations(findings, allowlist, require_clean)
        if require_clean else []
    )

    total = len(findings)
    print(f"\nSUMMARY: {total} finding(s) — {blocking} non-allowlisted, "
          f"{allowlisted} allowlisted.")
    if require_clean:
        print(f"--require-clean {require_clean}: {len(rc_violations)} violation(s).")
        for finding in sorted(rc_violations, key=lambda f: (f.path, f.line))[:20]:
            print(f"  DIRTY {finding.category_id} {finding.path}:{finding.line}")

    if rc_violations:
        return 1
    if args.warn_only:
        print("MODE=warn — exit 0 (findings are advisory this phase).")
        return 0
    return 1 if blocking else 0


def main() -> int:
    # Harness errors return 2 from run(); argparse raises SystemExit (0 on
    # --help, non-zero on a usage error) — remap the usage path to 64.
    try:
        return run(sys.argv[1:])
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 64


if __name__ == "__main__":
    raise SystemExit(main())
