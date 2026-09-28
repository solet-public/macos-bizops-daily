#!/usr/bin/env python3
"""Repo-level shipped-smoke checkout-only contract consistency gate (iss_59385149).

WHAT THIS CLOSES. ``shipped_smoke_contract._checkout_only_violations`` (V1)
checks that every checkout-only smoke declaration agrees across three
surfaces — a ``# not shipped:`` marker in ``quality_gates/gate_smokes.txt``,
a ``checkout_only`` entry in ``shipped_smoke_contract.yaml``, and a
``seed_manifest.yaml`` ``exclude_paths`` line — but it previously ran ONLY
inside ``assemble()``. ``unt_85473403`` landed a smoke with only the marker;
the full pre-landing battery (840/845) and two reviews passed it, because
nothing at landing time read the contract at all, and the gap surfaced one
publish lap later when r50's assembly failed closed (``rrun_78ae868f``).
This gate runs the same V1 predicate at REPO scope, before a commit exists,
so that exact class goes red here instead of at a later assembly.

BUNDLE-FREE AND RETAINED-SET-FREE. It calls
``checkout_only_consistency_violations``, which passes an EMPTY retained set
on purpose — see that function's own docstring: at repo level only the
three-surface agreement is checked, never which entries a particular
capability bundle would still keep. Whether a checkout-only entry is
(wrongly) still retained in some bundle is a separate, per-profile question
that ``check_source_tree_contract`` / ``validate_contract`` still answer at
assembly; this gate does not duplicate that check.

WRONG-TREE DEFENCE. The installed ``seed_factory_plugin`` package resolves
through this checkout's ``.venv``, which in a lane worktree is a SYMLINK to
the shared checkout's ``.venv`` — its editable ``.pth`` pointer names the
SHARED checkout's source, not the worktree's. A naive
``from seed_factory_plugin import ...`` would therefore silently measure the
wrong tree's contract logic. This script inserts
``<repo-root>/plugins/seed_factory_plugin/src`` at the FRONT of ``sys.path``
before that import, so the module actually under test in ``--repo-root`` is
the one that runs (schema_init_gate.py's own "WRONG-TREE DEFENCE" note
documents the same defect class, iss_ec0db9c7 / iss_77fe09ad).

EXIT CODES. 0 clean, 2 non-allowlisted findings, 64 usage error, 70 the gate
raised and produced no verdict. 2 rather than 1 for the same reason
``shipped_doc_gate.py`` uses 2: this gate is wired into
``code_quality_check.py``, whose ``_classify_gate_exit`` treats exit 1 as
Python's own unhandled-exception code, so a 1-is-blocking gate would read a
crash as a violation count over code that was never measured.

NOT MEANINGFULLY ALLOWLISTABLE. A three-surface disagreement IS the r50
defect class, not a style preference with a legitimate exception — this
mirrors the schema-init gate's standing ruling (2026-09-20, under
``rul_367d8bd6``) that a live boot-crash is never allowlisted. The
``--allowlist`` flag still exists, for uniformity with every other gate
``code_quality_check.py`` wires in this same way, and so this module is not
blocked from growing a legitimately-tolerable finding shape later without a
wiring change — but it starts, and is expected to stay, empty.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

_REPO_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_IMPORT_ROOT))

from quality_gates.allowlist_schema import load_allowlist  # noqa: E402

EXIT_OK: Final[int] = 0
EXIT_BLOCKING: Final[int] = 2
EXIT_USAGE_ERROR: Final[int] = 64
EXIT_GATE_CRASH: Final[int] = 70

_CONTRACT_RELPATH: Final[str] = "plugins/seed_factory_plugin/knowledge_base/shipped_smoke_contract.yaml"
_PLUGIN_SRC_RELPATH: Final[str] = "plugins/seed_factory_plugin/src"
_DEFAULT_ALLOWLIST_RELPATH: Final[str] = "quality_gates/smoke_contract_lint_gate_allowlist.txt"


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repo-level shipped-smoke checkout-only contract consistency gate."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(),
                        help="checkout root to measure (default: cwd)")
    parser.add_argument("--allowlist", type=Path, default=None,
                        help=f"tracked-debt register (default: <repo-root>/{_DEFAULT_ALLOWLIST_RELPATH})")
    return parser.parse_args(list(argv))


def run(argv: Sequence[str]) -> int:
    args = _parse_args(argv)
    repo_root: Path = args.repo_root.resolve()
    contract_path = repo_root / _CONTRACT_RELPATH
    allowlist_path: Path = (
        args.allowlist if args.allowlist is not None else repo_root / _DEFAULT_ALLOWLIST_RELPATH
    )

    if not contract_path.is_file():
        print(f"❌ usage: shipped-smoke contract not found: {contract_path}")
        return EXIT_USAGE_ERROR
    if not allowlist_path.is_file():
        print(f"❌ usage: allowlist not found: {allowlist_path}")
        return EXIT_USAGE_ERROR

    plugin_src = repo_root / _PLUGIN_SRC_RELPATH
    if str(plugin_src) not in sys.path:
        sys.path.insert(0, str(plugin_src))

    try:
        from seed_factory_plugin.shipped_smoke_contract import (
            ShippedSmokeContractError,
            checkout_only_consistency_violations,
        )
    except ImportError as exc:
        print(
            "🛑 GATE CRASH: smoke_contract_lint_gate cannot import "
            f"seed_factory_plugin from {plugin_src}: {exc}"
        )
        return EXIT_GATE_CRASH

    try:
        violations = checkout_only_consistency_violations(contract_path)
        allowlist = load_allowlist(allowlist_path)
    except (ShippedSmokeContractError, OSError) as exc:
        print(f"🛑 GATE CRASH: smoke_contract_lint_gate produced NO VERDICT — {type(exc).__name__}: {exc}")
        return EXIT_GATE_CRASH

    return _verdict(violations, allowlist)


def _verdict(violations: Sequence[str], allowlist: frozenset[str]) -> int:
    blocking = [v for v in violations if v not in allowlist]
    allowed = [v for v in violations if v in allowlist]
    for violation in allowed:
        print(f"[allowlisted] {violation}")
    if blocking:
        print(f"\n❌ BLOCKING: checkout-only contract disagreement ({len(blocking)})")
        for violation in blocking:
            print(f"    {violation}")
        return EXIT_BLOCKING
    print(
        "✅ smoke_contract_lint_gate: checkout-only contract self-consistent "
        f"({len(violations)} tolerated)"
    )
    return EXIT_OK


def main() -> int:
    try:
        return run(sys.argv[1:])
    except SystemExit as exc:  # argparse
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE_ERROR


if __name__ == "__main__":
    sys.exit(main())
