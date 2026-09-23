#!/usr/bin/env python3
"""Joseki run driver smoke (no pytest) — Track A, spec 2026-07-05 v3.3.

Exercises the CORE-side engine (`joseki_run_engine`) through the REAL
plugin-side composition — `JosekiRunGateway` over the real
`joseki_instantiation` module and the real `JosekiRunStore` against an
envelope-faithful in-memory state double — plus the wiring's
`FocusBufferPlanInstaller`. Thirteen cases; each is a helper function and
``main`` only dispatches (cyclomatic-gate shape, mirroring the GTE-04
smoke):

  [1] kickoff happy path incl. the F1a chain-ignition stamp
  [2] serialization busy guard    [3] foreign-focus guard
  [4] terminal exactly-once evidence + duplicate noop
  [5] reconciler duty order (violation beats runtime failure)
  [6] reconciler terminal guard   [7] draft card refused
  [8] non-mechanizable rejection  [9] runtime-failure surfacing
  [10] stall self-clear at the attempts cap
  [11] kickoff-orphan containment on install failure
  [12] v1 deterministic-only scope rejection (delta-2 F3 option b)
  [13] progress stamps the cursor and resets attempts (delta-2 N)
  [14] reconciler filter columns are core-schema-true (live-proof fix 2)
  [15] terminal complete_joseki_run declares error customizations (§16)

The test card's step targets ``service_interface::knowledge_service::search``
— a REAL registered verb (whole-tree integration C3.1 requires referenced
service keys to exist; the semantics are irrelevant offline, only the key's
reality matters). Live-platform behaviors intentionally NOT covered here
(deploy-stage live verify per the spec): cron-shaped kickoff, MCP bridge
delivery of the run handle, and the coordinator's real continuation hops.

Run from repo root:
    .venv/bin/python3 plugins/default_thinking_plugin/tests/joseki_run_driver_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "default_thinking_plugin" / "src"))

from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.services.thinking_service.joseki_run_engine import (  # noqa: E402
    JosekiRunEngine,
)
from ananta.services.thinking_service.joseki_run_wiring import (  # noqa: E402
    FocusBufferPlanInstaller,
)
from default_thinking_plugin.joseki_run_gateway import JosekiRunGateway  # noqa: E402
from default_thinking_plugin.joseki_run_store import JosekiRunStore  # noqa: E402

_STEP_PROCESS_KEY = "service_interface::knowledge_service::search"

_CARD = """# Test Card
JOSEKI_KEY: smoke_gate_run

## Sequence

[ ] 1. Search for the bound topic
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Search (service_interface::knowledge_service::search)
        Arguments:
        {"query": "<<BIND:g>>"}

## Expected Step Count

1 step.
"""

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _error_code(exc: FrameworkError) -> str:
    return str(getattr(exc, "error_code", ""))


# -- doubles ---------------------------------------------------------------------


class _MemState:
    """Envelope-faithful in-memory RunStateStore double."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self._n = 0

    def write_state(self, namespace: str, data: dict[str, object]) -> dict[str, Any]:
        del namespace
        self._n += 1
        run_id = f"jrun-{self._n}"
        raw = data["record"]
        assert isinstance(raw, dict)
        record: dict[str, Any] = dict(raw)
        record["id"] = run_id
        record["is_deleted"] = 0
        self.rows[run_id] = record
        return {"data": {"result": {"generated_id": run_id}}}

    def read_state(self, namespace: str, query: dict[str, object]) -> dict[str, Any]:
        del namespace
        filters = query.get("filters", {})
        assert isinstance(filters, dict)
        matches = [
            row
            for row in self.rows.values()
            if all(row.get(k) == v for k, v in filters.items())
        ]
        limit = query.get("limit", 50)
        assert isinstance(limit, int)
        return {"data": {"records": matches[:limit]}}

    def update_state(
        self,
        namespace: str,
        query: dict[str, object],
        updates: dict[str, object],
    ) -> dict[str, Any]:
        del namespace
        filters = query.get("filters", {})
        assert isinstance(filters, dict)
        updated = 0
        for row in self.rows.values():
            if all(row.get(k) == v for k, v in filters.items()):
                row.update(updates)
                updated += 1
        return {"data": {"result": {"updated": updated}}}


