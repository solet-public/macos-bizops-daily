#!/usr/bin/env python3
"""W-INT Cycle 2 vault-key-declaration gate (W-PLUGIN-LAUNCH-KEYS) — warn mode.

Per master plan §3.3.7 + brief §5: every plugin source call site that
invokes a vault-service verb (``store`` / ``retrieve`` / ``delete`` /
``exists`` / ``rotate`` / ``rename`` / ``list``) on a vault-shaped
attribute must use a key argument the plugin has DECLARED via its
``VaultKeysProvider.get_declared_vault_keys()`` implementation.

Cycle 2 ships in WARN mode at sub-1 landing (warn-only at landing per
brief §5.3); findings still print so the gate stays honest, but no
commit is blocked. Sub-2 (W-VAULT-CALLER-ENFORCE) flips the static gate
to fail-mode alongside the runtime enforcement activation.

Acceptance modes (per Codex correction #5 + Coordinator-Dawn 2026-06-07
PT unblock):

1. **Constant resolution.** If the key arg is a ``Name`` node bound to a
   same-module module-level ``Final[str]`` assignment whose RHS is a
   string literal (incl. f-string with all-resolvable parts), resolve to
   that literal. Run FIRST so the common Tier-1 idiom
   ``self._vault.retrieve(VAULT_KEY_BOT_TOKEN)`` matches the declared
   scoped form without an annotation.

2. **Literal / prefix.** Accept if the (literal or resolved) key
   matches a declared key exactly OR matches a declared prefix
   (declared key terminated in ``*``).

3. **Annotation.** Accept if the call site's line (or the immediately
   preceding line, for multi-line calls) carries a
   ``# vault-key: <declared-key-or-prefix>`` annotation whose body
   matches a declared key/prefix. One syntax only — ``# vault-key:``,
   never ``@vault-key`` — per Codex correction #5.

4. **Allowlist.** Accept if the call site is enumerated in the
   tracked-debt allowlist (initial seed: address-book-chain consumers
   per brief §5.2, since the key flows through an operator-authored
   address-book entry rather than from plugin-owned literals).

Otherwise FAIL (warn-mode prints; fail-mode contributes to exit 1).

Heuristic for the vault-shaped attribute (per brief §5.1, broadened
per Coordinator-Dawn 2026-06-07 PT acceptance of the inventory delta):
the receiver of the verb call is plausibly a VaultServiceProxy if it
is ``vault_service`` OR a Name / Attribute whose final segment is one
of ``_vault_service``, ``vault_service``, ``vault_proxy``, or
``_vault`` (the schwab/soundcloud TokenStore idiom). False positives
on non-vault attributes named the same way are acceptable in warn-mode
(operator may add an allowlist entry if they show up).

Allowlist format mirrors the Cycle 1 register:

  <check_id>::<scope_qualifier>::<specifier>
    check_id        — "D1.2" (vault-key declaration drift)
    scope_qualifier — repo-relative POSIX path
    specifier       — "<lineno>::<key-or-marker>" OR "*" wildcard

Allowlisted findings STILL print (prefixed ``[allowlisted]``); they do
NOT contribute to the exit verdict. Under the KB "Gate Allowlist
Conventions", adding entries without operator approval defeats the gate's
purpose.

Exit codes (mirror wint2_driver_import_check.py):
  0  — clean, all findings allowlisted, OR ``--warn-only`` mode
  1  — non-allowlisted findings present AND ``--warn-only`` NOT passed
  2  — harness error
  64 — usage error (argparse)

Reference: master plan §3.3.7 + brief §5 + Codex correction #5.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from quality_gates.allowlist_schema import load_allowlist as _load_tagged_allowlist  # noqa: E402

CHECK_ID = "D1.2"

# Sentinel inserted into a resolved string when an f-string
# interpolation slot cannot be evaluated statically (e.g.
# ``f"{_SOLET}.X.Y"`` where ``_SOLET`` derives from a
# runtime env-var lookup). Both the call-site arg and the declared-key
# resolution paths run through the same resolver, so the sentinel
# appears symmetrically on both sides and exact-match still succeeds
# for the dominant Tier-1-migrated pattern. Pick a value that cannot
# appear in a real vault key.
_UNRESOLVED_PART = "\x00UNRESOLVED\x00"

# Verb methods that operate on a vault key (first positional arg).
_VAULT_VERBS = frozenset({
    "store", "retrieve", "delete", "exists", "rotate", "rename", "list",
    "store_random",
})

# Verbs whose first positional is NOT a key (excluded from key-arg
# scanning even when the receiver matches the vault heuristic).
_NON_KEY_VERBS = frozenset({
    "list",  # `vault.list(tag=...)` — first arg is a tag string, not a key.
})

# Attribute / Name final-segment match for the vault-proxy heuristic.
# Receivers whose name ends in any of these are treated as plausible
# vault proxies (the verb-name match adds the second confirmation).
_VAULT_RECEIVER_SEGMENTS = frozenset({
    "vault_service",
    "_vault_service",
    "vault_proxy",
    "_vault",
})

# Line-suffix annotation. Strict syntax per Codex correction #5: only
# ``# vault-key:`` accepted, never ``@vault-key`` or other variants.
_ANNOTATION_TOKEN = "# vault-key:"

_SCAN_ROOTS = (
    REPO_ROOT / "plugins",
)

_OPERATOR_TOOLING_PLUGIN_SEGMENTS = frozenset({
    "research", "tools", "migrations", "parity_tests",
})

_BUNDLED_VENV_PREFIX = ".venv"
_PRUNE_DIRS = frozenset({"__pycache__", ".mypy_cache", ".pytest_cache"})
_PLUGIN_SCOPE_SEGMENTS = frozenset({"src", "tests"})


def _rel(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------------------
# Data classes
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
        for entry in self.entries:
            if entry.check_id != finding.check_id:
                continue
            if entry.scope_qualifier != finding.scope_qualifier:
                continue
            if entry.specifier in ("*", finding.specifier):
                return True
        return False


def load_allowlist(path: Path) -> Allowlist:
    """Mandatory owner/reason/expires schema (D-3-structural, iss_23fa51b5)."""

    if not path.exists():
        return Allowlist()
    entries: set[AllowlistEntry] = set()
    for key in _load_tagged_allowlist(path):
        parts = key.split("::", 2)
        if len(parts) < 3:
            print(
                f"WARN: malformed allowlist line "
                f"(need <check>::<scope>::<spec>): {key!r}",
                file=sys.stderr,
            )
            continue
        entries.add(AllowlistEntry(
            parts[0].strip(), parts[1].strip(), parts[2].strip(),
        ))
    return Allowlist(frozenset(entries))


# ---------------------------------------------------------------------------
# Path filtering
# ---------------------------------------------------------------------------


def _is_in_scope(path: Path) -> bool:
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
# Plugin → declared keys discovery
# ---------------------------------------------------------------------------


def _plugin_name_for_path(path: Path) -> str | None:
    """Return the owning plugin's directory name, or None if outside plugins/."""
    parts = path.relative_to(REPO_ROOT).parts
    if len(parts) >= 2 and parts[0] == "plugins":
        return parts[1]
    return None


