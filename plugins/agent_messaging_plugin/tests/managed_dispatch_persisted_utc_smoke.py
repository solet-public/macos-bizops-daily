#!/usr/bin/env python3
"""Regression smoke for naive-UTC managed-dispatch state-service readback."""

from __future__ import annotations

import sys
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from _recorded_lane_worktree_fixture import RecordedLaneWorktreeFixture  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from managed_dispatch_smoke import (  # noqa: E402
    T0,
    _ack,
    _coordinator_actor,
    _first_turn,
    _prepare,
    _seed_binding,
    _state,
    _worker_actor,
)

from agent_messaging_plugin.managed_dispatch import (  # noqa: E402
    DISPATCH_ACTIVE,
    DISPATCH_BLOCKED_INTERNAL,
    DispatchError,
    managed_dispatch_status,
    read_managed_dispatch,
    record_dispatch_liveness,
    report_managed_dispatch,
    resolve_managed_dispatch,
    supervise_managed_dispatches,
)
from agent_messaging_plugin.schema import TABLE_MANAGED_DISPATCH  # noqa: E402

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


class _NaiveUtcRoundtripState(RealShapeState):
    """Fake the state service returning a mix of naive datetime and ISO values."""

    _FIELDS = (
        "uptake_due_at",
        "report_by",
        "watchdog_due_at",
        "decision_due_at",
        "next_liveness_probe_at",
        "liveness_escalation_due_at",
    )

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        result = super().query_state(namespace, query)
        for record in result.get("data", {}).get("records", []):
            if str(query.get("table")) == TABLE_MANAGED_DISPATCH and isinstance(record, dict):
                self._roundtrip(record)
        return result

    @classmethod
    def _roundtrip(cls, record: dict[str, Any]) -> None:
        for index, field in enumerate(cls._FIELDS):
            raw = record.get(field)
            if not raw:
                continue
            try:
                parsed = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
            except ValueError:
                continue
            naive = parsed.astimezone(UTC).replace(tzinfo=None)
            record[field] = naive if index % 2 else naive.isoformat()


class _OptionalDatetimeBoundaryState(RealShapeState):
    """Reject empty strings at the typed managed-dispatch timestamp boundary."""

    _OPTIONAL_FIELDS = frozenset(
        {
            "blocked_at",
            "completion_accepted_at",
            "completion_reported_at",
            "decision_due_at",
            "first_turn_at",
            "host_liveness_observed_at",
            "last_ack_at",
            "last_milestone_at",
            "last_reconciled_at",
            "liveness_escalation_due_at",
            "next_liveness_probe_at",
            "watchdog_fired_at",
        }
    )

    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any]
    ) -> dict[str, Any]:
        if str(query.get("table")) == TABLE_MANAGED_DISPATCH and any(
            updates.get(field) == "" for field in self._OPTIONAL_FIELDS
        ):
            return {
                "action_status": "failed",
                "error": {"message": "invalid input syntax for type timestamp: empty string"},
                "data": {},
            }
        return super().update_state(namespace, query, updates)


def _naive_state() -> Any:
    state = _NaiveUtcRoundtripState()
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


def _datetime_boundary_state() -> Any:
    state = _OptionalDatetimeBoundaryState()
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


def _active_naive(tmp: Path) -> tuple[Any, dict[str, Any]]:
    state = _naive_state()
    _prepare(state, tmp)
    _first_turn(state, delivered=True)
    return state, _ack(state)


