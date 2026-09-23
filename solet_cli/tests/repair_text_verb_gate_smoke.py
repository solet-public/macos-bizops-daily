#!/usr/bin/env python3
"""Gate manager repair prose against the parser's registered verb surface."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO = Path(__file__).resolve().parents[2]
_MANAGER_SOURCE = _REPO / "solet_cli" / "src" / "solet_manager"
sys.path.insert(0, str(_MANAGER_SOURCE.parent))

from solet_manager.cli import build_parser  # noqa: E402

_EXPLICIT_INVOCATION = re.compile(r"\bsolet\s+([a-z][a-z0-9-]*)\b")
_ACTION_WORD = re.compile(
    r"\b(abandon|resume|repair|inspect|reconcile|doctor|start|create|status|list)\b",
    re.IGNORECASE,
)
_ACTION_TO_VERBS: dict[str, frozenset[str]] = {
    "abandon": frozenset({"abandon"}),
    "repair": frozenset({"repair"}),
    "inspect": frozenset({"inspect"}),
    "reconcile": frozenset({"reconcile-contract", "reconcile-adapter", "reconcile-identity"}),
    "doctor": frozenset({"doctor"}),
    "start": frozenset({"start"}),
    "create": frozenset({"create"}),
    "status": frozenset({"status"}),
    "list": frozenset({"list"}),
}
_ACTION_EXEMPTIONS = {
    "resume": "Resume is a documented create re-invocation, not a standalone manager verb.",
}
# Dated bridge: landing 3 removes this after `solet abandon` is registered and
# transaction.py carries an explicit dry-run command (iss_be0ae962, 2026-09-08).
_SITE_ACTION_EXEMPTIONS = {
    ("transaction.py", 633): "abandon",
}
@dataclass(frozen=True)
class RepairSite:
    """One statically visible manager ``repair=`` carrier."""

    relative_path: str
    line: int
    expression: str
    unresolved_name: str | None = None

    @property
    def label(self) -> str:
        return f"{self.relative_path}:{self.line}"


def _registered_verbs() -> frozenset[str]:
    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return frozenset(subparsers.choices)


def _assignment_parts(statement: ast.stmt) -> tuple[ast.expr | None, list[ast.expr]]:
    if isinstance(statement, ast.Assign):
        return statement.value, statement.targets
    if isinstance(statement, ast.AnnAssign):
        return statement.value, [statement.target]
    return None, []


def _module_string_bindings(tree: ast.Module) -> dict[str, str]:
    """Return module constants whose repair prose is statically inspectable."""
    bindings: dict[str, str] = {}
    for node in tree.body:
        value, targets = _assignment_parts(node)
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bindings[target.id] = value.value
    return bindings


def _function_binding(node: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> str | None:
    for statement in node.body:
        value, targets = _assignment_parts(statement)
        if value is not None and any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return ast.unparse(value)
    return None


def _local_bindings(tree: ast.Module, line: int, name: str) -> str | None:
    """Return one enclosing function's direct binding for a bare repair name."""
    matches: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.end_lineno is not None:
            if node.lineno <= line <= node.end_lineno:
                binding = _function_binding(node, name)
                if binding is not None:
                    matches.append((node.end_lineno - node.lineno, binding))
    if not matches:
        return None
    _, expression = min(matches)
    return expression


def _forwarded_repair_names(tree: ast.Module) -> set[tuple[int, str]]:
    """Identify repair values forwarded from the enclosing function's parameters."""

    forwarded: set[tuple[int, str]] = set()

    class Finder(ast.NodeVisitor):
        parameter_scopes: list[frozenset[str]]

        def __init__(self) -> None:
            self.parameter_scopes = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node)

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            arguments = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            names = {argument.arg for argument in arguments}
            if node.args.vararg is not None:
                names.add(node.args.vararg.arg)
            if node.args.kwarg is not None:
                names.add(node.args.kwarg.arg)
            self.parameter_scopes.append(frozenset(names))
            for statement in node.body:
                self.visit(statement)
            self.parameter_scopes.pop()

        def visit_Call(self, node: ast.Call) -> None:
            for keyword in node.keywords:
                if (
                    keyword.arg == "repair"
                    and isinstance(keyword.value, ast.Name)
                    and self.parameter_scopes
                    and keyword.value.id in self.parameter_scopes[-1]
                ):
                    forwarded.add((node.lineno, keyword.value.id))
            self.generic_visit(node)

    Finder().visit(tree)
    return forwarded


def _repair_expression(
    value: ast.expr,
    bindings: dict[str, str],
    forwarded_names: set[tuple[int, str]],
    tree: ast.Module,
    line: int,
) -> tuple[str, str | None]:
    """Resolve authored repair text while preserving intentional field forwarding."""
    if not isinstance(value, ast.Name):
        return ast.unparse(value), None
    if value.id in bindings:
        return bindings[value.id], None
    local = _local_bindings(tree, line, value.id)
    if local is not None:
        return local, None
    if (line, value.id) in forwarded_names or value.id == "repair":
        return "", None
    return value.id, value.id


