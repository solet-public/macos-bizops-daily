"""RIDER-1: withhold sensitive finding evidence before an OFF-OPERATOR forward.

Vetting findings carry raw operator content in their ``evidence``: a SAST finding
quotes the exact source line bandit/semgrep flagged; an ``identity_leak`` finding
quotes the matched operator name/handle; a ``secrets`` finding carries a (already
redacted) match; a ``network_bind`` finding quotes the bind configuration. On the
operator's OWN session — the local vetting report — that is correct and useful.

But the dispatcher forwards findings to reviewer sessions OFF the operator's own
session: a ``claude -p`` subprocess (the operator's subscription, i.e. Anthropic's
servers) or a future advertised bridge backend. Raw operator source and PII must
not cross that trust boundary. This is the W3-A A2 security-review RIDER-1.

Two design properties:

- **Boundary, not scan-time.** Redaction is applied at the dispatch boundary (what
  LEAVES the operator's trust domain), never at scan time — the local report
  legitimately shows the operator full evidence.
- **Substrate-aware.** It applies ONLY to off-operator substrates. The LOCAL
  inference substrate (the privacy profile) keeps code on the machine, so it needs
  and receives the full evidence — redacting there would blind it for nothing.

The *evidence* AND *fix_suggestion* of SENSITIVE dimensions are withheld (both can
carry a raw quoted source line — RIDER-3); the dimension, ``constraint_violated``,
``file:line``, and severity are preserved so the off-operator skeptic can still
adjudicate the finding against the rulebook — it simply does not receive the raw
quoted content (``constraint_violated`` is a generic rule-id, not raw content). The
``finding_id`` is unchanged (it hashes the dedup tuple, not the evidence), so the
candidate→verdict trail holds.
"""

from __future__ import annotations

from dataclasses import replace

from ..models import Dimension, Finding

# Dimensions whose evidence definitionally carries raw operator content — source
# lines (SAST), operator PII (identity leak), secret matches, or bind config —
# that must not leave the operator's trust domain. Non-sensitive dimensions (gate
# output, dependency/license facts, structural counts) carry no secret and the
# skeptic needs their evidence to adjudicate, so they pass through unchanged.
_SENSITIVE_DIMENSIONS: frozenset[Dimension] = frozenset(
    {
        Dimension.SECURITY,
        Dimension.SECRETS,
        Dimension.IDENTITY_LEAK,
        Dimension.NETWORK_BIND,
    }
)


def is_sensitive(finding: Finding) -> bool:
    """True when the finding's evidence carries raw operator content (RIDER-1)."""
    return finding.dimension in _SENSITIVE_DIMENSIONS


def _redaction_marker(finding: Finding) -> str:
    return (
        f"[REDACTED — {finding.dimension.value} evidence withheld from an off-operator "
        "reviewer (RIDER-1). Adjudicate against the constraint and file:line above; the "
        "raw quoted content stays on the operator's machine.]"
    )


def redact_for_off_operator(finding: Finding) -> Finding:
    """Return a copy safe to forward off-operator: sensitive-dimension evidence withheld.

    A non-sensitive finding is returned unchanged (same object). For a sensitive
    finding, ``evidence`` is replaced with a marker and ``fix_suggestion`` is dropped
    (RIDER-3 — a fix can quote the vulnerable line); ``finding_id``, dimension,
    severity, ``constraint_violated``, ``file``, and ``line`` are all preserved, so
    the off-operator skeptic still knows exactly what rule at what site to judge.
    """
    if not is_sensitive(finding):
        return finding
    return replace(finding, evidence=_redaction_marker(finding), fix_suggestion=None)