def _collect_module_constants(module: ast.Module) -> dict[str, str | None]:
    """Map module-level Name -> literal string value (None if unresolvable).

    Resolves:
        FOO: Final[str] = "literal"
        FOO = "literal"
        FOO = f"{x}.y"   # parts that don't resolve become _UNRESOLVED_PART

    Walks top-level Assign / AnnAssign only — nested-scope assigns are
    intentionally NOT followed (the call-site keys we care about are
    module-level constants by convention).

    The SOLET-segment substitution (per master plan §3.3.1) is
    handled by replacing unresolvable f-string interpolation slots with
    the sentinel ``_UNRESOLVED_PART``. The call-site scanner and the
    declared-key gatherer both walk through ``_collect_module_constants``,
    so both sides produce symmetric sentinel-substituted forms and match
    exactly when the only unresolvable part is the runtime solet
    name (the common Tier-1-migrated idiom
    ``f"{_SOLET}.{plugin}.{credential}"``).
    """
    out: dict[str, str | None] = {}

    def _resolve_value(node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                elif isinstance(v, ast.FormattedValue):
                    sub = _resolve_value(v.value)
                    parts.append(sub if sub is not None else _UNRESOLVED_PART)
                else:
                    parts.append(_UNRESOLVED_PART)
            return "".join(parts)
        if isinstance(node, ast.Name) and node.id in out and out[node.id] is not None:
            return out[node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = _resolve_value(node.left)
            right = _resolve_value(node.right)
            if left is None or right is None:
                return None
            return left + right
        return None

    for stmt in module.body:
        if isinstance(stmt, ast.Assign):
            value = _resolve_value(stmt.value)
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name):
                    out[tgt.id] = value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            if isinstance(stmt.target, ast.Name):
                out[stmt.target.id] = _resolve_value(stmt.value)
    return out


def _resolve_imports(module: ast.Module) -> dict[str, tuple[str, str]]:
    """Map local Name -> (module_path, original_name) for `from X import Y`.

    Only intra-plugin (relative or same-plugin absolute) imports are
    chased by the cross-module constant lookup below.
    """
    out: dict[str, tuple[str, str]] = {}
    for stmt in module.body:
        if isinstance(stmt, ast.ImportFrom):
            mod = stmt.module or ""
            for alias in stmt.names:
                local = alias.asname or alias.name
                out[local] = (mod, alias.name)
    return out


def _plugin_py_files(src_root: Path) -> list[Path]:
    """Walk src_root for .py files, excluding bundled venvs + cache dirs."""
    return [
        p for p in src_root.rglob("*.py")
        if not any(part in _PRUNE_DIRS for part in p.parts)
        and not any(
            part.startswith(_BUNDLED_VENV_PREFIX) for part in p.parts
        )
    ]


def _build_module_index(
    py_files: list[Path],
) -> tuple[
    dict[Path, ast.Module],
    dict[Path, dict[str, str | None]],
    dict[Path, dict[str, tuple[str, str]]],
]:
    module_ast: dict[Path, ast.Module] = {}
    module_consts: dict[Path, dict[str, str | None]] = {}
    module_imports: dict[Path, dict[str, tuple[str, str]]] = {}
    for path in py_files:
        node = _parse_safely(path)
        if node is None:
            continue
        module_ast[path] = node
        module_consts[path] = _collect_module_constants(node)
        module_imports[path] = _resolve_imports(node)
    return module_ast, module_consts, module_imports


def _make_cross_module_resolver(
    py_files: list[Path],
    module_consts: dict[Path, dict[str, str | None]],
    module_imports: dict[Path, dict[str, tuple[str, str]]],
) -> Any:
    """Build a `_resolve(path, name)` closure that chases relative imports."""
    def _resolve(
        path: Path, name: str, visited: set[tuple[Path, str]] | None = None,
    ) -> str | None:
        visited = visited or set()
        key = (path, name)
        if key in visited:
            return None
        visited.add(key)
        local = module_consts.get(path, {}).get(name)
        if local is not None:
            return local
        imports = module_imports.get(path, {})
        if name not in imports:
            return None
        mod, orig = imports[name]
        target_tail = mod.replace(".", "/") + ".py"
        for candidate in py_files:
            if candidate.as_posix().endswith(target_tail) or (
                candidate.name == mod.split(".")[-1] + ".py"
            ):
                return _resolve(candidate, orig, visited)
        return None
    return _resolve


def _binop_part_value(
    node: ast.expr, path: Path, resolve: Any,
) -> str | None:
    if isinstance(node, ast.Name):
        return resolve(path, node.id)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _resolve_list_element(
    elt: ast.expr, path: Path, resolve: Any,
) -> str | None:
    """Resolve one list-element AST node to its literal string value, or None."""
    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
        return elt.value
    if isinstance(elt, ast.Name):
        return resolve(path, elt.id)
    if isinstance(elt, ast.BinOp) and isinstance(elt.op, ast.Add):
        left = _binop_part_value(elt.left, path, resolve)
        right = _binop_part_value(elt.right, path, resolve)
        if left is not None and right is not None:
            return left + right
    return None


def _is_declaration_method(method: ast.AST) -> bool:
    return (
        isinstance(method, ast.FunctionDef)
        and method.name in {
            "get_required_vault_keys", "get_declared_vault_keys",
        }
    )


def _collect_declaration_returns(
    method: ast.FunctionDef, path: Path, resolve: Any, out: set[str],
) -> None:
    for sub in ast.walk(method):
        if not isinstance(sub, ast.Return) or not isinstance(sub.value, ast.List):
            continue
        for elt in sub.value.elts:
            resolved = _resolve_list_element(elt, path, resolve)
            if resolved is not None:
                out.add(resolved)


def _gather_plugin_declared_keys(plugin_dir: Path) -> set[str]:
    """Pull declared keys from every plugin-class declaration method."""
    declared: set[str] = set()
    src_root = plugin_dir / "src"
    if not src_root.exists():
        return declared
    py_files = _plugin_py_files(src_root)
    module_ast, module_consts, module_imports = _build_module_index(py_files)
    resolve = _make_cross_module_resolver(
        py_files, module_consts, module_imports,
    )
    for path, node in module_ast.items():
        for cls in (n for n in ast.walk(node) if isinstance(n, ast.ClassDef)):
            for method in cls.body:
                if not _is_declaration_method(method):
                    continue
                assert isinstance(method, ast.FunctionDef)
                _collect_declaration_returns(method, path, resolve, declared)
    return declared


# ---------------------------------------------------------------------------
# Call-site scanning
# ---------------------------------------------------------------------------


def _is_vault_receiver(node: ast.expr) -> bool:
    """True iff ``node`` plausibly references a VaultServiceProxy."""
    if isinstance(node, ast.Name):
        return node.id in _VAULT_RECEIVER_SEGMENTS
    if isinstance(node, ast.Attribute):
        return node.attr in _VAULT_RECEIVER_SEGMENTS
    return False


def _key_matches_declared(key: str, declared: set[str]) -> bool:
    if key in declared:
        return True
    for d in declared:
        if d.endswith("*") and key.startswith(d[:-1]):
            return True
    return False


def _joinedstr_to_literal(
    arg: ast.JoinedStr, module_consts: dict[str, str | None],
) -> str | None:
    """Resolve an f-string arg to a literal; sentinel-substitutes unknown parts."""
    parts: list[str] = []
    for v in arg.values:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            parts.append(v.value)
        elif isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name):
            resolved = module_consts.get(v.value.id)
            parts.append(resolved if resolved is not None else _UNRESOLVED_PART)
        else:
            parts.append(_UNRESOLVED_PART)
    return "".join(parts)


