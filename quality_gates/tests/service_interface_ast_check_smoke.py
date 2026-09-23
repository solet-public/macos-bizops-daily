#!/usr/bin/env python3
"""Acceptance smoke for the service-interface AST gate.

Per the service-interface-AST-gate design record v1 §9 (dev-checkout
workbench — not part of the shipped tree) acceptance criterion #4: the
gate must empirically catch synthetic
re-creations of BOTH origin incidents.

Coverage:

* **Synthetic Tier α decorator-stack bug.** Construct a tmp
  ``interfaces/public.py`` with the canonical stacked-decorator shape
  that produced the 2026-06-11 backfill orphan; assert gate emits a
  CHECK (a) finding.
* **Synthetic bare @abstractmethod.** Construct a tmp file with a public
  ``@abstractmethod`` lacking ``@service_interface_process``; assert
  gate emits a CHECK (b) finding.
* **Synthetic orphan JSON.** Construct a tmp ``processes/<provider>/
  <name>.json`` whose ``<name>`` has no matching decorator; assert gate
  emits a CHECK (c) finding.
* **Corrected versions.** After fix-up edits to each synthetic file,
  re-run the gate against the corrected fixture and assert EXIT 0.
* **Allowlist absorption.** With each finding allowlisted, assert
  EXIT 0 + allowlisted-marker in output.

Run:

    .venv/bin/python3 quality_gates/tests/service_interface_ast_check_smoke.py

Exit codes:
  0 — every synthetic case detected + corrected + allowlist-absorbed
  1 — any case failed
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GATE_SCRIPT = REPO_ROOT / "quality_gates" / "service_interface_ast_check.py"
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python3"


def _run_gate(repo_root: Path, allowlist: Path | None = None) -> tuple[int, str]:
    """Invoke the gate from ``repo_root``; return (exit_code, combined_output)."""
    argv = [str(VENV_PYTHON), str(GATE_SCRIPT)]
    if allowlist is not None:
        argv.extend(["--allowlist", str(allowlist)])
    proc = subprocess.run(  # noqa: S603
        argv,
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


_PUBLIC_PY_HEADER = '''"""Synthetic test interface (fixture for service_interface_ast_check_smoke.py)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


def service_interface_process(**kwargs: Any) -> Any:
    """Synthetic decorator stand-in (acts as identity at AST time)."""
    def _decorator(fn: Any) -> Any:
        return fn
    return _decorator


_PROVIDER = "synthetic_smoke_service"


'''


def _build_fixture(repo_root: Path, public_py_body: str, json_files: list[str]) -> None:
    """Create a minimal fixture mirroring the platform layout."""
    services_dir = (
        repo_root
        / "ananta"
        / "src"
        / "ananta"
        / "services"
        / "synthetic_smoke_service"
        / "interfaces"
    )
    services_dir.mkdir(parents=True, exist_ok=True)
    (services_dir / "public.py").write_text(_PUBLIC_PY_HEADER + public_py_body)

    processes_dir = (
        repo_root
        / "ananta"
        / "knowledge_base"
        / "processes"
        / "synthetic_smoke_service"
    )
    processes_dir.mkdir(parents=True, exist_ok=True)
    for name in json_files:
        (processes_dir / f"{name}.json").write_text(
            f'{{"process_key": "service_interface::synthetic_smoke_service::{name}"}}\n'
        )


_BUG_DECORATOR_STACK = '''class SyntheticSmokeAPI(ABC):
    """Synthetic ABC with the Tier α decorator-stack bug."""

    @service_interface_process(
        name="backfill_orphan_verb",
        provider=_PROVIDER,
    )
    @abstractmethod
    def lift_canonical_verb(self) -> None:
        """Decorator misplaced — its name= says 'backfill_orphan_verb' but the function is 'lift_canonical_verb'."""

    @abstractmethod
    def backfill_orphan_verb(self) -> None:
        """Bare @abstractmethod (silently orphaned by Tier α decorator stacking)."""
'''


_CORRECTED_BODY = '''class SyntheticSmokeAPI(ABC):
    """Corrected synthetic ABC — both verbs registered cleanly."""

    @service_interface_process(
        name="lift_canonical_verb",
        provider=_PROVIDER,
    )
    @abstractmethod
    def lift_canonical_verb(self) -> None:
        """Now registered."""

    @service_interface_process(
        name="backfill_orphan_verb",
        provider=_PROVIDER,
    )
    @abstractmethod
    def backfill_orphan_verb(self) -> None:
        """Now registered."""
