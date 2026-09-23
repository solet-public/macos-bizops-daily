"""Non-public cutover controller for manager-driven target reconciliation.

The adjudicated cutover interface (``workbench/2026-09-03_adjudication_pair150_
cutover_interface.md``, D4) gives the reconciliation path a SECOND ENTRY POINT
into the blue-green machinery, never a second implementation.  The old
router-served process predates this interface and cannot be asked to answer a
new verb, so the refreshed target-local adapter imports this controller from
refreshed plugin code and hands it a validated typed request.

What lives here is exactly the part that is NOT blue-green: the closed request
shape, the compare-and-swap legs that must refuse BEFORE anything is spawned,
and the provenance the candidate's ``VERSION`` has to carry.  The swap itself
is delegated through :class:`CutoverExecutor` to the one
:class:`~macos_self_deployment_plugin.swap_orchestrator.SwapOrchestrator`
implementation the ordinary service verbs already use.  Review fails if swap
logic appears in this module.

Digests are computed by the shared
:mod:`~macos_self_deployment_plugin.surface_digest` library introduced by T1;
this module adds no second digest construction.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol

from ananta.interfaces.lifecycle_result_types import RestartResult

from macos_self_deployment_plugin.cutover_capability import require_cutover_capability

__all__ = [
    "CutoverExecutor",
    "CutoverJournalStore",
    "CutoverOutcome",
    "SwapEvidence",
    "CutoverProvenance",
    "CutoverRefusalError",
    "ReconciliationCutoverController",
    "ReconciliationCutoverRequest",
    "TargetRuntimeObservation",
    "observation_from_attestation",
    "swap_evidence_from_restart_result",
]

_SHA256: Final[re.Pattern[str]] = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RECONCILIATION_ID: Final[re.Pattern[str]] = re.compile(r"rec_[a-zA-Z0-9_-]+\Z")

#: The one ``reason`` this controller accepts.  A free-text reason would let a
#: caller disguise a reconciliation cutover as an ordinary deploy in the audit
#: trail, which is precisely the distinction the receipt has to preserve.
CUTOVER_REASON: Final[str] = "manager_adapter_reconciliation"

REFUSAL_CURRENT_RELEASE: Final[str] = "current_release_mismatch"
REFUSAL_ACTIVE_INSTANCE: Final[str] = "active_instance_mismatch"
REFUSAL_ACTIVE_START_TOKEN: Final[str] = "active_start_token_mismatch"
REFUSAL_MANIFEST_ETAG: Final[str] = "manifest_etag_mismatch"
REFUSAL_SOURCE_SURFACE: Final[str] = "source_surface_mismatch"
REFUSAL_RELEASE_SURFACE: Final[str] = "release_surface_mismatch"
REFUSAL_NOTHING_TO_RECOVER: Final[str] = "nothing_to_recover"
#: An apply arrived for an id whose attempt is still in flight.  This is the
#: leg that actually prevents a second candidate: the durable-replay check
#: above only catches attempts that already REACHED a terminal result, and an
#: attempt interrupted mid-swap has none.  Without this, a retried apply for
#: an interrupted cutover passes replay, passes CAS, and spawns again.
REFUSAL_ATTEMPT_IN_FLIGHT: Final[str] = "attempt_already_in_flight"


class CutoverRefusalError(Exception):
    """A compare-and-swap leg refused; no candidate was spawned.

    ``code`` is the machine-readable reason the manager maps onto the
    ``approval_stale`` terminal status.  ``observed`` / ``expected`` are carried
    so the receipt can name the exact drift rather than a prose summary.
    """

    def __init__(self, code: str, *, expected: str, observed: str) -> None:
        super().__init__(f"{code}: expected {expected!r}, observed {observed!r}")
        self.code = code
        self.expected = expected
        self.observed = observed


@dataclass(frozen=True, slots=True)
class ReconciliationCutoverRequest:
    """The closed typed input the refreshed adapter hands the controller.

    Field set is codex design §3.3 exactly.  It is deliberately narrow: the
    manager supplies identities and expected hashes only — never a path, plist
    label, router socket, command vector, PID, or environment map.  The
    controller derives everything operational from its own installation.
    """

    reconciliation_id: str
    expected_source_surface_sha256: str
    expected_release_surface_sha256: str
    expected_manifest_etag: str
    expected_current_release_id: str
    expected_active_instance_id: str
    expected_active_start_token: str
    reason: str = CUTOVER_REASON

    def __post_init__(self) -> None:
        if _RECONCILIATION_ID.fullmatch(self.reconciliation_id) is None:
            raise ValueError(
                f"reconciliation_id must match rec_<token>: {self.reconciliation_id!r}",
            )
        _require_digest(self.expected_source_surface_sha256, "expected_source_surface_sha256")
        _require_digest(self.expected_release_surface_sha256, "expected_release_surface_sha256")
        _require_present(self.expected_manifest_etag, "expected_manifest_etag")
        _require_present(self.expected_current_release_id, "expected_current_release_id")
        _require_present(self.expected_active_instance_id, "expected_active_instance_id")
        _require_present(self.expected_active_start_token, "expected_active_start_token")
        if self.reason != CUTOVER_REASON:
            raise ValueError(f"reason must be {CUTOVER_REASON!r}: {self.reason!r}")


@dataclass(frozen=True, slots=True)
class TargetRuntimeObservation:
    """What the target actually looks like right now, read by the caller.

    Observation is injected rather than performed here so the CAS legs stay a
    pure comparison: a controller that both observes and decides can hide a
    stale read inside a passing check.
    """

    current_release_id: str
    active_instance_id: str
    active_pid: int
    active_color: str
    active_start_token: str
    manifest_etag: str
    source_surface_sha256: str
    release_surface_sha256: str


def observation_from_attestation(payload: dict[str, object]) -> TargetRuntimeObservation:
    """Read the CAS observation out of a T1 ``attest_runtime_code`` payload.

    The attestation already reports every identity the CAS legs compare, so
    deriving the observation from it keeps ONE reader of runtime identity.  A
    second reader is not merely duplication: the two could disagree, and the
    CAS would then be comparing against a view nothing else in the system
    holds.

    Missing keys are a hard failure rather than an empty-string default — an
    absent identity that silently compares equal to an absent expectation is
    the exact shape of a CAS that passes without checking anything.
    """
    return TargetRuntimeObservation(
        current_release_id=_require_field(payload, "current_release_id"),
        active_instance_id=_require_field(payload, "router_active_instance_id"),
        active_pid=_require_pid(payload, "self_pid"),
        active_color=_require_field(payload, "router_active_color"),
        active_start_token=_require_field(payload, "self_start_token"),
        manifest_etag=_require_field(payload, "manifest_etag"),
        source_surface_sha256=_require_field(payload, "source_surface_sha256"),
        release_surface_sha256=_require_field(payload, "release_surface_sha256"),
    )


def _require_field(payload: dict[str, object], key: str) -> str:
    if key not in payload:
        raise KeyError(f"attestation payload is missing {key!r}")
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"attestation field {key!r} must be a non-empty string: {value!r}")
    return value


def _require_pid(payload: dict[str, object], key: str) -> int:
    """Read one positive process identity without coercing an untrusted value."""
    if key not in payload:
        raise KeyError(f"attestation payload is missing {key!r}")
    value = payload[key]
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"attestation field {key!r} must be a positive integer: {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class CutoverProvenance:
    """What the new immutable release's ``VERSION`` must record.

    Adjudication convergence 6: on a seed-materialized target there is no git
    identity, so the reconciliation fingerprint IS the provenance.  Stamping it
    into ``VERSION`` is what later lets an attestation say which approved act
    produced the bytes it is serving.
    """

    reconciliation_id: str
    source_surface_sha256: str
    release_surface_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "reconciliation_id": self.reconciliation_id,
            "source_surface_sha256": self.source_surface_sha256,
            "release_surface_sha256": self.release_surface_sha256,
        }


@dataclass(frozen=True, slots=True)
class SwapEvidence:
    """What the shared orchestrator observed, carried out structurally.

    Item 3 of the pair-150 block: the 3.3 wire contract requires the CANDIDATE
    release/instance/colour and router transition observations, not just a
    status.  These are read from the orchestrator's own result fields — never
    parsed back out of a human-readable message, which would make an audit
    record depend on prose formatting.
    """

    status: str
    reason_code: str
    candidate_release_id: str = ""
    candidate_instance_id: str = ""
    candidate_color: str = ""
    router_transitions: tuple[str, ...] = ()
    finisher: str = ""
    poller_gate: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "candidate_release_id": self.candidate_release_id,
            "candidate_instance_id": self.candidate_instance_id,
            "candidate_color": self.candidate_color,
            "router_transitions": list(self.router_transitions),
            "finisher": self.finisher,
            "poller_gate": self.poller_gate,
        }


def swap_evidence_from_restart_result(result: RestartResult) -> SwapEvidence:
    """Decode only the shared executor's structured cutover evidence.

    ``RestartResult.message`` remains operator prose and is intentionally not
    an identity carrier. The executor places this object in its typed ``probe``
    mapping so both service and target-local entries preserve the same observed
    candidate and router facts without reparsing text.
    """
    probe = result.probe
    payload = probe.get("cutover_evidence") if isinstance(probe, dict) else None
    if not isinstance(payload, dict):
        raise RuntimeError("shared swap result omitted structured cutover evidence")
    transitions = payload.get("router_transitions")
    if not isinstance(transitions, list) or not all(isinstance(item, str) for item in transitions):
        raise RuntimeError("shared swap evidence has invalid router_transitions")
    return SwapEvidence(
        status=result.status.value,
        reason_code=result.reason_code,
        candidate_release_id=_required_evidence_text(payload, "candidate_release_id"),
        candidate_instance_id=_required_evidence_text(payload, "candidate_instance_id"),
        candidate_color=_required_evidence_text(payload, "candidate_color"),
        router_transitions=tuple(transitions),
        finisher=_required_evidence_text(payload, "finisher"),
        poller_gate=_required_evidence_text(payload, "poller_gate"),
    )


def _required_evidence_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"shared swap evidence has no {key!r}")
    return value


@dataclass(frozen=True, slots=True)
class CutoverOutcome:
    """The controller's typed return: identities plus the swap's own verdict.

    ``prior_*`` is captured from the pre-swap observation, not re-read
    afterwards — after activation the "prior" is exactly what a fresh read no
    longer shows, and a rollback needs the captured instance id (D-1.6).
    """

    reconciliation_id: str
    status: str
    reason_code: str
    prior_release_id: str
    prior_instance_id: str
    prior_color: str
    provenance: CutoverProvenance
    dry_run: bool
    prior_pid: int = 0
    prior_start_token: str = ""
    evidence: SwapEvidence | None = None
    #: True when this call returned a DURABLE prior result instead of acting.
    #: Item 4: a repeated apply must never build a second candidate, and the
    #: caller has to be able to tell "resumed" from "acted" — otherwise a
    #: replayed request looks like a fresh successful cutover.
    resumed: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "reconciliation_id": self.reconciliation_id,
            "status": self.status,
            "reason_code": self.reason_code,
            "prior_release_id": self.prior_release_id,
            "prior_instance_id": self.prior_instance_id,
            "prior_color": self.prior_color,
            "provenance": self.provenance.to_dict(),
            "dry_run": self.dry_run,
            "prior_pid": self.prior_pid,
            "prior_start_token": self.prior_start_token,
            "resumed": self.resumed,
            "evidence": None if self.evidence is None else self.evidence.to_dict(),
        }


class CutoverExecutor(Protocol):
    """The shared blue-green entry point, dependency-inverted.

    Production binds this to the plugin method that already drives
    ``SwapOrchestrator.restart`` for service-driven deploys — the same state
    machine, locks, router client, release manager, and durable markers.  It is
    a Protocol so smokes can prove the CAS legs refuse WITHOUT a swap
    implementation present at all: an executor that raises on call is the
    sharpest possible "no spawn happened" assertion.
    """

    def __call__(
        self,
        *,
        reason: str,
        expected_etag: str,
        dry_run: bool,
        provenance: CutoverProvenance,
        prior_pid: int,
        prior_instance_id: str,
        prior_color: str,
        prior_start_token: str,
    ) -> SwapEvidence:
        """Return the swap's status plus the candidate identities it produced."""
        ...


