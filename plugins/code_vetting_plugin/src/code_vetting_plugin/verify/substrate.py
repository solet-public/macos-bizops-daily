"""The inference-substrate selector — A0 R3 binding order, made structural.

The L3 skeptic dispatch is *substrate-pluggable*. This module encodes which
substrate a run uses and, critically, the security invariants that ride on that
choice. Binding order (A0 R3):

  1. **Subscription coding-agent sessions** — the operator's already-paid-for
     Claude Code / Codex subscription (the automated in-plugin path is the
     ``claude -p`` :class:`SubprocessSkepticTransport`; the agent-driven bridge
     path is W3-C's joseki). This is an OFF-OPERATOR forward: the review runs off
     the operator's own session, so the prompt is redacted (RIDER-1).
  2. **Platform inference service** — the operator's OWN local models (LM Studio /
     Ollama), the $0-marginal overflow absorber AND the only substrate for a
     privacy-profile run where code must never leave the machine. Local ⇒ NOT an
     off-operator forward ⇒ full evidence, no redaction.
  3. **A metered API key is STRUCTURALLY BANNED.** There is no binding for it and —
     load-bearing — no parameter anywhere in this module (or the transports it
     composes) ACCEPTS an API key / token / secret. The ban is enforced by absence,
     not by a runtime rejection: the surface a caller can reach offers no field to
     put a key in. (Same posture as ``publish_seed``'s no-token rule; guarded by a
     structural test that asserts no key-shaped field exists on the surface.)

The dynamic overflow fallback (subscription throttles → retry on local) is a
caller/joseki orchestration concern; this selector resolves ONE explicit substrate
choice into its dispatcher + off-operator disposition.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .dispatch import SkepticDispatcher
from .inference import InferenceSkepticDispatcher, SkepticTransport, TransportLocality


class Substrate(StrEnum):
    """Which inference substrate an L3 run uses (A0 R3)."""

    SUBSCRIPTION = "subscription"
    """Binding 1 — coding-agent subscription sessions (off-operator)."""
    LOCAL = "local"
    """Binding 2 — platform inference service, local models (overflow; on-machine)."""
    PRIVACY = "privacy"
    """Binding 2, forced — a privacy run that REQUIRES code stay on the machine."""


# The binding order a run tries, most-preferred first (A0 R3). A metered key is
# deliberately absent — it is not a lower-priority option, it is no option at all.
BINDING_ORDER: tuple[Substrate, ...] = (Substrate.SUBSCRIPTION, Substrate.LOCAL)


@dataclass(frozen=True, slots=True)
class SubstrateSelection:
    """A resolved substrate: the dispatcher plus whether it forwards off-operator."""

    dispatcher: SkepticDispatcher
    off_operator: bool


def select_substrate(
    substrate: Substrate,
    *,
    subscription_transport: SkepticTransport | None = None,
    local_transport: SkepticTransport | None = None,
) -> SubstrateSelection:
    """Resolve one substrate choice into its dispatcher + off-operator disposition.

    ``subscription_transport`` is the off-operator substrate (e.g. the ``claude -p``
    subprocess transport); ``local_transport`` is the on-machine platform-inference
    transport. Neither parameter, nor any field of this module, accepts an API key:
    a metered key is structurally unreachable (A0 R3 binding 3).

    Raises ``ValueError`` if the chosen substrate's transport was not supplied — a
    run must not silently fall through to a different substrate than it asked for.
    """
    if substrate is Substrate.SUBSCRIPTION:
        if subscription_transport is None:
            raise ValueError("SUBSCRIPTION substrate selected but no subscription_transport supplied")
        transport = subscription_transport
    else:  # LOCAL / PRIVACY both bind to the on-machine platform inference service.
        if local_transport is None:
            raise ValueError(f"{substrate.value.upper()} substrate selected but no local_transport supplied")
        transport = local_transport
    # RIDER-1: off_operator is derived from the TRANSPORT's OWN declared locality, never
    # from the substrate label — the transport is the ground truth of where the review goes,
    # so a transport cannot be mislabeled into forwarding raw evidence off-machine (the hole
    # Reviewer-C found: PRIVACY bound to a claude -p transport must not full-forward).
    off_operator = transport.locality is TransportLocality.OFF_MACHINE
    # PRIVACY hard gate: the privacy profile requires code NEVER leave the machine, so an
    # off-machine transport is REFUSED outright — redaction (which still emits the locus) is
    # insufficient for the privacy contract; fail loud rather than half-leak.
    if substrate is Substrate.PRIVACY and off_operator:
        raise ValueError(
            "PRIVACY substrate requires an on-machine transport — code must not leave the "
            "machine; refusing an off-machine transport (redaction is not enough for privacy)."
        )
    return SubstrateSelection(InferenceSkepticDispatcher(transport), off_operator=off_operator)
