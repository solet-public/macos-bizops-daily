"""Prior-pass cross-check: the Codex seed review folded into the F1 schema.

``workbench/2026-07-19_codex_seed_review.md`` is a fixed prior review of a published
seed bundle: a minted seed, reviewed as shipped rather than as assembled, which is why
it caught the identity-bleed class the manifest-side checks did not. Its findings are
encoded here as F1 records tagged ``prior_pass:codex_seed_review`` so they ride
the same register as the fresh L1 scan. Where a fresh scanner independently
reproduces one of these classes on the platform tree (e.g. the identity-bleed class,
the mcp CVE), that corroboration is the cross-check the dispatch asked for.

These reference *seed* paths, not platform-tree paths — ``file`` is prefixed with the
seed marker so they never masquerade as fresh platform findings.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..coverage import CoverageRecord, ScannerResult
from ..models import (
    ContextProfile,
    Dimension,
    Finding,
    Layer,
    Provenance,
    Severity,
)
from ..targets import TargetTree

_SCANNER = "prior_pass:codex_seed_review"
_SEED_REF = "seed@3b2e2082"


@dataclass(frozen=True, slots=True)
class _PriorFinding:
    file: str
    line: int | None
    dimension: Dimension
    severity: Severity
    constraint: str
    evidence: str
    fix: str


_PRIOR: tuple[_PriorFinding, ...] = (
    _PriorFinding(
        "bootstrap.py",
        374,
        Dimension.SECURITY,
        Severity.BLOCKER,
        "codex:genesis_sql_injection",
        "unvalidated SOLET_NAME interpolated into admin SQL before Layer-1 validation (first code a stranger runs)",
        "Validate SOLET_NAME against NAME_PATTERN before Layer-0 Postgres access; centralize safe SQL literal rendering.",
    ),
    _PriorFinding(
        "plugins/agent_messaging_plugin/pyproject.toml",
        13,
        Dimension.DEPS,
        Severity.HIGH,
        "codex:mcp_cve",
        "shipped mcp==1.23.3 has CVE-2026-52870/52869/59950; fixed in mcp>=1.28.1",
        "Upgrade the mcp pin to >=1.28.1 and rerun the bridge/OAuth smokes + pip-audit.",
    ),
    _PriorFinding(
        "root_manifest.yaml",
        15,
        Dimension.IDENTITY_LEAK,
        Severity.HIGH,
        "codex:identity_bleed",
        "internal identity/context bleed not zero: root_manifest solet_name <origin>, role labels in shipped src+KB",
        "Rewrite shipped code/KB to neutral examples; add a seed-surface scanner for the blocked identity list.",
    ),
    _PriorFinding(
        "CONTRIBUTING.md",
        1,
        Dimension.KB_DOC_FIDELITY,
        Severity.MEDIUM,
        "codex:contrib_policy_contradiction",
        "shipped CONTRIBUTING/README invite DCO contributions, contradicting the approved no-inbound-contributions release model",
        "Replace CONTRIBUTING with a no-inbound-contributions policy + fork-and-grow guidance; align README/license KB.",
    ),
    _PriorFinding(
        "plugins",
        None,
        Dimension.DEPS,
        Severity.MEDIUM,
        "codex:vulnerable_dep_floors",
        "declared floors (cryptography>=41, pydantic>=2.0, click>=8.1, pynacl>=1.5.0, setuptools>=45) still allow vulnerable versions",
        "Raise minimums above vulnerable versions; ship an audited constraints/lock artifact with hashes.",
    ),
    _PriorFinding(
        "workbench/2026-07-19_seed_public_release_final_review_plan.md",
        39,
        Dimension.KB_DOC_FIDELITY,
        Severity.LOW,
        "codex:plan_inventory_mismatch",
        "review plan scopes ~27 bundle plugins; actual tarball ships 13 plugin directories",
        "Regenerate the plan/scorecard from an actual tarball inventory before assigning lanes.",
    ),
)


def scan(_tree: TargetTree, run_id: str) -> ScannerResult:
    """Emit the Codex seed-review findings as F1 prior-pass records."""
    findings: list[Finding] = []
    for prior in _PRIOR:
        findings.append(
            Finding.build(
                run_id=run_id,
                layer=Layer.L1_DETERMINISTIC,
                dimension=prior.dimension,
                severity=prior.severity,
                file=f"[{_SEED_REF}] {prior.file}",
                line=prior.line,
                constraint_violated=prior.constraint,
                evidence=f"(prior pass, target {_SEED_REF}) {prior.evidence}",
                fix_suggestion=prior.fix,
                provenance=Provenance(source=_SCANNER, rule_id=prior.constraint),
                context_profile=ContextProfile.PRODUCTION,
            )
        )
    return ScannerResult(
        findings=findings,
        coverage=CoverageRecord(scanner=_SCANNER, ran=True, files_examined=len(_PRIOR)),
    )
