#!/usr/bin/env python3
"""God-class detector for the solet codebase, coherence-aware edition.

Runs Python AST analysis against the files passed on the command line and
reports classes that violate Single-Responsibility-Principle heuristics
once the platform-process veneer is stripped.

The reform (per the plugin-god-class-remediation design record §5–§6 /
§8.4, dev-checkout workbench — not part of the shipped tree): the
textbook OO heuristics (LOC, method count, public method
count, instance-attribute count) all penalize raw size. In this platform's plugin
model that's the wrong signal — a *coherent* plugin can carry hundreds
of `@platform_process`-decorated methods as ad-hoc use cases get concretized
into formal processes. The platform's chosen architecture is "larger
plugins organized via submodules, bounded by coherence not method count."
So the default counting model excludes the platform-process veneer:

  - `@platform_process`-decorated methods are excluded from the
    public-method count and LOC count.
  - `@service_lifecycle`-decorated methods are excluded from the
    public-method count and LOC count (lifecycle hooks are
    interface-mandated scaffolding, not structural complexity).
  - Methods that are concrete implementations of `@abstractmethod`
    declarations in a parent interface are excluded from both counts.
    Detection is cross-file: parent class source files are parsed via
    `importlib.util.find_spec` to collect all `@abstractmethod` names;
    results are cached per module path for the duration of the run.
  - The contract-mandated `get_edge_process_definitions` body (one entry
    per EDGE process by `EdgeProcessProvider` protocol requirement) is
    excluded from the LOC count but still counted as a public method,
    because it is part of the structural class surface.

What's left is the structural / helper veneer — that's where a genuine
god class shows up (a class doing audio AND credentials AND messaging
carries large non-process helpers spanning domains). A coherent audio
plugin with 200 processes shows a tiny non-process surface.

Default thresholds (any one trips a god-class verdict):

  - LOC (non-process)            > 500    (`--loc-max`)
  - Total method count           > 300    (`--methods-max`, sanity check)
  - Public methods (non-process) > 15     (`--public-methods-max`)
  - Instance-attribute count     > 15     (`--attrs-max`)

The `--raw` flag restores the original textbook counting (everything
counted, no exclusions) for diagnostic A/B comparisons. The
`git-controller-commit` SKILL gate uses the default coherence-aware
counting.

Exit codes:
  0 — no god classes detected
  2 — one or more god classes detected
  64 — usage error (bad arguments / non-existent paths)
  70 — GATE CRASH: the analyser raised, so no verdict exists for the tree.
       A crash is NOT a violation count; see `gate_scope.GateCrashError`.

Output: human-readable lines on stdout, structured one-finding-per-line:
  GOD CLASS: <path>:<lineno> <class_name>: <metric>=<value> (>limit), ...

The script intentionally does NOT scan the whole repo unless the caller
passes the whole repo as arguments. Scoping is the caller's job — the
skill scopes by the staged file set so an in-flight commit isn't blocked
by pre-existing god classes elsewhere in the tree.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

from gate_scope import (
    GATE_CRASH_EXIT,
    GateCrashError,
    repo_python_files,
)

_REPO_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_IMPORT_ROOT))

from quality_gates.allowlist_schema import load_allowlist as _load_tagged_allowlist  # noqa: E402

_module_path_cache: dict[str, Path | None] = {}
_abstract_name_cache: dict[Path, frozenset[str]] = {}


@dataclass(frozen=True)
class Thresholds:
    loc_max: int
    methods_max: int
    public_methods_max: int
    attrs_max: int


@dataclass(frozen=True)
class Finding:
    path: Path
    lineno: int
    class_name: str
    violations: tuple[str, ...]


def _has_platform_process_decorator(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    """Return True if the method carries an `@platform_process` decorator.

    Recognizes all three syntactic forms:
      - `@platform_process` (bare Name)
      - `@platform_process(...)` (Call on Name)
      - `@foo.platform_process(...)` (Call on Attribute) — namespaced
    """
    for dec in method.decorator_list:
        target: ast.expr = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name) and target.id == "platform_process":
            return True
        if isinstance(target, ast.Attribute) and target.attr == "platform_process":
            return True
    return False


def _has_service_lifecycle_decorator(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    """Return True if the method carries a `@service_lifecycle` decorator."""
    for dec in method.decorator_list:
        target: ast.expr = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name) and target.id == "service_lifecycle":
            return True
        if isinstance(target, ast.Attribute) and target.attr == "service_lifecycle":
            return True
    return False


def _decorator_is_abstractmethod(dec: ast.expr) -> bool:
    """Return True if dec is an `@abstractmethod` decorator node."""
    target: ast.expr = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Name) and target.id == "abstractmethod":
        return True
    if isinstance(target, ast.Attribute) and target.attr == "abstractmethod":
        return True
    return False


def _find_module_source_path(module_name: str) -> Path | None:
    """Resolve a dotted module name to its source `.py` path via importlib."""
    if module_name in _module_path_cache:
        return _module_path_cache[module_name]
    try:
        spec = importlib.util.find_spec(module_name)
    except (ModuleNotFoundError, ValueError):
        _module_path_cache[module_name] = None
        return None
    path: Path | None = Path(spec.origin) if (spec and spec.origin) else None
    _module_path_cache[module_name] = path
    return path


def _parse_abstract_names_from_path(source_path: Path) -> frozenset[str]:
    """Return the set of `@abstractmethod` method names declared in source_path."""
    if source_path in _abstract_name_cache:
        return _abstract_name_cache[source_path]
    try:
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))
    except (OSError, UnicodeDecodeError, SyntaxError):
        _abstract_name_cache[source_path] = frozenset()
        return frozenset()
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(_decorator_is_abstractmethod(dec) for dec in node.decorator_list):
            names.add(node.name)
    result = frozenset(names)
    _abstract_name_cache[source_path] = result
    return result


def _build_import_map(tree: ast.Module) -> dict[str, str]:
    """Build `{local_name: module_name}` from all `from X import Y` statements."""
    result: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        for alias in node.names:
            local_name = alias.asname if alias.asname else alias.name
            result[local_name] = node.module
    return result


def _base_class_name(base: ast.expr) -> str | None:
    """Extract the simple name string from a base class AST expression."""
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return None


def _collect_interface_method_names(
    class_node: ast.ClassDef,
    import_map: dict[str, str],
) -> frozenset[str]:
    """Union of @abstractmethod names from all importable parent classes."""
    names: set[str] = set()
    for base in class_node.bases:
        base_name = _base_class_name(base)
        if not base_name:
            continue
        module_name = import_map.get(base_name)
        if not module_name:
            continue
        source_path = _find_module_source_path(module_name)
        if not source_path:
            continue
        names.update(_parse_abstract_names_from_path(source_path))
    return frozenset(names)


def _function_extent_loc(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """LOC spanned by a function definition including its decorator block.

    The decorator block sits visually and logically above the `def` line.
    For `@platform_process`-decorated methods on plugin classes, the
    decorator carries the bulk of the prose (parameters / return_value_schema
    / examples / etc., all destined for JSON migration). Treating the
    decorator + def + body as one coherent unit matches `god_class_check.py`'s
    coherence-aware framing — exclude the whole process surface, not just
    the executable body.
    """
    end = fn.end_lineno if fn.end_lineno is not None else fn.lineno
    start = fn.lineno
    for dec in fn.decorator_list:
        if dec.lineno < start:
            start = dec.lineno
    return end - start + 1


def _instance_attrs(class_node: ast.ClassDef) -> set[str]:
    """Return the set of `self.<attr>` assignment targets across the class body."""
    attrs: set[str] = set()
    for node in ast.walk(class_node):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets: list[ast.expr] = (
            list(node.targets) if isinstance(node, ast.Assign) else [node.target]
        )
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                attrs.add(target.attr)
    return attrs


def _class_total_loc(node: ast.ClassDef) -> int:
    """Total LOC spanned by a class body, defensively handling end_lineno=None."""
    end = node.end_lineno if node.end_lineno is not None else node.lineno
    return end - node.lineno + 1


def _class_methods(
    node: ast.ClassDef,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Direct (non-inherited) methods on the class body."""
    return [
        n
        for n in node.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _split_process_methods(
    methods: list[ast.FunctionDef | ast.AsyncFunctionDef],
    interface_method_names: frozenset[str] = frozenset(),
) -> tuple[
    list[ast.FunctionDef | ast.AsyncFunctionDef],
    list[ast.FunctionDef | ast.AsyncFunctionDef],
    list[ast.FunctionDef | ast.AsyncFunctionDef],
]:
    """Partition methods into (loc_pub_excluded, loc_only_excluded, non_excluded).

    loc_pub_excluded — stripped from both LOC and public-method count:
      @platform_process methods, @service_lifecycle methods, and concrete
      implementations of @abstractmethod interface requirements.

    loc_only_excluded — stripped from LOC but still counted as public methods:
      `get_edge_process_definitions` (contract-mandated EdgeProcessProvider boilerplate).

    non_excluded — structural surface the gate actually measures:
      lifecycle helpers, private helpers, and other class-specific methods.
    """
    loc_pub_excluded: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    loc_only_excluded: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    excluded_ids: set[int] = set()

    for m in methods:
        if (
            _has_platform_process_decorator(m)
            or _has_service_lifecycle_decorator(m)
            or m.name in interface_method_names
        ):
            loc_pub_excluded.append(m)
            excluded_ids.add(id(m))
        elif m.name == "get_edge_process_definitions":
            loc_only_excluded.append(m)
            excluded_ids.add(id(m))

    non_excluded = [m for m in methods if id(m) not in excluded_ids]
    return loc_pub_excluded, loc_only_excluded, non_excluded


def _compute_metrics(
    node: ast.ClassDef,
    methods: list[ast.FunctionDef | ast.AsyncFunctionDef],
    raw: bool,
    import_map: dict[str, str],
) -> tuple[int, int]:
    """Return (loc, public_method_count) under raw or coherence-aware counting."""
    total_loc = _class_total_loc(node)
    if raw:
        public = sum(1 for m in methods if not m.name.startswith("_"))
        return total_loc, public
    interface_method_names = _collect_interface_method_names(node, import_map)
    loc_pub_excluded, loc_only_excluded, _ = _split_process_methods(
        methods, interface_method_names
    )
    excluded_loc = sum(_function_extent_loc(m) for m in loc_pub_excluded) + sum(
        _function_extent_loc(m) for m in loc_only_excluded
    )
    loc = max(0, total_loc - excluded_loc)
    # get_edge_process_definitions is in loc_only_excluded — still a public method
    loc_pub_excluded_ids = {id(m) for m in loc_pub_excluded}
    public = sum(
        1 for m in methods
        if not m.name.startswith("_") and id(m) not in loc_pub_excluded_ids
    )
    return loc, public


def _violation_for(metric: str, value: int, limit: int) -> str | None:
    """Format `metric=value (>limit)` if value exceeds limit, otherwise None."""
    if value > limit:
        return f"{metric}={value} (>{limit})"
    return None


def _check_class(
    node: ast.ClassDef,
    thresholds: Thresholds,
    raw: bool,
    import_map: dict[str, str],
) -> tuple[str, ...]:
    methods = _class_methods(node)
    loc, public_method_count = _compute_metrics(node, methods, raw, import_map)
    attrs = _instance_attrs(node)

    candidates = [
        _violation_for("loc", loc, thresholds.loc_max),
        _violation_for("methods", len(methods), thresholds.methods_max),
        _violation_for("public_methods", public_method_count, thresholds.public_methods_max),
        _violation_for("instance_attrs", len(attrs), thresholds.attrs_max),
    ]
    return tuple(v for v in candidates if v is not None)


def _scan(path: Path, thresholds: Thresholds, raw: bool) -> list[Finding]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"WARN: cannot read {path}: {exc}", file=sys.stderr)
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        print(f"WARN: syntax error in {path}: {exc}", file=sys.stderr)
        return []
    except Exception as exc:  # noqa: BLE001 — any analyser failure is a non-verdict
        raise GateCrashError(path, f"{type(exc).__name__} in ast.parse: {exc}") from exc
    import_map = {} if raw else _build_import_map(tree)
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        violations = _check_class(node, thresholds, raw, import_map)
        if violations:
            findings.append(Finding(path, node.lineno, node.name, violations))
    return findings