def _key_arg_literal(
    call: ast.Call, module_consts: dict[str, str | None],
) -> str | None:
    """Resolve the first positional arg of `call` to a literal if possible."""
    if not call.args:
        return None
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name):
        return module_consts.get(arg.id)
    if isinstance(arg, ast.JoinedStr):
        return _joinedstr_to_literal(arg, module_consts)
    return None


def _annotation_for_line(source_lines: list[str], lineno: int) -> str | None:
    """Return the trimmed annotation body if line OR prior line has one."""
    # lineno is 1-indexed
    for offset in (0, -1):
        idx = lineno - 1 + offset
        if 0 <= idx < len(source_lines):
            line = source_lines[idx]
            pos = line.find(_ANNOTATION_TOKEN)
            if pos != -1:
                return line[pos + len(_ANNOTATION_TOKEN):].strip()
    return None


def _is_vault_verb_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in _VAULT_VERBS or func.attr in _NON_KEY_VERBS:
        return False
    return _is_vault_receiver(func.value)


def _classify_rename_args(
    hits: list[tuple[int, str, str, str | None]],
    call: ast.Call,
    constants: dict[str, str | None],
    declared: set[str],
    source_lines: list[str],
) -> None:
    """Rename takes (old_key, new_key) — check both positional args."""
    for arg_idx in (0, 1):
        arg = call.args[arg_idx]
        resolved = _resolve_single_arg(arg, constants)
        _classify_and_append(
            hits, call.lineno, "rename", resolved, declared,
            source_lines, marker=f"arg{arg_idx}",
        )


