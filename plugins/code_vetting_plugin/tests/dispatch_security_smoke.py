"""Hermetic security smoke for the W3-B B1 inference-dispatch surface.

Pins the two SECURITY-critical invariants of the substrate/dispatch layer (the A2
security-review riders + A0 R3), as durable build-time guards:

- **RIDER-1 redaction.** Sensitive-dimension evidence (SAST source lines, operator
  PII, secret matches, bind config) is withheld from an OFF-OPERATOR forward, while
  the locus (dimension / constraint / file:line / severity / finding_id) is preserved
  so the reviewer can still adjudicate. Non-sensitive findings pass through. The raw
  ``extra_context`` code pack is withheld too. The LOCAL substrate keeps full evidence.
- **Metered-key ban (A0 R3 binding 3), STRUCTURAL.** No field on the dispatch surface
  (transports / dispatchers / the selector) and no verb parameter ACCEPTS an API key /
  token / secret. RED-FIRST: adding such a field fails the scan. Enforcement by absence,
  the durable guard (mirrors the A2.1 deploy-invariant assertion).

Pure Python — no live solet / LM Studio / subprocess / network / mocks. Run via the
gate-smoke runner or directly; the ``code_vetting_plugin`` package must be installed.
"""

from __future__ import annotations

# ruff: noqa: E402
import dataclasses
import inspect
import re
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_vetting_plugin import inference_wiring
from code_vetting_plugin.models import (
    ContextProfile,
    Dimension,
    Finding,
    Layer,
    Provenance,
    Severity,
)
from code_vetting_plugin.plugin import CodeVettingPlugin
from code_vetting_plugin.verify import dispatch, inference, substrate
from code_vetting_plugin.verify.inference import (
    LocalInferenceSkepticTransport,
    SubprocessSkepticTransport,
    TransportLocality,
)
from code_vetting_plugin.verify.lenses import SkepticLens
from code_vetting_plugin.verify.prompts import build_skeptic_prompt
from code_vetting_plugin.verify.redaction import redact_for_off_operator
from code_vetting_plugin.verify.substrate import (
    BINDING_ORDER,
    Substrate,
    select_substrate,
)

# RIDER-2: widened to also catch bare key / auth(orization) / pat / pwd — hardening the
# regression guard (the ban holds by absence today; this makes the tripwire less bypassable).
_KEY_SHAPED = re.compile(
    r"api[_-]?key|token|secret|bearer|access[_-]?key|credential|passw|pwd|\bauth|\bpat\b|\bkey\b",
    re.IGNORECASE,
)
# RIDER-2b (Reviewer-C B3 rider): `token` is a credential marker (api_token / access_token /
# foo_token), but an LLM token-COUNT field (max_tokens, token_count, …) is NOT a credential —
# an unbounded `token` substring false-positives on it. Exclude that count-field class so the
# guard stays honest without weakening `*_token` credential detection.
_TOKEN_COUNT_FIELD = re.compile(r"(?:max|min|num|n)_tokens\b|token_(?:count|limit|budget|size)s?\b", re.IGNORECASE)
_RAW_SAST_LINE = "subprocess.call(user_supplied, shell=True)  # the exact flagged source line"


def _sensitive_finding() -> Finding:
    return Finding.build(
        run_id="rid",
        layer=Layer.L1_DETERMINISTIC,
        dimension=Dimension.SECURITY,
        severity=Severity.HIGH,
        file="plugins/foo_plugin/src/foo_plugin/exec.py",
        line=42,
        constraint_violated="bandit:B602:subprocess_popen_with_shell_equals_true",
        evidence=_RAW_SAST_LINE,
        fix_suggestion="rewrite `subprocess.call(user_supplied, shell=True)` as shell=False with a list argv",
        provenance=Provenance(source="bandit"),
        context_profile=ContextProfile.PRODUCTION,
    )


def _nonsensitive_finding() -> Finding:
    return Finding.build(
        run_id="rid",
        layer=Layer.L1_DETERMINISTIC,
        dimension=Dimension.CODE_QUALITY,
        severity=Severity.HIGH,
        file="plugins/foo_plugin/src/foo_plugin/widget.py",
        line=None,
        constraint_violated="gate:radon_cc",
        evidence="radon cc: _dispatch ranked C (16) — not in the allowlist",
        provenance=Provenance(source="gate:code_quality_check"),
        context_profile=ContextProfile.PRODUCTION,
    )