class CutoverJournalStore(Protocol):
    """M1's target-local journal/receipt stage machine, as the controller uses it.

    Declared structurally rather than imported so this module stays testable
    without a materialized target, and so the controller depends on the
    QUESTIONS it must ask rather than on M1's storage layout.

    Both the read and the write side are here deliberately.  A read-only view
    is what made item 4 look closed while it was not: replay can only return a
    durable result that something else recorded, so a controller that reads but
    never writes has a replay leg that is unreachable in production.
    """

    def terminal_outcome(self, reconciliation_id: str) -> CutoverOutcome | None:
        """The durable result for this id, or ``None`` if it never completed."""
        ...

    def active_attempt(self, reconciliation_id: str) -> bool:
        """True when an attempt for this id is durably started but unfinished."""
        ...

    def record_intent(self, reconciliation_id: str) -> None:
        """Durably record that a swap is about to be attempted.

        Called BEFORE the executor, never after: the whole value of this write
        is that a process which dies mid-swap leaves evidence a later call can
        find.  Recorded after the fact it would prove nothing.
        """
        ...

    def resume_active(self, reconciliation_id: str) -> CutoverOutcome | None:
        """Finish an interrupted attempt from durable state and observation."""
        ...

    def record_terminal(self, reconciliation_id: str, outcome: CutoverOutcome) -> None:
        """Persist the terminal result so a later repeat replays it."""
        ...