def _scan_module_for_vault_calls(
    module: ast.Module,
    declared: set[str],
    source_lines: list[str],
    resolved_consts: dict[str, str | None] | None = None,
) -> list[tuple[int, str, str, str | None]]:
    """Return (lineno, verb, status, detail) tuples for every vault call."""
    constants = (
        resolved_consts if resolved_consts is not None
        else _collect_module_constants(module)
    )
    hits: list[tuple[int, str, str, str | None]] = []
    for node in ast.walk(module):
        if not _is_vault_verb_call(node):
            continue
        assert isinstance(node, ast.Call)
        assert isinstance(node.func, ast.Attribute)
        verb = node.func.attr
        if verb == "rename" and len(node.args) >= 2:
            _classify_rename_args(hits, node, constants, declared, source_lines)
            continue
        resolved = _key_arg_literal(node, constants)
        _classify_and_append(
            hits, node.lineno, verb, resolved, declared, source_lines,
        )
    return hits


def _resolve_single_arg(arg: ast.expr, consts: dict[str, str | None]) -> str | None:
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name):
        return consts.get(arg.id)
    return None


def _classify_resolved_key(
    resolved: str, declared: set[str], lineno: int, verb: str,
) -> tuple[int, str, str, str | None] | None:
    """Return an ok-* tuple if `resolved` matches declared, else None."""
    if _key_matches_declared(resolved, declared):
        return (lineno, verb, "ok-literal", resolved)
    if any(
        d.endswith("*") and resolved.startswith(d[:-1]) for d in declared
    ):
        return (lineno, verb, "ok-prefix", resolved)
    return None


