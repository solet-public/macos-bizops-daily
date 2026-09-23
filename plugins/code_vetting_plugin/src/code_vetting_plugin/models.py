"""models.py — the F1 shared binding for the AI code-vetting suite.

The single canonical Python encoding of the F1 finding schema
(``workbench/2026-07-19_vetting_finding_schema_v1.md``). Every layer imports the
finding record, the enums, and the ``finding_id`` hash FROM HERE — L1 scanners,
L2 critics, the L3 verifier, and the Stream-O orchestrator/report/metrics all
emit and read this one shape. The dedup hash (:meth:`Finding.compute_id`) is
defined here and nowhere else: cross-layer and cross-run deduplication depends on
exactly one definition of ``finding_id``.

Kept deliberately tiny — the finding record, its enums, and JSON round-trip only.
The ``vetting_runs`` metrics row and the per-layer scanner types live in their
owning modules (Stream O's ``metrics.py``; L1's scanner package), not here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum


class Layer(StrEnum):
    """Which vetting layer produced (or last stamped) the finding (F1 §1)."""

    L1_DETERMINISTIC = "L1_deterministic"
    L2_CRITIC = "L2_critic"
    L3_VERIFIED = "L3_verified"


class Dimension(StrEnum):
    """Finding category (F1 §2).

    Dimensions are **orthogonal to ``layer``** — most can be produced
    deterministically (an L1 tool) AND/OR by a critic (L2); the ``layer`` +
    ``provenance`` fields carry the source. ``security`` in particular spans both
    (L1 SAST — bandit/semgrep/sql_access — and the L2 security critic). The
    groupings below are "typical origin", not a partition the schema enforces.
    Adding a member is a one-line change here plus a note in the run report,
    never an ad-hoc string in a scanner, so the metrics aggregate cleanly.
    """

    # Typically deterministic (L1 tools/gates)
    SECRETS = "secrets"
    DEPS = "deps"
    LICENSE = "license"
    IDENTITY_LEAK = "identity_leak"
    HIDDEN_UNICODE = "hidden_unicode"
    NETWORK_BIND = "network_bind"
    DUP = "dup"
    ORPHAN = "orphan"
    COMPLEXITY = "complexity"
    DEAD_CODE = "dead_code"
    TYPE_COVERAGE = "type_coverage"
    CODE_QUALITY = "code_quality"  # generic gate/lint findings (ruff, god-class, service-interface-ast)
    TEST_REACH = "test_reach"  # deterministic: an owned module/verb surface with zero test reach (evidence, not a %)
    # Typically critic (L2)
    CORRECTNESS = "correctness"
    ARCHITECTURE = "architecture"
    AI_SLOP = "ai_slop"
    TEST_ADEQUACY = "test_adequacy"
    KB_DOC_FIDELITY = "kb_doc_fidelity"
    # Spans both layers (L1 SAST + L2 critic)
    SECURITY = "security"


class Severity(StrEnum):
    """Blocking weight, calibrated by ``context_profile`` at the report stage."""

    BLOCKER = "blocker"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    ADVISORY = "advisory"


class Verdict(StrEnum):
    """Lifecycle state. L1/L2 emit ``candidate``; only L3 sets confirmed/refuted."""

    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REFUTED = "refuted"


class ContextProfile(StrEnum):
    """Stakes tier driving blocking-vs-advisory. Self-vet of the platform is ``production``."""

    PROTOTYPE = "prototype"
    PRODUCTION = "production"
    ENTERPRISE = "enterprise"


def _require_str(source: Mapping[str, object], key: str) -> str:
    """Extract a required string field, failing loud on absence or wrong type."""
    value = source[key]
    if not isinstance(value, str):
        raise TypeError(f"field {key!r} must be a string, got {type(value).__name__}")
    return value


def _optional_str(source: Mapping[str, object], key: str) -> str | None:
    """Extract an optional string field (``None`` passes through)."""
    value = source[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"field {key!r} must be a string or null, got {type(value).__name__}")
    return value


def _optional_int(source: Mapping[str, object], key: str) -> int | None:
    """Extract an optional 1-indexed line number (``None`` for file-level findings)."""
    value = source[key]
    if value is None:
        return None
    if not isinstance(value, int):
        raise TypeError(f"field {key!r} must be an int or null, got {type(value).__name__}")
    return value


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a finding came from — the tool/gate/critic that produced it (F1 §1)."""

    source: str
    tool_version: str | None = None
    critic_lens: str | None = None
    rule_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping."""
        return {
            "source": self.source,
            "tool_version": self.tool_version,
            "critic_lens": self.critic_lens,
            "rule_id": self.rule_id,
        }

    @classmethod
    def from_dict(cls, source: Mapping[str, object]) -> Provenance:
        """Reconstruct from a mapping produced by :meth:`to_dict`."""
        return cls(
            source=_require_str(source, "source"),
            tool_version=_optional_str(source, "tool_version"),
            critic_lens=_optional_str(source, "critic_lens"),
            rule_id=_optional_str(source, "rule_id"),
        )


@dataclass(frozen=True, slots=True)
class Finding:
    """One structured finding, emitted identically by every layer (F1 §1).

    Construct through :meth:`build` so ``finding_id`` stays a stable hash of the
    dedup tuple ``(run_id, file, line, dimension, constraint_violated)``. L3
    re-stamps an existing finding with :meth:`with_verdict` rather than creating
    a new record, preserving the id that links a candidate to its verdict.
    """

    finding_id: str
    run_id: str
    layer: Layer
    dimension: Dimension
    severity: Severity
    file: str
    line: int | None
    constraint_violated: str
    evidence: str
    fix_suggestion: str | None
    provenance: Provenance
    verdict: Verdict
    context_profile: ContextProfile

    @staticmethod
    def compute_id(
        run_id: str,
        file: str,
        line: int | None,
        dimension: Dimension,
        constraint_violated: str,
    ) -> str:
        """The one canonical dedup hash across layers and runs (F1 §1).

        Defined only here; no layer reimplements it, so a finding surfaced by L1
        and re-stamped by L3 keeps one identity.
        """
        payload = " ".join(
            (run_id, file, "" if line is None else str(line), dimension.value, constraint_violated)
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"vf-{digest[:16]}"

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        layer: Layer,
        dimension: Dimension,
        severity: Severity,
        file: str,
        line: int | None,
        constraint_violated: str,
        evidence: str,
        provenance: Provenance,
        context_profile: ContextProfile,
        fix_suggestion: str | None = None,
        verdict: Verdict = Verdict.CANDIDATE,
    ) -> Finding:
        """Construct a finding with a derived ``finding_id``.

        ``constraint_violated`` and ``evidence`` are both required and must be
        non-empty (not just whitespace): a finding with no named constraint is not
        a finding, and a finding with no proof is an evidence-less survivor L3
        cannot adjudicate (F1 §1).
        """
        if not constraint_violated.strip():
            raise ValueError("constraint_violated is required — a finding must name the rule it breaks")
        if not evidence.strip():
            raise ValueError("evidence is required — a finding must carry the proof it exists (F1 §1)")
        finding_id = cls.compute_id(run_id, file, line, dimension, constraint_violated)
        return cls(
            finding_id=finding_id,
            run_id=run_id,
            layer=layer,
            dimension=dimension,
            severity=severity,
            file=file,
            line=line,
            constraint_violated=constraint_violated,
            evidence=evidence,
            fix_suggestion=fix_suggestion,
            provenance=provenance,
            verdict=verdict,
            context_profile=context_profile,
        )

    def with_verdict(self, verdict: Verdict, layer: Layer = Layer.L3_VERIFIED) -> Finding:
        """Return a copy re-stamped by L3 (F1 §1).

        L3 creates no findings — it stamps ``verdict`` (confirmed/refuted) and
        promotes ``layer`` to ``L3_verified``. The ``finding_id`` is preserved
        (the dedup tuple is unchanged), so the trail links candidate to verdict.
        """
        return replace(self, verdict=verdict, layer=layer)

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping with enums flattened to their string values."""
        return {
            "finding_id": self.finding_id,
            "run_id": self.run_id,
            "layer": self.layer.value,
            "dimension": self.dimension.value,
            "severity": self.severity.value,
            "file": self.file,
            "line": self.line,
            "constraint_violated": self.constraint_violated,
            "evidence": self.evidence,
            "fix_suggestion": self.fix_suggestion,
            "provenance": self.provenance.to_dict(),
            "verdict": self.verdict.value,
            "context_profile": self.context_profile.value,
        }

    @classmethod
    def from_dict(cls, source: Mapping[str, object]) -> Finding:
        """Reconstruct from a mapping produced by :meth:`to_dict`."""
        provenance_raw = source["provenance"]
        if not isinstance(provenance_raw, Mapping):
            raise TypeError("field 'provenance' must be a mapping")
        return cls(
            finding_id=_require_str(source, "finding_id"),
            run_id=_require_str(source, "run_id"),
            layer=Layer(_require_str(source, "layer")),
            dimension=Dimension(_require_str(source, "dimension")),
            severity=Severity(_require_str(source, "severity")),
            file=_require_str(source, "file"),
            line=_optional_int(source, "line"),
            constraint_violated=_require_str(source, "constraint_violated"),
            evidence=_require_str(source, "evidence"),
            fix_suggestion=_optional_str(source, "fix_suggestion"),
            provenance=Provenance.from_dict(provenance_raw),
            verdict=Verdict(_require_str(source, "verdict")),
            context_profile=ContextProfile(_require_str(source, "context_profile")),
        )


def findings_to_json(findings: Sequence[Finding]) -> str:
    """Serialize a finding list to deterministic JSON (stable key order)."""
    return json.dumps([finding.to_dict() for finding in findings], indent=2, ensure_ascii=True, sort_keys=True)


def findings_from_json(payload: str) -> list[Finding]:
    """Deserialize a JSON array of finding records, failing loud on shape drift."""
    raw = json.loads(payload)
    if not isinstance(raw, list):
        raise TypeError("expected a JSON array of finding records")
    return [Finding.from_dict(item) for item in raw]
