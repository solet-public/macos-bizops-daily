#!/usr/bin/env python3
"""Service-interface AST consistency gate.

Three-check static analysis of the platform's service-interface ABCs +
their KB JSON manifests. See the service-interface-AST-gate design
record v1 (dev-checkout workbench — not part of the shipped tree) for
the full design + KB precedents.

Checks (a/b/c, per design §1):

  (a) For every ``@service_interface_process(name="X")``-decorated
      ``FunctionDef`` in any ``ananta/src/ananta/services/<service>/
      interfaces/public.py``, the decorator's ``name=`` argument must
      match the function's ``__name__``. Mismatch indicates decorator-
      stacking drift (Tier α 2026-06-11 backfill orphan incident) or
      copy-paste rename error.

  (b) Every ``@abstractmethod``-decorated ``FunctionDef`` in a
      service-interface ABC (class name suffix ``API``) must also
      carry ``@service_interface_process``. Bare ``@abstractmethod``
      indicates a silently-orphaned verb (Tier α backfill direction)
      or a missed registration on a newly authored verb. **Strict —
      no underscore-prefix exemption** per design §1.2 + §8.1; rely
      on the allowlist for legitimate non-verb abstract helpers.

  (c) Every JSON file at ``ananta/knowledge_base/processes/<provider>/
      <name>.json`` must match a registered
      ``@service_interface_process(name="<name>", provider="<provider>")``
      somewhere in the AST sweep. Orphan JSON indicates a verb renamed
      or removed without sweeping the JSON (kb_overlay_loader silent
      skip, per design §0.2).

The gate joins the CRITICAL bucket in ``code_quality_check.py``
alongside ``god_class``, ``radon_cc``, ``radon_mi``, ``whole_tree_
integration``. Exit codes: 0 clean, 2 blocking findings, 64 invocation
error. See design §3.

Allowlist semantics mirror the existing gates (god_class, radon_mi,
radon_cc, whole_tree_integration). The allowlist is a tracked-debt
register per ``20_security_and_sandboxing/01_security_scanning_phase_1
_static.md``; entries are still printed in the report but don't
contribute to the exit-2 verdict.

Allowlist entry shape per check (design §4.2):

  (a) ``a::<file_posix_path>::<class_name>::<function_name>``
  (b) ``b::<file_posix_path>::<class_name>::<function_name>``
  (c) ``c::<provider>::<json_stem>``

Suffix-match on file path; exact match on class/function/provider/stem.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

SERVICES_GLOB = "ananta/src/ananta/services/*/interfaces/public.py"
_PROCESSES_RELPATH = Path("ananta") / "knowledge_base" / "processes"


def _repo_root() -> Path:
    """Resolve the repo root used for both anchors.

    Uses ``Path.cwd()`` so callers (the wrapper + the smoke fixture) can
    isolate the gate to a tmp directory by setting cwd. Default workflow
    runs from the repo root, matching the existing god_class/radon_mi
    gate conventions.
    """
    return Path.cwd()

_WRAPPER_OK = 0
_WRAPPER_BLOCKING = 2
_WRAPPER_USAGE_ERROR = 64


@dataclass(frozen=True)
class Finding:
    """One gate finding. ``identifier`` is the allowlist match key."""

    check: str  # "a" / "b" / "c"
    file: Path  # POSIX-relative path (or JSON path)
    line: int
    identifier: str  # allowlist-matchable key per design §4.2
    message: str
    recommend: str


def _is_service_interface_process(dec: ast.expr) -> bool:
    """True if ``dec`` is a ``@service_interface_process(...)`` Call."""
    if not isinstance(dec, ast.Call):
        return False
    target = dec.func
    if isinstance(target, ast.Name):
        return target.id == "service_interface_process"
    if isinstance(target, ast.Attribute):
        return target.attr == "service_interface_process"
    return False


def _is_abstractmethod(dec: ast.expr) -> bool:
    """True if ``dec`` is a bare ``@abstractmethod`` decorator."""
    if isinstance(dec, ast.Name):
        return dec.id == "abstractmethod"
    if isinstance(dec, ast.Attribute):
        return dec.attr == "abstractmethod"
    return False


def _decorator_keyword_value(
    dec: ast.Call, key: str, constants: dict[str, str] | None = None
) -> str | None:
    """Extract a string-keyword from a decorator Call.

    Handles two shapes:

      (1) ``key="literal"`` — string-constant value.
      (2) ``key=NAME`` — Name reference to a module-level ``NAME = "literal"``
          assignment. Resolved via the optional ``constants`` table built by
          ``_collect_module_string_constants``.

    Returns ``None`` if the keyword is absent OR the value is non-string OR
    a Name that doesn't resolve in the constants table (no false positive —
    the caller treats unresolvable provider as "skip from registered set").
    """
    for kw in dec.keywords:
        if kw.arg != key:
            continue
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
        if isinstance(kw.value, ast.Name) and constants is not None:
            return constants.get(kw.value.id)
    return None


def _collect_module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Walk top-level ``NAME = "literal"`` assignments; return Name→str table.

    Handles the canonical ``_PROVIDER = "session_ledger_service"`` /
    ``PROVIDER = "discovery_service"`` pattern used in every service's
    ``interfaces/public.py`` for the ``@service_interface_process(provider=_PROVIDER)``
    decorator argument. Empirically every services/*/interfaces/public.py
    uses this pattern (2026-06-12 grep), so the lookup is load-bearing for
    (c) to be true.
    """
    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not (
            isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        ):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = node.value.value
    return constants


