#!/usr/bin/env python3
"""Process return-shape gate (GTE-07) with tracked-debt allowlist.

Every ``@service_interface_process`` / ``@platform_process`` decorated method
is callable over ``process_call``, and the act-time dispatch contract rejects
non-dict returns:

- service_interface path: ``ActionProcessor._execute_standard_service_method``
  raises ``FrameworkError`` on any non-dict return
  (``ananta/src/ananta/core/actions/action_processor.py:848``). Dataclass
  instances are exempt — the dispatch auto-serializes them via
  ``dataclasses.asdict`` (``:844-845``, the D2-closure DTO pattern).
- plugin path: ``ActionProcessor._execute_plugin_action`` raises on any
  non-dict return (``:443``) and additionally requires the full ActionResult
  field set. There is NO dataclass auto-serialization on this path.

A decorated verb whose return annotation is not a dict is therefore dead on
arrival over ``process_call`` — the REL-11 class
(``memory_service::get_focused`` shipped broken for its entire life until
2026-07-06). This gate is the promoted REL-11 Part-2 sweep: it AST-scans every
decorated def and fails on non-dict return annotations. Annotations are a
reliable proxy under the platform's 100% type-hint policy.

Allowed return annotations:
- ``dict`` / ``dict[...]`` — both decorator kinds.
- A class name resolving (name-level, see residuals) to a ``TypedDict`` —
  both decorator kinds: a TypedDict IS a plain dict at runtime
  (Rev-C finding, e.g. ``ActionResult``).
- A class name resolving to a ``@dataclass`` — ``@service_interface_process``
  ONLY (mirrors the ``:844`` auto-serialize allowance; the plugin path has
  none).
- Unions (``X | Y`` / ``Optional[...]``) FAIL even when one arm is a dict:
  the non-dict arm dies at dispatch.

The ``--allowlist <path>`` file is a tracked-debt register, NOT a skip path:
one ``<file_path>::<function_name>`` per line (POSIX-suffix-matched path,
bare function name — the radon_cc_check convention). Allowlisted findings are
still printed so the gate stays honest; removing an entry is the unit of
remediation progress; adding entries requires operator ratification. The
register ships EMPTY as of the REL-11 follow-up slice.

Documented residuals (what this gate deliberately cannot see):
- ``return_value_schema`` drift: the decorator's declared schema can disagree
  with the actual return shape without changing the return ANNOTATION — this
  gate only reads annotations (Rev-C N1). Schema-vs-return fidelity is a
  review-craft concern pinned per-verb by smokes.
- Runtime-vs-annotation divergence (an annotated dict that returns a list at
  runtime) — guarded by pyright/mypy strict, not by this gate.
- Symbol resolution is name-level over the scanned tree (no import graph):
  a return-type name is classified by the union of same-named class
  definitions found in-scope.

Exit codes:
  0 — every decorated verb annotates an allowed shape (or is allowlisted)
  2 — one or more non-allowlisted return-shape violations
  64 — usage error (bad arguments / unresolvable roots)
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_IMPORT_ROOT))

from quality_gates.allowlist_schema import load_allowlist as _load_tagged_allowlist  # noqa: E402

_SERVICE_DECORATOR = "service_interface_process"
_PLUGIN_DECORATOR = "platform_process"
_DECORATOR_NAMES = frozenset({_SERVICE_DECORATOR, _PLUGIN_DECORATOR})

_DICT_NAMES = frozenset({"dict", "Dict"})
_UNION_NAMES = frozenset({"Optional", "Union"})
_TYPEDDICT_BASE = "TypedDict"
_DATACLASS_DECORATOR = "dataclass"

_BUNDLED_VENV_PREFIX = ".venv"


@dataclass(frozen=True)
class _Finding:
    file_path: Path
    lineno: int
    func_name: str
    decorator: str
    returns: str
    reason: str
    allowlisted: bool


@dataclass(frozen=True)
class _SymbolIndex:
    """Name-level classification of class definitions across the scanned tree."""

    typeddict_names: frozenset[str]
    dataclass_names: frozenset[str]


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _scan_roots(root: Path) -> list[Path]:
    roots = [root / "ananta" / "src"]
    roots.extend(sorted((root / "plugins").glob("*/src")))
    return [r for r in roots if r.is_dir()]


def _iter_py_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for scan_root in roots:
        for path in sorted(scan_root.rglob("*.py")):
            if any(part.startswith(_BUNDLED_VENV_PREFIX) for part in path.parts):
                continue
            files.append(path)
    return files


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        print(f"WARN: cannot parse {path}: {exc}", file=sys.stderr)
        return None


def _bare_name(expr: ast.expr) -> str | None:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


def _class_kinds(node: ast.ClassDef) -> tuple[bool, bool]:
    """(is_typeddict, is_dataclass) for one class definition."""
    is_typeddict = any(_bare_name(base) == _TYPEDDICT_BASE for base in node.bases)
    is_dataclass = False
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if _bare_name(target) == _DATACLASS_DECORATOR:
            is_dataclass = True
    return is_typeddict, is_dataclass


def _build_symbol_index(trees: dict[Path, ast.Module]) -> _SymbolIndex:
    typeddicts: set[str] = set()
    dataclasses_found: set[str] = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            is_typeddict, is_dataclass = _class_kinds(node)
            if is_typeddict:
                typeddicts.add(node.name)
            if is_dataclass:
                dataclasses_found.add(node.name)
    return _SymbolIndex(
        typeddict_names=frozenset(typeddicts),
        dataclass_names=frozenset(dataclasses_found),
    )


def _decorator_kind(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = _bare_name(target)
        if name in _DECORATOR_NAMES:
            return name
    return None


_UNION_REASON = "union return — the non-dict arm dies at dispatch"


def _classify_string_annotation(
    value: str, decorator: str, index: _SymbolIndex,
) -> str | None:
    """Parse a quoted annotation and classify its inner expression."""
    try:
        inner = ast.parse(value, mode="eval").body
    except SyntaxError:
        return "unparseable string annotation"
    return _classify_return(inner, decorator, index)


def _classify_subscript(annotation: ast.Subscript) -> str | None:
    """Classify a subscripted annotation (``dict[...]`` / ``Optional[...]`` / other)."""
    head = _bare_name(annotation.value)
    if head in _DICT_NAMES:
        return None
    if head in _UNION_NAMES:
        return _UNION_REASON
    return f"subscripted non-dict return '{head}'"


def _classify_symbol(name: str, decorator: str, index: _SymbolIndex) -> str | None:
    """Classify a bare/dotted name via the name-level symbol index.

    R1 collision hardening: resolution is name-level (no import graph), so a
    name defined as a TypedDict in one module and a dataclass in another is
    AMBIGUOUS. On the service path both kinds are allowed, so the collision
    is harmless; on the plugin path a dataclass return is dead at dispatch,
    so an ambiguous name FAILS CLOSED (real in-tree collisions exist:
    ColumnDefinition, PluginConfig, ValidationResult).
    """
    if name in _DICT_NAMES:
        return None
    is_typeddict = name in index.typeddict_names
    is_dataclass = name in index.dataclass_names
    if is_typeddict and is_dataclass and decorator != _SERVICE_DECORATOR:
        return (
            f"ambiguous return '{name}' (TypedDict AND dataclass definitions "
            "in-tree) on the plugin path — fail closed"
        )
    if is_typeddict:
        return None  # TypedDict IS a plain dict at runtime
    if is_dataclass:
        if decorator == _SERVICE_DECORATOR:
            return None  # auto-serialized at action_processor.py:844
        return "dataclass return on the plugin path (no auto-serialization)"
    return f"non-dict return '{name}'"


def _classify_return(
    annotation: ast.expr, decorator: str, index: _SymbolIndex,
) -> str | None:
    """None when the annotation satisfies the dispatch contract, else a reason."""
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return _classify_string_annotation(annotation.value, decorator, index)
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _UNION_REASON
    if isinstance(annotation, ast.Subscript):
        return _classify_subscript(annotation)
    name = _bare_name(annotation)
    if name is None:
        return "unrecognized return annotation shape"
    return _classify_symbol(name, decorator, index)


def _load_allowlist(path: Path) -> frozenset[tuple[str, str]]:
    """Read the `<file_path>::<function_name>` register (radon_cc convention).

    Mandatory owner/reason/expires schema (D-3-structural, iss_23fa51b5).
    """
    entries: set[tuple[str, str]] = set()
    for key in _load_tagged_allowlist(path):
        if "::" not in key:
            print(
                f"WARN: malformed allowlist line (missing '::'): {key!r}",
                file=sys.stderr,
            )
            continue
        file_part, func_part = key.split("::", 1)
        entries.add((file_part.strip(), func_part.strip()))
    return frozenset(entries)


def _is_allowlisted(
    file_path: Path, func_name: str, allowlist: frozenset[tuple[str, str]],
) -> bool:
    candidate = file_path.as_posix()
    return any(
        candidate.endswith(entry_file) and func_name == entry_func
        for entry_file, entry_func in allowlist
    )


def _scan(
    trees: dict[Path, ast.Module],
    index: _SymbolIndex,
    allowlist: frozenset[tuple[str, str]],
    raw: bool,
) -> tuple[int, list[_Finding]]:
    """(decorated_def_count, findings) across every parsed tree."""
    decorated_count = 0
    findings: list[_Finding] = []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorator = _decorator_kind(node)
            if decorator is None:
                continue
            decorated_count += 1
            if node.returns is None:
                reason: str | None = "missing return annotation"
            else:
                reason = _classify_return(node.returns, decorator, index)
            if reason is None:
                continue
            returns = ast.unparse(node.returns) if node.returns else "<missing>"
            allowlisted = (
                False if raw else _is_allowlisted(path, node.name, allowlist)
            )
            findings.append(
                _Finding(
                    file_path=path,
                    lineno=node.lineno,
                    func_name=node.name,
                    decorator=decorator,
                    returns=returns,
                    reason=reason,
                    allowlisted=allowlisted,
                )
            )
    return decorated_count, findings


def _print_findings(findings: list[_Finding]) -> None:
    for f in findings:
        kind = "service_interface" if f.decorator == _SERVICE_DECORATOR else "plugin"
        marker = " [allowlisted]" if f.allowlisted else ""
        print(
            f"RETURN-SHAPE ({kind}): {f.file_path}:{f.lineno} "
            f"def {f.func_name}(...) -> {f.returns} — {f.reason}{marker}"
        )


def _report(
    decorated_count: int,
    file_count: int,
    findings: list[_Finding],
    allowlist_active: bool,
) -> int:
    if not findings:
        print(
            f"OK: {decorated_count} decorated verb(s) across {file_count} file(s), "
            "0 return-shape violations."
        )
        return 0
    total = len(findings)
    blocking = [f for f in findings if not f.allowlisted]
    if allowlist_active:
        print(
            f"\n{total} return-shape violation(s) "
            f"({total - len(blocking)} allowlisted; {len(blocking)} still failing).",
            file=sys.stderr,
        )
        return 2 if blocking else 0
    print(
        f"\n{total} return-shape violation(s) across {file_count} file(s).",
        file=sys.stderr,
    )
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help=(
            "Tracked-debt register: one `<file_path>::<function_name>` per "
            "line (POSIX-suffix-matched path, bare function name — the "
            "radon_cc_check convention). Allowlisted findings still print; "
            "they do not contribute to the exit-2 verdict. Removing an entry "
            "is the unit of remediation progress."
        ),
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help=(
            "Diagnostic flag: same measurement, allowlist IGNORED — every "
            "violation contributes to the exit-2 verdict."
        ),
    )
    return parser


def main(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)

    roots = _scan_roots(_repo_root())
    if not roots:
        print("ERROR: no scan roots found (ananta/src, plugins/*/src).", file=sys.stderr)
        return 64

    allowlist: frozenset[tuple[str, str]] = frozenset()
    allowlist_active = args.allowlist is not None and not args.raw
    if args.allowlist is not None:
        try:
            allowlist = _load_allowlist(args.allowlist)
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 64

    trees: dict[Path, ast.Module] = {}
    for path in _iter_py_files(roots):
        tree = _parse(path)
        if tree is not None:
            trees[path] = tree

    index = _build_symbol_index(trees)
    decorated_count, findings = _scan(trees, index, allowlist, args.raw)
    _print_findings(findings)
    return _report(decorated_count, len(trees), findings, allowlist_active)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
