"""Core-side joseki run driver (Track A, spec v3.1 §0/§4.2).

The engine that makes a registered joseki card EXECUTE platform-side:
``run_joseki`` instantiates the card into a joseki-scoped WBS (plugin-side
pure logic), mints a REAL run-scoped platform session (operator ruling
2026-07-05 option B — no phantom session ids; the MCP bridge's
``create_session``-owned session is the precedent), focuses the run plan,
writes the run row, and returns the FIRST step's action definition in the
result envelope — Pattern 6a: the poller submits returned actions at Step 4
for every processor kind including EDGE_SINK, injecting parent session/flow
only when absent, so the returned action carries the RUN session and the
coordinator's native continuation machinery owns every subsequent hop
(contracts.py full validation on each deterministic step = SUB-03 by reuse).
``complete_joseki_run`` is the instantiator-appended terminal step: the run's
completion is itself a plan step; run evidence records ONLY on the winning
status CAS. The reconciler duties (spec §4.3, ordered) surface failures from
the core-owned violation/action tables — this module homes CORE-side exactly
so those reads are legitimately owned (the INF-02 completion-machinery
precedent; no foreign-namespace ``query_state``, no raw SQL).

Collaborators are injected as Protocols (the ``authored_lifecycle`` /
``DeterministicContinuationProcessor`` house pattern): the engine is fully
exercisable offline; the thin adapters that bind real services are wired
where ``ThinkingService`` is constructed (mirroring ``InferenceService``'s
post-INF-02 ``state_service``/``orchestrator`` collaborators).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from ananta.error_handling import FrameworkError

logger = logging.getLogger(__name__)

# Run-session identity conventions (spec v3 D4: label joseki-run:<run_id> so
# ledger tooling can filter run sessions mechanically).
RUN_SESSION_NAMESPACE = "thinking_service"
RUN_SESSION_CONTEXT_TYPE = "joseki_run"

# Card lifecycle states eligible to run (mirror of the plugin's constants —
# the engine treats them as data; the AUTHORITATIVE check also happens
# plugin-side at record_run time).
_RUN_ELIGIBLE_STATES = frozenset({"candidate", "proven"})

_ERROR_CODE_STATE_CONFLICT = "thinking_service.joseki_run_state_conflict"
_ERROR_CODE_NOT_RUNNABLE = "thinking_service.joseki_not_runnable"
_ERROR_CODE_RUN_BUSY = "thinking_service.joseki_run_busy"


class JosekiRunPluginGateway(Protocol):
    """The bound thinking plugin's joseki-run surface (4-layer seam).

    Everything domain-shaped stays plugin-side: card resolution, mechanical
    instantiation (typed ``JOSEKI_NOT_MECHANIZABLE`` rejection), WBS
    registration (author-by-value validation), the run-row store, and run
    evidence. The engine orchestrates; it never reimplements these.
    """

    def get_authored_joseki(self, joseki_key: str) -> dict[str, Any]: ...

    def read_joseki_card(self, joseki_key: str) -> str:
        """The card markdown, or ``""`` when absent."""
        ...

    def instantiate_run_wbs(
        self,
        *,
        card_content: str,
        joseki_key: str,
        bindings: dict[str, Any],
        wbs_id: str,
        manifest_id: str,
    ) -> dict[str, Any]:
        """Mechanical instantiation → ``{content, executable_step_count,
        terminal_step_number}`` (raises typed on non-mechanizable)."""
        ...

    def mint_run_wbs_id(self, joseki_key: str) -> str: ...

    def register_run_wbs(
        self, *, content: str, wbs_id: str, manifest_id: str, session_id: str,
    ) -> dict[str, Any]:
        """Author-by-value registration (validates-then-stores).

        ``session_id`` is the RUN session — the registered WBS document's
        focus pin lands in the run session's buffer (JOS-02).
        """
        ...

    def create_run_row(
        self,
        *,
        joseki_key: str,
        wbs_id: str,
        session_id: str,
        flow_id: str,
        requester: str,
        label: str,
    ) -> str: ...

    def get_run_row(self, run_id: str) -> dict[str, Any] | None: ...

    def get_run_row_by_wbs(self, wbs_id: str) -> dict[str, Any] | None: ...

    def list_run_rows(
        self,
        *,
        status: str | None,
        joseki_key: str | None,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    def cas_run_status(
        self,
        *,
        run_id: str,
        from_status: str,
        to_status: str,
        extra_updates: dict[str, Any] | None,
    ) -> bool: ...

    def cas_increment_attempts(
        self, *, run_id: str, prior_attempts: int,
    ) -> bool: ...

    def record_current_step(
        self, *, run_id: str, step_number: int,
    ) -> bool: ...

    def record_joseki_run_evidence(
        self, *, joseki_key: str, wbs_id: str,
    ) -> dict[str, Any]: ...


class RunSessionFactory(Protocol):
    """Mint a REAL run-scoped platform session + flow (core-owned)."""

    def create_run_session(self, *, run_label: str) -> tuple[str, str]:
        """Return ``(session_id, flow_id)`` for a fresh run session."""
        ...


class RunPlanInstaller(Protocol):
    """Install/release the run plan in the RUN SESSION's focus buffer.

    JOS-02 (landed): focus is session-scoped — each run session owns its
    own buffer, so concurrent runs of different cards never contend and a
    foreign focused plan cannot wedge a kickoff. Terminal transitions
    release the run session's ENTIRE buffer (plan + WBS pin + any artifact
    pins — the R1 whole-buffer ruling; run sessions are ephemeral and
    single-purpose, so every pin in one is run-scoped by construction).
    """

    def has_focused_active_plan(self, *, session_id: str) -> bool: ...

    def install_focused_plan(
        self, *, session_id: str, wbs_id: str, plan_content: str,
    ) -> None: ...

    def release_session_focus(self, *, session_id: str) -> None:
        """Release EVERY pin in the run session (idempotent; empty is a no-op)."""
        ...


class RunFlowInspector(Protocol):
    """Core-owned reads over the run flow (violations + action rows).

    Homing these reads core-side is the v3.1 ruling's B-V6 resolution — the
    engine's owner legitimately reads the core namespace; no thin foreign
    read verbs, no raw SQL.
    """

    def latest_contract_violation(self, *, flow_id: str) -> dict[str, Any] | None: ...

    def latest_failed_action(self, *, flow_id: str) -> dict[str, Any] | None: ...

    def has_inflight_action(self, *, flow_id: str) -> bool: ...

    def completed_action_count(self, *, flow_id: str) -> int:
        """Completed action events on the run flow — the progress signal."""
        ...


@dataclass(frozen=True)
class JosekiRunEngine:
    """Orchestrates joseki runs over injected collaborators. Stateless."""

    plugin: JosekiRunPluginGateway
    sessions: RunSessionFactory
    plans: RunPlanInstaller
    flows: RunFlowInspector
    run_manifest_id: str
    # Consecutive stalled reconciliation passes before the run terminal-fails
    # (duty-4; the self-clearing guarantee for the global-focus lane).
    stall_attempts_cap: int = 5

    # -- kickoff (the run_joseki verb body) -----------------------------------

    def run_joseki(
        self,
        *,
        joseki_key: str,
        bindings: dict[str, Any],
        label: str = "",
        requester: str = "",
    ) -> dict[str, Any]:
        """Instantiate + bootstrap one run; return the run handle envelope.

        The returned envelope's top-level ``actions`` carries the FIRST
        step's action definition stamped with the run session — Pattern 6a
        submits it; the coordinator chain owns everything after.
        """
        card_state = self._require_runnable_card(joseki_key)
        self._require_run_slot_free(joseki_key)
        card_content = self.plugin.read_joseki_card(joseki_key)

        # Mint the run session BEFORE registration: the registered WBS
        # document's focus pin belongs in the RUN session's buffer (JOS-02),
        # so the session must exist first.
        session_id, flow_id = self.sessions.create_run_session(
            run_label=label or joseki_key,
        )
        if self.plans.has_focused_active_plan(session_id=session_id):
            raise FrameworkError(
                message=(
                    f"fresh run session {session_id!r} already holds a focused "
                    f"plan — session-scoped focus invariant violated"
                ),
                error_code=_ERROR_CODE_STATE_CONFLICT,
            )

        wbs_id = self.plugin.mint_run_wbs_id(joseki_key)
        instantiated = self.plugin.instantiate_run_wbs(
            card_content=card_content,
            joseki_key=joseki_key,
            bindings=bindings,
            wbs_id=wbs_id,
            manifest_id=self.run_manifest_id,
        )
        content = str(instantiated["content"])
        self.plugin.register_run_wbs(
            content=content,
            wbs_id=wbs_id,
            manifest_id=self.run_manifest_id,
            session_id=session_id,
        )
        # Derive the first action BEFORE occupying the focus buffer: it is a
        # pure function of the instantiated content, so nothing fallible runs
        # after install — a raise here can never leak a focused plan that the
        # serialization guard would then treat as a permanent busy state.
        first_action = _first_step_action(content, session_id, flow_id)
        run_id = self.plugin.create_run_row(
            joseki_key=joseki_key,
            wbs_id=wbs_id,
            session_id=session_id,
            flow_id=flow_id,
            requester=requester,
            label=label,
        )
        try:
            self.plans.install_focused_plan(
                session_id=session_id,
                wbs_id=wbs_id,
                plan_content=_mark_first_step_current(content),
            )
        except Exception as exc:
            # Rev-A build F2 (kickoff orphan): an install failure must not
            # leave a 'running' row with no focus and no in-flight action —
            # that row would wedge the serialization guard forever. Fail the
            # row loudly, then re-raise.
            self.plugin.cas_run_status(
                run_id=run_id,
                from_status="running",
                to_status="failed",
                extra_updates={
                    "failure_detail": f"focused-plan install failed: {exc}",
                },
            )
            raise
        self.plugin.record_current_step(run_id=run_id, step_number=0)
        logger.info(
            "JOSEKI_RUN_START: run=%s joseki=%s wbs=%s session=%s "
            "card_state=%s steps=%s",
            run_id,
            joseki_key,
            wbs_id,
            session_id,
            card_state,
            instantiated.get("executable_step_count"),
        )
        return {
            "run_id": run_id,
            "wbs_id": wbs_id,
            "session_id": session_id,
            "status": "running",
            "actions": [first_action],
        }

    # -- terminal step (the complete_joseki_run verb body) ---------------------

    def complete_joseki_run(self, *, wbs_id: str) -> dict[str, Any]:
        """Terminal plan step: CAS the run → completed; record evidence.

        Evidence records ONLY on the winning CAS (Rev-A N-fold: a lost race
        — reconciler already terminal-ed the run, or a duplicate terminal
        re-drive — is a benign no-op that must never double-record).
        """
        row = self.plugin.get_run_row_by_wbs(wbs_id)
        if row is None:
            raise FrameworkError(
                message=(
                    f"complete_joseki_run: no run row for wbs {wbs_id!r} — "
                    f"a terminal step fired outside any known run"
                ),
                error_code=_ERROR_CODE_STATE_CONFLICT,
            )
        run_id = str(row["id"])
        won = self.plugin.cas_run_status(
            run_id=run_id,
            from_status="running",
            to_status="completed",
            extra_updates=None,
        )
        if not won:
            fresh = self.plugin.get_run_row(run_id) or {}
            logger.info(
                "JOSEKI_RUN_COMPLETE_NOOP: run=%s already %s (benign lost CAS)",
                run_id,
                fresh.get("status"),
            )
            return {
                "run_id": run_id,
                "wbs_id": wbs_id,
                "status": str(fresh.get("status", "")),
                "outcome": "noop_lost_cas",
                "joseki_state": None,
                "run_count": None,
            }
        # R1 whole-buffer release: the run session's plan AND its WBS/artifact
        # pins all go (the Track-A run orphaned its WBS pin under plan-only
        # release — the live specimen behind the ruling).
        self.plans.release_session_focus(session_id=str(row["session_id"]))
        evidence = self.plugin.record_joseki_run_evidence(
            joseki_key=str(row["joseki_key"]), wbs_id=wbs_id,
        )
        logger.info(
            "JOSEKI_RUN_COMPLETED: run=%s joseki=%s run_count=%s state=%s",
            run_id,
            row["joseki_key"],
            evidence.get("run_count"),
            evidence.get("state"),
        )
        return {
            "run_id": run_id,
            "wbs_id": wbs_id,
            "status": "completed",
            "outcome": "completed",
            "joseki_state": evidence.get("state"),
            "run_count": evidence.get("run_count"),
        }

    # -- observability ----------------------------------------------------------

    def get_joseki_run(self, *, run_id: str) -> dict[str, Any]:
        row = self.plugin.get_run_row(run_id)
        if row is None:
            return {"found": False, "run_id": run_id}
        return {"found": True, **_project_run_row(row)}

    def list_joseki_runs(
        self,
        *,
        status: str | None = None,
        joseki_key: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        rows = self.plugin.list_run_rows(
            status=status, joseki_key=joseki_key, limit=limit,
        )
        return {"runs": [_project_run_row(r) for r in rows], "count": len(rows)}

    # -- reconciler (spec §4.3 — ORDERED duties; rides the sweeper tick) --------

    def reconcile_run(self, *, run_id: str) -> dict[str, Any]:
        """One reconciliation pass for one run. Returns the duty taken.

        Duty ORDER is load-bearing (Rev-A D2) and encoded by the dispatch
        below: violation surfacing precedes runtime-failure surfacing
        precedes in-flight/progress checks precedes stall handling —
        otherwise a deterministically-failing step is masked as generic
        staleness. Every CAS loss is a benign no-op (the live path won).
        """
        row = self.plugin.get_run_row(run_id)
        if row is None or str(row.get("status")) != "running":
            return {"run_id": run_id, "duty": "none", "reason": "not running"}
        flow_id = str(row["flow_id"])
        return (
            self._duty_violation(row, flow_id)
            or self._duty_runtime_failure(row, flow_id)
            or self._duty_inflight(run_id, flow_id)
            or self._duty_progress(row, flow_id)
            or self._duty_stall(row)
        )

    def _duty_violation(
        self, row: dict[str, Any], flow_id: str,
    ) -> dict[str, Any] | None:
        violation = self.flows.latest_contract_violation(flow_id=flow_id)
        if violation is None:
            return None
        return self._fail_run(
            row,
            duty="violation_surfaced",
            detail=(
                f"contract violation [{violation.get('invariant', '?')}]: "
                f"{violation.get('message', '')}"
            ),
        )

    def _duty_runtime_failure(
        self, row: dict[str, Any], flow_id: str,
    ) -> dict[str, Any] | None:
        failed = self.flows.latest_failed_action(flow_id=flow_id)
        if failed is None:
            return None
        return self._fail_run(
            row,
            duty="runtime_failure_surfaced",
            detail=(
                f"step action {failed.get('process_key', '?')} failed: "
                f"{failed.get('error_message', '')}"
            ),
        )

    def _duty_inflight(
        self, run_id: str, flow_id: str,
    ) -> dict[str, Any] | None:
        if self.flows.has_inflight_action(flow_id=flow_id):
            return {"run_id": run_id, "duty": "none", "reason": "in flight"}
        return None

    def _duty_progress(
        self, row: dict[str, Any], flow_id: str,
    ) -> dict[str, Any] | None:
        """Progress check BEFORE stall counting (Rev-A delta-2 N).

        The progress cursor is the completed-action count on the run flow;
        advancement stamps it AND resets the stall counter, so attempts
        count CONSECUTIVE no-progress sweeps only.
        """
        run_id = str(row["id"])
        completed = self.flows.completed_action_count(flow_id=flow_id)
        cursor = int(row.get("current_step") or 0)
        if completed <= cursor:
            return None
        self.plugin.record_current_step(run_id=run_id, step_number=completed)
        return {
            "run_id": run_id,
            "duty": "progress_observed",
            "reason": f"completed actions {cursor} -> {completed}",
        }

    def _duty_stall(self, row: dict[str, Any]) -> dict[str, Any]:
        """Duty 2/4 (Rev-A build F2): stalls SELF-CLEAR, never wedge.

        Each CONSECUTIVE no-progress sweep increments the attempts counter
        (CAS-predicated — a lost race means the live path moved, benign);
        at the cap the run terminal-fails LOUD and releases focus. Full
        duty-2 re-drive is the JOS-03 follow-up; the cap is the wedge-fix.
        """
        run_id = str(row["id"])
        prior = int(row.get("attempts") or 0)
        if prior + 1 >= self.stall_attempts_cap:
            return self._fail_run(
                row,
                duty="stall_attempts_exhausted",
                detail=(
                    f"stalled with no in-flight action for "
                    f"{prior + 1} consecutive reconciliation passes"
                ),
            )
        bumped = self.plugin.cas_increment_attempts(
            run_id=run_id, prior_attempts=prior,
        )
        return {
            "run_id": run_id,
            "duty": "stall_detected",
            "reason": "no in-flight action",
            "attempts": prior + 1 if bumped else prior,
        }

    def _fail_run(
        self, row: dict[str, Any], *, duty: str, detail: str,
    ) -> dict[str, Any]:
        run_id = str(row["id"])
        won = self.plugin.cas_run_status(
            run_id=run_id,
            from_status="running",
            to_status="failed",
            extra_updates={"failure_detail": detail},
        )
        if won:
            self.plans.release_session_focus(session_id=str(row["session_id"]))
        outcome = "failed" if won else "noop_lost_cas"
        log = logger.error if won else logger.info
        log("JOSEKI_RUN_%s: run=%s %s", duty.upper(), run_id, detail)
        return {"run_id": run_id, "duty": duty, "outcome": outcome, "detail": detail}

    # -- guards -------------------------------------------------------------------

    def _require_run_slot_free(self, joseki_key: str) -> None:
        """Per-card serialization guard (JOS-02 §8.2).

        Focus is session-scoped, so runs of DIFFERENT cards no longer share
        any slot and start concurrently. Same-card runs stay serialized one
        more cycle: ``record_joseki_run_evidence`` increments the card's
        run_count plugin-side and its atomicity under concurrent completes
        is unverified (design V-4) — cheap to relax later, expensive to
        retrofit. Typed busy rejection — callers retry later; the
        scheduler's next tick naturally re-attempts.
        """
        running = self.plugin.list_run_rows(
            status="running", joseki_key=joseki_key, limit=1,
        )
        if running:
            raise FrameworkError(
                message=(
                    f"cannot start joseki {joseki_key!r} — run "
                    f"{running[0].get('id')!r} of the SAME card is already "
                    f"running (same-card runs serialize pending V-4 evidence "
                    f"atomicity; different cards run concurrently)"
                ),
                error_code=_ERROR_CODE_RUN_BUSY,
            )

    def _require_runnable_card(self, joseki_key: str) -> str:
        lifecycle = self.plugin.get_authored_joseki(joseki_key)
        if not lifecycle.get("found", False):
            raise FrameworkError(
                message=f"joseki {joseki_key!r} is not registered",
                error_code=_ERROR_CODE_NOT_RUNNABLE,
            )
        state = str(lifecycle.get("state", ""))
        if state not in _RUN_ELIGIBLE_STATES:
            raise FrameworkError(
                message=(
                    f"joseki {joseki_key!r} is in state {state!r} — only "
                    f"{sorted(_RUN_ELIGIBLE_STATES)} may run (validate to "
                    f"'candidate' first; drafts and retired cards never run)"
                ),
                error_code=_ERROR_CODE_NOT_RUNNABLE,
            )
        return state


# -- pure helpers ----------------------------------------------------------------


def _mark_first_step_current(content: str) -> str:
    """The instantiated sequence with step 1 marked ``[>]`` (the current step)."""
    return content.replace("[ ] 1.", "[>] 1.", 1)


def _first_step_action(
    content: str, session_id: str, flow_id: str,
) -> dict[str, Any]:
    """Build step 1's action definition from the instantiated document.

    The step's ``RESULT_PROCESSOR_KIND`` annotation is stamped onto the
    action definition — WITHOUT it the coordinator's deterministic branch
    never engages and the chain does not start (Rev-A build F1a). Every
    SUBSEQUENT hop is stamped by the existing continuation builder
    (``contracts._build_next_action_definition``). §16 error-handler note:
    the action factory auto-injects the target process's registered
    error customizations, so a deterministic first step passes the
    ``error_processor_required`` invariant with an INFERENCE error handler
    — runtime errors route to error-inference (spec v3.2 posture; the
    canonical ``status=failed`` on the action row remains duty-1's signal).

    Import is local to keep this module's import surface minimal at
    service-construction time; the parser is the CORE plan parser (spec AV7 —
    never a parallel parser).
    """
    from ananta.core.plans.parser import parse

    parsed = parse(content)
    first = next((s for s in parsed.steps if s.process_keys), None)
    if first is None or not first.bound_sub_steps:
        raise FrameworkError(
            message=(
                "instantiated run WBS has no executable first step — the "
                "instantiator's admission checks should have rejected this card"
            ),
            error_code=_ERROR_CODE_NOT_RUNNABLE,
        )
    if first.result_processor_kind is None:
        raise FrameworkError(
            message=(
                "instantiated run WBS first step carries no "
                "RESULT_PROCESSOR_KIND — the admission checks should have "
                "rejected this card"
            ),
            error_code=_ERROR_CODE_NOT_RUNNABLE,
        )
    sub = first.bound_sub_steps[0]
    return {
        "process_key": sub.process_key,
        "arguments": dict(sub.arguments or {}),
        "session_id": session_id,
        "flow_id": flow_id,
        "result_processor_kind": first.result_processor_kind.value,
    }


def _project_run_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": str(row.get("id", "")),
        "joseki_key": row.get("joseki_key"),
        "wbs_id": row.get("wbs_id"),
        "session_id": row.get("session_id"),
        "flow_id": row.get("flow_id"),
        "status": row.get("status"),
        "current_step": row.get("current_step"),
        "failure_detail": row.get("failure_detail"),
        "label": row.get("label"),
        "attempts": row.get("attempts"),
    }