class _Lifecycle:
    def __init__(self, state: str = "candidate") -> None:
        self.state = state
        self.recorded: list[str | None] = []

    def get(self, *, joseki_key: str) -> dict[str, Any]:
        del joseki_key
        return {"found": True, "state": self.state}

    def record_run(
        self, *, joseki_key: str, wbs_id: str | None = None,
    ) -> dict[str, Any]:
        del joseki_key
        self.recorded.append(wbs_id)
        return {"state": "proven", "run_count": len(self.recorded)}


class _Cards:
    def __init__(self, card: str | None = None) -> None:
        self._card = card if card is not None else _CARD

    def read(self, path: str) -> str:
        del path
        return self._card


class _PermissiveLookup:
    """Offline stand-in for the live process-registry lookup.

    Every key exists and declares no argument schema — so the FULL
    ``validate_authored_wbs`` chain runs here while the two genuinely
    registry-dependent checks (key existence, argument schemas) stay
    permissive. Those two are exactly what the mandatory pre-deploy LIVE
    run covers; everything structural is enforced offline.
    """

    def get_arg_properties(
        self, process_key: str,
    ) -> dict[str, dict[str, object]]:
        del process_key
        return {}

    def key_exists(self, process_key: str) -> bool:
        del process_key
        return True


class _Registrar:
    """Registrar fake that enforces the REAL registrar's validation contract.

    The live path runs ``validate_authored_wbs``; a fake that discards
    ``content`` lets emission-shape drift escape to the production boot
    (it did: the ``### Work Item 1:`` emission passed this fake and was
    rejected live by ``validate_work_item_terminal_steps`` — run WBS
    documents must be the validator-blessed joseki-scoped FRAGMENT shape).
    The fake therefore runs the ONE entrypoint the register verb runs —
    ``validate_authored_wbs``, the full chain — not a curated subset that
    would re-open the class (Rev-A live-fix delta rider).
    """

    def __init__(self) -> None:
        self.registered: list[str] = []

    def register(
        self, *, content: str, wbs_id: str, manifest_id: str, session_id: str,
    ) -> dict[str, Any]:
        del manifest_id, session_id
        from default_thinking_plugin.authored_validation import (
            validate_authored_wbs,
        )

        report = validate_authored_wbs(content, wbs_id, 1, _PermissiveLookup())
        if report.errors:
            raise ValueError(
                f"registrar contract violated: {'; '.join(report.errors)}"
            )
        self.registered.append(wbs_id)
        return {"wbs_id": wbs_id}


class _PlanBuffer:
    """Session-scoped focus stub (JOS-02): one flag per session."""

    def __init__(self) -> None:
        self.focused_sessions: set[str] = set()
        self.installs = 0
        self.releases = 0

    def has_focused_plan(self, *, session_id: str) -> bool:
        return session_id in self.focused_sessions

    def upsert_plan(self, content: str, *, session_id: str) -> dict[str, Any]:
        del content
        self.focused_sessions.add(session_id)
        self.installs += 1
        return {}

    def release_session_focus(self, *, session_id: str) -> None:
        if session_id in self.focused_sessions:
            self.releases += 1
        self.focused_sessions.discard(session_id)


class _Sessions:
    def create_run_session(self, *, run_label: str) -> tuple[str, str]:
        del run_label
        return ("sess-run", "flow-run")


class _Flows:
    def __init__(self) -> None:
        self.violation: dict[str, Any] | None = None
        self.failed: dict[str, Any] | None = None
        self.inflight = False
        self.completed = 0

    def latest_contract_violation(self, *, flow_id: str) -> dict[str, Any] | None:
        del flow_id
        return self.violation

    def latest_failed_action(self, *, flow_id: str) -> dict[str, Any] | None:
        del flow_id
        return self.failed

    def has_inflight_action(self, *, flow_id: str) -> bool:
        del flow_id
        return self.inflight

    def completed_action_count(self, *, flow_id: str) -> int:
        del flow_id
        return self.completed