def _repair_sites(source_root: Path) -> list[RepairSite]:
    sites: list[RepairSite] = []
    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bindings = _module_string_bindings(tree)
        forwarded_names = _forwarded_repair_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "repair":
                    expression, unresolved_name = _repair_expression(
                        keyword.value, bindings, forwarded_names, tree, node.lineno
                    )
                    sites.append(
                        RepairSite(
                            relative_path=str(path.relative_to(source_root)),
                            line=node.lineno,
                            expression=expression,
                            unresolved_name=unresolved_name,
                        )
                    )
    return sites


def _check_repair_sites(
    sites: list[RepairSite],
    verbs: frozenset[str],
    site_action_exemptions: dict[tuple[str, int], str],
) -> None:
    failures = _stale_exemption_failures(verbs, site_action_exemptions)
    for site in sites:
        failures.extend(_site_repair_failures(site, verbs, site_action_exemptions))
    if failures:
        raise AssertionError("\n".join(failures))


def _stale_exemption_failures(
    verbs: frozenset[str], site_action_exemptions: dict[tuple[str, int], str]
) -> list[str]:
    failures: list[str] = []
    for label, action in site_action_exemptions.items():
        if _ACTION_TO_VERBS[action].intersection(verbs):
            failures.append(
                f"{label[0]}:{label[1]}: stale exemption for registered action "
                f"{action!r}; delete the exemption entry"
            )
    return failures


def _site_repair_failures(
    site: RepairSite, verbs: frozenset[str], site_action_exemptions: dict[tuple[str, int], str]
) -> list[str]:
    if site.unresolved_name is not None:
        return [
            f"{site.label}: bare repair value {site.unresolved_name!r} is neither a module string "
            "constant nor an enclosing function parameter; inline the prose or expose its source"
        ]
    failures = [
            f"{site.label}: explicit invocation names unregistered verb {verb!r}"
            for verb in _EXPLICIT_INVOCATION.findall(site.expression)
            if verb not in verbs
        ]
    if (site.relative_path, site.line) in site_action_exemptions:
        return failures
    for action in (match.group(1).lower() for match in _ACTION_WORD.finditer(site.expression)):
        if action not in _ACTION_EXEMPTIONS and not _ACTION_TO_VERBS[action].intersection(verbs):
            failures.append(f"{site.label}: prose action {action!r} has no registered verb")
    return failures


def _assert_rejected(
    sites: list[RepairSite],
    verbs: frozenset[str],
    site_action_exemptions: dict[tuple[str, int], str],
    expected: str,
) -> None:
    try:
        _check_repair_sites(sites, verbs, site_action_exemptions)
    except AssertionError as error:
        if expected not in str(error):
            raise AssertionError(
                f"expected failure containing {expected!r}, got {error}"
            ) from error
    else:
        raise AssertionError(f"expected repair-text gate to reject {expected!r}")


def main() -> int:
    sites = _repair_sites(_MANAGER_SOURCE)
    if len(sites) < 66:
        raise AssertionError(f"repair-text audit unexpectedly shrank below 66 sites: {len(sites)}")
    verbs = _registered_verbs()
    _assert_rejected(
        [RepairSite("fixture.py", 1, "'Explicitly abandon the transaction.'")],
        verbs,
        {},
        "prose action 'abandon' has no registered verb",
    )
    _assert_rejected(
        [RepairSite("fixture.py", 1, "'Use solet unregistered-verb --dry-run.'")],
        verbs,
        {},
        "explicit invocation names unregistered verb 'unregistered-verb'",
    )
    with TemporaryDirectory() as temporary:
        fixture = Path(temporary) / "fixture.py"
        fixture.write_text(
            "MESSAGE = 'Use solet unregistered-verb --dry-run.'\n"
            "emit(repair=MESSAGE)\n",
            encoding="utf-8",
        )
        _assert_rejected(
            _repair_sites(Path(temporary)),
            verbs,
            {},
            "explicit invocation names unregistered verb 'unregistered-verb'",
        )
        fixture.write_text(
            "def emit():\n"
            "    MESSAGE = 'Use solet unregistered-verb --dry-run.'\n"
            "    report(repair=MESSAGE)\n",
            encoding="utf-8",
        )
        _assert_rejected(
            _repair_sites(Path(temporary)),
            verbs,
            {},
            "explicit invocation names unregistered verb 'unregistered-verb'",
        )
    _check_repair_sites(sites, verbs, _SITE_ACTION_EXEMPTIONS)
    future_verbs = verbs | {"abandon"}
    _assert_rejected(
        [RepairSite("transaction.py", 633, "'Explicitly abandon the transaction.'")],
        future_verbs,
        _SITE_ACTION_EXEMPTIONS,
        "transaction.py:633: stale exemption for registered action 'abandon'; delete the exemption entry",
    )
    print(f"repair_text_verb_gate_smoke: {len(sites)} repair sites passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
