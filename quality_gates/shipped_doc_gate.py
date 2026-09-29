#!/usr/bin/env python3
"""Shipped-document gate (GTE-10) — the seal-time content checks, at commit time.

WHAT PROBLEM THIS SOLVES. Two instruments judge the CONTENT of what a seed
ships: the cited-path gate (does a backticked repo-relative citation resolve
inside the bundle?) and the reserved-identity scan (does an origin identity
token appear in shipped bytes or paths?). Both run at SEAL time, inside the
seed factory, over an assembled ``bundle_dir`` — and assembling a bundle
requires a committed ref. So neither could run before a commit existed, and
"run the shipped-doc gate before handing off" was an instruction no lane could
execute.

The cost was measured, not imagined. Of six landings on the night of
2026-08-17/18, three were remediations of the other three, and each of those
three was caught at MINT time or by a lane refusing to proceed — not one by
pre-land review:

  1. a release note asserting nothing in the release could break an adopter,
     in the release that broke them;
  2. two dead citations in a shipped KB article, which stopped a publish in
     flight (``[UNPARSEABLE] C:/ProgramData/...`` and ``[DEAD]
     .claude/settings.json``);
  3. a shipped smoke reading an unshipped path, which REFUSED the born-clone
     gate in an adopter's clone.

Each cost a full re-assemble / re-seal / re-verify lap. This gate moves checks
(2)'s and (3)'s CLASS to the pre-commit battery. It does not move (1)'s: see
"What this gate cannot do" below, and do not read a green here as covering it.

HOW IT IS BUNDLE-FREE. ``seed_factory_plugin.shipped_surface`` derives what
this checkout WOULD ship straight from ``seed_manifest.yaml``, using assemble's
own manifest parser, pathspec builder and exclusion predicate, with ``git
ls-files --cached --others --exclude-standard`` substituted for ``git archive``
at a ref. ``shipped_doc_lint`` then runs the two gates' own grammars over that
set, reached through their public seams. No grammar, no pattern, and no
tolerance rule is restated in this file or in either of those.

The derivation is faithful to a known, fully-attributed CONSTANT OFFSET. The
four GENERATED bundle-root files (``PROVENANCE.json``, ``README.md``,
``CLAUDE.md``, ``AGENTS.md``) exist only in an assembled bundle, so this gate
sees exactly **4 fewer files and 1 fewer tolerated citation** per profile than
the seal does, in every profile, the 1 being the generated ``README.md``.

The offset is the durable claim; the absolute counts are not, because every
landing that adds a file under a shipped path moves them by construction. At
``4fc06b84b`` the absolutes were 2702 / 3268 / 3153 files here against the
seal's 2706 / 3272 / 3157, and 119 / 166 / 126 tolerated against 120 / 167 /
127. Compare the OFFSET when reconciling the two instruments; compare the
absolute only against the same ref that produced it. ``shipped_surface``'s
docstring carries the proof that the census half of the offset cannot produce a
false finding, and the bound on the walk half.

WHERE IT DOES NOT APPLY. ``seed_factory_plugin`` is NO-FACTORY: the resolver
fail-louds if it is ever named in a bundle, so a born clone carries neither the
factory nor ``seed_manifest.yaml``. This script, however, DOES ship —
``quality_gates/`` is copied wholesale. A shipped executable that assumes an
unshipped path is defect (3) above, so the applicability test is the FIRST
thing that runs, before any factory import: no manifest, no seed to mint, no
shipped-doc obligation, and the gate prints a named skip and exits 0. That is a
DECLARED absence, printed every run, never a silent pass.

CHECKOUT-ROOT INSTRUCTIONS. The root ``AGENTS.md`` and ``CLAUDE.md`` files are
not part of a seed profile, but they govern every contributor in this checkout.
After the shipped-surface measurement, this gate resolves their backticked
path citations against the actual checkout census (excluding Git bookkeeping).
That is a checkout-scope leg, not a claim that either file is shipped in a
capability bundle.

EXIT CODES. 0 clean (or not applicable), 2 non-allowlisted findings, 64 usage
error, 70 the gate raised and produced no verdict. Blocking is deliberately 2
rather than the 1 that ``sql_access_gate.py`` uses: this gate is wired into
``code_quality_check.py``, whose ``_classify_gate_exit`` documents that exit 1
collides with Python's own unhandled-exception code, so a crash in a
1-is-blocking gate reads as a violation count over code that was never
measured. 70 is the same crash code ``gate_scope.GATE_CRASH_EXIT`` declares.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    # Types only — this import never executes, so the born-clone skip path
    # below is unaffected by it. It exists so the formatting helpers are
    # checked against the real measurement shape instead of being written
    # against `object` and reaching into it with getattr, which would type
    # -check a gate that had stopped being verifiable.
    from seed_factory_plugin.shipped_doc_lint import (
        LintReport,
        ProfileReport,
        ToleratedSubject,
    )

EXIT_OK: Final[int] = 0
EXIT_BLOCKING: Final[int] = 2
EXIT_USAGE_ERROR: Final[int] = 64
EXIT_GATE_CRASH: Final[int] = 70

_FACTORY_KB: Final[str] = "plugins/seed_factory_plugin/knowledge_base"
_MANIFEST_RELPATH: Final[str] = f"{_FACTORY_KB}/seed_manifest.yaml"
_BUNDLES_RELPATH: Final[str] = f"{_FACTORY_KB}/capability_bundles.yaml"
_DEFAULT_ALLOWLIST_RELPATH: Final[str] = "quality_gates/cited_path_gate_allowlist.txt"
_DEFAULT_BASELINE_RELPATH: Final[str] = "quality_gates/shipped_doc_baseline.txt"
_ROOT_INSTRUCTION_RELPATHS: Final[tuple[str, str]] = ("AGENTS.md", "CLAUDE.md")


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Shipped-document gate (GTE-10).")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(),
                        help="checkout root to measure (default: cwd)")
    parser.add_argument("--allowlist", type=Path, default=None,
                        help=f"tracked-debt register (default: <repo-root>/{_DEFAULT_ALLOWLIST_RELPATH})")
    parser.add_argument("--baseline", type=Path, default=None,
                        help=f"declared tolerated subjects (default: <repo-root>/{_DEFAULT_BASELINE_RELPATH})")
    parser.add_argument("--profile", action="append", default=[], metavar="BUNDLE",
                        help="capability bundle to measure; repeatable "
                             "(default: every bundle in capability_bundles.yaml)")
    return parser.parse_args(list(argv))


def _print_not_applicable(manifest_path: Path) -> None:
    print("⏭️  SKIPPED: shipped_doc_gate — no seed manifest in this checkout")
    print(f"    looked for: {manifest_path}")
    print("    This checkout mints no seed (seed_factory_plugin is NO-FACTORY and never")
    print("    ships), so it has no shipped-document surface to police. Declared skip,")
    print("    not a pass: nothing was measured.")


def _render_profile(report: ProfileReport) -> str:
    """One profile's measurement line."""
    return (
        f"    {report.profile:<22} files={report.file_count:>5}  "
        f"markdown={report.markdown_count:>4}  "
        f"sources={report.source_count:>4}  "
        f"blocking={len(report.blocking):>3}  "
        f"tolerated={report.tolerated_count:>4}"
    )