def _service_public_py_files() -> list[Path]:
    """Enumerate every interface .py file under ``services/*/interfaces/``.

    Post-2026-06-14 ABC decomposition (W5.Q+R+S+T pattern): the
    ``@service_interface_process`` decorators may live on the aggregate
    ``public.py`` OR on sub-ABC sibling files like ``lifecycle.py``,
    ``search.py``, etc. Pre-decomposition services keep all decorators on
    ``public.py`` and that path is still covered by the broader glob.
    Excludes ``__init__.py`` (no abstract surface). The aggregation in
    ``main()`` unions ``registered`` sets across every returned file, so
    the (c) orphan-JSON check sees the union surface regardless of which
    file each decorator lives in.
    """
    interfaces_glob = "ananta/src/ananta/services/*/interfaces/*.py"
    return [
        path
        for path in sorted(_repo_root().glob(interfaces_glob))
        if path.name != "__init__.py"
    ]


def _process_json_files() -> list[Path]:
    """Enumerate every JSON file under ``ananta/knowledge_base/processes/``."""
    processes_dir = _repo_root() / _PROCESSES_RELPATH
    if not processes_dir.exists():
        return []
    return sorted(processes_dir.glob("*/*.json"))


def _safe_parse(public_py: Path, posix: str) -> tuple[ast.Module | None, Finding | None]:
    """Parse ``public_py``; return (tree, None) or (None, parse-error Finding)."""
    try:
        tree = ast.parse(public_py.read_text(encoding="utf-8"), filename=str(public_py))
    except SyntaxError as exc:
        return None, Finding(
            check="a",
            file=public_py,
            line=exc.lineno or 1,
            identifier=f"a::{posix}::<parse_error>",
            message=f"SyntaxError parsing {posix}: {exc.msg}",
            recommend="Fix the syntax error before the AST gate can analyze this file.",
        )
    return tree, None


def _check_a_name_match(
    sip: ast.Call,
    item: ast.FunctionDef | ast.AsyncFunctionDef,
    class_node: ast.ClassDef,
    public_py: Path,
    posix: str,
    constants: dict[str, str],
) -> tuple[Finding | None, tuple[str, str] | None]:
    """Run check (a) for one decorated method.

    Returns (finding_or_None, registered_entry_or_None). ``registered_entry``
    is ``(provider, item.name)`` when the method's
    ``@service_interface_process`` carries a resolvable provider — added
    to the caller's registered-set for the (c) join. Runtime registration
    uses ``func.__name__`` per ``service_interface_decorator.py:234`` +
    ``service_interface_scanner.py:199``.
    """
    sip_name = _decorator_keyword_value(sip, "name", constants)
    provider = _decorator_keyword_value(sip, "provider", constants)
    registered_entry = (provider, item.name) if provider is not None else None
    if sip_name is None or sip_name == item.name:
        return None, registered_entry
    finding = Finding(
        check="a",
        file=public_py,
        line=item.lineno,
        identifier=f"a::{posix}::{class_node.name}::{item.name}",
        message=(
            f'@service_interface_process(name="{sip_name}") '
            f"on def {item.name} — decorator/function-name mismatch"
        ),
        recommend=(
            f"Either rename the function to {sip_name!r} or "
            f"update the decorator to name={item.name!r}."
        ),
    )
    return finding, registered_entry


