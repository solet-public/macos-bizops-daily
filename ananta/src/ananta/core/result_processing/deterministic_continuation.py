"""Deterministic continuation processor.

The processor consumes a :class:`ValidatedDeterministicContinuation`
(already validated by
:mod:`ananta.core.result_processing.contracts`) and submits the next
planned action without calling any inference path.  Plan advancement is
delegated to a platform-owned advancer; FRG parent-token context is
preserved through ``result_processor_context``.

Per handoff Section 12 the processor must not:

* parse plans (parsing belongs in validation);
* repair missing arguments (validation owns the closed-world rule);
* choose among multiple possible actions (validation produced exactly one);
* call inference (deterministic by definition);
* silently skip result processing.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from ananta.core.result_processing.contracts import (
    ValidatedDeterministicContinuation,
)
from ananta.core.result_processing.coordinator import (
    CompletedAction,
    DeterministicSubmitter,
)
from ananta.core.state.execution_token_context import result_processor_context

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Collaborator Protocols
# ---------------------------------------------------------------------------


class ActionSubmissionService(Protocol):
    """Submit an already-validated action definition through the platform.

    The concrete implementation wraps
    :class:`~ananta.core.actions.action_factory.ActionFactory` plus the
    standard owned-slot / composed-reference / work-product injection
    pipeline already used for plan-derived EDGE actions.
    """

    def submit_action(
        self,
        *,
        action_definition: Mapping[str, object],
        parent_action_id: str,
    ) -> str:
        """Submit *action_definition* and return the generated action ID."""
        ...


class PlanAdvancer(Protocol):
    """Advance the acting session's focused plan past the just-completed step.

    Mirrors the inference plugin's start-of-VERTEX advancement path
    (:func:`ananta.core.plans.advancement.maybe_advance_plan`).  The
    deterministic processor must not hand-roll marker mutation.  Focus is
    session-scoped (JOS-02) — the advancer keys by the completed action's
    session.
    """

    def advance(self, *, session_id: str) -> None: ...


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeterministicContinuationProcessor(DeterministicSubmitter):
    """Submit a validated deterministic continuation and advance the plan.

    Stateless; safe to share across actions.
    """

    submission_service: ActionSubmissionService
    plan_advancer: PlanAdvancer

    def submit(
        self,
        *,
        completed: CompletedAction,
        continuation: ValidatedDeterministicContinuation,
        flow_token_id: str | None,
    ) -> None:
        """Submit the validated next action; then advance the plan.

        FRG parent-token preservation: ``result_processor_context``
        propagates ``flow_token_id`` to the recorder so the new action
        becomes a child of the just-completed action's token.
        """
        logger.info(
            "DETERMINISTIC_SUBMIT: completed_step=%d next_step=%d "
            "completed_pk=%s next_pk=%s",
            continuation.completed_step_number,
            continuation.next_step_number,
            continuation.completed_process_key,
            continuation.next_action_definition.get("process_key"),
        )
        with result_processor_context(flow_token_id):
            submitted_id = self.submission_service.submit_action(
                action_definition=continuation.next_action_definition,
                parent_action_id=completed.action_id,
            )
        logger.info(
            "DETERMINISTIC_SUBMIT: queued next action %s", submitted_id,
        )
        self.plan_advancer.advance(session_id=completed.session_id or "")
