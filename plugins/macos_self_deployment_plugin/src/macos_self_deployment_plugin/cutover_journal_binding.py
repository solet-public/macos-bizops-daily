"""Bind the cutover controller to M1's target-local journal/receipt machine.

Item 4 of the pair-150 block requires execution to be bound to the durable
stage machine *before* the action is callable.  The controller declares the
questions it must ask as
:class:`~macos_self_deployment_plugin.reconciliation_cutover.CutoverJournalStore`;
this module answers them over the landed
:class:`~solet_manager.cutover_receipts.CutoverReceiptStore` rather than
inventing storage.

Why the write side lives here and not in the controller: the receipt store is
target-local infrastructure (its own docstring: "recovery evidence belongs to
the target"), while the controller is a pure decision object that must stay
testable with no materialized target.  Keeping the storage adapter separate is
what lets the controller's smokes prove the CAS and replay legs without a
filesystem, and lets this module's smokes prove the durability without a swap.

``solet_manager`` is imported lazily and its absence is reported as a refusal
rather than a crash: a target that cannot record recovery evidence must not
perform a cutover at all, and saying so is more useful than an ImportError
traceback from inside a swap.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Final

from macos_self_deployment_plugin.reconciliation_cutover import (
    CutoverOutcome,
    CutoverProvenance,
    SwapEvidence,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from solet_manager.cutover_receipts import (
        CutoverJournal,
        CutoverReceiptStore,
        CutoverRuntimeObservation,
        TerminalStatus,
    )

__all__ = [
    "CUTOVER_REQUESTED_STAGE",
    "JournalUnavailableError",
    "ReceiptJournalStore",
]

#: The stage a durable intent is recorded at.  The controller writes intent
#: immediately before delegating the swap, which is exactly what
#: ``recover_requested`` later requires to be resumable.
CUTOVER_REQUESTED_STAGE: Final[str] = "cutover_requested"


class JournalUnavailableError(RuntimeError):
    """This target cannot record recovery evidence, so it must not cut over."""


class ReceiptJournalStore:
    """``CutoverJournalStore`` over M1's target-local receipt store.

    One instance is bound to one target directory.  Every method is keyed on
    the reconciliation id the controller was handed, never on "the active
    journal" alone: an active journal for a DIFFERENT id is not this
    reconciliation's business, and treating it as one is how a recover for id A
    would finish id B's attempt.
    """

    def __init__(
        self,
        target: Path,
        *,
        observe_runtime: Callable[[CutoverJournal], CutoverRuntimeObservation],
    ) -> None:
        self._target = target
        self._store = _open_store(target)
        self._observe_runtime = observe_runtime

    def terminal_outcome(self, reconciliation_id: str) -> CutoverOutcome | None:
        """Read the immutable terminal receipt for this id, if it exists."""
        path = self._store.receipt_path(reconciliation_id)
        if not path.exists():
            return None
        return _outcome_from_receipt(_read_json(path))

    def active_attempt(self, reconciliation_id: str) -> bool:
        """True when THIS id has a durably started, unfinished attempt."""
        journal = self._load_journal()
        return journal is not None and journal.reconciliation_id == reconciliation_id

    def record_intent(self, reconciliation_id: str) -> None:
        """Advance the active journal to ``cutover_requested`` and fsync it.

        Refuses when no journal is active for this id.  The journal is opened
        by the adapter when it applies bytes, so its absence here means the
        controller was driven outside the reconciliation flow that prepared it
        — and a cutover with no durable intent is the one case item 4 forbids.
        """
        journal = self._load_journal()
        if journal is None or journal.reconciliation_id != reconciliation_id:
            raise JournalUnavailableError(
                f"no active cutover journal for {reconciliation_id!r}; "
                "the adapter must prepare the journal before a swap is attempted",
            )
        if journal.stage == CUTOVER_REQUESTED_STAGE:
            # Already durable for this attempt.  Re-writing would be harmless
            # but the forward-only journal refuses a same-stage advance, and a
            # crash-retry landing here is normal rather than exceptional.
            return
        self._store.write_active(journal.advance(CUTOVER_REQUESTED_STAGE))

    def resume_active(self, reconciliation_id: str) -> CutoverOutcome | None:
        """Finish an interrupted attempt from durable state plus observation.

        Delegates to M1's ``recover_requested``, which decides the terminal
        status by OBSERVING the runtime against the journal's approved terms —
        never by assuming the interrupted swap succeeded.  The prior identities
        come from those same durable terms rather than a fresh read: after an
        activation the "prior" is precisely what a current read no longer
        shows.
        """
        journal = self._load_journal()
        if journal is None or journal.reconciliation_id != reconciliation_id:
            return None
        if journal.stage != CUTOVER_REQUESTED_STAGE:
            # Recoverable only from the stage a durable intent was written at.
            # An earlier stage never reached a swap, so there is nothing to
            # finish; a later one is already past the interruption window.
            return None
        resumed, status = self._store.recover_requested(self._observe_runtime)
        if status is None:
            return None
        terms = resumed.terms
        return CutoverOutcome(
            reconciliation_id=resumed.reconciliation_id,
            status=status,
            reason_code="resumed_from_durable_journal",
            prior_release_id=terms.current_release_id,
            prior_instance_id=terms.active_instance_id,
            prior_color=terms.active_color,
            provenance=CutoverProvenance(
                reconciliation_id=resumed.reconciliation_id,
                source_surface_sha256=terms.source_surface_sha256,
                release_surface_sha256=terms.release_surface_sha256,
            ),
            dry_run=False,
        )

    def record_terminal(self, reconciliation_id: str, outcome: CutoverOutcome) -> None:
        """Persist the immutable terminal receipt for this id."""
        journal = self._load_journal()
        if journal is None or journal.reconciliation_id != reconciliation_id:
            raise JournalUnavailableError(
                f"no active cutover journal to finalize for {reconciliation_id!r}",
            )
        self._store.finalize(journal, _terminal_status(outcome))

    def _load_journal(self) -> CutoverJournal | None:
        from solet_manager.cutover_receipts import CutoverJournal  # noqa: PLC0415

        active = self._store.load_active()
        return active if isinstance(active, CutoverJournal) else None


def _open_store(target: Path) -> CutoverReceiptStore:
    try:
        from solet_manager.cutover_receipts import CutoverReceiptStore  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise JournalUnavailableError(
            "solet_manager is not importable on this target; cutover recovery "
            "evidence cannot be recorded, so no cutover may be attempted",
        ) from exc
    return CutoverReceiptStore(target)


#: Terminal receipt status for a swap that reached its desired end state.  The
#: mapping is explicit rather than a passthrough because the two vocabularies
#: are owned by different layers: RestartStatus describes a swap, TerminalStatus
#: describes a reconciliation, and silently reusing one as the other is how a
#: failed swap would be recorded as a reconciled target.
_TERMINAL_BY_STATUS: Final[dict[str, TerminalStatus]] = {
    "queued": "reconciled",
    "completed": "reconciled",
    "no_op": "already_reconciled",
    "failed": "failed_prior_serving",
}


def _terminal_status(outcome: CutoverOutcome) -> TerminalStatus:
    status = _TERMINAL_BY_STATUS.get(outcome.status)
    if status is None:
        # An unmapped swap status is not assumed benign.  "needs_intervention"
        # is the honest terminal state for an outcome this layer cannot
        # classify, and it is the one that summons a human.
        return "needs_intervention"
    return status


def _read_json(path: Path) -> dict[str, object]:
    import json  # noqa: PLC0415

    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise JournalUnavailableError(f"cutover receipt is not an object: {path}")
    return loaded


def _outcome_from_receipt(receipt: dict[str, object]) -> CutoverOutcome:
    """Rebuild the controller's typed outcome from a durable receipt.

    Only fields the receipt actually carries are reconstructed.  Anything the
    receipt does not record is left at its declared default rather than
    guessed: a replayed outcome that invents evidence it never observed is
    worse than one that admits it has none.
    """
    outcome = receipt.get("cutover_outcome")
    if not isinstance(outcome, dict):
        raise JournalUnavailableError(
            "cutover terminal receipt carries no cutover_outcome block",
        )
    provenance = outcome.get("provenance")
    provenance_fields = provenance if isinstance(provenance, dict) else {}
    evidence = outcome.get("evidence")
    return CutoverOutcome(
        reconciliation_id=_text(outcome, "reconciliation_id"),
        status=_text(outcome, "status"),
        reason_code=_text(outcome, "reason_code"),
        prior_release_id=_text(outcome, "prior_release_id"),
        prior_instance_id=_text(outcome, "prior_instance_id"),
        prior_color=_text(outcome, "prior_color"),
        provenance=CutoverProvenance(
            reconciliation_id=_text(provenance_fields, "reconciliation_id"),
            source_surface_sha256=_text(provenance_fields, "source_surface_sha256"),
            release_surface_sha256=_text(provenance_fields, "release_surface_sha256"),
        ),
        dry_run=bool(outcome.get("dry_run", False)),
        evidence=_evidence_from_dict(evidence) if isinstance(evidence, dict) else None,
    )


def _evidence_from_dict(evidence: dict[str, object]) -> SwapEvidence:
    transitions = evidence.get("router_transitions")
    return SwapEvidence(
        status=_text(evidence, "status"),
        reason_code=_text(evidence, "reason_code"),
        candidate_release_id=_text(evidence, "candidate_release_id"),
        candidate_instance_id=_text(evidence, "candidate_instance_id"),
        candidate_color=_text(evidence, "candidate_color"),
        router_transitions=tuple(str(item) for item in transitions)
        if isinstance(transitions, list)
        else (),
    )


def _text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key, "")
    return value if isinstance(value, str) else ""
