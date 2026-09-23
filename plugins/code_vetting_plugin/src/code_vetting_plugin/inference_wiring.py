"""inference_wiring.py — the STAGED SEAM composing the B1 dispatch substrate into a driver.

W3-B B3c wires the B1 inference-dispatch substrate (the substrate SELECTOR + the inference
SkepticDispatcher + the off-operator REDACTION) into the Stream-O ``VettingDriver``: given a
``Substrate`` choice and its transport(s), it resolves the off-operator-safe
``SubstrateSelection`` (RIDER-1: ``off_operator`` is derived from the transport's OWN declared
locality, never the substrate label) and assembles a driver whose L3 is the inference-backed
``AdversarialVerifier``.

**This is a PLUGIN-LAYER PRIMITIVE, not a live caller — deliberately staged.** Neither EDGE
verb (``vet_codebase`` / ``scan_quality_guidelines``) calls anything here: they stay L1-only
(A0 R6), and the plugin imports this module only inside its seam methods so the inference
subsystem never enters the verbs' import closure. The LIVE LOCAL/SUBSCRIPTION caller is
W3-C's joseki (and the dogfood W3-C homes) — the same staged pattern B1 shipped ("plugin-layer
primitives; W3-C wires a caller", Reviewer-C-concurred). The seam is NOT dead code: the
``inference_wiring_smoke`` drives ``assemble_inference_driver`` end-to-end with a
``RecordedTransport`` (subscription) and a fake ``infer_fn`` (local), proving the substrate ->
verifier -> driver -> report path runs and the locality-derived redaction disposition holds.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from ananta.interfaces.inference_service_interface import InferenceRequest

from .driver import Clock, VettingDriver
from .l1_scanner import L1DeterministicScanner
from .l3_adapter import VerifierL3Adapter
from .metrics import MetricsWriter
from .models import ContextProfile
from .report import ReportRenderer
from .verify.inference import SkepticTransport
from .verify.rulebook import Rulebook
from .verify.substrate import Substrate, select_substrate
from .verify.tiers import ALL_TIERS, PolicyTier
from .verify.verifier import AdversarialVerifier

# Skeptic verdicts are short structured replies (vote / dispositive / rule_id / rationale),
# temperature 0 for determinism — NOT a model path (the bound provider owns model choice).
_SKEPTIC_TEMPERATURE = 0.0
_SKEPTIC_MAX_TOKENS = 4096


@runtime_checkable
class InferenceCompleter(Protocol):
    """The ONE method the L3 skeptic needs from the platform inference service.

    A structural (``runtime_checkable``) Protocol rather than the concrete
    ``InferenceServiceInterface`` so an ``isinstance`` narrow is wrapper-safe:
    ``orchestrator.get_service('inference_service')`` may return a service wrapper that
    delegates ``generate_completion`` without literally subclassing the ABC. No key/token
    field exists here — the local model is configured platform-side (A0 R3 binding 3).
    """

    def generate_completion(self, request: InferenceRequest) -> object: ...

# Completion-text locations inside a usable generate_completion envelope, canonical first.
_COMPLETION_TEXT_PATHS: tuple[tuple[str, ...], ...] = (
    ("result", "completion"),
    ("completion",),
    ("text",),
    ("message", "content"),
)


def _envelope_data(result: object) -> dict[str, object] | None:
    """Unwrap a usable generate_completion envelope to its data dict (else None → fail-loud)."""
    if not isinstance(result, dict) or result.get("error"):
        return None
    if result.get("action_status") not in (None, "completed"):
        return None
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    return data if isinstance(data, dict) else None


def _text_at(data: dict[str, object], path: tuple[str, ...]) -> str:
    """Non-blank string content at ``path`` inside ``data``, else ``""``."""
    node: object = data
    for key in path:
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
    return node if isinstance(node, str) and node.strip() else ""


def _extract_completion_text(result: object) -> str:
    """Pull completion text out of a generate_completion envelope (empty string if none)."""
    data = _envelope_data(result)
    if data is None:
        return ""
    for path in _COMPLETION_TEXT_PATHS:
        text = _text_at(data, path)
        if text:
            return text
    return ""


def make_local_infer_fn(inference_service: InferenceCompleter) -> Callable[[str], str]:
    """A ``prompt -> reply text`` closure over ``inference_service.generate_completion``.

    The ON-MACHINE binding-2 substrate (A0 R3): the platform's own local models. Freeform
    reply (``use_structured_output=False``); a provider envelope with no usable completion is
    a fail-loud RuntimeError (never a silent empty skeptic → the aggregator would drop the
    finding on a false negative). A metered key is structurally absent — the local model is
    configured platform-side and no key/token is threaded here.
    """

    def _infer(prompt: str) -> str:
        request = InferenceRequest(
            [{"role": "user", "content": prompt}],
            temperature=_SKEPTIC_TEMPERATURE,
            max_tokens=_SKEPTIC_MAX_TOKENS,
            use_structured_output=False,
            context_metadata={"purpose": "code_vetting_l3_skeptic"},
        )
        result = inference_service.generate_completion(request)
        text = _extract_completion_text(result)
        if not text:
            raise RuntimeError(
                "inference_service returned no usable completion for an L3 skeptic — the "
                "provider envelope carried an error, a non-completed status, or empty text"
            )
        return text

    return _infer


def build_substrate_verifier(
    substrate: Substrate,
    *,
    subscription_transport: SkepticTransport | None,
    local_transport: SkepticTransport | None,
    rulebook: Rulebook,
    active_tiers: frozenset[PolicyTier] = ALL_TIERS,
) -> VerifierL3Adapter:
    """Resolve a substrate choice into the L3 adapter: select -> AdversarialVerifier -> adapter.

    ``select_substrate`` supplies the dispatcher AND the locality-derived ``off_operator``
    disposition (RIDER-1) and PRIVACY hard-refuses an off-machine transport; the verifier
    carries ``off_operator`` so the forwarded prompt is redacted on an off-operator hop.
    ``active_tiers`` (FT-2, DERIVED from the target class) filters the refute-directive to
    the active tier stack — a foreign target drops the project-local POLICY clauses. Default =
    the full self-vet stack.
    """
    selection = select_substrate(
        substrate,
        subscription_transport=subscription_transport,
        local_transport=local_transport,
    )
    return VerifierL3Adapter(
        AdversarialVerifier(
            selection.dispatcher, rulebook, off_operator=selection.off_operator, active_tiers=active_tiers
        )
    )


def assemble_inference_driver(
    *,
    root: Path,
    substrate: Substrate,
    subscription_transport: SkepticTransport | None,
    local_transport: SkepticTransport | None,
    rulebook: Rulebook,
    metrics_writer: MetricsWriter,
    clock: Clock,
    context_profile: ContextProfile,
    active_tiers: frozenset[PolicyTier] = ALL_TIERS,
) -> VettingDriver:
    """Assemble the inference-backed ``VettingDriver`` (L1 + substrate-selected L3 + report).

    L2 critics stay empty (inference AI critics are agent-orchestrated, W3-C); the L3 verifier
    is inference-backed via ``build_substrate_verifier``. ``metrics_writer`` is injected by the
    caller (W3-C / the dogfood supply the live state writer; the smoke supplies an in-memory
    one), so this seam takes on no state-service concern of its own. ``substrate`` is recorded
    on the metrics row so the provenance names which engine reviewed the run (B2).
    ``active_tiers`` (FT-2, from the target class) tier-filters the L3 POLICY directive so a
    foreign target's skeptic never refutes a real finding on an project-local ground.
    """
    return VettingDriver(
        l1=L1DeterministicScanner(root=root),
        l2_critics=(),
        l3=build_substrate_verifier(
            substrate,
            subscription_transport=subscription_transport,
            local_transport=local_transport,
            rulebook=rulebook,
            active_tiers=active_tiers,
        ),
        renderer=ReportRenderer(),
        metrics_writer=metrics_writer,
        clock=clock,
        context_profile=context_profile,
        substrate=substrate.value,
    )
