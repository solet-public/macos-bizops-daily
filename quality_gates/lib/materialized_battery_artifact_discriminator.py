#!/usr/bin/env python3
"""GTE-13 materialized-battery artifact discriminator utility.

Formalizes the hand-applied check Git-Controller has been running by eye on
every materialized-worktree landing tonight: "not staged AND byte-identical
to HEAD AND clean on a direct checkout run => artifact." Filed as GTE-13 in
the project's workbench backlog register — read that entry for the root
cause this exists to disclose, not re-litigate here. (Deliberately not a
backtick-quoted path citation: workbench/ is never shipped, so a formal
citation into it from shipped source reads DEAD in every profile — GTE-11's
own established defect class.)

ROOT CAUSE THIS DISCRIMINATES (one line): a materialized worktree's own copy
of a file and the SAME package resolved through the venv's editable-install
``.pth`` (which bakes in the ORIGIN checkout's absolute path at ``pip install
-e`` time, never worktree-aware) can diverge onto two different absolute
paths for byte-identical content, and pyright's strict nominal typing treats
values flowing between them as two different types. It fires ONLY inside the
deliberate worktree-isolation technique Git-Controller uses to materialize a
scoped battery — the routine per-commit gate (run directly against the live
checkout) is unaffected, because there the two paths always coincide.

LIBRARY INTENT — READ THIS BEFORE WIRING IT ANYWHERE. This conditional utility classifies ONE
whole-tree TYPE-CHECKER finding (pyright, or a structurally identical
type-checker run) against ONE file. It has no notion of a smoke and MUST
NEVER be used to decide whether a failing smoke is an artifact — that
question (whether ``binary_libraries_smoke.py``'s spawned ``mypy --strict``
subprocess is exposed to this same class) is explicitly UNCONFIRMED per the
GTE-13 register entry, and a script that auto-passed smoke failures on an
unproven theory would manufacture the exact masking risk this tool exists to
remove. If a smoke fails, a human decides. Full stop.

THE ASYMMETRY IS DELIBERATE. Any finding this tool cannot POSITIVELY prove
is an artifact is reported REAL — never "unknown", never "probably fine". A
false REAL costs someone ten minutes re-checking a clean file; a false
ARTIFACT lands a defect. Every failure mode below (a missing file, git
unavailable, the direct pyright run itself erroring) is caught, printed
loudly, and resolved to REAL — this tool has no silent-pass branch.

Verdict requires ALL three of:
  1. the file carries no local changes in the live checkout (``git status
     --porcelain`` empty for that path);
  2. the worktree's copy, the live checkout's copy, and ``git show
     HEAD:<path>`` are all byte-identical;
  3. a DIRECT single-file pyright run against the live checkout's copy,
     using the project's own ``pyproject.toml``, is clean.
Any single failure, or any inability to run a check, is REAL.

Every run prints its full evidence (both observations, not a verdict alone)
— the record is what let Git-Controller reclassify safely a dozen times
tonight, and a discriminator that only prints "artifact" or "real" would be
a rubber stamp, not this.

Exit codes (mirrors sql_access_gate.py):
  0  — ARTIFACT, positively proven
  1  — REAL (genuinely real, OR any check could not be completed — same bucket
       by design)
  2  — harness error (unexpected exception, not one of the anticipated
       cannot-verify branches)
  64 — usage error (argparse)

Run:
    .venv/bin/python3 quality_gates/lib/materialized_battery_artifact_discriminator.py \\
        --worktree-root /path/to/materialized/worktree \\
        --rel-path plugins/default_thinking_plugin/src/default_thinking_plugin/plugin.py \\
        --message "Argument of type ... cannot be assigned to parameter ..." \\
        --line 2886
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[2]


@dataclass
class Evidence:
    verdict: str = "real"  # "artifact" | "real" — REAL is the default, never assigned away from it
    lines: list[str] = field(default_factory=list)

    def note(self, line: str) -> None:
        self.lines.append(line)

    def render(self) -> str:
        return "\n".join(self.lines)


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        check=False,
    )


def _check_clean(repo_root: Path, rel_path: str, ev: Evidence) -> bool:
    """True iff the live checkout has NO local changes on ``rel_path``.

    A ``git status`` failure (git unavailable, not a repo, etc.) is a
    cannot-verify fault — reported and treated as NOT clean, never silently
    skipped.
    """
    result = _run_git(repo_root, "status", "--porcelain", "--", rel_path)
    if result.returncode != 0:
        ev.note(
            f"FAULT: `git status --porcelain -- {rel_path}` failed "
            f"(exit {result.returncode}): {result.stderr.decode(errors='replace').strip()}",
        )
        return False
    out = result.stdout.decode(errors="replace")
    if out.strip():
        ev.note(f"git status: DIRTY — local changes present on {rel_path}:\n{out.rstrip()}")
        return False
    ev.note(f"git status: clean — no local changes on {rel_path}")
    return True


def _check_byte_identical(
    repo_root: Path, worktree_root: Path, rel_path: str, ev: Evidence,
) -> bool:
    """True iff ``git show HEAD:<rel_path>``, the live checkout's file, and
    the worktree's file are all byte-identical.

    Any missing file or a failed ``git show`` is a cannot-verify fault —
    reported and treated as NOT identical.
    """
    head_result = _run_git(repo_root, "show", f"HEAD:{rel_path}")
    if head_result.returncode != 0:
        ev.note(
            f"FAULT: `git show HEAD:{rel_path}` failed (exit {head_result.returncode}): "
            f"{head_result.stderr.decode(errors='replace').strip()}",
        )
        return False
    head_bytes = head_result.stdout

    origin_file = repo_root / rel_path
    if not origin_file.is_file():
        ev.note(f"FAULT: live-checkout file does not exist: {origin_file}")
        return False
    origin_bytes = origin_file.read_bytes()

    worktree_file = worktree_root / rel_path
    if not worktree_file.is_file():
        ev.note(f"FAULT: worktree file does not exist: {worktree_file}")
        return False
    worktree_bytes = worktree_file.read_bytes()

    if head_bytes == origin_bytes == worktree_bytes:
        ev.note(
            f"byte-identical: HEAD:{rel_path}, live checkout, and worktree copy all match "
            f"({len(head_bytes)} bytes)",
        )
        return True

    if head_bytes != origin_bytes:
        ev.note(
            f"MISMATCH: live checkout's {rel_path} differs from HEAD:{rel_path} "
            f"({len(origin_bytes)} vs {len(head_bytes)} bytes) — this should be impossible "
            "if git status reported clean; treat as REAL and investigate the discrepancy",
        )
    if head_bytes != worktree_bytes:
        ev.note(
            f"MISMATCH: worktree copy of {rel_path} differs from HEAD:{rel_path} "
            f"({len(worktree_bytes)} vs {len(head_bytes)} bytes) — the worktree carries "
            "a real, intentional change to this file; it is not a materialization artifact",
        )
    return False


def _check_direct_run_clean(
    repo_root: Path, rel_path: str, ev: Evidence, *, pyright_bin: Path,
) -> bool:
    """True iff a single-file pyright run against the live checkout's copy,
    using the project's own pyproject.toml, is clean.

    A missing pyright binary or a non-parseable/erroring invocation is a
    cannot-verify fault — reported and treated as NOT clean.
    """
    if not pyright_bin.is_file():
        ev.note(f"FAULT: pyright binary not found at {pyright_bin}")
        return False
    project_config = repo_root / "pyproject.toml"
    if not project_config.is_file():
        ev.note(f"FAULT: project config not found at {project_config}")
        return False
    origin_file = repo_root / rel_path
    result = subprocess.run(
        [str(pyright_bin), "--project", str(project_config), str(origin_file)],
        capture_output=True,
        check=False,
    )
    output = result.stdout.decode(errors="replace")
    # pyright exits 0 clean, 1 when it reports findings, >1 on a harness fault
    # of its own (bad config, crash) — the latter is ALSO a cannot-verify fault,
    # not evidence of cleanliness.
    if result.returncode not in (0, 1):
        ev.note(
            f"FAULT: direct pyright run itself faulted (exit {result.returncode}): "
            f"{result.stderr.decode(errors='replace').strip() or output.strip()}",
        )
        return False
    if result.returncode == 0:
        ev.note(f"direct pyright run against the live checkout: clean\n{output.strip()}")
        return True
    ev.note(
        f"direct pyright run against the live checkout: NOT clean — real findings present\n"
        f"{output.strip()}",
    )
    return False


def classify(
    *,
    worktree_root: Path,
    repo_root: Path,
    rel_path: str,
    message: str,
    line: int | None,
    pyright_bin: Path,
) -> Evidence:
    ev = Evidence()
    ev.note(f"=== GTE-13 materialized-battery artifact check: {rel_path} ===")
    if line is not None:
        ev.note(f"reported finding: {rel_path}:{line} — {message}")
    elif message:
        ev.note(f"reported finding: {rel_path} — {message}")
    ev.note(f"repo_root={repo_root}  worktree_root={worktree_root}")
    ev.note("")

    clean = _check_clean(repo_root, rel_path, ev)
    ev.note("")
    identical = _check_byte_identical(repo_root, worktree_root, rel_path, ev)
    ev.note("")
    # Short-circuit: a direct pyright run is pointless (and would confuse the
    # evidence trail) once the file is already known to be dirty or diverged
    # from HEAD — those are already sufficient to call it REAL on their own.
    if clean and identical:
        direct_clean = _check_direct_run_clean(
            repo_root, rel_path, ev, pyright_bin=pyright_bin,
        )
    else:
        ev.note("direct pyright run: SKIPPED (already REAL on the checks above)")
        direct_clean = False
    ev.note("")

    if clean and identical and direct_clean:
        ev.verdict = "artifact"
        ev.note(
            "VERDICT: ARTIFACT — a materialized-battery false positive per GTE-13. "
            "The live checkout is clean, unmodified, and matches HEAD; this finding "
            "exists only inside the materialized-worktree resolution divergence.",
        )
    else:
        ev.verdict = "real"
        ev.note(
            "VERDICT: REAL — treat this finding as genuine. It did not positively "
            "clear all three checks (default is REAL, never 'unknown').",
        )
    return ev


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="GTE-13 materialized-battery artifact discriminator "
        "(whole-tree type-checker findings ONLY — never a smoke).",
    )
    parser.add_argument(
        "--worktree-root", type=Path, required=True,
        help="Root of the materialized worktree that produced the finding.",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=REPO_ROOT_DEFAULT,
        help=f"Root of the live checkout (default: {REPO_ROOT_DEFAULT}).",
    )
    parser.add_argument(
        "--rel-path", required=True,
        help="Repo-relative path of the flagged file, e.g. "
        "plugins/default_thinking_plugin/src/default_thinking_plugin/plugin.py",
    )
    parser.add_argument(
        "--message", default="",
        help="The type-checker's finding text, for the evidence record only "
        "(not used in the verdict).",
    )
    parser.add_argument(
        "--line", type=int, default=None,
        help="The reported line number, for the evidence record only.",
    )
    parser.add_argument(
        "--pyright-bin", type=Path, default=None,
        help="Override the pyright binary (default: <repo-root>/.venv/bin/pyright).",
    )
    return parser


def run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    pyright_bin = args.pyright_bin or (args.repo_root / ".venv" / "bin" / "pyright")

    if not args.worktree_root.is_dir():
        print(f"ERROR: --worktree-root does not exist: {args.worktree_root}", file=sys.stderr)
        return 2
    if not args.repo_root.is_dir():
        print(f"ERROR: --repo-root does not exist: {args.repo_root}", file=sys.stderr)
        return 2

    ev = classify(
        worktree_root=args.worktree_root,
        repo_root=args.repo_root,
        rel_path=args.rel_path,
        message=args.message,
        line=args.line,
        pyright_bin=pyright_bin,
    )
    print(ev.render())
    return 0 if ev.verdict == "artifact" else 1


def main() -> int:
    # Harness errors return 2 from run(); argparse raises SystemExit (0 on
    # --help, non-zero on a usage error) — remap the usage path to 64.
    try:
        return run(sys.argv[1:])
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 64


if __name__ == "__main__":
    sys.exit(main())