def _render_subject(subject: ToleratedSubject) -> str:
    check_id, key, occurrence = subject
    return f"{check_id} {key} (occurrence {occurrence})"


def _baseline_violations(
    report: LintReport, declared: dict[str, frozenset[ToleratedSubject]],
) -> list[str]:
    """Tolerated-subject drift, in BOTH directions.

    A subject set retains the profile, finding key, and occurrence ordinal.
    Unlike the old count pin, it refuses a same-count substitution and still
    detects a repeated occurrence of one tolerated citation.
    """
    violations: list[str] = []
    for profile_report in report.profiles:
        name = profile_report.profile
        measured = frozenset(profile_report.tolerated_subjects)
        if name not in declared:
            if measured:
                violations.append(f"{name}: tolerated subjects present but no baseline is declared")
            continue
        unexpected = measured - declared[name]
        missing = declared[name] - measured
        violations.extend(
            f"{name}: unexpected tolerated subject {_render_subject(subject)}"
            for subject in sorted(unexpected)
        )
        violations.extend(
            f"{name}: declared tolerated subject missing {_render_subject(subject)}"
            for subject in sorted(missing)
        )
    return violations


def _print_findings(header: str, lines: Sequence[str]) -> None:
    print(f"\n❌ BLOCKING: {header} ({len(lines)})")
    for line in lines:
        print(f"    {line}")


def _identity_verdict_lines(report: LintReport) -> tuple[list[str], list[str]]:
    blocking: list[str] = []
    classified: list[str] = []
    for finding in report.identity:
        destination = classified if finding.classification is not None else blocking
        destination.append(finding.render())
    return blocking, classified


def _print_classified_identity(lines: Sequence[str]) -> None:
    if not lines:
        return
    print(
        f"\nℹ️  CLASSIFIED: reason-bearing reserved-identity findings "
        f"({len(lines)})"
    )
    for line in lines:
        print(f"    {line}")