def test_status_milestone_and_blocker() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state, active = _active_naive(Path(raw))
        expiry = datetime.fromisoformat(str(active["report_by"]))
        before = managed_dispatch_status(state, "mdp-fixture", now=expiry - timedelta(seconds=1))
        at_utc = managed_dispatch_status(state, "mdp-fixture", now=expiry)
        at_local = managed_dispatch_status(
            state, "mdp-fixture", now=expiry.astimezone(timezone(timedelta(hours=-7)))
        )
        after = managed_dispatch_status(state, "mdp-fixture", now=expiry + timedelta(seconds=1))
        _check(
            not before["deadline_overdue"]["report_by"]
            and at_utc["deadline_overdue"]["report_by"]
            and after["deadline_overdue"]["report_by"]
            and at_utc["deadline_overdue"] == at_local["deadline_overdue"],
            "naive persisted UTC status is identical before/at/after the report deadline and across zones",
        )
        milestone = report_managed_dispatch(
            state,
            dispatch_id="mdp-fixture",
            event_id="evt-persisted-utc-milestone",
            event_kind="milestone",
            attempt_agent_instance_id="agi-worker-1",
            actor=_worker_actor(),
            prior_version=int(active["version"]),
            payload={
                "completed_work": ["decode persisted UTC"],
                "current_evidence": {"focused": "green"},
                "next_action": "continue",
                "next_report_deadline": (T0 + timedelta(minutes=20)).isoformat(),
            },
            observed_at=T0 + timedelta(minutes=3),
        )
        blocked = report_managed_dispatch(
            state,
            dispatch_id="mdp-fixture",
            event_id="evt-persisted-utc-blocker",
            event_kind="blocked",
            attempt_agent_instance_id="agi-worker-1",
            actor=_worker_actor(),
            prior_version=int(milestone["version"]),
            payload={
                "blocker_class": "internal",
                "blocker_owner": "Coordinator-Main",
                "question": "Which repair path proceeds?",
                "safe_options": ["repair", "cancel"],
                "evidence": {"storage": "naive UTC"},
                "decision_due_at": (T0 + timedelta(minutes=5)).isoformat(),
            },
            observed_at=T0 + timedelta(minutes=3),
        )
        _check(
            milestone["state"] == DISPATCH_ACTIVE and blocked["state"] == DISPATCH_BLOCKED_INTERNAL,
            "worker milestone and internal blocker compare external aware dates to persisted naive UTC",
        )


def test_retry_and_supervision() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _naive_state()
        _prepare(state, Path(raw))
        _first_turn(state, delivered=False, error="fixture unavailable")
        expired = supervise_managed_dispatches(state, now=T0 + timedelta(hours=5))
        retried = resolve_managed_dispatch(
            state,
            dispatch_id="mdp-fixture",
            event_id="evt-persisted-utc-retry",
            action="request_retry",
            actor=_coordinator_actor(),
            prior_version=int(read_managed_dispatch(state, "mdp-fixture")["version"]),
            payload={"reason": "stored UTC retry"},
            observed_at=T0 + timedelta(hours=5, seconds=1),
        )
        after_retry = supervise_managed_dispatches(state, now=T0 + timedelta(hours=5, seconds=2))
        _check(
            bool(expired["conditions"])
            and datetime.fromisoformat(str(retried["report_by"])) > T0 + timedelta(hours=5)
            and not any(item["condition"] == "ttl_expired" for item in after_retry["conditions"]),
            "legal retry refreshes persisted naive UTC deadlines",
        )

    with tempfile.TemporaryDirectory() as raw:
        state = _naive_state()
        _prepare(state, Path(raw))
        record_dispatch_liveness(
            state,
            dispatch_id="mdp-fixture",
            liveness="unknown",
            observed_at=T0,
            detail="fixture probe unavailable",
        )
        first = supervise_managed_dispatches(state, now=T0 + timedelta(seconds=70))
        second = supervise_managed_dispatches(state, now=T0 + timedelta(seconds=71))
        _check(
            first["notices_emitted"] == 1 and second["notices_emitted"] == 0,
            "naive persisted liveness deadlines supervise and deduplicate notices",
        )


