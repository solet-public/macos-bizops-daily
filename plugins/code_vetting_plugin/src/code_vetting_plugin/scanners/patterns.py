"""rg-battery content scanner: operator/fleet identity, secret-shape, PII, network-bind.

Regex sweeps (via ``rg --json``) over the *shipping* surface — the code and KB a
seed would carry. Operator tooling (``workbench/``, ``deployment/``), the operator
profile config (legitimately carries operator paths), and tests are excluded.

Each pattern group maps to one F1 dimension:
- operator/fleet identity + PII → ``identity_leak`` (the seed's target-zero bar)
- secret-shape backstop → ``secrets`` (match redacted in evidence)
- external-interface bind (``0.0.0.0``) → ``network_bind``

The identity groups reproduce the Codex seed-review's identity-bleed class as a
deterministic cross-check.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

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
from ..toolrun import run, tool_available, tool_unavailable_reason

_RG = "rg"
_GIT = "git"
_SCANNER = "patterns"
_GIT_CONFIG_TIMEOUT_S = 10
_PII_GAP_REASON = (
    "identity:operator_pii not scanned — the running operator's git user.name and "
    "user.email are both unset, so no PII atom could be derived (the pattern is "
    "composed at runtime and is never a hardcoded literal)"
)

# Directories excluded from the shipping-surface sweep. The top-level
# ``knowledge_bases/`` aggregation dir holds content/research KBs (compositions,
# test-run diff reports) that are not the platform seed surface — the shipping
# platform KB is reached via ``ananta/knowledge_bases/`` and
# ``plugins/*/knowledge_base/``, so excluding the aggregation dir drops content
# noise without losing the platform KB.
_EXCLUDE_GLOBS: tuple[str, ...] = (
    "!workbench/**",
    "!deployment/**",
    "!profile/**",
    "!knowledge_bases/**",
    "!**/test_run_diffs/**",
    "!**/tests/**",
    "!**/research/**",
    "!**/tools/**",
    "!**/migrations/**",
    "!**/parity_tests/**",
    "!**/*.lock",
    "!plugins/code_vetting_plugin/**",
)


@dataclass(frozen=True, slots=True)
class _Pattern:
    regex: str
    dimension: Dimension
    severity: Severity
    constraint: str
    description: str
    fix: str
    redact: bool = False


# Fleet role / session identifiers and operator PII that must not ship in a seed.
# Self-vet severity: identity references are expected on the platform tree itself (the
# seed factory neutralizes them at mint), so fleet-role / operator-path are
# ADVISORY here — they are seed-readiness signals, not platform defects. Against a seed
# target the orchestrator's context profile would raise them to blocker/high.
_PATTERNS: tuple[_Pattern, ...] = (
    _Pattern(
        r"Coordinator-(Day|Dusk|Dawn)|Claude-[ABC]\b|Reviewer-[ABC]\b|Git-Controller",
        Dimension.IDENTITY_LEAK,
        Severity.ADVISORY,
        "identity:fleet_role",
        "fleet role / peer-session identifier",
        "Replace with a neutral placeholder (e.g. <role>, worker-1, Reviewer-A example).",
    ),
    # NOTE: the operator-PII pattern is NOT here — it is composed at scan time from the
    # RUNNING operator's git identity by `_operator_pii_pattern()`. See that function for
    # why a literal in this file was both a guard and a live leak.
    _Pattern(
        # Composed via implicit concatenation so this file's own source never
        # carries the contiguous operator-home literal: this module ships in
        # seeds, and the seed content validator fail-closes on that exact
        # bounded-context marker (same sourcing discipline as the validator's
        # own secret patterns).
        r"/Users/" "dw" r"(/|\b)",
        Dimension.IDENTITY_LEAK,
        Severity.ADVISORY,
        "identity:operator_path",
        "hardcoded operator home path",
        "Derive the path at runtime (app-home / env) instead of hardcoding the operator home path.",
    ),
    # Secret-shape backstop (gitleaks is primary; this catches shapes it may miss).
    _Pattern(
        r"AKIA[0-9A-Z]{16}",
        Dimension.SECRETS,
        Severity.HIGH,
        "secret_shape:aws_access_key",
        "AWS access-key-id shape",
        "Remove and rotate the key; store via the vault.",
        redact=True,
    ),
    _Pattern(
        r"gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}",
        Dimension.SECRETS,
        Severity.HIGH,
        "secret_shape:github_token",
        "GitHub token shape",
        "Remove and rotate the token; store via the vault.",
        redact=True,
    ),
    _Pattern(
        r"sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}",
        Dimension.SECRETS,
        Severity.HIGH,
        "secret_shape:api_key",
        "OpenAI/Anthropic API-key shape",
        "Remove and rotate the key; store via the vault.",
        redact=True,
    ),
    _Pattern(
        r"xox[baprs]-[A-Za-z0-9-]{10,}",
        Dimension.SECRETS,
        Severity.HIGH,
        "secret_shape:slack_token",
        "Slack token shape",
        "Remove and rotate the token; store via the vault.",
        redact=True,
    ),
    _Pattern(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        Dimension.SECRETS,
        Severity.BLOCKER,
        "secret_shape:private_key",
        "PEM private-key header",
        "Remove the private key from source; rotate it; store via the vault.",
        redact=True,
    ),
    # External-interface network bind.
    _Pattern(
        r"0\.0\.0\.0",
        Dimension.NETWORK_BIND,
        Severity.LOW,
        "network_bind:all_interfaces",
        "bind to all interfaces (0.0.0.0)",
        "Confirm the all-interfaces bind is intentional and opt-in; default to 127.0.0.1.",
    ),
)


def _as_str(value: Any) -> str:  # noqa: ANN401 — narrows untyped rg JSON
    return value if isinstance(value, str) else ""


def _as_int(value: Any) -> int | None:  # noqa: ANN401 — narrows untyped rg JSON
    return value if isinstance(value, int) else None


def _extract_submatch(submatches: object) -> str:
    """First submatch's matched text from an ``rg --json`` match event."""
    if not isinstance(submatches, list) or not submatches:
        return ""
    first = submatches[0]
    if not isinstance(first, dict):
        return ""
    match_obj = first.get("match")
    return _as_str(match_obj.get("text")) if isinstance(match_obj, dict) else ""