class ReconciliationCutoverController:
    """Second entry point into the one cutover implementation.

    Sequence, and the order is the contract: validate → observe → CAS → swap.
    Every refusal reachable here happens strictly before the executor is
    called, so a refused reconciliation cannot have spawned a candidate.
    """

    def __init__(
        self,
        *,
        observe: Callable[[], TargetRuntimeObservation],
        execute_swap: CutoverExecutor,
        journal: CutoverJournalStore | None = None,
    ) -> None:
        self._observe = observe
        self._execute_swap = execute_swap
        self._journal = journal

    def cutover(
        self,
        request: ReconciliationCutoverRequest,
        *,
        dry_run: bool = False,
        recover: bool = False,
    ) -> CutoverOutcome:
        """Gate, replay, CAS, then delegate the swap — in that order.

        The order is the contract, and each step exists because the one before
        it cannot cover for it:

        1. **Capability gate.** Refuses before anything else when the channel is
           not activatable. Cheapest possible refusal, and the only one that is
           free of side effects by construction.
        2. **Durable replay.** A reconciliation id that already reached a
           terminal result returns THAT result. Item 4 of the pair-150 block:
           without this, a repeated apply or a ``recover`` builds a second
           candidate for an act that already completed.
        3. **In-flight refusal.** An id whose attempt is durably started but
           unfinished is refused for apply and resumed for recover. Replay
           alone cannot cover this: an attempt interrupted mid-swap has no
           terminal result, so it would pass replay and spawn again.
        4. **CAS legs**, all before any spawn.
        5. **Durable intent**, written before the executor is entered.
        6. **Delegate** to the one shared swap implementation, then persist the
           terminal result so the next repeat replays instead of acting.

        ``recover`` distinguishes resuming an interrupted attempt from starting
        one. A recover that finds no durable result must NOT silently become a
        fresh cutover: the manager asked to finish something, and inventing a
        new attempt in its place is how an operator ends up with a candidate
        nobody approved.
        """
        require_cutover_capability()
        journal = self._journal
        reconciliation_id = request.reconciliation_id
        durable = self._replay_or_resume(journal, reconciliation_id, recover)
        if durable is not None:
            return durable
        observation = self._observe()
        _require_compare_and_swap(request, observation)
        provenance = CutoverProvenance(
            reconciliation_id=reconciliation_id,
            source_surface_sha256=observation.source_surface_sha256,
            release_surface_sha256=observation.release_surface_sha256,
        )
        # Durable BEFORE the swap. A crash between here and the executor leaves
        # an in-flight attempt, which the legs above turn into a refusal or a
        # resume -- never a second candidate.
        if journal is not None:
            journal.record_intent(reconciliation_id)
        evidence = self._execute_swap(
            reason=request.reason,
            expected_etag=request.expected_manifest_etag,
            dry_run=dry_run,
            provenance=provenance,
            prior_pid=observation.active_pid,
            prior_instance_id=observation.active_instance_id,
            prior_color=observation.active_color,
            prior_start_token=observation.active_start_token,
        )
        outcome = CutoverOutcome(
            reconciliation_id=reconciliation_id,
            status=evidence.status,
            reason_code=evidence.reason_code,
            prior_release_id=observation.current_release_id,
            prior_instance_id=observation.active_instance_id,
            prior_color=observation.active_color,
            provenance=provenance,
            dry_run=dry_run,
            prior_pid=observation.active_pid,
            prior_start_token=observation.active_start_token,
            evidence=evidence,
        )
        if journal is not None:
            journal.record_terminal(reconciliation_id, outcome)
        return outcome

    @staticmethod
    def _replay_or_resume(
        journal: CutoverJournalStore | None,
        reconciliation_id: str,
        recover: bool,
    ) -> CutoverOutcome | None:
        """Return a durable result or refuse an in-flight replay before CAS."""
        durable = None if journal is None else journal.terminal_outcome(reconciliation_id)
        if durable is not None:
            return _resumed(durable)
        if not recover:
            if journal is not None and journal.active_attempt(reconciliation_id):
                raise CutoverRefusalError(
                    REFUSAL_ATTEMPT_IN_FLIGHT,
                    expected=reconciliation_id,
                    observed="an attempt for this reconciliation is already in flight",
                )
            return None
        if journal is None or not journal.active_attempt(reconciliation_id):
            raise CutoverRefusalError(
                REFUSAL_NOTHING_TO_RECOVER,
                expected=reconciliation_id,
                observed="no durable terminal result and no attempt in flight",
            )
        resumed = journal.resume_active(reconciliation_id)
        if resumed is None:
            raise CutoverRefusalError(
                REFUSAL_NOTHING_TO_RECOVER,
                expected=reconciliation_id,
                observed="attempt in flight is not resumable from durable state",
            )
        journal.record_terminal(reconciliation_id, resumed)
        return _resumed(resumed)