def _checkout_census(repo_root: Path) -> frozenset[str]:
    """All present checkout files, excluding Git's private bookkeeping.

    Root instruction files govern this checkout rather than a capability bundle,
    so their citations must resolve against the checkout's actual content surface
    instead of any one profile's shipped surface.
    """
    return frozenset(
        path.relative_to(repo_root).as_posix()
        for path in repo_root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repo_root).parts
    )


def _root_instruction_findings(repo_root: Path) -> list[str]:
    """Citation findings in the checkout-root instruction pair.

    This intentionally reuses the cited-path grammar. It is a checkout-scope
    leg, not a claim that these two documents are present in every assembled
    capability bundle.
    """
    from seed_factory_plugin import cited_path_gate

    census = _checkout_census(repo_root)
    findings = []
    for relpath in _ROOT_INSTRUCTION_RELPATHS:
        path = repo_root / relpath
        if not path.is_file():
            raise RuntimeError(f"root instruction file missing: {path}")
        findings.extend(
            cited_path_gate.find_findings_in_text(
                path.read_text(encoding="utf-8"),
                citing_file=relpath,
                census=census,
            )
        )
    blocking, _ = cited_path_gate.partition_findings(tuple(findings), frozenset())
    return [f"{finding.check_id} {finding.key}" for finding in blocking]


def _verdict(
    report: LintReport, declared: dict[str, frozenset[ToleratedSubject]],
    root_instruction_findings: Sequence[str],
) -> int:
    """Print the measurement, then the blocking policy's answer to it."""
    print(f"📊 shipped_doc_gate — {len(report.profiles)} profile(s), "
          f"{report.union_file_count} distinct shipped files")
    for profile_report in report.profiles:
        print(_render_profile(profile_report))

    citations = [
        f"[{profile_report.profile}] {line}"
        for profile_report in report.profiles for line in profile_report.blocking
    ]
    identity, classified_identity = _identity_verdict_lines(report)
    _print_classified_identity(classified_identity)
    drift = _baseline_violations(report, declared)

    for header, lines in (
        ("non-allowlisted cited-path findings in shipped markdown", citations),
        ("reserved-identity matches in shipped files", identity),
        ("folded identity classification anchor drift", report.identity_anchor_violations),
        ("minting-identity markers in shipped files", report.operator_identity),
        ("tolerated-debt baseline drift", drift),
        ("cited-path findings in root instruction docs", root_instruction_findings),
    ):
        if lines:
            _print_findings(header, lines)

    if any((
        citations, identity, report.identity_anchor_violations, report.operator_identity,
        drift, root_instruction_findings,
    )):
        return EXIT_BLOCKING
    print("✅ shipped_doc_gate: no blocking findings; tolerated debt at declared baseline")
    return EXIT_OK


def run(argv: Sequence[str]) -> int:
    args = _parse_args(argv)
    repo_root: Path = args.repo_root.resolve()
    manifest_path = repo_root / _MANIFEST_RELPATH
    allowlist_path: Path = args.allowlist if args.allowlist is not None else repo_root / _DEFAULT_ALLOWLIST_RELPATH
    baseline_path: Path = args.baseline if args.baseline is not None else repo_root / _DEFAULT_BASELINE_RELPATH

    if not manifest_path.is_file():
        _print_not_applicable(manifest_path)
        return EXIT_OK
    if not allowlist_path.is_file():
        print(f"❌ usage: allowlist not found: {allowlist_path}")
        return EXIT_USAGE_ERROR

    try:
        from seed_factory_plugin import shipped_doc_lint
    except ImportError as exc:
        print("🛑 GATE CRASH: shipped_doc_gate found a seed manifest but cannot import")
        print(f"    seed_factory_plugin, so nothing was measured: {exc}")
        return EXIT_GATE_CRASH

    try:
        report = shipped_doc_lint.lint_shipped_docs(
            repo_root,
            manifest_path=manifest_path,
            bundles_path=repo_root / _BUNDLES_RELPATH,
            tolerance_path=allowlist_path,
            profiles=args.profile or None,
        )
        declared = shipped_doc_lint.load_tolerated_baseline(baseline_path)
        root_instruction_findings = _root_instruction_findings(repo_root)
    except (OSError, RuntimeError) as exc:
        print(f"🛑 GATE CRASH: shipped_doc_gate produced NO VERDICT — {type(exc).__name__}: {exc}")
        return EXIT_GATE_CRASH

    return _verdict(report, declared, root_instruction_findings)


def main() -> int:
    try:
        return run(sys.argv[1:])
    except SystemExit as exc:  # argparse
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE_ERROR


if __name__ == "__main__":
    sys.exit(main())