'''


def case_synthetic_bug_detected(repo_root: Path) -> bool:
    """Plant Tier α stack-bug + orphan JSON; assert (a)+(b)+(c) findings + EXIT 2."""
    _build_fixture(
        repo_root,
        _BUG_DECORATOR_STACK,
        json_files=[
            "lift_canonical_verb",  # decorator registers as backfill_orphan_verb
            "backfill_orphan_verb",  # bare abstractmethod, no decorator
            "stale_renamed_verb",  # orphan JSON
        ],
    )
    exit_code, output = _run_gate(repo_root)
    ok = exit_code == 2
    print(f"  exit={exit_code} (expected 2)")
    a_found = "CHECK (a)" in output
    b_found = "CHECK (b)" in output
    c_orphan = "stale_renamed_verb" in output
    if not (a_found and b_found and c_orphan and ok):
        print("FAIL: synthetic bug case missed one or more findings")
        print(f"  CHECK (a) found: {a_found}")
        print(f"  CHECK (b) found: {b_found}")
        print(f"  CHECK (c) for stale_renamed_verb: {c_orphan}")
        print(output)
        return False
    print("  OK: synthetic bug case → (a)+(b)+(c) all detected, EXIT 2")
    return True


def case_corrected_passes(repo_root: Path) -> bool:
    """Replace fixture with corrected body + matching JSONs; assert EXIT 0."""
    _build_fixture(
        repo_root,
        _CORRECTED_BODY,
        json_files=["lift_canonical_verb", "backfill_orphan_verb"],
    )
    exit_code, output = _run_gate(repo_root)
    if exit_code != 0:
        print(f"FAIL: corrected fixture → exit={exit_code} (expected 0)")
        print(output)
        return False
    print(f"  OK: corrected fixture → EXIT 0 ({output.strip()[:120]})")
    return True


def case_allowlist_absorbs(repo_root: Path) -> bool:
    """Plant the synthetic bug; allowlist each finding; assert EXIT 0 + markers."""
    _build_fixture(
        repo_root,
        _BUG_DECORATOR_STACK,
        json_files=[
            "lift_canonical_verb",
            "backfill_orphan_verb",
            "stale_renamed_verb",
        ],
    )
    allowlist_path = repo_root / "service_interface_ast_smoke_allowlist.txt"
    allowlist_path.write_text(
        "# smoke allowlist — entries use POSIX-suffix-match for file portion (a/b)\n"
        "# and exact-match for (c). See design v1 §4.2.\n"
        "# (c) join key is func.__name__ per service_interface_decorator.py:234.\n"
        "a::interfaces/public.py::SyntheticSmokeAPI::lift_canonical_verb\n"
        "b::interfaces/public.py::SyntheticSmokeAPI::backfill_orphan_verb\n"
        "c::synthetic_smoke_service::stale_renamed_verb\n"
        "c::synthetic_smoke_service::backfill_orphan_verb\n"
    )
    exit_code, output = _run_gate(repo_root, allowlist=allowlist_path)
    if exit_code != 0:
        print(f"FAIL: allowlisted-fixture → exit={exit_code} (expected 0)")
        print(output)
        return False
    markers = output.count("[allowlisted]")
    if markers < 4:
        print(f"FAIL: only {markers} '[allowlisted]' markers (expected ≥4)")
        print(output)
        return False
    print(f"  OK: allowlist absorbed all findings → EXIT 0, {markers} markers")
    return True


def main() -> int:
    if not VENV_PYTHON.exists():
        print(f"FAIL: venv python not found at {VENV_PYTHON}")
        return 1
    if not GATE_SCRIPT.exists():
        print(f"FAIL: gate script not found at {GATE_SCRIPT}")
        return 1

    failures = 0
    with tempfile.TemporaryDirectory(prefix="si_ast_smoke_") as tmp:
        tmp_root = Path(tmp)

        print("\nCase 1: synthetic Tier-α-style decorator-stack + orphan JSON")
        if not case_synthetic_bug_detected(tmp_root):
            failures += 1

        # Reset fixture dirs between cases (write replaces files cleanly).
        shutil.rmtree(tmp_root / "ananta", ignore_errors=True)

        print("\nCase 2: corrected fixture passes clean")
        if not case_corrected_passes(tmp_root):
            failures += 1

        shutil.rmtree(tmp_root / "ananta", ignore_errors=True)

        print("\nCase 3: allowlist absorbs all 3 findings")
        if not case_allowlist_absorbs(tmp_root):
            failures += 1

    if failures:
        print(f"\n{failures} case(s) failed.")
        return 1
    print("\nOK: service-interface AST gate smoke clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