def _resumed(durable: CutoverOutcome) -> CutoverOutcome:
    """Return the durable result, marked so it cannot read as a fresh act."""
    return CutoverOutcome(
        reconciliation_id=durable.reconciliation_id,
        status=durable.status,
        reason_code=durable.reason_code,
        prior_release_id=durable.prior_release_id,
        prior_instance_id=durable.prior_instance_id,
        prior_color=durable.prior_color,
        provenance=durable.provenance,
        dry_run=durable.dry_run,
        prior_pid=durable.prior_pid,
        prior_start_token=durable.prior_start_token,
        evidence=durable.evidence,
        resumed=True,
    )


def _require_compare_and_swap(
    request: ReconciliationCutoverRequest,
    observation: TargetRuntimeObservation,
) -> None:
    """Refuse on the first drifted leg.

    Ordering is cheapest-and-most-diagnostic first: a release or instance that
    moved explains an etag that also moved, and reporting the etag would name
    the symptom instead of the cause.
    """
    legs = (
        (REFUSAL_CURRENT_RELEASE, request.expected_current_release_id, observation.current_release_id),
        (REFUSAL_ACTIVE_INSTANCE, request.expected_active_instance_id, observation.active_instance_id),
        (
            REFUSAL_ACTIVE_START_TOKEN,
            request.expected_active_start_token,
            observation.active_start_token,
        ),
        (REFUSAL_MANIFEST_ETAG, request.expected_manifest_etag, observation.manifest_etag),
        (
            REFUSAL_SOURCE_SURFACE,
            request.expected_source_surface_sha256,
            observation.source_surface_sha256,
        ),
        (
            REFUSAL_RELEASE_SURFACE,
            request.expected_release_surface_sha256,
            observation.release_surface_sha256,
        ),
    )
    for code, expected, observed in legs:
        if expected != observed:
            raise CutoverRefusalError(code, expected=expected, observed=observed)


def _require_digest(value: str, label: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be sha256:<64hex>: {value!r}")


def _require_present(value: str, label: str) -> None:
    if not value:
        raise ValueError(f"{label} must not be empty")