def test_redaction_withholds_sensitive_evidence_preserves_locus() -> None:
    f = _sensitive_finding()
    r = redact_for_off_operator(f)
    assert "REDACTED" in r.evidence
    assert _RAW_SAST_LINE not in r.evidence, "the raw SAST source line must NOT survive redaction"
    # RIDER-3: fix_suggestion is a second raw-content surface — it must be dropped off-operator.
    assert f.fix_suggestion is not None and r.fix_suggestion is None
    # Locus + identity preserved so the off-operator skeptic can still adjudicate.
    assert (r.finding_id, r.dimension, r.constraint_violated, r.file, r.line, r.severity) == (
        f.finding_id,
        f.dimension,
        f.constraint_violated,
        f.file,
        f.line,
        f.severity,
    )


def test_redaction_passes_through_non_sensitive() -> None:
    f = _nonsensitive_finding()
    assert redact_for_off_operator(f) is f, "non-sensitive findings must pass through unchanged"


def test_prompt_off_operator_withholds_both_leak_surfaces() -> None:
    f = _sensitive_finding()
    raw_pack = "def exec(): subprocess.call(user_supplied, shell=True)  # RAW SOURCE UNDER REVIEW"
    off = build_skeptic_prompt(f, SkepticLens.POLICY, "RULEBOOK", extra_context=raw_pack, off_operator=True)
    on = build_skeptic_prompt(f, SkepticLens.POLICY, "RULEBOOK", extra_context=raw_pack, off_operator=False)
    # Off-operator: neither the finding evidence nor the raw code pack may appear.
    assert _RAW_SAST_LINE not in off
    assert raw_pack not in off
    assert "RAW SOURCE UNDER REVIEW" not in off
    # But the locus is retained so the reviewer can still act.
    assert f.file in off and f.constraint_violated in off
    # On the operator's own session (or LOCAL), full evidence + code pack are present.
    assert _RAW_SAST_LINE in on and raw_pack in on


def test_selector_binding_order_and_off_operator_disposition() -> None:
    sub = SubprocessSkepticTransport(cwd=".")
    loc = LocalInferenceSkepticTransport(infer_fn=lambda _p: "vote: UPHOLD")
    subscription = select_substrate(Substrate.SUBSCRIPTION, subscription_transport=sub, local_transport=loc)
    assert subscription.off_operator is True, "the subscription substrate is an off-operator forward"
    for local_like in (Substrate.LOCAL, Substrate.PRIVACY):
        chosen = select_substrate(local_like, subscription_transport=sub, local_transport=loc)
        assert chosen.off_operator is False, f"{local_like} runs on-machine — not an off-operator forward"
    # A run must fail loud rather than silently forward to a substrate it did not ask for.
    try:
        select_substrate(Substrate.SUBSCRIPTION, local_transport=loc)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("missing subscription_transport must fail loud")
    # The binding order contains no metered-key option — it is absent, not deprioritized.
    assert set(BINDING_ORDER) == {Substrate.SUBSCRIPTION, Substrate.LOCAL}


def test_local_inference_transport_wraps_infer_fn() -> None:
    seen: dict[str, str] = {}

    def fake(prompt: str) -> str:
        seen["prompt"] = prompt
        return "vote: REFUTE\nrationale: refuted"

    request = dispatch.SkepticRequest(_sensitive_finding(), SkepticLens.CORRECTNESS, "PROMPT-TEXT")
    out = LocalInferenceSkepticTransport(infer_fn=fake).infer(request)
    assert seen["prompt"] == "PROMPT-TEXT" and "REFUTE" in out
    # An empty local reply becomes a no-verdict marker (parsed UNCERTAIN), never a silent pass.
    empty = LocalInferenceSkepticTransport(infer_fn=lambda _p: "   ").infer(request)
    assert "EMPTY" in empty


