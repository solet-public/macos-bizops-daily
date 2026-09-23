"""L3 driver adapter — wraps the L3 verifier as the async ``L3Verifier`` seam.

Stream L3's :class:`~verify.verifier.AdversarialVerifier` is synchronous and
returns rich :class:`VerificationOutcome`s (the re-stamped finding plus its
per-lens skeptic votes — the survival-rate audit trail). The Stream-O driver's
``L3Verifier`` Protocol wants ``async def verify(candidates) -> list[Finding]``.
This adapter bridges the two: it runs verification off the event loop (the
dispatcher may be I/O-bound once the inference substrate lands) and projects each
outcome to its stamped finding.

The wrapping is dispatcher-agnostic: the same adapter carries the deterministic
``HeuristicSkepticDispatcher`` (Wave 1) and the inference dispatcher (W3) with no
change, because both satisfy the ``AdversarialVerifier``'s dispatcher seam.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from .models import Finding
from .verify.verifier import AdversarialVerifier


@dataclass(frozen=True, slots=True)
class VerifierL3Adapter:
    """Adapts :class:`AdversarialVerifier` to the async ``L3Verifier`` Protocol."""

    verifier: AdversarialVerifier

    async def verify(self, candidates: Sequence[Finding]) -> list[Finding]:
        """Verify candidates off the event loop; return the stamped findings."""
        if not candidates:
            return []
        outcomes = await asyncio.to_thread(self.verifier.verify, list(candidates))
        return [outcome.finding for outcome in outcomes]