def _expand_targets(raw_paths: list[str]) -> list[Path]:
    """Resolve CLI path arguments to concrete `.py` files.

    A DIRECTORY expands to its in-repo `.py` files only (tracked, or
    untracked and not ignored). Measured
    2026-08-16: a bare run over `plugins/cosyvoice2_tts_plugin` reached
    18,321 files in `src/.venv_cosyvoice`, reported sympy's C-grade
    functions as this repo's findings, and then crashed on one of them.

    A file named EXPLICITLY is always scanned, in-repo or not — the caller
    asked for that path by name, and a brand-new file is the normal case.
    """
    out: list[Path] = []
    for raw in raw_paths:
        p = Path(raw)
        if not p.exists():
            continue
        if p.is_dir():
            out.extend(repo_python_files(p))
        elif p.suffix == ".py":
            out.append(p)
    return out


def _load_allowlist(path: Path) -> frozenset[tuple[str, str]]:
    """Read ``<repo-relative-path>::<class-name>`` debt entries.

    The allowlist is a tracked-debt register documenting classes whose
    decomposition is deferred (per the plugin-god-class-remediation
    design record §9.D–§9.Z + Task #74, dev-checkout workbench — not
    part of the shipped tree). Allowlisted findings are still printed in the
    report — the gate stays honest about known debt — but they do NOT
    contribute to the exit-2 verdict. Removing an entry from this list
    is the unit of remediation progress.

    Mandatory owner/reason/expires schema (D-3-structural, iss_23fa51b5).
    """
    entries: set[tuple[str, str]] = set()
    for key in _load_tagged_allowlist(path):
        entry_path, separator, class_name = key.rpartition("::")
        if not separator or not entry_path or not class_name:
            raise ValueError(
                "malformed god-class allowlist entry "
                f"(need <repo-relative-path>::<class-name>): {key!r}"
            )
        entries.add((entry_path, class_name))
    return frozenset(entries)