def _parse_match_event(parsed: object) -> tuple[str, int, str] | None:
    """Narrow one ``rg --json`` line into (path, line, matched_text), or None."""
    if not isinstance(parsed, dict) or parsed.get("type") != "match":
        return None
    data = parsed.get("data")
    if not isinstance(data, dict):
        return None
    path_obj = data.get("path")
    path = _as_str(path_obj.get("text")) if isinstance(path_obj, dict) else ""
    line = _as_int(data.get("line_number"))
    if not path or line is None:
        return None
    return path.removeprefix("./"), line, _extract_submatch(data.get("submatches"))


def _rg_matches(pattern: str, root: str) -> list[tuple[str, int, str]]:
    """Return (path, line, matched_text) tuples for one regex via ``rg --json``."""
    argv = [_RG, "--json", "--no-heading", "-e", pattern]
    for glob in _EXCLUDE_GLOBS:
        argv.extend(["-g", glob])
    argv.append(".")
    outcome = run(argv, cwd=root)
    matches: list[tuple[str, int, str]] = []
    for raw_line in outcome.stdout.splitlines():
        event = _parse_match_event(json.loads(raw_line))
        if event is not None:
            matches.append(event)
    return matches


def _git_global_config(key: str) -> str:
    """The RUNNING operator's ambient git identity value for ``key``; "" when unset.

    An unset key is a legitimate state (a fresh solet with no global git config),
    not a masked failure. The caller turns "" into a visible coverage gap rather than
    interpolating it into a regex — an empty alternation branch matches everywhere.
    """
    if not tool_available(_GIT):
        return ""
    outcome = run(
        [_GIT, "config", "--global", "--get", key],
        timeout_s=_GIT_CONFIG_TIMEOUT_S,
        raise_on_timeout=False,
    )
    return outcome.stdout.strip() if outcome.returncode == 0 else ""


