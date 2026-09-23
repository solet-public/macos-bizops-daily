#!/usr/bin/env python3
"""F2-A10 SKILL gate — prevent bloat-creep into ``ananta.cli``.

Enforces LOC ceilings on the load-bearing CLI functions + a forbidden-
phase AST walk that flags accretion vectors (pip install, shutil.rmtree,
psycopg.connect, os.kill SIGTERM/SIGKILL, materialize_*, etc.). The
Choice Y bloat-creep risk per design memo §5 is that 2118-line
``launch.py`` accretion migrates into ``ananta.cli`` once the script is
deleted; this gate catches that pattern directly.

Run from ``.githooks/pre-commit`` between the W-INT gate (Cycle 1
structural mode) and the per-file gates (ruff + pyright + radon).
Same exit-code semantics as the other ``quality_gates/`` scripts:

  0  — clean (under all ceilings, no forbidden phases)
  1  — one or more violations
  2  — harness error

Reference: the launch.py choice-Y design record §5 (dev-checkout
workbench — not part of the shipped tree).
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

CLI_FILE: Final[Path] = (
    Path(__file__).resolve().parent.parent
    / "ananta" / "src" / "ananta" / "cli.py"
)

# LOC ceilings per §5.2 + §14.5 P5 fold.
_FUNCTION_CEILINGS: Final[dict[str, int]] = {
    "main": 50,
    "sync_main": 50,
    "parse_cli_arguments": 100,
    "_setup_environment_or_exit": 100,
    "_run_orchestrator_or_exit": 100,
}
_MAIN_GROUP_CEILING: Final[int] = 50  # main + sync_main combined per §5.2
_TOTAL_FILE_CEILING: Final[int] = 800

# §5.3 forbidden-import-or-attr-access patterns the AST walk flags.
_FORBIDDEN_DOT_PATHS: Final[tuple[str, ...]] = (
    "shutil.rmtree",
    "psycopg.connect",
    "os.kill",
)

# Allowlist semantic per §14.5 P5: imports from these modules are OK;
# anything else triggers the forbidden-phase check at call-site level.
_ALLOWED_IMPORT_PREFIXES: Final[tuple[str, ...]] = (
    "ananta.core",
    "ananta.error_handling",
    "ananta.logging_setup",
    "ananta.constants",
    "ananta.interfaces",
)

# Function-name substrings whose call sites are forbidden in cli.py.
_FORBIDDEN_NAME_SUBSTRINGS: Final[tuple[str, ...]] = (
    "materialize_",
    "create_venv_",
    "bootstrap_vault",
    "start_cosyvoice2",
    "clear_caches",
    "drop_postgres_tables",
    "purge_stale_knowledge",
)


@dataclass(frozen=True)
class Violation:
    kind: str
    detail: str
    lineno: int


def _function_loc(node: ast.FunctionDef) -> int:
    """Body LOC: end_lineno - first body lineno + 1; tolerant of decorators."""
    if not node.body:
        return 0
    first = node.body[0].lineno
    last = max((n.end_lineno or n.lineno) for n in node.body)
    return max(0, last - first + 1)


def _flatten_attr(node: ast.AST) -> str:
    """Render ``ast.Attribute`` chains as dotted string ('os.kill')."""
    if isinstance(node, ast.Attribute):
        return f"{_flatten_attr(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _check_function_ceilings(module: ast.Module) -> list[Violation]:
    violations: list[Violation] = []
    sizes: dict[str, int] = {}
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef) and node.name in _FUNCTION_CEILINGS:
            loc = _function_loc(node)
            sizes[node.name] = loc
            ceiling = _FUNCTION_CEILINGS[node.name]
            if loc > ceiling:
                violations.append(Violation(
                    kind="function-ceiling",
                    detail=f"{node.name} body has {loc} LOC > ceiling {ceiling}",
                    lineno=node.lineno,
                ))
    combined = sizes.get("main", 0) + sizes.get("sync_main", 0)
    if combined > _MAIN_GROUP_CEILING:
        violations.append(Violation(
            kind="function-ceiling",
            detail=(
                f"main + sync_main combined {combined} LOC > "
                f"ceiling {_MAIN_GROUP_CEILING}"
            ),
            lineno=0,
        ))
    return violations


def _check_total_file_ceiling(source: str) -> list[Violation]:
    loc = sum(
        1
        for line in source.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    if loc > _TOTAL_FILE_CEILING:
        return [Violation(
            kind="total-file-ceiling",
            detail=f"cli.py has {loc} non-comment LOC > ceiling {_TOTAL_FILE_CEILING}",
            lineno=0,
        )]
    return []


_SUBPROCESS_RUNNERS: Final[frozenset[str]] = frozenset({
    "subprocess.run", "subprocess.check_call", "subprocess.check_output",
})


def _list_arg_carries_pip_install(node: ast.Call) -> bool:
    if not node.args or not isinstance(node.args[0], ast.List):
        return False
    strings = [
        e.value for e in node.args[0].elts
        if isinstance(e, ast.Constant) and isinstance(e.value, str)
    ]
    pip_idx = strings.index("pip") if "pip" in strings else -1
    return pip_idx >= 0 and "install" in strings[pip_idx + 1:]


def _is_pip_install_call(node: ast.Call) -> bool:
    """Detect ``subprocess.run([..., "pip", "install", ...])`` shape."""
    if _flatten_attr(node.func) not in _SUBPROCESS_RUNNERS:
        return False
    return _list_arg_carries_pip_install(node)


def _is_runtime_path_unlink(node: ast.Call) -> bool:
    """Detect ``Path(".../runtime/...").unlink(...)`` shape."""
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "unlink":
        return False
    receiver = node.func.value
    # Walk attribute / call chain looking for 'runtime' substring in Path arg.
    while isinstance(receiver, (ast.Attribute, ast.Call)):
        if isinstance(receiver, ast.Call):
            for arg in receiver.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if "runtime" in arg.value:
                        return True
            break
        receiver = receiver.value
    return False


def _is_os_kill_signal(node: ast.Call) -> bool:
    """Detect ``os.kill(pid, signal.SIGTERM | SIGKILL)`` shape."""
    if _flatten_attr(node.func) != "os.kill":
        return False
    if len(node.args) < 2:
        return False
    sig_arg = node.args[1]
    sig_name = _flatten_attr(sig_arg)
    return sig_name in ("signal.SIGTERM", "signal.SIGKILL")


def _scan_call_for_violation(node: ast.Call) -> Violation | None:
    func_name = _flatten_attr(node.func)
    if any(func_name == p or func_name.startswith(p) for p in _FORBIDDEN_DOT_PATHS):
        if func_name == "os.kill" and not _is_os_kill_signal(node):
            return None
        return Violation(
            kind="forbidden-call",
            detail=f"call to {func_name} is forbidden in ananta.cli",
            lineno=node.lineno,
        )
    if _is_pip_install_call(node):
        return Violation(
            kind="forbidden-call",
            detail="subprocess pip install is forbidden in ananta.cli",
            lineno=node.lineno,
        )
    if _is_runtime_path_unlink(node):
        return Violation(
            kind="forbidden-call",
            detail="direct runtime-path unlink is forbidden in ananta.cli",
            lineno=node.lineno,
        )
    for substring in _FORBIDDEN_NAME_SUBSTRINGS:
        if substring in func_name:
            return Violation(
                kind="forbidden-call",
                detail=(
                    f"call to {func_name!r} (matches forbidden substring "
                    f"{substring!r}) is forbidden in ananta.cli"
                ),
                lineno=node.lineno,
            )
    return None


def _check_forbidden_calls(module: ast.Module) -> list[Violation]:
    violations: list[Violation] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        v = _scan_call_for_violation(node)
        if v is not None:
            violations.append(v)
    return violations


def _check_imports(module: ast.Module) -> list[Violation]:
    """Allowlist-import sanity check.

    Per §5.3 + §14.5 P5 fold: imports from the ananta.* namespace must
    sit on the allowlist (ananta.core, ananta.error_handling, etc.). A
    new top-level ananta.foo import in cli.py that isn't on the list
    trips the gate so reviewers see the surface delta explicitly.
    """
    violations: list[Violation] = []
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom):
            module_name = node.module or ""
            if not module_name.startswith("ananta."):
                continue
            if not any(module_name.startswith(p) for p in _ALLOWED_IMPORT_PREFIXES):
                violations.append(Violation(
                    kind="forbidden-import",
                    detail=(
                        f"import 'from {module_name} import ...' is not on the "
                        f"ananta.cli allowlist; extend "
                        f"_ALLOWED_IMPORT_PREFIXES if intentional"
                    ),
                    lineno=node.lineno,
                ))
    return violations


def _format_violations(violations: Iterable[Violation]) -> str:
    lines = ["ananta-cli-minimal: VIOLATION"]
    lines.append(f"  file: {CLI_FILE}")
    for v in violations:
        lines.append(f"  - {v.kind} (line {v.lineno}): {v.detail}")
    lines.append(
        "  rationale: ananta.cli must stay minimal — destructive/lifecycle ops "
        "belong in the owning plugin's lifecycle hooks; midwife ops belong in "
        "birth_solet. See workbench/2026-06-16_launch_py_choice_y_design.md §5."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cli", default=str(CLI_FILE),
        help="Path to ananta/src/ananta/cli.py (default: repo-relative).",
    )
    args = parser.parse_args(argv)
    cli_path = Path(args.cli).resolve()
    if not cli_path.is_file():
        print(f"ERROR: cli.py not found at {cli_path}", file=sys.stderr)
        return 2
    source = cli_path.read_text(encoding="utf-8")
    try:
        module = ast.parse(source, filename=str(cli_path))
    except SyntaxError as exc:
        print(f"ERROR: {cli_path} failed to parse: {exc}", file=sys.stderr)
        return 2

    violations: list[Violation] = []
    violations.extend(_check_function_ceilings(module))
    violations.extend(_check_total_file_ceiling(source))
    violations.extend(_check_forbidden_calls(module))
    violations.extend(_check_imports(module))

    if not violations:
        print(f"OK: ananta.cli minimal-check passed against {cli_path}.")
        return 0
    print(_format_violations(violations), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