def test_optional_timestamp_clears_use_database_nulls() -> None:
    """Optional DATETIME resets reach the state interface as ``None``, never ``""``."""
    with tempfile.TemporaryDirectory() as raw:
        state = _datetime_boundary_state()
        _prepare(state, Path(raw))
        _first_turn(state, delivered=True)
        active = _ack(state)
        blocked = report_managed_dispatch(
            state,
            dispatch_id="mdp-fixture",
            event_id="evt-null-boundary-blocker",
            event_kind="blocked",
            attempt_agent_instance_id="agi-worker-1",
            actor=_worker_actor(),
            prior_version=int(active["version"]),
            payload={
                "blocker_class": "internal",
                "blocker_owner": "Coordinator-Main",
                "question": "Which repair path proceeds?",
                "safe_options": ["repair", "cancel"],
                "evidence": {"storage": "typed boundary"},
                "decision_due_at": (T0 + timedelta(minutes=5)).isoformat(),
            },
            observed_at=T0 + timedelta(minutes=3),
        )
        resolved = resolve_managed_dispatch(
            state,
            dispatch_id="mdp-fixture",
            event_id="evt-null-boundary-resolve",
            action="resolve_blocker",
            actor=_coordinator_actor(),
            prior_version=int(blocked["version"]),
            payload={"reason": "typed null clear"},
            observed_at=T0 + timedelta(minutes=4),
        )
        _check(
            resolved["state"] == DISPATCH_ACTIVE and resolved["decision_due_at"] is None,
            "blocker resolution clears optional decision deadline with database null",
        )

    with tempfile.TemporaryDirectory() as raw:
        state = _datetime_boundary_state()
        _prepare(state, Path(raw))
        _first_turn(state, delivered=False, error="fixture unavailable")
        supervise_managed_dispatches(state, now=T0 + timedelta(hours=5))
        retried = resolve_managed_dispatch(
            state,
            dispatch_id="mdp-fixture",
            event_id="evt-null-boundary-retry",
            action="request_retry",
            actor=_coordinator_actor(),
            prior_version=int(read_managed_dispatch(state, "mdp-fixture")["version"]),
            payload={"reason": "typed null reset"},
            observed_at=T0 + timedelta(hours=5, seconds=1),
        )
        _check(
            all(
                retried[field] is None
                for field in (
                    "blocked_at",
                    "completion_accepted_at",
                    "completion_reported_at",
                    "decision_due_at",
                    "host_liveness_observed_at",
                    "last_ack_at",
                    "last_milestone_at",
                    "liveness_escalation_due_at",
                    "next_liveness_probe_at",
                    "watchdog_fired_at",
                )
            ),
            "retry clears every optional managed-dispatch timestamp with database null",
        )

    with tempfile.TemporaryDirectory() as raw:
        state = _datetime_boundary_state()
        _prepare(state, Path(raw))
        record_dispatch_liveness(
            state,
            dispatch_id="mdp-fixture",
            liveness="unknown",
            observed_at=T0,
            detail="fixture probe unavailable",
        )
        reset = record_dispatch_liveness(
            state,
            dispatch_id="mdp-fixture",
            liveness="alive",
            observed_at=T0 + timedelta(seconds=1),
            detail="fixture probe healthy",
        )
        _check(
            reset["next_liveness_probe_at"] is None
            and reset["liveness_escalation_due_at"] is None,
            "liveness reset clears optional timestamps with database null",
        )


def test_malformed_and_public_inputs_fail_loudly() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _naive_state()
        _prepare(state, Path(raw))
        cast(RealShapeState, state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH)[0][
            "uptake_due_at"
        ] = "not-a-timestamp"
        status_code = ""
        try:
            managed_dispatch_status(state, "mdp-fixture", now=T0)
        except DispatchError as exc:
            status_code = exc.code
        _check(
            status_code == "uptake_due_at_invalid"
            and len(supervise_managed_dispatches(state, now=T0)["malformed_rows"]) == 1,
            "malformed persisted timestamp is loud and never healthy",
        )

    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        spec_code = event_code = ""
        try:
            _prepare(state, Path(raw), report_by=(T0 + timedelta(hours=4)).replace(tzinfo=None).isoformat())
        except DispatchError as exc:
            spec_code = exc.code
        _prepare(state, Path(raw))
        _first_turn(state, delivered=True)
        active = _ack(state)
        try:
            report_managed_dispatch(
                state,
                dispatch_id="mdp-fixture",
                event_id="evt-persisted-utc-naive-event",
                event_kind="milestone",
                attempt_agent_instance_id="agi-worker-1",
                actor=_worker_actor(),
                prior_version=int(active["version"]),
                payload={
                    "completed_work": ["strict input"],
                    "current_evidence": {"fixture": "naive"},
                    "next_action": "reject",
                    "next_report_deadline": "2026-08-22T12:20:00",
                },
                observed_at=T0 + timedelta(minutes=3),
            )
        except DispatchError as exc:
            event_code = exc.code
        _check(
            spec_code == "report_by_invalid" and event_code == "next_report_deadline_invalid",
            "naive public spec and event deadlines remain rejected",
        )


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        with RecordedLaneWorktreeFixture(Path(raw)):
            test_status_milestone_and_blocker()
            test_retry_and_supervision()
            test_optional_timestamp_clears_use_database_nulls()
            test_malformed_and_public_inputs_fail_loudly()
    print(f"\npersisted UTC dispatch smoke: {_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