def _check_b_bare_abstractmethod(
    item: ast.FunctionDef | ast.AsyncFunctionDef,
    class_node: ast.ClassDef,
    public_py: Path,
    posix: str,
) -> Finding:
    """Build the (b) finding for a bare ``@abstractmethod`` in a service-interface ABC."""
    return Finding(
        check="b",
        file=public_py,
        line=item.lineno,
        identifier=f"b::{posix}::{class_node.name}::{item.name}",
        message=(
            f"@abstractmethod {item.name} in {class_node.name} "
            f"lacks @service_interface_process"
        ),
        recommend=(
            "Add @service_interface_process(name=..., provider=..., "
            "parameters=..., return_value_schema=...) above @abstractmethod. "
            "If this is a legitimate non-verb abstract helper, add to "
            "quality_gates/service_interface_ast_allowlist.txt with a "
            "comment explaining why."
        ),
    )


def _check_method_decorators(
    item: ast.FunctionDef | ast.AsyncFunctionDef,
    class_node: ast.ClassDef,
    public_py: Path,
    posix: str,
    constants: dict[str, str],
) -> tuple[list[Finding], tuple[str, str] | None]:
    """Inspect one method's decorators; dispatch to (a) + (b) checks.

    Returns (findings, registered_entry-or-None). ``registered_entry`` is
    forwarded from ``_check_a_name_match`` (None when the method has no
    valid ``@service_interface_process`` with a resolvable provider).
    """
    findings: list[Finding] = []
    sip = next(
        (dec for dec in item.decorator_list if _is_service_interface_process(dec)),
        None,
    )
    is_abstract = any(_is_abstractmethod(dec) for dec in item.decorator_list)
    registered_entry: tuple[str, str] | None = None

    if sip is not None:
        assert isinstance(sip, ast.Call)
        a_finding, registered_entry = _check_a_name_match(
            sip, item, class_node, public_py, posix, constants
        )
        if a_finding is not None:
            findings.append(a_finding)
    elif is_abstract:
        findings.append(_check_b_bare_abstractmethod(item, class_node, public_py, posix))

    return findings, registered_entry


def _check_class(
    class_node: ast.ClassDef,
    public_py: Path,
    posix: str,
    constants: dict[str, str],
) -> tuple[list[Finding], set[tuple[str, str]]]:
    """Walk one service-interface ABC's methods; dispatch to _check_method_decorators."""
    findings: list[Finding] = []
    registered: set[tuple[str, str]] = set()
    for item in class_node.body:
        if not isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        method_findings, registered_entry = _check_method_decorators(
            item, class_node, public_py, posix, constants
        )
        findings.extend(method_findings)
        if registered_entry is not None:
            registered.add(registered_entry)
    return findings, registered


def _is_service_interface_abc(node: ast.stmt) -> bool:
    """True if ``node`` is a ``ClassDef`` whose name marks it a service-interface ABC."""
    return isinstance(node, ast.ClassDef) and node.name.endswith("API")


def _check_public_py(public_py: Path) -> tuple[list[Finding], set[tuple[str, str]]]:
    """Walk a single ``interfaces/public.py``; emit (a)+(b) findings + collect (provider, name) set."""
    findings: list[Finding] = []
    registered: set[tuple[str, str]] = set()
    posix = public_py.relative_to(_repo_root()).as_posix()

    tree, parse_error = _safe_parse(public_py, posix)
    if tree is None:
        assert parse_error is not None
        return [parse_error], registered

    constants = _collect_module_string_constants(tree)
    for class_node in tree.body:
        if not _is_service_interface_abc(class_node):
            continue
        assert isinstance(class_node, ast.ClassDef)
        class_findings, class_registered = _check_class(
            class_node, public_py, posix, constants
        )
        findings.extend(class_findings)
        registered.update(class_registered)

    return findings, registered


def _check_orphan_jsons(registered: set[tuple[str, str]]) -> list[Finding]:
    """Walk every ``processes/<provider>/<name>.json``; emit (c) findings."""
    findings: list[Finding] = []
    for json_path in _process_json_files():
        provider = json_path.parent.name
        name = json_path.stem
        if (provider, name) not in registered:
            findings.append(
                Finding(
                    check="c",
                    file=json_path,
                    line=1,
                    identifier=f"c::{provider}::{name}",
                    message=(
                        f"orphan JSON: processes/{provider}/{name}.json has no "
                        f"matching @service_interface_process decorator"
                    ),
                    recommend=(
                        f"Either restore the decorator at ananta/src/ananta/"
                        f"services/{provider}/interfaces/public.py, or delete "
                        f"the JSON file (per [[no-phantom-abstractions]])."
                    ),
                )
            )
    return findings


