"""Standalone L1 CLI — run the deterministic scanners over a tree and write the
F1 register + JSON sidecar.

This is the L1 dogfood/deliverable entry point (``python -m
code_vetting_plugin.l1_cli``). It runs the full scanner pipeline against a
working tree, stamps the run metadata, writes the human register and the
machine-complete JSON, and prints a one-line summary. Stream O's ``driver.py``
drives the full L1→L2→L3→report pass separately via :class:`L1DeterministicScanner`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from .l1_register import render_register
from .models import findings_to_json
from .run_context import repo_root
from .runner import run_all
from .targets import TargetTree
from .toolrun import run

_SUITE_ROOT = repo_root()
_DEFAULT_REGISTER = "workbench/2026-07-19_vetting_L1_findings.md"
_DEFAULT_JSON = "workbench/2026-07-19_vetting_L1_findings.json"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _git_ref(root: Path) -> str:
    outcome = run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], timeout_s=30)
    return outcome.stdout.strip() or "unknown"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the L1 deterministic vetting scanners over a working tree.")
    parser.add_argument("--root", type=Path, default=_SUITE_ROOT, help="target checkout root (default: this repo)")
    parser.add_argument("--repo", default="platform", help="target repo label recorded in the run")
    parser.add_argument("--scope", default="whole-tree self-vet", help="scope label recorded in the run")
    parser.add_argument("--register", type=Path, default=None, help=f"register markdown path (default: {_DEFAULT_REGISTER})")
    parser.add_argument("--json", type=Path, default=None, help=f"findings JSON sidecar path (default: {_DEFAULT_JSON})")
    return parser


def main(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    root: Path = args.root.resolve()
    register_path: Path = args.register if args.register is not None else root / _DEFAULT_REGISTER
    json_path: Path = args.json if args.json is not None else root / _DEFAULT_JSON

    ref = _git_ref(root)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"vr-l1-{ref}-{stamp}"

    started = _now()
    tree = TargetTree.from_git(root)
    findings, coverage, _report_data = run_all(tree, run_id)
    finished = _now()

    json_rel = json_path.relative_to(root).as_posix() if json_path.is_relative_to(root) else str(json_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(findings_to_json(findings), encoding="utf-8")

    register = render_register(
        run_id=run_id,
        repo=args.repo,
        ref=ref,
        scope=args.scope,
        started=started,
        finished=finished,
        json_sidecar=json_rel,
        findings=findings,
        coverage=coverage,
    )
    register_path.parent.mkdir(parents=True, exist_ok=True)
    register_path.write_text(register + "\n", encoding="utf-8")

    ran = sum(1 for record in coverage if record.ran)
    gaps = sum(1 for record in coverage if not record.ran)
    print(
        f"L1 vetting complete: {len(findings)} finding(s) from {ran} scanner(s) "
        f"({gaps} coverage gap(s)). Register: {register_path}  JSON: {json_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