class _Rig:
    """One engine + its collaborator doubles, per case."""

    def __init__(
        self,
        lifecycle: _Lifecycle | None = None,
        card: str | None = None,
    ) -> None:
        self.life = lifecycle or _Lifecycle()
        self.registrar = _Registrar()
        self.buffer = _PlanBuffer()
        self.flows = _Flows()
        gateway = JosekiRunGateway(
            lifecycle=self.life,
            cards=_Cards(card),
            registrar=self.registrar,
            run_store=JosekiRunStore(
                state_store=_MemState(), namespace="default_thinking_plugin",
            ),
            plan_buffer=self.buffer,
        )
        self.engine = JosekiRunEngine(
            plugin=gateway,
            sessions=_Sessions(),
            plans=FocusBufferPlanInstaller(plans=self.buffer),
            flows=self.flows,
            run_manifest_id="wmf-joseki-runs",
        )

    def start(self, binding: str = "ruff") -> dict[str, Any]:
        return self.engine.run_joseki(
            joseki_key="smoke_gate_run", bindings={"g": binding},
        )


# -- cases -----------------------------------------------------------------------


def _case_kickoff_and_terminal(rig: _Rig) -> dict[str, Any]:
    """[1] + [2] + [4] share one rig (a started run)."""
    out = rig.start()
    action = out["actions"][0]
    row = rig.engine.get_joseki_run(run_id=out["run_id"])
    _check(
        out["status"] == "running"
        and action["process_key"] == _STEP_PROCESS_KEY
        and action["arguments"] == {"query": "ruff"}
        and action["session_id"] == "sess-run"
        # The RUN flow stamp is load-bearing: the poller's context injection
        # respects an explicit flow_id (only-when-absent), so this stamp is
        # what homes the whole chain on the run flow the reconciler reads.
        # Live-proven failure shape: the pre-fix unconditional overwrite
        # re-homed the chain onto the caller's flow (2026-07-05).
        and action["flow_id"] == "flow-run"
        and action["result_processor_kind"] == "deterministic_continuation"
        and rig.buffer.installs == 1
        and rig.registrar.registered == [out["wbs_id"]]
        and row["current_step"] == 0,
        "[1] kickoff: instantiate→register→row→focus→stamped Pattern-6a action",
    )
    return out


def _case_busy_guard(rig: _Rig) -> None:
    try:
        rig.start("mi")
        _check(False, "[2] busy guard rejects a second concurrent run (typed)")
    except FrameworkError as exc:
        _check(
            "joseki_run_busy" in _error_code(exc),
            "[2] busy guard rejects a second concurrent run (typed)",
        )


def _case_terminal_exactly_once(rig: _Rig, out: dict[str, Any]) -> None:
    first = rig.engine.complete_joseki_run(wbs_id=out["wbs_id"])
    second = rig.engine.complete_joseki_run(wbs_id=out["wbs_id"])
    _check(
        first["outcome"] == "completed"
        and first["run_count"] == 1
        and len(rig.life.recorded) == 1
        and rig.buffer.releases == 1
        and second["outcome"] == "noop_lost_cas"
        and len(rig.life.recorded) == 1,
        "[4] terminal wins once, evidence once, duplicate is a benign noop",
    )


def _case_foreign_focus_guard(rig: _Rig) -> None:
    del rig  # JOS-02: fresh rigs — the shared rig's rows must stay untouched
    # A focused plan in a FOREIGN session no longer blocks kickoff (the
    # pre-JOS-02 global-focus rejection class is structurally gone).
    rig_a = _Rig()
    rig_a.buffer.focused_sessions.add("sess-operator")
    out = rig_a.start("cc")
    _check(
        out["status"] == "running",
        "[3] kickoff proceeds despite a FOREIGN session's focused plan (JOS-02)",
    )
    # A FRESH run session already holding focus is an invariant breach (typed).
    rig_b = _Rig()
    rig_b.buffer.focused_sessions.add("sess-run")
    try:
        rig_b.start("cc")
        _check(False, "[3] fresh-run-session focus breaches the scoping invariant")
    except FrameworkError as exc:
        _check(
            "state_conflict" in _error_code(exc),
            "[3] fresh-run-session focus breaches the scoping invariant",
        )


