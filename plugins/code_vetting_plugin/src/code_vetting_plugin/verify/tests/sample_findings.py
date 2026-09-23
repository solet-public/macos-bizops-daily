"""Synthetic L2/L1 candidate findings for the L3 dogfood + unit tests.

One source of truth for both the JSON sample register (generated for the CLI
run) and the end-to-end test. The set is engineered to exercise every path
through the refute-harness under the deterministic heuristic dispatcher:

  - real findings that must SURVIVE (raw-SQL RB-STATE, off-by-one, secret-in-
    workbench which the safety dimensions carry past RB-SCOPE);
  - DO-NOT-FLAG traps that must be killed DISPOSITIVELY (try/except, multi-tenant,
    backwards-compat, RB-SCOPE gate-nit, test-file Any);
  - a vague + unsubstantiated finding killed by MAJORITY (correctness + reproduce);
  - a pinned-but-unsubstantiated finding that SURVIVES on a single dissent —
    the case that shows the majority threshold, and where the cheap heuristic
    reproduce-lens is weaker than an inference skeptic would be.

Each ``expect_confirmed`` flag is the ground truth the test asserts against.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...models import (
    ContextProfile,
    Dimension,
    Finding,
    Layer,
    Provenance,
    Severity,
)

_RUN_ID = "sample-l3"


@dataclass(frozen=True, slots=True)
class SampleCase:
    """A synthetic candidate plus its expected post-verification verdict."""

    finding: Finding
    expect_confirmed: bool
    note: str


def _finding(
    *,
    layer: Layer,
    dimension: Dimension,
    severity: Severity,
    file: str,
    line: int | None,
    constraint_violated: str,
    evidence: str,
    source: str,
    fix_suggestion: str | None,
) -> Finding:
    return Finding.build(
        run_id=_RUN_ID,
        layer=layer,
        dimension=dimension,
        severity=severity,
        file=file,
        line=line,
        constraint_violated=constraint_violated,
        evidence=evidence,
        provenance=Provenance(source=source),
        context_profile=ContextProfile.PRODUCTION,
        fix_suggestion=fix_suggestion,
    )


def sample_cases() -> list[SampleCase]:
    """The engineered candidate set with ground-truth verdicts."""
    return [
        SampleCase(
            _finding(
                layer=Layer.L2_CRITIC,
                dimension=Dimension.ARCHITECTURE,
                severity=Severity.HIGH,
                file="plugins/foo_plugin/src/store.py",
                line=42,
                constraint_violated="RB-STATE: new raw SQL executed via execute_sql",
                evidence='cur.execute(f"SELECT * FROM t WHERE id={uid}")',
                source="critic:architecture",
                fix_suggestion="Route through StateManagementInterface.query_state.",
            ),
            expect_confirmed=True,
            note="Real RB-STATE violation — pinned, evidenced, not a DO-NOT-FLAG class.",
        ),
        SampleCase(
            _finding(
                layer=Layer.L2_CRITIC,
                dimension=Dimension.CORRECTNESS,
                severity=Severity.MEDIUM,
                file="ananta/src/ananta/util/ring.py",
                line=88,
                constraint_violated="off-by-one: loop over range(len(items)) indexes items[i+1]",
                evidence="for i in range(len(items)): use(items[i + 1])",
                source="critic:correctness",
                fix_suggestion="Iterate range(len(items) - 1).",
            ),
            expect_confirmed=True,
            note="Real correctness defect — universal-tier off-by-one.",
        ),
        SampleCase(
            _finding(
                layer=Layer.L1_DETERMINISTIC,
                dimension=Dimension.SECRETS,
                severity=Severity.BLOCKER,
                file="workbench/2026-07-19_scratch.md",
                line=5,
                constraint_violated="secrets: committed AWS access key id",
                # Fragmented (not written whole): AWS's own canonical
                # documentation example key id, fed to Finding.build() as
                # synthetic evidence data — but the seal validator scans
                # shipped bytes for exactly this shape, so it must be
                # assembled rather than appear as a literal.
                evidence="AKIA" + "IOSFODNN7EXAMPLE",
                source="gitleaks",
                fix_suggestion="Rotate the key and remove it from the tree.",
            ),
            expect_confirmed=True,
            note="Secret in workbench/ — safety dimension carries past RB-SCOPE (not a gate-nit).",
        ),
        SampleCase(
            _finding(
                layer=Layer.L2_CRITIC,
                dimension=Dimension.CORRECTNESS,
                severity=Severity.MEDIUM,
                file="plugins/foo_plugin/src/client.py",
                line=30,
                constraint_violated="unhandled exception: network call not guarded",
                evidence="resp = httpx.get(url)",
                source="critic:correctness",
                fix_suggestion="Wrap in try/except and add error recovery.",
            ),
            expect_confirmed=False,
            note="DO-NOT-FLAG F2§4.1 — RB-FASTFAIL: absent try/except is policy, not a bug.",
        ),
        SampleCase(
            _finding(
                layer=Layer.L2_CRITIC,
                dimension=Dimension.SECURITY,
                severity=Severity.HIGH,
                file="plugins/foo_plugin/src/api.py",
                line=12,
                constraint_violated="missing per-user rate limiting on endpoint",
                evidence="no rate limiting middleware present",
                source="critic:security",
                fix_suggestion="Add rate limiting and session isolation.",
            ),
            expect_confirmed=False,
            note="DO-NOT-FLAG F2§4.4 — RB-SINGLEUSER: multi-tenant/rate-limit is a category error.",
        ),
        SampleCase(
            _finding(
                layer=Layer.L2_CRITIC,
                dimension=Dimension.ARCHITECTURE,
                severity=Severity.LOW,
                file="ananta/src/ananta/config.py",
                line=5,
                constraint_violated="no backwards-compatibility for the old config schema",
                evidence="load_config assumes v2 keys only",
                source="critic:architecture",
                fix_suggestion="Add a compatibility shim for v1 configs.",
            ),
            expect_confirmed=False,
            note="DO-NOT-FLAG F2§4.6 — RB-FASTFAIL forbids backwards-compat shims.",
        ),
        SampleCase(
            _finding(
                layer=Layer.L1_DETERMINISTIC,
                dimension=Dimension.COMPLEXITY,
                severity=Severity.LOW,
                file="workbench/analyze.py",
                line=100,
                constraint_violated="radon_cc: function rank D",
                evidence="cc=22 on analyze()",
                source="gate:radon_cc",
                fix_suggestion="Decompose the function.",
            ),
            expect_confirmed=False,
            note="DO-NOT-FLAG F2§4.8 — RB-SCOPE: gate-style nit in operator-tooling (workbench/).",
        ),
        SampleCase(
            _finding(
                layer=Layer.L2_CRITIC,
                dimension=Dimension.TYPE_COVERAGE,
                severity=Severity.LOW,
                file="plugins/foo_plugin/tests/test_client.py",
                line=7,
                constraint_violated="broad Any type on fixture return",
                evidence="def fixture() -> Any: ...",
                source="critic:correctness",
                fix_suggestion="Annotate the concrete return type.",
            ),
            expect_confirmed=False,
            note="DO-NOT-FLAG F2§4.10 — the pyright gate excludes test files.",
        ),
        SampleCase(
            _finding(
                layer=Layer.L2_CRITIC,
                dimension=Dimension.CORRECTNESS,
                severity=Severity.LOW,
                file="plugins/foo_plugin/src/util.py",
                line=None,
                constraint_violated="this code could be cleaner",
                evidence="the _normalize helper reads as a bit convoluted",
                source="critic:ai_slop",
                fix_suggestion=None,
            ),
            expect_confirmed=False,
            note="Majority-refute — vague (unpinned) AND unlocatable (code-site, no line): correctness + reproduce both refute.",
        ),
        SampleCase(
            _finding(
                layer=Layer.L2_CRITIC,
                dimension=Dimension.SECURITY,
                severity=Severity.MEDIUM,
                file="plugins/foo_plugin/src/crypto.py",
                line=None,
                constraint_violated="weak crypto: MD5 used for signing (CWE-327)",
                evidence="hashlib.md5(payload).hexdigest() used as a signature",
                source="critic:security",
                fix_suggestion="Use SHA-256.",
            ),
            expect_confirmed=True,
            note="Single-dissent survivor — pinned (CWE-327) so only reproduce refutes (no line to locate); minority. Shows the majority threshold and where the heuristic reproduce-lens is shallower than an inference skeptic.",
        ),
    ]


def sample_candidates() -> list[Finding]:
    """Just the findings, for feeding the verifier / serializing the register."""
    return [case.finding for case in sample_cases()]