def _is_allowlisted(finding: Finding, allowlist: frozenset[tuple[str, str]]) -> bool:
    """Return whether a finding matches its exact path-qualified debt entry."""
    finding_path = finding.path.as_posix()
    return any(
        finding.class_name == class_name
        and (finding_path == entry_path or finding_path.endswith(f"/{entry_path}"))
        for entry_path, class_name in allowlist
    )


def _print_findings(
    findings: list[Finding], allowlist: frozenset[tuple[str, str]],
) -> None:
    for finding in findings:
        joined = ", ".join(finding.violations)
        marker = " [allowlisted]" if _is_allowlisted(finding, allowlist) else ""
        print(
            f"GOD CLASS: {finding.path}:{finding.lineno} "
            f"{finding.class_name}: {joined}{marker}"
        )


def _summarize(
    findings: list[Finding],
    allowlist: frozenset[tuple[str, str]],
    allowlist_active: bool,
    target_count: int,
) -> int:
    if not findings:
        print(f"OK: {target_count} file(s) scanned, 0 god-class violations.")
        return 0
    total = len(findings)
    if allowlist_active:
        allowlisted = sum(1 for finding in findings if _is_allowlisted(finding, allowlist))
        failing = total - allowlisted
        print(
            f"\n{total} god-class violation(s) "
            f"({allowlisted} allowlisted; {failing} still failing).",
            file=sys.stderr,
        )
        return 2 if failing > 0 else 0
    print(
        f"\n{total} god-class violation(s) across {target_count} file(s).",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="+",
        help="Python files or directories to scan (recursive for dirs).",
    )
    parser.add_argument("--loc-max", type=int, default=500)
    parser.add_argument("--methods-max", type=int, default=300)
    parser.add_argument("--public-methods-max", type=int, default=15)
    parser.add_argument("--attrs-max", type=int, default=15)
    parser.add_argument(
        "--raw",
        action="store_true",
        help=(
            "Restore the textbook counting model: count @platform_process "
            "methods and the get_edge_process_definitions body in both LOC "
            "and public-method totals. Use for diagnostic A/B comparison; "
            "the SKILL gate uses the default coherence-aware counting."
        ),
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help=(
            "Path to a tracked-debt register file listing class names that "
            "should be reported but not block (one class name per line; "
            "blank lines and `#` comments ignored). Allowlisted findings "
            "are still printed so the gate stays honest; they just do not "
            "contribute to the exit-2 verdict. Removing an entry from the "
            "allowlist is the unit of remediation progress. See the "
            "plugin-god-class-remediation design record "
            "§9.D–§9.Z + Task #74."
        ),
    )
    args = parser.parse_args(argv)

    thresholds = Thresholds(
        loc_max=args.loc_max,
        methods_max=args.methods_max,
        public_methods_max=args.public_methods_max,
        attrs_max=args.attrs_max,
    )

    allowlist: frozenset[tuple[str, str]] = frozenset()
    allowlist_active = args.allowlist is not None
    if allowlist_active:
        try:
            allowlist = _load_allowlist(args.allowlist)
        except (FileNotFoundError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 64

    try:
        targets = _expand_targets(args.paths)
        if not targets:
            print("ERROR: no Python files to scan from given paths.", file=sys.stderr)
            return 64
        findings: list[Finding] = []
        for target in targets:
            findings.extend(_scan(target, thresholds, args.raw))
    except GateCrashError as crash:
        print(f"GATE-CRASH: {crash}", file=sys.stderr)
        print(
            "No verdict was produced. This is NOT a violation count: the "
            "analyser aborted, so this run measured nothing about the tree.",
            file=sys.stderr,
        )
        return GATE_CRASH_EXIT

    _print_findings(findings, allowlist)
    return _summarize(findings, allowlist, allowlist_active, len(targets))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