def _load_allowlist(path: Path) -> frozenset[str]:
    """Read the allowlist; one entry per line; ``#`` comments + blanks ignored."""
    if not path.exists():
        raise FileNotFoundError(f"allowlist file not found: {path}")
    entries: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        entries.add(line)
    return frozenset(entries)


def _is_allowlisted(identifier: str, allowlist: frozenset[str]) -> bool:
    """Match per design §4.2 — structured per-segment.

    Identifier shape:
      * ``a::<file_posix>::<class>::<func>`` for check (a)
      * ``b::<file_posix>::<class>::<func>`` for check (b)
      * ``c::<provider>::<json_stem>`` for check (c)

    Match semantics:
      * (a)/(b): allowlist entry's ``<file_posix>`` is a POSIX-suffix of
        the identifier's ``<file_posix>``; class + func match exactly.
      * (c): exact match on the whole identifier.
    """
    if identifier.startswith("c::"):
        return identifier in allowlist
    id_parts = identifier.split("::")
    if len(id_parts) < 4:
        return identifier in allowlist
    id_check, id_file, id_class, id_func = id_parts[0], id_parts[1], id_parts[2], id_parts[3]
    for entry in allowlist:
        entry_parts = entry.split("::")
        if len(entry_parts) != 4:
            continue
        e_check, e_file, e_class, e_func = entry_parts
        if (
            e_check == id_check
            and e_class == id_class
            and e_func == id_func
            and (id_file == e_file or id_file.endswith("/" + e_file))
        ):
            return True
    return False


def _print_findings(findings: list[Finding], allowlist: frozenset[str]) -> None:
    repo_root = _repo_root()
    for f in findings:
        posix = (
            f.file.relative_to(repo_root).as_posix()
            if f.file.is_absolute() and repo_root in f.file.parents
            else str(f.file)
        )
        marker = " [allowlisted]" if _is_allowlisted(f.identifier, allowlist) else ""
        print(f"{posix}:{f.line}")
        print(f"  CHECK ({f.check}) — {f.message}{marker}")
        print(f"  RECOMMEND: {f.recommend}")


def _summarize(
    findings: list[Finding], allowlist: frozenset[str], allowlist_active: bool
) -> int:
    if not findings:
        print(
            "OK: service-interface AST gate clean "
            "(0 violations across all 3 checks)."
        )
        return _WRAPPER_OK
    total = len(findings)
    if allowlist_active:
        allowlisted = sum(1 for f in findings if _is_allowlisted(f.identifier, allowlist))
        failing = total - allowlisted
        print(
            f"\n{total} service-interface AST finding(s) "
            f"({allowlisted} allowlisted; {failing} still failing).",
            file=sys.stderr,
        )
        return _WRAPPER_BLOCKING if failing > 0 else _WRAPPER_OK
    print(
        f"\n{total} service-interface AST finding(s).",
        file=sys.stderr,
    )
    return _WRAPPER_BLOCKING


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help=(
            "Path to a tracked-debt register file listing identifiers that "
            "should be reported but not block. See design v1 §4.2 for the "
            "per-check entry shape. Allowlisted findings are still printed "
            "so the gate stays honest; they just don't contribute to the "
            "exit-2 verdict. Adding a new entry is not a scanner fix."
        ),
    )
    args = parser.parse_args(argv)

    allowlist: frozenset[str] = frozenset()
    allowlist_active = args.allowlist is not None
    if allowlist_active:
        try:
            allowlist = _load_allowlist(args.allowlist)
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return _WRAPPER_USAGE_ERROR

    all_findings: list[Finding] = []
    all_registered: set[tuple[str, str]] = set()
    for public_py in _service_public_py_files():
        per_file_findings, per_file_registered = _check_public_py(public_py)
        all_findings.extend(per_file_findings)
        all_registered.update(per_file_registered)

    all_findings.extend(_check_orphan_jsons(all_registered))

    _print_findings(all_findings, allowlist)
    return _summarize(all_findings, allowlist, allowlist_active)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
