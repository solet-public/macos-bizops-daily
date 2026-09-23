"""Wiring adapters binding :mod:`joseki_run_engine` Protocols to platform services.

Each adapter is a thin, stateless translation — no logic beyond shaping the
call (logic lives in the engine or the owning service). Construction happens
where ``ThinkingService`` is built, mirroring ``InferenceService``'s
post-INF-02 ``state_service``/``orchestrator`` collaborators.

The ``CoreFlowInspector`` reads live here BY DESIGN (spec v3.1 ruling): this
module is core-side, so the ``core`` namespace reads over
``result_processing_violations`` and ``action_events`` are legitimately
owned — the cross-plugin data-access mandate's prohibition on
foreign-namespace ``query_state`` is exactly why the reconciler does NOT
live in the thinking plugin.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_CORE_NAMESPACE = "core"
_VIOLATIONS_TABLE = "result_processing_violations"
_ACTIONS_TABLE = "action_events"
# Both core tables key their flow column by the FK naming convention —
# there is NO plain ``flow_id`` column on either (DB-verified 2026-07-05;
# a plain-name filter errors, which the ActionResult envelope would have
# surfaced as zero rows — the reconciler would go silently blind).
_FLOW_COLUMN = "core__flows_id"
_RUN_FLOW_TRIGGER_TYPE = "joseki_run"

# Action statuses that count as "in flight" for stall detection.
_INFLIGHT_STATUSES = ("queued", "running")


class SessionManagerLike(Protocol):
    def create_session(
        self,
        namespace: str,
        context_type: str,
        metadata: dict[str, object] | None = None,
    ) -> str: ...


class FlowCreatorLike(Protocol):
    def create_flow(
        self,
        session_id: str,
        trigger_type: str,
        trigger_data: dict[str, object],
        priority: int = 5,
    ) -> str: ...


class StateReaderLike(Protocol):
    def read_state(
        self, namespace: str, query: dict[str, object],
    ) -> dict[str, Any]: ...

    def query_ordered(
        self, namespace: str, data: dict[str, object],
    ) -> dict[str, Any]: ...


class PlanBufferLike(Protocol):
    """The thinking plugin's session-scoped plan surface (JOS-02)."""

    def has_focused_plan(self, *, session_id: str) -> bool: ...

    def upsert_plan(self, content: str, *, session_id: str) -> dict[str, Any]: ...

    def release_session_focus(self, *, session_id: str) -> None: ...


def build_joseki_run_engine(*, gateway: Any, orchestrator: Any) -> Any:
    """The ONE construction path for the run engine (wrapper AND plugin).

    The gateway satisfies the engine's plugin seam and the installer's
    plan-buffer seam; sessions/flows and the reconciler's core-owned reads
    come from the orchestrator. Import is local so this module stays
    import-light at service construction time.
    """
    from ananta.services.thinking_service.joseki_run_engine import (
        JosekiRunEngine,
    )

    if orchestrator is None:
        raise RuntimeError(
            "joseki run driver requires the orchestrator collaborator"
        )
    return JosekiRunEngine(
        plugin=gateway,
        sessions=SessionManagerRunSessions(
            session_manager=orchestrator.session_manager,
            flow_creator=orchestrator,
        ),
        plans=FocusBufferPlanInstaller(plans=gateway.plan_buffer),
        flows=CoreFlowInspector(state_reader=orchestrator.state_service),
        run_manifest_id="wmf-joseki-runs",
    )


@dataclass(frozen=True)
class SessionManagerRunSessions:
    """Mint a REAL run session + flow (v3.1 ruling — no phantom ids)."""

    session_manager: SessionManagerLike
    flow_creator: FlowCreatorLike

    def create_run_session(self, *, run_label: str) -> tuple[str, str]:
        session_id = self.session_manager.create_session(
            namespace="thinking_service",
            context_type="joseki_run",
            metadata={"joseki_run": run_label},
        )
        flow_id = self.flow_creator.create_flow(
            session_id,
            _RUN_FLOW_TRIGGER_TYPE,
            # source_namespace is load-bearing: flows lacking it fail
            # _resolve_io_process_key with "Empty source_namespace in flow
            # trigger_data" (the pre-P1-A cron failure class).
            {"source_namespace": "thinking_service", "joseki_run": run_label},
        )
        return session_id, str(flow_id)


@dataclass(frozen=True)
class FocusBufferPlanInstaller:
    """Run-plan install/release over the plugin's own plan machinery.

    Reuses ``upsert_plan`` (create-and-focus when no plan exists) and the
    plan store's focus buffer — the SAME path every live plan rides, so the
    ``ACTIVE_PLAN_MARKER`` framing the deterministic-context resolver reads
    is produced by the production code, not re-implemented here. Every
    operation keys by the RUN session (JOS-02); terminal release clears the
    run session's WHOLE buffer (R1).
    """

    plans: PlanBufferLike

    def has_focused_active_plan(self, *, session_id: str) -> bool:
        return self.plans.has_focused_plan(session_id=session_id)

    def install_focused_plan(
        self, *, session_id: str, wbs_id: str, plan_content: str,
    ) -> None:
        header = f"ACTIVE_WBS: {wbs_id}\n\n"
        self.plans.upsert_plan(header + plan_content, session_id=session_id)

    def release_session_focus(self, *, session_id: str) -> None:
        self.plans.release_session_focus(session_id=session_id)


@dataclass(frozen=True)
class CoreFlowInspector:
    """Core-owned reads for the reconciler's ordered duties (spec §4.3)."""

    state_reader: StateReaderLike

    def latest_contract_violation(self, *, flow_id: str) -> dict[str, Any] | None:
        return self._first(
            _VIOLATIONS_TABLE, {_FLOW_COLUMN: flow_id},
        )

    def latest_failed_action(self, *, flow_id: str) -> dict[str, Any] | None:
        return self._first(
            _ACTIONS_TABLE, {_FLOW_COLUMN: flow_id, "status": "failed"},
        )

    def has_inflight_action(self, *, flow_id: str) -> bool:
        return any(
            self._first(_ACTIONS_TABLE, {_FLOW_COLUMN: flow_id, "status": status})
            is not None
            for status in _INFLIGHT_STATUSES
        )

    def completed_action_count(self, *, flow_id: str) -> int:
        """Completed actions on the run flow (bounded read; runs are small)."""
        result = self.state_reader.read_state(
            namespace=_CORE_NAMESPACE,
            query={
                "table": _ACTIONS_TABLE,
                "filters": {_FLOW_COLUMN: flow_id, "status": "completed"},
                "limit": 500,
            },
        )
        records = result.get("data", {}).get("records") or []
        return len(records)

    def _first(
        self, table: str, filters: dict[str, object],
    ) -> dict[str, Any] | None:
        # query_ordered, NOT read_state: the plain select composes no ORDER BY
        # (provider.build_select_sql — live-verified during the Track-A e2e
        # proof), so "latest" over read_state would return an arbitrary row.
        # The tie-safe composite is the ordered-query contract's minimum.
        result = self.state_reader.query_ordered(
            namespace=_CORE_NAMESPACE,
            data={
                "table": table,
                "filters": filters,
                "order_by": [["created_at", "desc"], ["id", "desc"]],
                "limit": 1,
            },
        )
        records = result.get("data", {}).get("records") or []
        return records[0] if records else None