def _dispatch_surface_field_names() -> list[str]:
    """Every field a caller can set on the transports / dispatchers / selector surface."""
    names: list[str] = []
    surface = [
        inference.SubprocessSkepticTransport,
        inference.LocalInferenceSkepticTransport,
        inference.InferenceSkepticDispatcher,
        inference.RecordedTransport,
        dispatch.HeuristicSkepticDispatcher,
        substrate.SubstrateSelection,
    ]
    for cls in surface:
        names.extend(field.name for field in dataclasses.fields(cls))
    names.extend(inspect.signature(select_substrate).parameters)
    # RIDER-A (Reviewer-C B3 rider): the B3c inference-wiring seam is caller-reachable too —
    # extend the same inspect.signature ban-scan to its transport/driver factories and the
    # inference-completer Protocol, so a future key-shaped param there can't evade the ban.
    for fn in (
        inference_wiring.make_local_infer_fn,
        inference_wiring.build_substrate_verifier,
        inference_wiring.assemble_inference_driver,
        inference_wiring.InferenceCompleter.generate_completion,
        CodeVettingPlugin.build_inference_driver,
        CodeVettingPlugin.build_local_skeptic_transport,
        CodeVettingPlugin.build_subscription_skeptic_transport,
    ):
        names.extend(inspect.signature(fn).parameters)
    return names


def _key_shaped(name: str) -> bool:
    """True when ``name`` is a credential-shaped field — excluding the LLM token-COUNT class (RIDER-2b)."""
    return bool(_KEY_SHAPED.search(name)) and not _TOKEN_COUNT_FIELD.search(name)


def test_metered_key_ban_is_structural() -> None:
    # A0 R3 binding 3: no field on the dispatch surface may ACCEPT a metered API key.
    # Enforcement is by ABSENCE — this is the red-first guard against a future key field.
    offenders = [name for name in _dispatch_surface_field_names() if _key_shaped(name)]
    assert not offenders, f"metered-key ban violated — key-shaped field(s) on the dispatch surface: {offenders}"
    # The verbs accept no key/token param either.
    for verb in ("vet_codebase", "scan_quality_guidelines"):
        params = getattr(CodeVettingPlugin, verb)._platform_process_metadata.parameters
        bad = [name for name in params if _key_shaped(name)]
        assert not bad, f"{verb} accepts a key-shaped parameter: {bad}"
    # RIDER-2b red-first: the token-COUNT exclusion must NOT let a real credential field through,
    # and must NOT flag a legitimate token-count field.
    assert _key_shaped("api_token") and _key_shaped("access_token") and _key_shaped("foo_token"), "credential *_token fields must still be caught"
    assert not _key_shaped("max_tokens") and not _key_shaped("token_count") and not _key_shaped("num_tokens"), "LLM token-count fields must not false-positive"


def test_off_operator_derives_from_transport_locality_not_substrate_label() -> None:
    # RIDER-1 (the security fix): off_operator follows the TRANSPORT's declared locality,
    # not the substrate label — so a PRIVACY/LOCAL run can never full-forward off-machine.
    off = SubprocessSkepticTransport(cwd=".")
    on = LocalInferenceSkepticTransport(infer_fn=lambda _p: "vote: UPHOLD")
    assert off.locality is TransportLocality.OFF_MACHINE
    assert on.locality is TransportLocality.ON_MACHINE
    # PRIVACY hard-refuses an off-machine transport — nothing may leave the machine.
    try:
        select_substrate(Substrate.PRIVACY, subscription_transport=off, local_transport=off)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("PRIVACY must refuse an off-machine transport")
    # LOCAL mistakenly bound to an off-machine transport still REDACTS (never full-forwards):
    # off_operator is derived from the transport, so it is True even though the label is LOCAL.
    assert select_substrate(Substrate.LOCAL, local_transport=off).off_operator is True
    # A normal on-machine local run keeps full evidence.
    assert select_substrate(Substrate.LOCAL, local_transport=on).off_operator is False


def main() -> int:
    tests = [
        test_redaction_withholds_sensitive_evidence_preserves_locus,
        test_redaction_passes_through_non_sensitive,
        test_prompt_off_operator_withholds_both_leak_surfaces,
        test_selector_binding_order_and_off_operator_disposition,
        test_off_operator_derives_from_transport_locality_not_substrate_label,
        test_local_inference_transport_wraps_infer_fn,
        test_metered_key_ban_is_structural,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"OK: {len(tests)} code_vetting_plugin dispatch-security smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