def _case_duty_order_and_terminal_guard(rig: _Rig) -> None:
    run = rig.start("mi")
    rig.flows.violation = {"invariant": "auto_safe", "message": "tampered args"}
    rig.flows.failed = {"process_key": "x", "error_message": "boom"}
    verdict = rig.engine.reconcile_run(run_id=run["run_id"])
    row = rig.engine.get_joseki_run(run_id=run["run_id"])
    _check(
        verdict["duty"] == "violation_surfaced"
        and verdict["outcome"] == "failed"
        and "auto_safe" in row["failure_detail"]
        and rig.buffer.releases == 2,
        "[5] duty-0 violation surfaced before duty-1; typed detail; focus released",
    )
    again = rig.engine.reconcile_run(run_id=run["run_id"])
    _check(again["duty"] == "none", "[6] reconciler no-ops on a terminal run")
    rig.flows.violation = None
    rig.flows.failed = None


def _case_runtime_failure(rig: _Rig) -> None:
    run = rig.start("gc")
    rig.flows.failed = {"process_key": "search", "error_message": "exit 2"}
    verdict = rig.engine.reconcile_run(run_id=run["run_id"])
    _check(
        verdict["duty"] == "runtime_failure_surfaced"
        and "exit 2" in verdict["detail"],
        "[9] duty-1 surfaces the failed action's typed error",
    )
    rig.flows.failed = None


def _case_draft_refused() -> None:
    rig = _Rig(lifecycle=_Lifecycle(state="draft"))
    try:
        rig.start("x")
        _check(False, "[7] draft card refused")
    except FrameworkError as exc:
        _check(
            "joseki_not_runnable" in _error_code(exc),
            "[7] draft card refused with typed joseki_not_runnable",
        )


def _case_stall_self_clear() -> None:
    rig = _Rig()
    run = rig.start("gc")
    outcomes = [
        rig.engine.reconcile_run(run_id=run["run_id"])["duty"] for _ in range(5)
    ]
    row = rig.engine.get_joseki_run(run_id=run["run_id"])
    _check(
        outcomes[:4] == ["stall_detected"] * 4
        and outcomes[4] == "stall_attempts_exhausted"
        and row["status"] == "failed"
        and "consecutive reconciliation passes" in row["failure_detail"]
        and not rig.buffer.focused_sessions,
        "[10] stalls self-clear: attempts climb then terminal-fail at cap",
    )


