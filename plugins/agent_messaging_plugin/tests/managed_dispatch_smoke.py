#!/usr/bin/env python3
"""Red-first smoke for the durable managed-dispatch contract.

Each test names the corresponding fixture in section 11 of the accepted
2026-08-22 coordination-efficiency design.  Host-driver reconciliation and
raw-spawn bypass have adapter-level fixtures in their existing smoke files;
this surface owns the pure dispatch/event state machine.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from _recorded_lane_worktree_fixture import RecordedLaneWorktreeFixture  # noqa: E402
from ananta.core.process_registry.invocation_schema_generator import (  # noqa: E402
    InvocationSchemaGenerator,
)
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
    COL_AGENT_INSTANCE_ID,
    COL_AGENT_SESSION_ID,
    COL_CLAIM_EPOCH,
    COL_HOLDER_IDENTITY,
    COL_HOLDER_KIND,
    COL_ROLE,
    HOLDER_KIND_SESSION,
    TABLE_ROLE_BINDING,
    role_binding_external_id,
)

import agent_messaging_plugin.session_hosts as session_hosts  # noqa: E402
from agent_messaging_plugin.managed_dispatch import (  # noqa: E402
    DISPATCH_ACTIVE,
    DISPATCH_BLOCKED_INTERNAL,
    DISPATCH_COMPLETED,
    DISPATCH_COMPLETION_REPORTED,
    DISPATCH_EXPIRED,
    DISPATCH_FAILED_START,
    DISPATCH_UPTAKE_PENDING,
    DISPATCH_UPTAKE_UNCERTAIN,
    DispatchActor,
    DispatchError,
    DispatchSpec,
    dispatch_managed_work,
    managed_dispatch_status,
    mark_dispatch_worker_lost,
    prepare_managed_dispatch,
    read_managed_dispatch,
    record_first_turn_evidence,
    report_managed_dispatch,
    resolve_managed_dispatch,
    supervise_managed_dispatches,
)
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    TABLE_MANAGED_DISPATCH,
    TABLE_MANAGED_DISPATCH_EVENT,
)
from agent_messaging_plugin.session_hosts import HostCannotSpawnError  # noqa: E402
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    SpawnSessionRequest,
    _dispatch_contract_mismatches,
)

T0 = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)
_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


def _state() -> StateManagementInterface:
    state = cast("StateManagementInterface", RealShapeState())
    _seed_binding(
        state,
        role="Codex-Fixture-Builder",
        instance="agi-worker-1",
        session="ases-worker-1",
    )
    _seed_binding(
        state,
        role="Coordinator-Main",
        instance="agi-coordinator",
        session="ases-coordinator",
    )
    return state


def _seed_binding(
    state: StateManagementInterface,
    *,
    role: str,
    instance: str,
    session: str,
) -> None:
    state.upsert_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_ROLE_BINDING,
            "record": {
                "external_id": role_binding_external_id(role),
                COL_ROLE: role,
                COL_HOLDER_KIND: HOLDER_KIND_SESSION,
                COL_AGENT_INSTANCE_ID: instance,
                COL_AGENT_SESSION_ID: session,
                COL_HOLDER_IDENTITY: {"agent_id": "codex", "session_label": role},
                COL_CLAIM_EPOCH: 1,
            },
            "conflict_columns": ["external_id"],
        },
    )


def _worker_actor(instance: str = "agi-worker-1") -> DispatchActor:
    return DispatchActor(
        agent_instance_id=instance,
        agent_session_id="ases-worker-1" if instance == "agi-worker-1" else "ases-attacker",
        authority_source="live_peer_binding",
    )


def _coordinator_actor() -> DispatchActor:
    return DispatchActor(
        agent_instance_id="agi-coordinator",
        agent_session_id="ases-coordinator",
        authority_source="live_peer_binding",
    )


def _dispatch_events(
    state: StateManagementInterface,
    *,
    event_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_MANAGED_DISPATCH_EVENT,
            "filters": {"dispatch_id": "mdp-fixture", "is_deleted": 0},
        },
    )
    records = cast(list[dict[str, Any]], result["data"]["records"])
    if event_ids is None:
        return records
    return [record for record in records if str(record["event_id"]) in event_ids]


def _brief(tmp: Path) -> tuple[Path, str]:
    path = tmp / "brief.md"
    path.write_text("exact immutable brief\n", encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _spec(tmp: Path, **overrides: Any) -> DispatchSpec:
    brief, brief_sha256 = _brief(tmp)
    base: dict[str, Any] = {
        "dispatch_id": "mdp-fixture",
        "lane_id": "coordination-fixture",
        "role_name": "Codex-Fixture-Builder",
        "role_class": "project",
        "work_class": "production_mutation",
        "budget_line": "coordination-fixture-budget",
        "brief_ref": str(brief),
        "unit_id": "unt-dispatch-replay-01234567",
        "brief_sha256": brief_sha256,
        "expected_path": str(tmp / "report.md"),
        "completion_contract": {
            "evidence_obligations": [
                {"id": "focused", "allowed_statuses": ["pass"]},
                {"id": "registered", "allowed_statuses": ["pass", "skipped"]},
            ],
            "allowed_verdicts": ["READY-FOR-REVIEW", "BLOCKED"],
        },
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
        "agent_runtime": "codex",
        "allowed_hosts": ["headless", "tmux"],
        "host": "headless",
        "visibility": "headless",
        "local_name": "Codex-Fixture-Builder",
        "report_by_seconds": 900,
        "ttl_seconds": 14400,
        "allowed_tools": ("Bash", "Read"),
        "permission_mode": "bypassPermissions",
        "transport": "mcp",
        "allow_askuserquestion": False,
        "degraded_hooks_acknowledged": False,
        "spawned_by_instance_id": "agi-coordinator",
        "spawned_by_role": "Coordinator-Main",
        "directed_by": "operator:seat",
        "uptake_due_at": (T0 + timedelta(minutes=2)).isoformat(),
        "report_by": (T0 + timedelta(minutes=15)).isoformat(),
        "watchdog_due_at": (T0 + timedelta(minutes=3)).isoformat(),
        "expires_at": (T0 + timedelta(hours=4)).isoformat(),
        "dispatch_kind": "infrastructure",
    }
    base.update(overrides)
    return DispatchSpec(**base)


def _prepare(state: StateManagementInterface, tmp: Path, **overrides: Any) -> dict[str, Any]:
    return prepare_managed_dispatch(state, _spec(tmp, **overrides), now=T0)


def _spawn_request(spec: DispatchSpec) -> SpawnSessionRequest:
    return SpawnSessionRequest(
        role_class=spec.role_class,
        lane_id=spec.lane_id,
        brief_ref=spec.brief_ref,
        unit_id=spec.unit_id,
        repository_root=spec.repository_root,
        work_class=spec.work_class,
        budget_line=spec.budget_line,
        agent_runtime=spec.agent_runtime,
        role_name=spec.role_name,
        host=spec.host,
        visibility=spec.visibility,
        model=spec.model,
        effort=spec.effort,
        report_by_seconds=spec.report_by_seconds,
        ttl_seconds=spec.ttl_seconds,
        spawned_by_instance_id=spec.spawned_by_instance_id,
        spawned_by_role=spec.spawned_by_role,
        directed_by=spec.directed_by,
        allowed_tools=spec.allowed_tools,
        permission_mode=spec.permission_mode,
        transport=spec.transport,
        allow_askuserquestion=spec.allow_askuserquestion,
        local_name=spec.local_name,
        degraded_hooks_acknowledged=spec.degraded_hooks_acknowledged,
        dispatch_kind=spec.dispatch_kind,
        reviewed_report_vendor=spec.reviewed_report_vendor,
        pair_id=spec.pair_id,
    )


class _ReplacementChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


class _ReplacementDriver:
    def __init__(self, *, fail_start: bool) -> None:
        self.fail_start = fail_start
        self.spawned_instances: list[str] = []
        self.spawn_specs: list[dict[str, object]] = []
        self.channel = _ReplacementChannel()

    def spawn(self, spec: dict[str, object]) -> str:
        if self.fail_start:
            raise HostCannotSpawnError("fixture start refusal")
        instance = str(spec["agent_instance_id"])
        self.spawned_instances.append(instance)
        self.spawn_specs.append(spec)
        return f"fixture-host:{instance}"

    def alive(self, host_ref: str) -> bool:
        del host_ref
        return True

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del host_ref, grace_seconds

    def driver_channel(self, host_ref: str) -> _ReplacementChannel:
        del host_ref
        return self.channel

    def capability_report(self) -> dict[str, object]:
        return {}

    def verify_config(self) -> list[str]:
        return []


class _ActorRegistry:
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._bindings = (
            {"agi-authenticated": "ases-authenticated"}
            if bindings is None
            else bindings
        )

    def agent_session_id_for_instance(self, instance: str) -> str:
        return self._bindings.get(instance, "")


def _first_turn(
    state: StateManagementInterface,
    *,
    delivered: bool,
    error: str = "",
    instance: str = "agi-worker-1",
) -> dict[str, Any]:
    return record_first_turn_evidence(
        state,
        dispatch_id="mdp-fixture",
        agent_instance_id=instance,
        source="charter",
        delivered=delivered,
        error=error,
        host="headless",
        host_ref="12345",
        agent_runtime="codex",
        observed_at=T0 + timedelta(seconds=1),
    )


def _ack(
    state: StateManagementInterface, *, event_id: str = "evt-ack", version: int = 1
) -> dict[str, Any]:
    return report_managed_dispatch(
        state,
        dispatch_id="mdp-fixture",
        event_id=event_id,
        event_kind="ack",
        attempt_agent_instance_id="agi-worker-1",
        actor=_worker_actor(),
        prior_version=version,
        payload={
            "brief_sha256": read_managed_dispatch(state, "mdp-fixture")["brief_sha256"],
            "role_binding": "Codex-Fixture-Builder",
            "scope_readback_sha256": "a" * 64,
            "plan_sha256": "b" * 64,
        },
        observed_at=T0 + timedelta(seconds=2),
    )


def test_02_headless_submission_failure_is_durable() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(state, Path(raw))
        row = _first_turn(state, delivered=False, error="unsupported_on_host")
        _check(
            row["state"] == DISPATCH_UPTAKE_UNCERTAIN, "02 failed submission is uptake_uncertain"
        )
        _check(
            row["first_turn_error"] == "unsupported_on_host", "02 exact submission error is durable"
        )
        _check(not row.get("last_ack_at"), "02 no ACK is invented")


def test_03_transport_is_not_uptake() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(state, Path(raw))
        pending = _first_turn(state, delivered=True)
        _check(
            pending["state"] == DISPATCH_UPTAKE_PENDING,
            "03 delivered transport remains uptake_pending",
        )
        active = _ack(state)
        _check(active["state"] == DISPATCH_ACTIVE, "03 structured model-turn ACK alone activates")


def test_06_atomic_requirements_fail_before_dispatch() -> None:
    required = (
        "brief_sha256",
        "completion_contract",
        "uptake_due_at",
        "report_by",
        "watchdog_due_at",
        "expires_at",
    )
    for field in required:
        with tempfile.TemporaryDirectory() as raw:
            state = _state()
            bad: Any = {} if field == "completion_contract" else ""
            code = ""
            try:
                _prepare(state, Path(raw), **{field: bad})
            except DispatchError as exc:
                code = exc.code
            _check(code == f"{field}_required", f"06 missing {field} fails before a dispatch row")


def test_07_silent_internal_blocker_is_supervised_once_per_version() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(state, Path(raw))
        _first_turn(state, delivered=True)
        _ack(state)
        blocked = report_managed_dispatch(
            state,
            dispatch_id="mdp-fixture",
            event_id="evt-blocked",
            event_kind="blocked",
            attempt_agent_instance_id="agi-worker-1",
            actor=_worker_actor(),
            prior_version=2,
            payload={
                "blocker_class": "internal",
                "blocker_owner": "Coordinator-Main",
                "question": "Grant shared-checkout isolation?",
                "safe_options": ["grant", "schedule", "deny"],
                "evidence": {"suite": "registered"},
                "decision_due_at": (T0 + timedelta(minutes=5)).isoformat(),
            },
            observed_at=T0 + timedelta(minutes=1),
        )
        _check(
            blocked["state"] == DISPATCH_BLOCKED_INTERNAL, "07 internal blocker is explicit state"
        )
        first = supervise_managed_dispatches(state, now=T0 + timedelta(minutes=6))
        second = supervise_managed_dispatches(state, now=T0 + timedelta(minutes=7))
        _check(first["notices_emitted"] == 1, "07 overdue blocker emits one notice")
        _check(second["notices_emitted"] == 0, "07 unchanged blocker does not duplicate a wake")


def test_08_overdue_worker_and_ttl_surface_exact_action() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(state, Path(raw), report_by=(T0 + timedelta(minutes=3)).isoformat())
        _first_turn(state, delivered=True)
        _ack(state)
        result = supervise_managed_dispatches(state, now=T0 + timedelta(minutes=4))
        status = managed_dispatch_status(state, "mdp-fixture", now=T0 + timedelta(minutes=4))
        _check(
            result["conditions"][0]["condition"] == "milestone_overdue", "08 silence is classified"
        )
        _check(
            status["next_required_action"] == "request_worker_milestone",
            "08 status names the action",
        )
        _check(status["responsible_role"] == "Coordinator-Main", "08 status names the owner")
        _check(status["deadline_overdue"]["watchdog_due_at"], "08 watchdog debt is explicit")

        expired = supervise_managed_dispatches(state, now=T0 + timedelta(hours=4))
        expired_again = supervise_managed_dispatches(state, now=T0 + timedelta(hours=5))
        expired_status = managed_dispatch_status(state, "mdp-fixture", now=T0 + timedelta(hours=5))
        _check(expired["conditions"][0]["condition"] == "ttl_expired", "08 TTL is classified")
        _check(expired["conditions"][0]["state"] == DISPATCH_EXPIRED, "08 TTL becomes terminal")
        _check(
            expired_status["state"] == DISPATCH_EXPIRED,
            "08 TTL cannot remain indefinitely live",
        )
        _check(expired_again["conditions"] == [], "08 terminal expiry does not wake every tick")

    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(state, Path(raw), report_by=(T0 + timedelta(minutes=3)).isoformat())
        _first_turn(state, delivered=True)
        _ack(state)
        milestone = report_managed_dispatch(
            state,
            dispatch_id="mdp-fixture",
            event_id="evt-milestone",
            event_kind="milestone",
            attempt_agent_instance_id="agi-worker-1",
            actor=_worker_actor(),
            prior_version=2,
            payload={
                "completed_work": ["durable state slice"],
                "current_evidence": {"focused": "green"},
                "next_action": "finish_adapter_slice",
                "next_report_deadline": (T0 + timedelta(minutes=10)).isoformat(),
            },
            observed_at=T0 + timedelta(minutes=2),
        )
        no_longer_overdue = supervise_managed_dispatches(state, now=T0 + timedelta(minutes=4))
        _check(
            milestone["report_by"] == (T0 + timedelta(minutes=10)).isoformat(),
            "08 structured milestone advances report-by",
        )
        _check(
            [item["condition"] for item in no_longer_overdue["conditions"]]
            == ["watchdog_overdue"],
            "08 new report deadline governs silence while watchdog remains operational",
        )

    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(
            state,
            Path(raw),
            report_by=(T0 + timedelta(minutes=2)).isoformat(),
            watchdog_due_at=(T0 + timedelta(minutes=3)).isoformat(),
        )
        _first_turn(state, delivered=True)
        _ack(state)
        first = supervise_managed_dispatches(state, now=T0 + timedelta(minutes=4))
        second = supervise_managed_dispatches(state, now=T0 + timedelta(minutes=5))
        first_names = [item["condition"] for item in first["conditions"]]
        supervised_events = [
            event
            for event in _dispatch_events(state)
            if str(event["event_id"]).startswith("supervision:")
        ]
        _check(
            set(first_names) == {"milestone_overdue", "watchdog_overdue"}
            and len(first_names) == 2,
            "08 simultaneous milestone and watchdog debt are both supervised",
        )
        _check(first["notices_emitted"] == 2, "08 each newly owed condition emits once")
        _check(
            second["notices_emitted"] == 0,
            "08 repeated sweep deduplicates both unchanged causal conditions",
        )
        _check(
            len(supervised_events) == 2,
            "08 per-condition audit dedup stores one event for each causal debt",
        )


def test_08_staggered_milestone_survives_watchdog_version_change() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(
            state,
            Path(raw),
            report_by=(T0 + timedelta(minutes=2)).isoformat(),
            watchdog_due_at=(T0 + timedelta(minutes=3)).isoformat(),
        )
        _first_turn(state, delivered=True)
        _ack(state)
        staggered = [
            supervise_managed_dispatches(state, now=T0 + timedelta(minutes=2, seconds=30)),
            supervise_managed_dispatches(state, now=T0 + timedelta(minutes=4)),
            supervise_managed_dispatches(state, now=T0 + timedelta(minutes=5)),
        ]
        staggered_events = [
            event
            for event in _dispatch_events(state)
            if str(event["event_id"]).startswith("supervision:")
        ]
        staggered_event_conditions = [
            str(cast(dict[str, Any], event["payload"])["condition"])
            for event in staggered_events
        ]
        _check(
            [sweep["notices_emitted"] for sweep in staggered] == [1, 1, 0],
            "08 earlier milestone debt survives the later watchdog transition",
        )
        _check(
            len(staggered_events) == 2
            and staggered_event_conditions.count("milestone_overdue") == 1
            and staggered_event_conditions.count("watchdog_overdue") == 1,
            "08 staggered debts store exactly one audit episode per condition",
        )


def test_09_reconnect_and_duplicate_ack_are_idempotent() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(state, Path(raw))
        _first_turn(state, delivered=True)
        _first_turn(state, delivered=True)
        first = _ack(state)
        duplicate = _ack(state)
        _check(
            first["version"] == duplicate["version"] == 2,
            "09 duplicate ACK preserves causal version",
        )
        _check(
            duplicate["current_agent_instance_id"] == "agi-worker-1",
            "09 reconnect preserves attempt identity",
        )


def test_10_retry_request_is_idempotent() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(state, Path(raw))
        _first_turn(state, delivered=False, error="unsupported_on_host")
        kwargs = {
            "dispatch_id": "mdp-fixture",
            "event_id": "evt-retry",
            "action": "request_retry",
            "actor": _coordinator_actor(),
            "prior_version": 1,
            "payload": {"reason": "definitive start failure"},
            "observed_at": T0 + timedelta(minutes=1),
        }
        first = resolve_managed_dispatch(state, **kwargs)
        duplicate = resolve_managed_dispatch(state, **kwargs)
        _check(
            first["attempt_number"] == duplicate["attempt_number"] == 2,
            "10 retry increments attempt once",
        )
        _check(first["version"] == duplicate["version"], "10 duplicate retry is a no-op")


def test_11_supervisor_does_not_depend_on_coordinator_plan() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(state, Path(raw), uptake_due_at=(T0 + timedelta(seconds=10)).isoformat())
        result = supervise_managed_dispatches(state, now=T0 + timedelta(minutes=1))
        _check(result["evaluated"] == 1, "11 durable row is found without a coordinator plan")
        _check(
            result["conditions"][0]["condition"] == "uptake_overdue",
            "11 durable deadline advances independently",
        )
        status = managed_dispatch_status(state, "mdp-fixture", now=T0 + timedelta(minutes=1))
        _check(
            "unverified projections" in status["plan_projection_warning"],
            "11 status emits plan-projection drift warning only",
        )


def _report_completion(
    state: StateManagementInterface,
    *,
    event_id: str,
    prior_version: int,
    artifact_path: str,
    artifact_sha256: str,
    contract_sha256: str,
    attempt: str = "agi-worker-1",
    evidence: dict[str, Any] | None = None,
) -> str:
    try:
        report_managed_dispatch(
            state,
            dispatch_id="mdp-fixture",
            event_id=event_id,
            event_kind="completion",
            attempt_agent_instance_id=attempt,
            actor=_worker_actor(attempt),
            prior_version=prior_version,
            payload={
                "artifact_path": artifact_path,
                "artifact_sha256": artifact_sha256,
                "completion_contract_sha256": contract_sha256,
                "evidence": evidence
                or {
                    "focused": {"status": "pass", "detail": "focused suite green"},
                    "registered": {"status": "pass", "detail": "registered suite green"},
                },
                "verdict": "READY-FOR-REVIEW",
            },
            observed_at=T0 + timedelta(minutes=2),
        )
    except DispatchError as exc:
        return exc.code
    return ""


def test_12_completion_false_positives_remain_noncomplete() -> None:
    cases = ("wrong_hash", "wrong_contract", "stale_attempt")
    for case in cases:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            state = _state()
            row = _prepare(state, tmp)
            _first_turn(state, delivered=True)
            _ack(state)
            report = Path(row["expected_path"])
            report.write_text("draft is not acceptance\n", encoding="utf-8")
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
            code = _report_completion(
                state,
                event_id=f"evt-completion-{case}",
                prior_version=2,
                artifact_path=str(report),
                artifact_sha256="0" * 64 if case == "wrong_hash" else digest,
                contract_sha256=(
                    "f" * 64 if case == "wrong_contract" else row["completion_contract_sha256"]
                ),
                attempt="agi-stale" if case == "stale_attempt" else "agi-worker-1",
            )
            _check(bool(code), f"12 {case} is rejected")
            _check(
                read_managed_dispatch(state, "mdp-fixture")["state"] != DISPATCH_COMPLETED,
                f"12 {case} cannot complete",
            )


def test_13_only_coordinator_acceptance_completes() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        state = _state()
        row = _prepare(state, tmp)
        _first_turn(state, delivered=True)
        _ack(state)
        report = Path(row["expected_path"])
        report.write_text("exact final report\n", encoding="utf-8")
        digest = hashlib.sha256(report.read_bytes()).hexdigest()
        code = _report_completion(
            state,
            event_id="evt-completion",
            prior_version=2,
            artifact_path=str(report),
            artifact_sha256=digest,
            contract_sha256=row["completion_contract_sha256"],
        )
        reported = read_managed_dispatch(state, "mdp-fixture")
        _check(
            not code and reported["state"] == DISPATCH_COMPLETION_REPORTED,
            "13 worker report is not completion",
        )
        accepted = resolve_managed_dispatch(
            state,
            dispatch_id="mdp-fixture",
            event_id="evt-accept",
            action="accept_completion",
            actor=_coordinator_actor(),
            prior_version=3,
            payload={
                "artifact_sha256": digest,
                "acceptance_evidence": {
                    "evidence": {
                        "focused": {"status": "pass", "detail": "independent focused"},
                        "registered": {"status": "pass", "detail": "independent registered"},
                    },
                    "verdict": "READY-FOR-REVIEW",
                    "worker_evidence_sha256": hashlib.sha256(
                        b'{"focused":{"detail":"focused suite green","status":"pass"},'
                        b'"registered":{"detail":"registered suite green","status":"pass"}}'
                    ).hexdigest(),
                },
            },
            observed_at=T0 + timedelta(minutes=3),
        )
        _check(
            accepted["state"] == DISPATCH_COMPLETED,
            "13 coordinator revalidation is the only completion edge",
        )


def test_15_causal_version_rejects_races_but_keeps_audit() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(state, Path(raw))
        _first_turn(state, delivered=True)
        _ack(state)
        stale = ""
        try:
            report_managed_dispatch(
                state,
                dispatch_id="mdp-fixture",
                event_id="evt-stale-blocker",
                event_kind="blocked",
                attempt_agent_instance_id="agi-worker-1",
                actor=_worker_actor(),
                prior_version=1,
                payload={"blocker_class": "internal"},
                observed_at=T0 + timedelta(minutes=1),
            )
        except DispatchError as exc:
            stale = exc.code
        status = managed_dispatch_status(state, "mdp-fixture", now=T0 + timedelta(minutes=1))
        _check(stale == "stale_dispatch_version", "15 stale race loses loudly")
        _check(status["rejected_event_count"] == 1, "15 rejected event remains auditable")
        _check(status["state"] == DISPATCH_ACTIVE, "15 legal winner remains authoritative")


def test_review_f1_forged_worker_and_coordinator_authority_are_rejected() -> None:
    """F1 red: caller prose cannot authorize an ACK or self-acceptance."""
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        row = _prepare(state, Path(raw))
        _first_turn(state, delivered=True)
        forged = ""
        try:
            report_managed_dispatch(
                state,
                dispatch_id="mdp-fixture",
                event_id="evt-forged-ack",
                event_kind="ack",
                attempt_agent_instance_id="agi-worker-1",
                actor=_worker_actor("agi-unrelated-attacker"),
                prior_version=1,
                payload={
                    "brief_sha256": row["brief_sha256"],
                    "role_binding": "Codex-Fixture-Builder",
                    "scope_readback_sha256": "a" * 64,
                    "plan_sha256": "b" * 64,
                },
                observed_at=T0 + timedelta(seconds=2),
            )
        except DispatchError as exc:
            forged = exc.code
        _check(forged == "worker_authority_denied", "F1 forged actor ACK is rejected")

    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        row = _prepare(state, Path(raw))
        _first_turn(state, delivered=True)
        _ack(state)
        report = Path(row["expected_path"])
        report.write_text("worker cannot accept itself\n", encoding="utf-8")
        digest = hashlib.sha256(report.read_bytes()).hexdigest()
        _report_completion(
            state,
            event_id="evt-worker-report-before-self-accept",
            prior_version=2,
            artifact_path=str(report),
            artifact_sha256=digest,
            contract_sha256=row["completion_contract_sha256"],
        )
        denied = ""
        try:
            resolve_managed_dispatch(
                state,
                dispatch_id="mdp-fixture",
                event_id="evt-worker-self-accept",
                action="accept_completion",
                actor=_worker_actor(),
                prior_version=3,
                payload={},
                observed_at=T0 + timedelta(minutes=3),
            )
        except DispatchError as exc:
            denied = exc.code
        _check(
            denied == "coordinator_authority_denied",
            "F1 worker cannot self-authorize coordinator acceptance",
        )


def test_review_f1_public_authority_is_server_derived() -> None:
    plugin = AgentMessagingPlugin()
    plugin._peer_registry = cast(Any, _ActorRegistry())  # noqa: SLF001
    actor = plugin._dispatch_actor_from_state(  # noqa: SLF001
        {
            "authenticated_principal": {
                "client_id": "client-authenticated",
                "agent_id": "codex",
                "agent_instance_id": "agi-authenticated",
                "bridge_id": "agc-authenticated",
                "session_id": "session-authenticated",
            }
        }
    )
    unauthenticated = ""
    try:
        plugin._dispatch_actor_from_state({})  # noqa: SLF001
    except DispatchError as exc:
        unauthenticated = exc.code
    local_state = {"inference_vertex_session_id": "agi-local"}
    registered_local = AgentMessagingPlugin()
    registered_local._peer_registry = _ActorRegistry(  # noqa: SLF001
        {"agi-local": "ases-local"},
    )
    local_actor = registered_local._dispatch_actor_from_state(local_state)  # noqa: SLF001
    unregistered_local = AgentMessagingPlugin()
    unregistered_local._peer_registry = _ActorRegistry({})  # noqa: SLF001
    local_unregistered = ""
    try:
        unregistered_local._dispatch_actor_from_state(local_state)  # noqa: SLF001
    except DispatchError as exc:
        local_unregistered = exc.code
    report_parameters = AgentMessagingPlugin.report_managed_dispatch._platform_process_metadata.parameters  # type: ignore[attr-defined]  # noqa: E501,SLF001
    resolve_parameters = AgentMessagingPlugin.resolve_managed_dispatch._platform_process_metadata.parameters  # type: ignore[attr-defined]  # noqa: E501,SLF001
    dispatch_parameters = AgentMessagingPlugin.dispatch_managed_work._platform_process_metadata.parameters  # type: ignore[attr-defined]  # noqa: E501,SLF001
    _check(
        actor == DispatchActor(
            "agi-authenticated",
            "ases-authenticated",
            "oauth_principal",
        ),
        "F1 authority derives from authenticated principal plus registered session",
    )
    _check(
        unauthenticated == "dispatch_authentication_required",
        "F1 public authority fails closed without trusted call context",
    )
    _check(
        local_actor == DispatchActor("agi-local", "ases-local", "live_peer_binding"),
        "F1 registered local bridge identity reaches managed dispatch",
    )
    _check(
        local_unregistered == "dispatch_identity_unregistered",
        "F1 local bridge identity fails closed when its live binding is absent",
    )
    _check(
        not {"actor_role", "actor_instance_id"}
        & (set(report_parameters) | set(resolve_parameters))
        and "spawned_by_instance_id" not in dispatch_parameters,
        "F1 caller-supplied actor/spawner fields are absent from public authority schemas",
    )


def test_review_f2_prepared_contract_rejects_every_altered_spawn_field() -> None:
    """F2 red: every spawn-affecting and lineage field is immutable."""
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        row = _prepare(state, Path(raw))
        baseline = SpawnSessionRequest(
            role_class="project",
            lane_id="coordination-fixture",
            brief_ref=str(row["brief_ref"]),
            repository_root="/different-checkout",
            work_class="production_mutation",
            budget_line="coordination-fixture-budget",
            dispatch_id="mdp-fixture",
            agent_runtime="codex",
            role_name="Codex-Fixture-Builder",
            host="tmux",
            visibility="visible",
            model="different-model",
            effort="low",
            report_by_seconds=17,
            ttl_seconds=18,
            spawned_by_instance_id="agi-unrelated",
            spawned_by_role="Not-Coordinator-Main",
            directed_by="external:forged",
            allowed_tools=("Bash",),
            permission_mode="default",
            transport="watch",
            allow_askuserquestion=True,
            local_name="Different-Local-Name",
            degraded_hooks_acknowledged=True,
        )
        mismatches = set(_dispatch_contract_mismatches(row, baseline))
        required = {
            "host",
            "visibility",
            "model",
            "effort",
            "report_by_seconds",
            "ttl_seconds",
            "spawned_by_instance_id",
            "spawned_by_role",
            "directed_by",
            "allowed_tools",
            "permission_mode",
            "transport",
            "allow_askuserquestion",
            "local_name",
            "repository_root",
            "degraded_hooks_acknowledged",
        }
        _check(required <= mismatches, "F2 every altered spawn/lineage field mismatches")


def test_review_f3_incomplete_completion_evidence_is_rejected() -> None:
    """F3 red: a non-empty mapping is not satisfaction of the contract."""
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        state = _state()
        row = _prepare(state, tmp)
        _first_turn(state, delivered=True)
        _ack(state)
        report = Path(row["expected_path"])
        report.write_text("incomplete evidence\n", encoding="utf-8")
        digest = hashlib.sha256(report.read_bytes()).hexdigest()
        code = _report_completion(
            state,
            event_id="evt-incomplete-completion",
            prior_version=2,
            artifact_path=str(report),
            artifact_sha256=digest,
            contract_sha256=row["completion_contract_sha256"],
            evidence={"focused": {"status": "pass", "detail": "only one"}},
        )
        _check(code == "completion_evidence_incomplete", "F3 missing required gate is rejected")

    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        invalid_contract = ""
        try:
            _prepare(
                state,
                Path(raw),
                completion_contract={"required_gates": ["focused"]},
            )
        except DispatchError as exc:
            invalid_contract = exc.code
        _check(
            invalid_contract == "completion_contract_invalid",
            "F3 malformed completion contract is rejected at prepare",
        )

    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        row = _prepare(state, Path(raw))
        _first_turn(state, delivered=True)
        _ack(state)
        report = Path(row["expected_path"])
        report.write_text("complete worker evidence\n", encoding="utf-8")
        digest = hashlib.sha256(report.read_bytes()).hexdigest()
        _report_completion(
            state,
            event_id="evt-complete-worker-evidence",
            prior_version=2,
            artifact_path=str(report),
            artifact_sha256=digest,
            contract_sha256=row["completion_contract_sha256"],
        )
        reported = read_managed_dispatch(state, "mdp-fixture")
        worker_evidence = reported["reported_completion_evidence"]
        worker_digest = hashlib.sha256(
            json.dumps(
                worker_evidence,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        acceptance = ""
        try:
            resolve_managed_dispatch(
                state,
                dispatch_id="mdp-fixture",
                event_id="evt-incomplete-acceptance",
                action="accept_completion",
                actor=_coordinator_actor(),
                prior_version=int(reported["version"]),
                payload={
                    "artifact_sha256": digest,
                    "acceptance_evidence": {
                        "evidence": {"focused": {"status": "pass"}},
                        "verdict": "READY-FOR-REVIEW",
                        "worker_evidence_sha256": worker_digest,
                    },
                },
                observed_at=T0 + timedelta(minutes=3),
            )
        except DispatchError as exc:
            acceptance = exc.code
        _check(
            acceptance == "acceptance_evidence_incomplete",
            "F3 independent acceptance must satisfy every evidence obligation",
        )


def test_review_f4_malformed_blocker_is_rejected_before_supervision() -> None:
    """F4 red: invalid deadline/full payload never becomes durable blocker state."""
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(state, Path(raw))
        _first_turn(state, delivered=True)
        _ack(state)
        code = ""
        try:
            report_managed_dispatch(
                state,
                dispatch_id="mdp-fixture",
                event_id="evt-bad-blocker",
                event_kind="blocked",
                attempt_agent_instance_id="agi-worker-1",
                actor=_worker_actor(),
                prior_version=2,
                payload={
                    "blocker_class": "internal",
                    "blocker_owner": "Coordinator-Main",
                    "question": "Which safe repair path should proceed?",
                    "safe_options": ["repair", "cancel"],
                    "evidence": {"fixture": "invalid-deadline"},
                    "decision_due_at": "not-a-date",
                },
                observed_at=T0 + timedelta(minutes=1),
            )
        except DispatchError as exc:
            code = exc.code
        _check(code == "decision_due_at_invalid", "F4 malformed blocker is rejected")

    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(state, Path(raw))
        _prepare(
            state,
            Path(raw),
            dispatch_id="mdp-good-row",
            uptake_due_at=(T0 + timedelta(seconds=10)).isoformat(),
        )
        rows = cast(RealShapeState, state).rows(
            AGENT_ROLE_BINDING_NAMESPACE,
            TABLE_MANAGED_DISPATCH,
        )
        malformed = next(row for row in rows if row["dispatch_id"] == "mdp-fixture")
        malformed["state"] = DISPATCH_BLOCKED_INTERNAL
        malformed["decision_due_at"] = "not-a-date"
        result = supervise_managed_dispatches(state, now=T0 + timedelta(minutes=1))
        _check(len(result["malformed_rows"]) == 1, "F4 malformed persisted row is isolated")
        _check(
            any(item["dispatch_id"] == "mdp-good-row" for item in result["conditions"]),
            "F4 a malformed row cannot abort later owed rows",
        )


def test_review_f5_watchdog_is_operational_and_deduplicated() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(state, Path(raw), watchdog_due_at=(T0 + timedelta(minutes=2)).isoformat())
        _first_turn(state, delivered=True)
        _ack(state)
        first = supervise_managed_dispatches(state, now=T0 + timedelta(minutes=3))
        second = supervise_managed_dispatches(state, now=T0 + timedelta(minutes=4))
        first_names = [item["condition"] for item in first["conditions"]]
        _check("watchdog_overdue" in first_names, "F5 overdue watchdog emits a condition")
        _check(second["notices_emitted"] == 0, "F5 watchdog notice is deduplicated")


def test_review_f6_expired_retry_gets_fresh_bounded_deadlines() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(state, Path(raw))
        expired = supervise_managed_dispatches(state, now=T0 + timedelta(hours=5))
        row = read_managed_dispatch(state, "mdp-fixture")
        retried = resolve_managed_dispatch(
            state,
            dispatch_id="mdp-fixture",
            event_id="evt-fresh-retry",
            action="request_retry",
            actor=_coordinator_actor(),
            prior_version=int(row["version"]),
            payload={"reason": "retry with immutable windows"},
            observed_at=T0 + timedelta(hours=5, seconds=1),
        )
        after = supervise_managed_dispatches(state, now=T0 + timedelta(hours=5, seconds=2))
        _check(bool(expired["conditions"]), "F6 fixture reaches expiry")
        _check(
            datetime.fromisoformat(str(retried["expires_at"])) > T0 + timedelta(hours=5),
            "F6 retry refreshes TTL",
        )
        _check(
            not any(item["condition"] == "ttl_expired" for item in after["conditions"]),
            "F6 replacement is not immediately re-expired",
        )


def test_review_f7_failed_start_is_identified_supervised_and_replaced() -> None:
    """F7/F6: a failed start has an address and retry creates a working attempt."""
    host = "repair-round-two-replacement"
    key = (session_hosts.AGENT_RUNTIME_CODEX, host)
    prior = session_hosts._REGISTRY.get(key)  # noqa: SLF001
    driver = _ReplacementDriver(fail_start=True)
    session_hosts._REGISTRY[key] = driver  # noqa: SLF001
    try:
        with tempfile.TemporaryDirectory() as raw:
            state = _state()
            tmp = Path(raw)
            spec = _spec(
                tmp,
                host=host,
                allowed_hosts=[host],
                local_name="Replacement-Fixture",
            )
            error_data: dict[str, Any] = {}
            try:
                dispatch_managed_work(
                    state,
                    spec,
                    _spawn_request(spec),
                    now=T0,
                )
            except DispatchError as exc:
                error_data = exc.data
            failed = read_managed_dispatch(state, spec.dispatch_id)
            first_notice = supervise_managed_dispatches(
                state,
                now=T0 + timedelta(seconds=1),
            )
            second_notice = supervise_managed_dispatches(
                state,
                now=T0 + timedelta(seconds=2),
            )
            _check(
                error_data.get("dispatch_id") == spec.dispatch_id
                and error_data.get("state") == DISPATCH_FAILED_START,
                "F7 failed-start error returns durable dispatch identity/state",
            )
            _check(
                first_notice["conditions"][0]["condition"]
                == "failed_start_decision_required",
                "F7 failed start is supervised with a coordinator decision",
            )
            _check(
                first_notice["notices_emitted"] == 1
                and second_notice["notices_emitted"] == 0,
                "F7 failed-start decision notice is deduplicated",
            )

            driver.fail_start = False
            retried = resolve_managed_dispatch(
                state,
                dispatch_id=spec.dispatch_id,
                event_id="evt-working-replacement",
                action="request_retry",
                actor=_coordinator_actor(),
                prior_version=int(failed["version"]),
                payload={"reason": "configured replacement host"},
                observed_at=T0 + timedelta(seconds=3),
            )
            replacement = str(retried["current_agent_instance_id"])
            _seed_binding(
                state,
                role=spec.role_name,
                instance=replacement,
                session=f"ases-{replacement}",
            )
            active = report_managed_dispatch(
                state,
                dispatch_id=spec.dispatch_id,
                event_id="evt-replacement-ack",
                event_kind="ack",
                attempt_agent_instance_id=replacement,
                actor=DispatchActor(
                    replacement,
                    f"ases-{replacement}",
                    "live_peer_binding",
                ),
                prior_version=int(retried["version"]),
                payload={
                    "brief_sha256": spec.brief_sha256,
                    "role_binding": spec.role_name,
                    "scope_readback_sha256": "a" * 64,
                    "plan_sha256": "b" * 64,
                },
                observed_at=T0 + timedelta(seconds=4),
            )
            _check(
                bool(replacement)
                and replacement in driver.spawned_instances
                and retried["state"] == DISPATCH_UPTAKE_PENDING,
                "F6 retry creates and supervises an actual replacement attempt",
            )
            _check(
                bool(driver.spawn_specs)
                and driver.spawn_specs[-1].get("unit_id") == spec.unit_id,
                "unit_id survives the persisted dispatch replay into the replacement host spec",
            )
            _check(
                active["state"] == DISPATCH_ACTIVE
                and datetime.fromisoformat(str(active["expires_at"]))
                > T0 + timedelta(hours=4),
                "F6 replacement reaches fresh ACK inside the renewed lifecycle",
            )
    finally:
        if prior is None:
            session_hosts._REGISTRY.pop(key, None)  # noqa: SLF001
        else:
            session_hosts._REGISTRY[key] = prior  # noqa: SLF001


def test_review_fixture_15_competing_event_orders_converge() -> None:
    """Fixture 15: host death wins against blocker/completion in either order."""
    outcomes: list[bool] = []
    for suffix, first_kind in (
        ("death-first", "death"),
        ("completion-first", "completion"),
        ("blocker-first", "blocked"),
    ):
        with tempfile.TemporaryDirectory() as raw:
            state = _state()
            tmp = Path(raw)
            row = _prepare(state, tmp)
            _first_turn(state, delivered=True)
            active = _ack(state)
            report = Path(row["expected_path"])
            report.write_text(f"{suffix}\n", encoding="utf-8")
            completion = {
                "artifact_path": str(report),
                "artifact_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                "completion_contract_sha256": row["completion_contract_sha256"],
                "evidence": {
                    "focused": {"status": "pass"},
                    "registered": {"status": "skipped", "reason": "held"},
                },
                "verdict": "READY-FOR-REVIEW",
            }
            blocker = {
                "blocker_class": "internal",
                "blocker_owner": "Coordinator-Main",
                "question": "Choose the safe ordering response?",
                "safe_options": ["retry", "cancel"],
                "evidence": {"race": suffix},
                "decision_due_at": (T0 + timedelta(minutes=5)).isoformat(),
            }
            if first_kind == "death":
                mark_dispatch_worker_lost(
                    state,
                    dispatch_id="mdp-fixture",
                    agent_instance_id="agi-worker-1",
                    observed_at=T0 + timedelta(seconds=3),
                    detail="confirmed death won",
                )
                late_kind, late_payload = "completion", completion
                late_version = int(active["version"])
            else:
                event_payload = completion if first_kind == "completion" else blocker
                won = report_managed_dispatch(
                    state,
                    dispatch_id="mdp-fixture",
                    event_id=f"evt-{suffix}-winner",
                    event_kind=first_kind,
                    attempt_agent_instance_id="agi-worker-1",
                    actor=_worker_actor(),
                    prior_version=int(active["version"]),
                    payload=event_payload,
                    observed_at=T0 + timedelta(seconds=3),
                )
                mark_dispatch_worker_lost(
                    state,
                    dispatch_id="mdp-fixture",
                    agent_instance_id="agi-worker-1",
                    observed_at=T0 + timedelta(seconds=4),
                    detail=f"confirmed death after {first_kind}",
                )
                late_kind = "blocked" if first_kind == "completion" else "completion"
                late_payload = blocker if late_kind == "blocked" else completion
                late_version = int(won["version"])
            rejected = ""
            try:
                report_managed_dispatch(
                    state,
                    dispatch_id="mdp-fixture",
                    event_id=f"evt-{suffix}-late",
                    event_kind=late_kind,
                    attempt_agent_instance_id="agi-worker-1",
                    actor=_worker_actor(),
                    prior_version=late_version,
                    payload=late_payload,
                    observed_at=T0 + timedelta(seconds=5),
                )
            except DispatchError as exc:
                rejected = exc.code
            final = read_managed_dispatch(state, "mdp-fixture")
            outcomes.append(
                final["state"] == "worker_lost"
                and rejected
                in {
                    "stale_dispatch_version",
                    "completion_not_allowed",
                    "blocker_not_allowed",
                }
            )
    _check(all(outcomes), "15 ACK/blocker/death/late-completion order matrix converges")


def _replay_late_worker_rejection(
    state: StateManagementInterface,
    *,
    event_id: str,
    event_kind: str,
    payload: dict[str, Any],
    prior_version: int,
    first_observed_second: int,
) -> list[str]:
    codes: list[str] = []
    for offset in (0, 1):
        try:
            report_managed_dispatch(
                state,
                dispatch_id="mdp-fixture",
                event_id=event_id,
                event_kind=event_kind,
                attempt_agent_instance_id="agi-worker-1",
                actor=_worker_actor(),
                prior_version=prior_version,
                payload=payload,
                observed_at=T0 + timedelta(seconds=first_observed_second + offset),
            )
        except DispatchError as exc:
            codes.append(exc.code)
    return codes


def _replay_late_acceptance_rejection(
    state: StateManagementInterface,
    *,
    prior_version: int,
) -> list[str]:
    codes: list[str] = []
    for observed_second in (6, 7):
        try:
            resolve_managed_dispatch(
                state,
                dispatch_id="mdp-fixture",
                event_id="evt-late-current-acceptance",
                action="accept_completion",
                actor=_coordinator_actor(),
                prior_version=prior_version,
                payload={},
                observed_at=T0 + timedelta(seconds=observed_second),
            )
        except DispatchError as exc:
            codes.append(exc.code)
    return codes


def test_review_fixture_15_current_version_rejections_are_audited_and_replayed() -> None:
    """Fixture 15: terminal race losers retain their first durable outcome."""
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        row = _prepare(state, Path(raw))
        _first_turn(state, delivered=True)
        active = _ack(state)
        report = Path(row["expected_path"])
        report.write_text("late completion\n", encoding="utf-8")
        completion = {
            "artifact_path": str(report),
            "artifact_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
            "completion_contract_sha256": row["completion_contract_sha256"],
            "evidence": {
                "focused": {"status": "pass"},
                "registered": {"status": "skipped", "reason": "held"},
            },
            "verdict": "READY-FOR-REVIEW",
        }
        blocker = {
            "blocker_class": "internal",
            "blocker_owner": "Coordinator-Main",
            "question": "Choose the safe ordering response?",
            "safe_options": ["retry", "cancel"],
            "evidence": {"race": "current-version"},
            "decision_due_at": (T0 + timedelta(minutes=5)).isoformat(),
        }
        lost = mark_dispatch_worker_lost(
            state,
            dispatch_id="mdp-fixture",
            agent_instance_id="agi-worker-1",
            observed_at=T0 + timedelta(seconds=3),
            detail="confirmed death won",
        )
        completion_codes = _replay_late_worker_rejection(
            state,
            event_id="evt-late-current-completion",
            event_kind="completion",
            payload=completion,
            prior_version=int(lost["version"]),
            first_observed_second=4,
        )
        blocker_codes = _replay_late_worker_rejection(
            state,
            event_id="evt-late-current-blocker",
            event_kind="blocked",
            payload=blocker,
            prior_version=int(lost["version"]),
            first_observed_second=4,
        )
        resolve_codes = _replay_late_acceptance_rejection(
            state,
            prior_version=int(lost["version"]),
        )

        replayed_ack = _ack(state)
        rejected = _dispatch_events(
            state,
            event_ids={
                "evt-late-current-completion",
                "evt-late-current-blocker",
                "evt-late-current-acceptance",
            },
        )
        _check(
            completion_codes == ["completion_not_allowed", "completion_not_allowed"]
            and blocker_codes == ["blocker_not_allowed", "blocker_not_allowed"],
            "15 current-version worker rejection replay preserves the original rejection",
        )
        _check(
            resolve_codes == ["acceptance_not_allowed", "acceptance_not_allowed"],
            "15 current-version coordinator rejection replay preserves the original rejection",
        )
        _check(
            len(rejected) == 3
            and all(not bool(event["accepted"]) for event in rejected)
            and {str(event["rejection_code"]) for event in rejected}
            == {"completion_not_allowed", "blocker_not_allowed", "acceptance_not_allowed"},
            "15 each authenticated terminal rejection has exactly one durable audit row",
        )
        _check(
            replayed_ack["state"] == DISPATCH_ACTIVE
            and replayed_ack["version"] == active["version"],
            "15 accepted event replay reproduces its accepted projection after later state change",
        )


def test_review_f8_spawn_root_is_in_discoverable_invocation_schema() -> None:
    metadata = AgentMessagingPlugin.spawn_session._platform_process_metadata  # type: ignore[attr-defined]  # noqa: SLF001
    schema = InvocationSchemaGenerator().generate(
        "plugin::agent_messaging_plugin::spawn_session",
        {name: parameter.to_dict() for name, parameter in metadata.parameters.items()},
    )
    arguments = cast(dict[str, Any], cast(dict[str, Any], schema["properties"])["arguments"])
    properties = cast(dict[str, Any], arguments["properties"])
    _check("local_name" in metadata.parameters, "F8 local_name is discoverable ParameterMetadata")
    _check("local_name" in properties, "F8 live invocation-schema construction carries local_name")
    _check(
        "repository_root" in metadata.parameters,
        "foreign lane repository_root is discoverable ParameterMetadata",
    )
    _check(
        "repository_root" in properties,
        "live invocation-schema construction carries repository_root",
    )
    _check(arguments["additionalProperties"] is False, "F8 invocation arguments stay closed")


def test_28_degraded_hooks_acknowledged_is_in_discoverable_schema() -> None:
    """§48.1/#28: ``degraded_hooks_acknowledged`` reached ``SpawnSessionRequest``
    (session_lifecycle_verbs.py) and ``DispatchSpec`` fine, but was absent
    from ``spawn_session``'s own declared ``ParameterMetadata`` -- exactly
    F8's ``local_name`` gap above, for a second field F8's own fix never
    covered. The generated invocation schema's ``additionalProperties: False``
    is what actually strips an undeclared argument before the verb body ever
    sees it (F8's own third assertion), so a field missing from
    ``metadata.parameters`` reproduces #28's ``host_cannot_spawn`` remedy
    that never changes behavior no matter what the caller passes."""
    metadata = AgentMessagingPlugin.spawn_session._platform_process_metadata  # type: ignore[attr-defined]  # noqa: SLF001
    schema = InvocationSchemaGenerator().generate(
        "plugin::agent_messaging_plugin::spawn_session",
        {name: parameter.to_dict() for name, parameter in metadata.parameters.items()},
    )
    arguments = cast(dict[str, Any], cast(dict[str, Any], schema["properties"])["arguments"])
    properties = cast(dict[str, Any], arguments["properties"])
    _check(
        "degraded_hooks_acknowledged" in metadata.parameters,
        "28 degraded_hooks_acknowledged is discoverable ParameterMetadata",
    )
    _check(
        "degraded_hooks_acknowledged" in properties,
        "28 live invocation-schema construction carries degraded_hooks_acknowledged",
    )
    _check(arguments["additionalProperties"] is False, "28 invocation arguments stay closed")


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        with RecordedLaneWorktreeFixture(Path(raw)) as fixture:
            test_02_headless_submission_failure_is_durable()
            test_03_transport_is_not_uptake()
            test_06_atomic_requirements_fail_before_dispatch()
            test_07_silent_internal_blocker_is_supervised_once_per_version()
            test_08_overdue_worker_and_ttl_surface_exact_action()
            test_08_staggered_milestone_survives_watchdog_version_change()
            test_09_reconnect_and_duplicate_ack_are_idempotent()
            test_10_retry_request_is_idempotent()
            test_11_supervisor_does_not_depend_on_coordinator_plan()
            test_12_completion_false_positives_remain_noncomplete()
            test_13_only_coordinator_acceptance_completes()
            test_15_causal_version_rejects_races_but_keeps_audit()
            test_review_f1_forged_worker_and_coordinator_authority_are_rejected()
            test_review_f1_public_authority_is_server_derived()
            test_review_f2_prepared_contract_rejects_every_altered_spawn_field()
            test_review_f3_incomplete_completion_evidence_is_rejected()
            test_review_f4_malformed_blocker_is_rejected_before_supervision()
            test_review_f5_watchdog_is_operational_and_deduplicated()
            test_review_f6_expired_retry_gets_fresh_bounded_deadlines()
            test_review_f7_failed_start_is_identified_supervised_and_replaced()
            test_review_f8_spawn_root_is_in_discoverable_invocation_schema()
            test_28_degraded_hooks_acknowledged_is_in_discoverable_schema()
            test_review_fixture_15_competing_event_orders_converge()
            test_review_fixture_15_current_version_rejections_are_audited_and_replayed()
            _check(
                fixture.has_recorded_provisioning(),
                "recorded dispatch fixture contains every provisioning target under its temp root",
            )
    print(f"\nmanaged dispatch smoke: {_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