def _operator_pii_pattern() -> _Pattern | None:
    """The operator-PII pattern, composed from the RUNNING operator's git identity.

    Why this is derived and not a literal: a PII detector has to carry the value it
    detects, so the natural implementation is to hardcode it — and this module SHIPS in
    seeds. A hardcoded name/email is therefore simultaneously a contamination guard and
    the exact leak the dimension exists to catch. It also only ever protects ONE
    operator. Deriving at scan time satisfies both constraints: the guard works against
    whoever is actually running it, and nothing personal ships.

    Atoms are ``re.escape``d before they reach the regex — an unescaped ``.`` in a name
    or email would silently widen the pattern into a wildcard. Returns None when neither
    atom is derivable, so the caller can record the gap instead of scanning for "".
    """
    atoms = [
        atom
        for atom in (_git_global_config("user.name"), _git_global_config("user.email"))
        if atom
    ]
    if not atoms:
        return None
    return _Pattern(
        "|".join(re.escape(atom) for atom in atoms),
        Dimension.IDENTITY_LEAK,
        Severity.HIGH,
        "identity:operator_pii",
        "operator email / name (PII)",
        "Remove operator PII; use a generic contact placeholder.",
        redact=True,
    )


def _redacted(matched: str) -> str:
    return f"<redacted {len(matched)}-char match>" if matched else "<redacted>"


def _pattern_findings(pattern: _Pattern, root: str, run_id: str) -> list[Finding]:
    """One aggregated finding per file for a pattern (count + first occurrence).

    High-volume content patterns (operator paths, fleet roles) collapse to one
    record per file rather than one per line, keeping the signal without flooding.
    """
    by_file: dict[str, list[tuple[int, str]]] = {}
    for path, line, matched in _rg_matches(pattern.regex, root):
        by_file.setdefault(path, []).append((line, matched))
    findings: list[Finding] = []
    for path, hits in by_file.items():
        hits.sort()
        first_line, first_match = hits[0]
        shown = _redacted(first_match) if pattern.redact else first_match
        suffix = f"{len(hits)} occurrence(s); first at line {first_line}: {shown}"
        findings.append(
            Finding.build(
                run_id=run_id,
                layer=Layer.L1_DETERMINISTIC,
                dimension=pattern.dimension,
                severity=pattern.severity,
                file=path,
                line=first_line,
                constraint_violated=pattern.constraint,
                evidence=f"{pattern.description}: {suffix}",
                fix_suggestion=pattern.fix,
                provenance=Provenance(source=f"gate:{_SCANNER}", rule_id=pattern.constraint),
                context_profile=ContextProfile.PRODUCTION,
            )
        )
    return findings


def scan(tree: TargetTree, run_id: str) -> ScannerResult:
    """Run the full rg battery over the shipping surface."""
    if not tool_available(_RG):
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(
                scanner=_SCANNER, ran=False, files_examined=0, gap_reason=tool_unavailable_reason(_RG)
            ),
        )
    # The operator-PII pattern is runtime-composed, so its absence is a real reduction in
    # what this scan covered and is disclosed rather than silently passing (F1 §3).
    operator_pii = _operator_pii_pattern()
    patterns = (*_PATTERNS, operator_pii) if operator_pii is not None else _PATTERNS
    findings: list[Finding] = []
    for pattern in patterns:
        findings.extend(_pattern_findings(pattern, str(tree.root), run_id))
    return ScannerResult(
        findings=findings,
        coverage=CoverageRecord(
            scanner=_SCANNER,
            ran=True,
            files_examined=len(tree.model_facing()),
            gap_reason=None if operator_pii is not None else _PII_GAP_REASON,
        ),
    )
