#!/usr/bin/env python3
"""SUB-04(b) — call-site wiring smokes for the two Phase-0 validation seams.

The Phase-0 contract smokes (``deterministic_continuation_single_step_smoke``,
``pre_dispatch_validation_smoke``) freeze the validation LOGIC by calling the
validator functions directly. They would stay GREEN if a refactor deleted the
PRODUCTION call site — the platform would silently stop validating and no
contract smoke would notice. These wiring smokes pin the call sites themselves,
so removing a call turns a smoke red:

  Seam 1 (deterministic-continuation). ``coordinator._handle_deterministic``
  calls ``validate_deterministic_continuation`` (imported from
  ``ananta.core.result_processing.contracts``). Ref: coordinator.py ~:286.

  Seam 2 (pre-dispatch step contract). ``inference_transaction._run_pipeline``
  calls the module-level ``_validate_step_contract``. Ref:
  inference_transaction.py ~:206.

Static AST assertions over the REAL production source (located via
``inspect.getfile`` — resilient to line drift; the enclosing-function NAME is the
stable anchor). Each seam carries a negative control that proves the detector
localizes (returns False when the symbol is not called in that function), so a
green result cannot be a trivially-true detector.

Project policy: no pytest. Offline — no live solet / LM Studio / Postgres. Exits 0
on success, 1 on first-failed-check aggregate.

Run from repo root:
    .venv/bin/python3 ananta/tests/core/substrate_contracts/call_site_wiring_smoke.py
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Importing the production modules also asserts they are importable (a wiring
# check in itself) and gives inspect.getfile a real source path.
from ananta.core.result_processing import coordinator as coordinator_mod  # noqa: E402
from ananta.services.inference_service import (  # noqa: E402
    inference_transaction as itx_mod,
)
from substrate_contract_fixtures import Checker  # noqa: E402


def _module_tree(mod: ModuleType) -> ast.Module:
    src = Path(inspect.getfile(mod)).read_text(encoding="utf-8")
    return ast.parse(src)


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    """First FunctionDef named ``name`` at any nesting (methods included)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _calls_symbol(func: ast.FunctionDef, symbol: str) -> bool:
    """True if ``func``'s body contains a call to ``symbol`` (bare name or
    attribute tail), e.g. ``symbol(...)`` or ``x.symbol(...)``."""
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            called = node.func
            if isinstance(called, ast.Name) and called.id == symbol:
                return True
            if isinstance(called, ast.Attribute) and called.attr == symbol:
                return True
    return False


def _module_level_def(tree: ast.Module, name: str) -> bool:
    return any(isinstance(n, ast.FunctionDef) and n.name == name for n in tree.body)


def _imports_name_from(tree: ast.Module, symbol: str, module_suffix: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
            module_suffix
        ):
            if any(alias.name == symbol for alias in node.names):
                return True
    return False


def main() -> int:
    checker = Checker("SUB-04(b) call-site wiring")

    # ── Seam 1: deterministic-continuation (coordinator._handle_deterministic) ──
    coord_tree = _module_tree(coordinator_mod)
    checker.check(
        _imports_name_from(
            coord_tree,
            "validate_deterministic_continuation",
            "result_processing.contracts",
        ),
        "1a: coordinator imports validate_deterministic_continuation from "
        "result_processing.contracts (call binds to the real validator)",
    )
    handle_det = _find_function(coord_tree, "_handle_deterministic")
    checker.check(handle_det is not None, "1b: coordinator defines _handle_deterministic")
    checker.check(
        handle_det is not None
        and _calls_symbol(handle_det, "validate_deterministic_continuation"),
        "1c: _handle_deterministic CALLS validate_deterministic_continuation "
        "(deterministic seam wired)",
    )
    # Negative control: the sibling bridge validator IS imported and called in
    # _handle_bridge_delivery, but must NOT appear in the deterministic handler —
    # proves _calls_symbol returns False when a real symbol is not called here.
    checker.check(
        handle_det is not None
        and not _calls_symbol(handle_det, "validate_bridge_delivery_success"),
        "1d: negative control — _handle_deterministic does NOT call the bridge "
        "validator (detector localizes)",
    )

    # ── Seam 2: pre-dispatch step contract (inference_transaction._run_pipeline) ──
    itx_tree = _module_tree(itx_mod)
    checker.check(
        _module_level_def(itx_tree, "_validate_step_contract"),
        "2a: inference_transaction defines module-level _validate_step_contract",
    )
    run_pipeline = _find_function(itx_tree, "_run_pipeline")
    checker.check(run_pipeline is not None, "2b: inference_transaction defines _run_pipeline")
    checker.check(
        run_pipeline is not None and _calls_symbol(run_pipeline, "_validate_step_contract"),
        "2c: _run_pipeline CALLS _validate_step_contract (pre-dispatch seam wired)",
    )
    # Negative control: a different function in the same module that must NOT
    # invoke the step-contract validator — proves the detector localizes.
    resolve_io = _find_function(itx_tree, "_resolve_io_process_key")
    checker.check(
        resolve_io is not None and not _calls_symbol(resolve_io, "_validate_step_contract"),
        "2d: negative control — _resolve_io_process_key does NOT call "
        "_validate_step_contract (detector localizes)",
    )

    return checker.summary()


if __name__ == "__main__":
    sys.exit(main())