def _case_install_orphan_containment() -> None:
    rig = _Rig()

    def _boom(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("focus buffer unavailable")

    rig.buffer.upsert_plan = _boom  # type: ignore[method-assign]
    try:
        rig.start("x")
        _check(False, "[11] install failure re-raises")
    except RuntimeError:
        failed = rig.engine.list_joseki_runs(status="failed")
        running = rig.engine.list_joseki_runs(status="running")
        _check(
            failed["count"] == 1
            and "install failed" in failed["runs"][0]["failure_detail"]
            and running["count"] == 0,
            "[11] install failure → row CAS-failed, no running orphan",
        )


def _case_v1_inference_scope() -> None:
    inference_card = _CARD.replace(
        "RESULT_PROCESSOR_KIND: deterministic_continuation",
        "RESULT_PROCESSOR_KIND: inference",
    )
    rig = _Rig(card=inference_card)
    try:
        rig.start("x")
        _check(False, "[12] inference-kind card rejected in v1")
    except FrameworkError as exc:
        _check(
            "joseki_not_mechanizable" in _error_code(exc)
            and "deterministic-only" in str(exc),
            "[12] inference-kind step → typed v1-scope rejection at authoring",
        )


def _case_non_mechanizable() -> None:
    rig = _Rig()
    try:
        rig.engine.run_joseki(joseki_key="smoke_gate_run", bindings={})
        _check(False, "[8] non-mechanizable card refused")
    except FrameworkError as exc:
        _check(
            "joseki_not_mechanizable" in _error_code(exc)
            and "g" in str(exc)
            and rig.registrar.registered == []
            and rig.buffer.installs == 0,
            "[8] unbound slot → typed rejection naming it; nothing stored",
        )


def _case_progress_resets_attempts() -> None:
    rig = _Rig()
    run = rig.start("mi")
    rig.engine.reconcile_run(run_id=run["run_id"])  # stall pass 1
    rig.engine.reconcile_run(run_id=run["run_id"])  # stall pass 2
    rig.flows.completed = 1  # the flow advanced
    progress = rig.engine.reconcile_run(run_id=run["run_id"])
    row = rig.engine.get_joseki_run(run_id=run["run_id"])
    _check(
        progress["duty"] == "progress_observed"
        and row["attempts"] == 0
        and row["current_step"] == 1
        and row["status"] == "running",
        "[13] observed progress stamps the cursor and resets attempts",
    )


def _case_reconciler_columns_schema_true() -> None:
    """[14] the wiring's filter columns exist in the CORE schema truth.

    Live-proven escape shape (Track-A first production run): the wiring
    filtered ``action_events`` on a phantom ``flow_id`` column — the real
    column is the FK-named ``core__flows_id`` — and the error envelope
    reads as zero rows, so the reconciler goes silently blind. This pins
    every filter/order column against the schema DEFINITIONS (offline,
    the same source the DDL installs from).
    """
    from ananta.config.core_schemas import CoreSchemaDefinitions
    from ananta.services.thinking_service import joseki_run_wiring as wiring
    from ananta.types.schema_standardizer import StandardFieldDefinitions

    actions = (
        CoreSchemaDefinitions.get_action_events_schema()
        .tables["action_events"]
        .columns
    )
    violations = (
        CoreSchemaDefinitions.get_result_processing_violations_schema()
        .tables["result_processing_violations"]
        .columns
    )
    # The query_ordered tie-safe composite rides the platform's STANDARD
    # fields (injected on every table by the standardizer, never declared
    # per-table) — pin them at their source (Rev-A round-2 rider).
    standard = set(StandardFieldDefinitions.get_standard_fields())
    _check(
        wiring._FLOW_COLUMN in actions
        and "status" in actions
        and wiring._FLOW_COLUMN in violations
        and "flow_id" not in actions
        and {"created_at", "id"} <= standard,
        "[14] reconciler filter + order columns are core-schema-true",
    )


def _case_terminal_verb_carries_error_block() -> None:
    """[15] complete_joseki_run declares error customizations (§16).

    The terminal step is the ONLY EDGE_SINK the platform submits as a
    deterministic continuation; §16 (action_factory
    ``_require_error_processor_for_deterministic``) REJECTS the hop unless
    the registry carries error customizations to auto-inject. Live-proven:
    the first clean production chain died at exactly this submission
    (2026-07-05) because the EDGE_SINK declaration carried no blocks.
    """
    from ananta.services.thinking_service.interfaces.public import (
        ThinkingServiceAPI,
    )

    meta = getattr(
        ThinkingServiceAPI.complete_joseki_run,
        "_service_interface_metadata",
        None,
    )
    _check(
        meta is not None
        and meta.error_processor_customizations is not None,
        "[15] terminal complete_joseki_run declares error customizations (§16)",
    )


def main() -> int:
    print("Joseki run driver smoke")
    shared = _Rig()
    out = _case_kickoff_and_terminal(shared)
    _case_busy_guard(shared)
    _case_terminal_exactly_once(shared, out)
    _case_foreign_focus_guard(shared)
    _case_duty_order_and_terminal_guard(shared)
    _case_runtime_failure(shared)
    _case_draft_refused()
    _case_stall_self_clear()
    _case_install_orphan_containment()
    _case_v1_inference_scope()
    _case_progress_resets_attempts()
    _case_non_mechanizable()
    _case_reconciler_columns_schema_true()
    _case_terminal_verb_carries_error_block()

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