def _classify_and_append(
    hits: list[tuple[int, str, str, str | None]],
    lineno: int,
    verb: str,
    resolved: str | None,
    declared: set[str],
    source_lines: list[str],
    marker: str = "",
) -> None:
    if resolved is not None:
        ok = _classify_resolved_key(resolved, declared, lineno, verb)
        if ok is not None:
            hits.append(ok)
            return
    annotation = _annotation_for_line(source_lines, lineno)
    if annotation is not None and _key_matches_declared(annotation, declared):
        hits.append((lineno, verb, "ok-annotation", annotation))
        return
    if resolved is not None:
        detail = resolved + (f" ({marker})" if marker else "")
        hits.append((lineno, verb, "undeclared", detail))
        return
    hits.append((lineno, verb, "unresolved", marker or None))


# ---------------------------------------------------------------------------
# Module parsing
# ---------------------------------------------------------------------------


def _parse_safely(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        print(f"WARN: cannot parse {path}: {exc}", file=sys.stderr)
        return None


def _findings_from_module(
    module: ast.Module,
    path: Path,
    declared: set[str],
    resolved_consts: dict[str, str | None] | None = None,
) -> list[Finding]:
    rel = _rel(path)
    try:
        source_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        source_lines = []
    findings: list[Finding] = []
    for lineno, verb, status, detail in _scan_module_for_vault_calls(
        module, declared, source_lines, resolved_consts,
    ):
        if status.startswith("ok-"):
            continue
        if status == "undeclared":
            msg = (
                f"{rel}:{lineno} calls vault.{verb}({detail!s}) but the "
                f"plugin has not declared that key via "
                f"VaultKeysProvider.get_declared_vault_keys(). Either "
                f"add the key (or a matching prefix terminated in '*') "
                f"to the declaration, or add a `# vault-key: "
                f"<declared-key-or-prefix>` annotation on the call's "
                f"line (or the immediately preceding line)."
            )
        else:  # unresolved
            msg = (
                f"{rel}:{lineno} calls vault.{verb}(...) with a key "
                f"argument the static gate could not resolve to a "
                f"literal. Add a `# vault-key: <declared-key-or-prefix>` "
                f"annotation or refactor the call to use a module-level "
                f"`Final[str]` constant."
            )
        spec = f"{lineno}::{verb}::{detail or 'dynamic'}"
        findings.append(Finding(
            check_id=CHECK_ID,
            scope_qualifier=rel,
            specifier=spec,
            message=msg,
            file_path=rel,
            lineno=lineno,
        ))
    return findings


def _build_plugin_const_index(
    plugin_dir: Path,
) -> dict[Path, dict[str, str | None]]:
    """Pre-compute literal-resolved module constants for every .py in a plugin.

    Also walks ``from .X import Y`` imports so that a token_store.py call
    site referencing ``VAULT_KEY_REFRESH_TOKEN`` (imported from sibling
    ``.constants``) resolves to the same literal the constants module
    defines. Without this, cross-module constants surface as ``unresolved``
    findings even though their value is mechanically deducible.
    """
    src_root = plugin_dir / "src"
    if not src_root.exists():
        return {}

    py_files = _plugin_py_files(src_root)
    _, per_module_consts, per_module_imports = _build_module_index(py_files)
    resolve = _make_cross_module_resolver(
        py_files, per_module_consts, per_module_imports,
    )

    # Materialize a flat per-module map that includes both the module's
    # own Final[str] constants AND the resolved values of every name
    # imported from a sibling source. This is what the call-site scanner
    # consults; same-module hits dominate, imported hits cover the
    # token_store / constants pattern.
    resolved_per_module: dict[Path, dict[str, str | None]] = {}
    for path in py_files:
        merged: dict[str, str | None] = dict(per_module_consts.get(path, {}))
        for local in per_module_imports.get(path, {}):
            if local in merged and merged[local] is not None:
                continue
            merged[local] = resolve(path, local)
        resolved_per_module[path] = merged
    return resolved_per_module


def collect_findings() -> list[Finding]:
    out: list[Finding] = []
    # Walk plugin-by-plugin so we can pull each plugin's declared key
    # set ONCE rather than re-resolving for every file.
    plugins_root = REPO_ROOT / "plugins"
    if not plugins_root.exists():
        return out
    for plugin_dir in sorted(plugins_root.iterdir()):
        if not plugin_dir.is_dir():
            continue
        declared = _gather_plugin_declared_keys(plugin_dir)
        per_module_resolved = _build_plugin_const_index(plugin_dir)
        for path in _walk_python_files():
            if _plugin_name_for_path(path) != plugin_dir.name:
                continue
            module = _parse_safely(path)
            if module is None:
                continue
            out.extend(_findings_from_module(
                module, path, declared, per_module_resolved.get(path, {}),
            ))
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
        print(
            f"{finding.check_id}::{finding.scope_qualifier}::"
            f"{finding.specifier}{marker}",
        )
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
    parser.add_argument(
        "--allowlist", type=Path, default=None,
        help="Path to tracked-debt allowlist file.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of human text.",
    )
    parser.add_argument(
        "--warn-only", action="store_true",
        help=(
            "Always exit 0; print findings but do NOT contribute to "
            "the blocking verdict. This is the W-PLUGIN-LAUNCH-KEYS "
            "sub-1 mode. Sub-2 (W-VAULT-CALLER-ENFORCE) drops this "
            "flag to flip to fail-mode."
        ),
    )
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
                print(
                    "OK: 0 findings; W-PLUGIN-LAUNCH-KEYS vault-key "
                    "declaration gate clean.",
                )
            else:
                print(
                    f"OK: {len(findings)} finding(s) — all allowlisted; "
                    "W-PLUGIN-LAUNCH-KEYS vault-key declaration gate clean.",
                )
        else:
            mode = "warn" if args.warn_only else "fail"
            print(
                f"\n{len(findings)} W-PLUGIN-LAUNCH-KEYS finding(s) "
                f"({allowlisted} allowlisted; {blocking} non-allowlisted; "
                f"mode={mode}).",
                file=sys.stderr,
            )
    if args.warn_only:
        return 0
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
