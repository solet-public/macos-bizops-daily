#!/usr/bin/env python3
"""F1 legacy compatibility and F2 logical lifetime-removal regressions."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
)
from managed_dispatch_smoke import (  # noqa: E402
    T0,
    _ack,
    _coordinator_actor,
    _first_turn,
    _prepare,
    _state,
    _worker_actor,
)

from agent_messaging_plugin.managed_dispatch import (  # noqa: E402
    DISPATCH_ACTIVE,
    DISPATCH_BLOCKED_INTERNAL,
    DISPATCH_COMPLETED,
    DISPATCH_EXPIRED,
    DispatchError,
    _canonical_sha256,
    _replay_event,
    _write_event,
    managed_dispatch_events,
    managed_dispatch_inventory,
    managed_dispatch_status,
    read_managed_dispatch,
    report_managed_dispatch,
    resolve_managed_dispatch,
    supervise_managed_dispatches,
)
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    TABLE_MANAGED_DISPATCH,
    get_managed_dispatch_schema,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    list_sessions,
    report_alive,
    session_status,
)

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


def test_schema_keeps_nullable_columns_but_writers_omit_values() -> None:
    schema = get_managed_dispatch_schema()
    legacy_columns = ("ttl_seconds", "expires_at", "expires_at_window_seconds")
    _check(
        all(not schema.columns[name].not_null for name in legacy_columns),
        "F1 schema declares all three legacy managed-dispatch TTL columns nullable",
    )
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        row = _prepare(state, Path(raw))
        persisted = cast(RealShapeState, state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH)[0]
        _check(
            all(name not in row and name not in persisted for name in legacy_columns),
            "F2 prepare writer omits every legacy TTL value",
        )


def test_null_ttl_row_is_readable_and_never_expires() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(state, Path(raw))
        _first_turn(state, delivered=True)
        active = _ack(state)
        persisted = cast(RealShapeState, state).rows(
            AGENT_ROLE_BINDING_NAMESPACE,
            TABLE_MANAGED_DISPATCH,
        )[0]
        for name in ("ttl_seconds", "expires_at", "expires_at_window_seconds"):
            persisted[name] = None

        milestone = report_managed_dispatch(
            state,
            dispatch_id="mdp-fixture",
            event_id="evt-null-ttl-milestone",
            event_kind="milestone",
            attempt_agent_instance_id="agi-worker-1",
            actor=_worker_actor(),
            prior_version=int(active["version"]),
            payload={
                "completed_work": ["nullable read path"],
                "current_evidence": {"legacy_ttl": None},
                "next_action": "exercise blocker path",
                "next_report_deadline": (T0 + timedelta(hours=5, minutes=15)).isoformat(),
            },
            observed_at=T0 + timedelta(hours=5),
        )
        blocked = report_managed_dispatch(
            state,
            dispatch_id="mdp-fixture",
            event_id="evt-null-ttl-blocker",
            event_kind="blocked",
            attempt_agent_instance_id="agi-worker-1",
            actor=_worker_actor(),
            prior_version=int(milestone["version"]),
            payload={
                "blocker_class": "internal",
                "blocker_owner": "Coordinator-Main",
                "question": "Confirm the bridge row remains readable?",
                "safe_options": ["resolve"],
                "evidence": {"legacy_ttl": None},
                "decision_due_at": (T0 + timedelta(hours=5, minutes=10)).isoformat(),
            },
            observed_at=T0 + timedelta(hours=5, minutes=1),
        )
        supervised = supervise_managed_dispatches(state, now=T0 + timedelta(days=30))
        status = managed_dispatch_status(state, "mdp-fixture", now=T0 + timedelta(days=30))
        _check(
            blocked["state"] == DISPATCH_BLOCKED_INTERNAL,
            "F1 milestone and blocker validators accept a missing legacy expiry",
        )
        _check(
            not any(item["condition"] == "ttl_expired" for item in supervised["conditions"]),
            "F1 supervisor never treats a missing legacy expiry as elapsed",
        )
        _check(
            "expires_at" not in status
            and "expires_at" not in status["deadline_overdue"]
            and status["state"] != DISPATCH_EXPIRED,
            "F2 status omits legacy expiry without inventing overdue or expiry",
        )


def test_dispatch_inventory_and_events_are_tie_safe_pages() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        for index in range(3):
            dispatch_id = f"mdp-page-{index}"
            _prepare(state, Path(raw), dispatch_id=dispatch_id)
            _write_event(
                state,
                dispatch_id=dispatch_id,
                event_id=f"evt-page-{index}",
                event_kind="fixture",
                attempt_agent_instance_id="",
                actor_role="fixture",
                actor_instance_id="",
                prior_version=0,
                observed_at=T0,
                payload={"index": index},
                accepted=True,
            )

        first_inventory = managed_dispatch_inventory(state, limit=2)
        inventory_cursor = cast(dict[str, str], first_inventory["next_cursor"])
        second_inventory = managed_dispatch_inventory(
            state,
            limit=2,
            after_created_at=inventory_cursor["created_at"],
            after_id=inventory_cursor["id"],
        )
        inventory_ids = [
            str(row["dispatch_id"])
            for row in first_inventory["dispatches"] + second_inventory["dispatches"]
        ]
        _check(
            len(inventory_ids) == 3 and len(set(inventory_ids)) == 3,
            "F1 inventory cursor enumerates same-timestamp rows without gaps or repeats",
        )
        _check(
            first_inventory["truncated"] is True
            and second_inventory["truncated"] is False
            and second_inventory["next_cursor"] is None,
            "F1 inventory paging exposes an explicit terminal page",
        )

        first_events = managed_dispatch_events(state, limit=2)
        event_cursor = cast(dict[str, str], first_events["next_cursor"])
        second_events = managed_dispatch_events(
            state,
            limit=2,
            after_event_at=event_cursor["event_at"],
            after_id=event_cursor["id"],
        )
        event_ids = [
            str(row["event_id"])
            for row in first_events["events"] + second_events["events"]
        ]
        _check(
            len(event_ids) == 3 and len(set(event_ids)) == 3,
            "F1 event cursor enumerates same-timestamp rows without gaps or repeats",
        )


def _legacy_cancel_payload() -> dict[str, object]:
    return {
        "reason": "legacy expiry has no remaining work",
        "evidence": {
            "attempt_disposition": "absent",
            "work_disposition": "none_to_preserve",
            "evidence_refs": ["fixture:no-current-attempt"],
        },
    }


def _rejected_legacy_correction_code(
    state: object,
    *,
    event_id: str,
    actor: object,
    prior_version: int,
    payload: dict[str, object],
) -> str:
    try:
        resolve_managed_dispatch(  # type: ignore[arg-type]
            state,
            dispatch_id="mdp-fixture",
            event_id=event_id,
            action="cancel_legacy_expiry",
            actor=actor,  # type: ignore[arg-type]
            prior_version=prior_version,
            payload=payload,
            observed_at=T0 + timedelta(hours=5),
        )
    except DispatchError as exc:
        return exc.code
    return ""


def test_legacy_corrections_are_causal_authorized_and_evidence_bound() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(state, Path(raw))
        cast(RealShapeState, state).rows(
            AGENT_ROLE_BINDING_NAMESPACE,
            TABLE_MANAGED_DISPATCH,
        )[0]["state"] = DISPATCH_EXPIRED
        payload = _legacy_cancel_payload()
        wrong_role = _rejected_legacy_correction_code(
            state,
            event_id="evt-legacy-cancel-wrong-role",
            actor=_worker_actor("agi-attacker"),
            prior_version=0,
            payload=payload,
        )
        stale = _rejected_legacy_correction_code(
            state,
            event_id="evt-legacy-cancel-stale",
            actor=_coordinator_actor(),
            prior_version=99,
            payload=payload,
        )
        missing_evidence = _rejected_legacy_correction_code(
            state,
            event_id="evt-legacy-cancel-missing-evidence",
            actor=_coordinator_actor(),
            prior_version=0,
            payload={"reason": "assertion alone is insufficient"},
        )
        cancelled = resolve_managed_dispatch(
            state,
            dispatch_id="mdp-fixture",
            event_id="evt-legacy-cancel-valid",
            action="cancel_legacy_expiry",
            actor=_coordinator_actor(),
            prior_version=0,
            payload=payload,
            observed_at=T0 + timedelta(hours=5),
        )
        _check(
            wrong_role == "coordinator_authority_denied"
            and stale == "stale_dispatch_version"
            and missing_evidence == "legacy_expiry_evidence_required",
            "F1 legacy correction rejects wrong-role, stale-version, and assertion-only calls",
        )
        _check(
            cancelled["state"] == "cancelled"
            and str(cancelled["terminal_reason"]).startswith("legacy_expiry_cancelled:"),
            "F1 evidence-bearing legacy cancellation reaches an explicit terminal state",
        )

    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        row = _prepare(state, Path(raw))
        cast(RealShapeState, state).rows(
            AGENT_ROLE_BINDING_NAMESPACE,
            TABLE_MANAGED_DISPATCH,
        )[0]["state"] = DISPATCH_EXPIRED
        artifact = Path(str(row["expected_path"]))
        artifact.write_text("verified legacy result\n", encoding="utf-8")
        worker_evidence = {
            "focused": {"status": "pass"},
            "registered": {"status": "skipped", "reason": "legacy receipt"},
        }
        completed = resolve_managed_dispatch(
            state,
            dispatch_id="mdp-fixture",
            event_id="evt-legacy-completion-valid",
            action="accept_verified_legacy_completion",
            actor=_coordinator_actor(),
            prior_version=0,
            payload={
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "completion_contract_sha256": row["completion_contract_sha256"],
                "worker_completion": {
                    "evidence": worker_evidence,
                    "verdict": "READY-FOR-REVIEW",
                },
                "acceptance_evidence": {
                    "evidence": worker_evidence,
                    "verdict": "READY-FOR-REVIEW",
                    "worker_evidence_sha256": _canonical_sha256(worker_evidence),
                },
            },
            observed_at=T0 + timedelta(hours=5),
        )
        _check(
            completed["state"] == DISPATCH_COMPLETED
            and completed["terminal_reason"] == "verified_legacy_completion",
            "F1 verified legacy completion binds artifact, contract, worker, and acceptance evidence",
        )


def test_paged_reads_are_discoverable_processes() -> None:
    for name in ("managed_dispatch_inventory", "managed_dispatch_events"):
        metadata = getattr(AgentMessagingPlugin, name)._platform_process_metadata  # type: ignore[attr-defined]  # noqa: SLF001
        _check(
            "limit" in metadata.parameters and "after_id" in metadata.parameters,
            f"F1 {name} publishes bounded cursor metadata",
        )


def test_retired_arguments_are_absent_and_direct_calls_fail_before_side_effects() -> None:
    plugin = object.__new__(AgentMessagingPlugin)
    for name in ("spawn_session", "dispatch_managed_work", "provision_role_session"):
        method = getattr(plugin, name)
        metadata = method._platform_process_metadata  # noqa: SLF001
        _check(
            not {"ttl_seconds", "expires_at"}.intersection(metadata.parameters),
            f"F2 {name} publishes no lifetime parameters",
        )
        for field in ("ttl_seconds", "expires_at"):
            for value in (None, 0, "", "invalid"):
                result = method({field: value}, {})
                _check(
                    result["error"]["code"] == "retired_session_ttl_argument",
                    f"F2 direct {name} rejects {field}={value!r} before looking up state",
                )


def test_populated_legacy_values_cannot_expire_work_or_constrain_reports() -> None:
    for legacy in (None, "", "not-a-timestamp", (T0 - timedelta(days=1)).isoformat()):
        with tempfile.TemporaryDirectory() as raw:
            state = _state()
            _prepare(state, Path(raw))
            _first_turn(state, delivered=True)
            active = _ack(state)
            persisted = cast(RealShapeState, state).rows(
                AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH,
            )[0]
            persisted.update(ttl_seconds=1, expires_at=legacy, expires_at_window_seconds=1)
            milestone = report_managed_dispatch(
                state, dispatch_id="mdp-fixture", event_id="f2-late-milestone",
                event_kind="milestone", attempt_agent_instance_id="agi-worker-1",
                actor=_worker_actor(), prior_version=int(active["version"]),
                payload={"completed_work": ["lifetime removal"],
                         "current_evidence": {"legacy": legacy},
                         "next_action": "review", "next_report_deadline":
                         (T0 + timedelta(days=10000)).isoformat()},
                observed_at=T0 + timedelta(days=9999),
            )
            supervised = supervise_managed_dispatches(state, now=T0 + timedelta(days=10001))
            status = managed_dispatch_status(state, "mdp-fixture", now=T0 + timedelta(days=10001))
            _check(milestone["state"] == status["state"] == DISPATCH_ACTIVE,
                   f"F2 legacy expiry {legacy!r} never terminates work or caps reporting")
            _check(not {"ttl_seconds", "expires_at", "expires_at_window_seconds"}.intersection(status),
                   "F2 current projection contains no legacy lifetime fields")
            _check(all(item["condition"] != "ttl_expired" for item in supervised["conditions"]),
                   "F2 lifetime never generates a supervisor condition")
            inventory = managed_dispatch_inventory(state)["dispatches"][0]
            _check(inventory["expires_at"] == legacy, "F2 legacy inventory preserves reconciliation evidence")


def test_session_and_dispatch_report_clocks_remain_independent() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(state, Path(raw))
        _first_turn(state, delivered=True)
        active = _ack(state)
        session = insert_managed_session(state, ManagedSessionSpec(
            agent_instance_id="agi-worker-1", lane_id="clock-proof", brief_ref=raw,
            work_class="production_mutation", budget_line="clock-proof", host="operator",
            dispatch_id="mdp-fixture", report_by_seconds=600,
        ))
        _check("expires_at" not in session, "F2 new session insert omits legacy expiry")
        report_alive(state, agent_instance_id="agi-worker-1", status="working", directed_by="fixture")
        session_deadline = read_managed_session(state, "agi-worker-1")["report_by"]
        dispatch = read_managed_dispatch(state, "mdp-fixture")
        _check(dispatch["report_by"] == active["report_by"] and dispatch["version"] == active["version"],
               "B2 session report_alive does not rearm or version the dispatch")
        status = managed_dispatch_status(state, "mdp-fixture", now=T0 + timedelta(hours=5))
        _check(status["deadline_overdue"]["report_by"], "B2 session heartbeat leaves dispatch overdue")
        report_managed_dispatch(
            state, dispatch_id="mdp-fixture", event_id="f2-clock-milestone", event_kind="milestone",
            attempt_agent_instance_id="agi-worker-1", actor=_worker_actor(),
            prior_version=int(dispatch["version"]),
            payload={"completed_work": ["clock proof"], "current_evidence": {"independent": True},
                     "next_action": "review", "next_report_deadline": (T0 + timedelta(hours=6)).isoformat()},
            observed_at=T0 + timedelta(hours=5),
        )
        _check(read_managed_session(state, "agi-worker-1")["report_by"] == session_deadline,
               "B2 causal milestone does not rearm the session report clock")
        _check(not managed_dispatch_status(state, "mdp-fixture", now=T0 + timedelta(hours=5))["deadline_overdue"]["report_by"],
               "B2 causal milestone independently clears dispatch report debt")
        persisted = cast(RealShapeState, state).rows(AGENT_ROLE_BINDING_NAMESPACE, "managed_session")[0]
        persisted["expires_at"] = "legacy-evidence"
        _check("expires_at" not in session_status(state, "agi-worker-1"), "F2 session status omits legacy expiry")
        _check("expires_at" not in list_sessions(state, live_only=True)["sessions"][0],
               "F2 current fleet roster omits legacy expiry")


def test_legacy_event_replay_hides_lifetime_without_rewriting_receipt() -> None:
    from agent_messaging_plugin.managed_dispatch import _ACCEPTED_OUTCOME_PAYLOAD_KEY  # noqa: PLC0415

    outcome = {"state": DISPATCH_ACTIVE, "ttl_seconds": 1, "expires_at": "legacy",
               "expires_at_window_seconds": 1, "version": 2}
    receipt = {"accepted": True, "payload": {_ACCEPTED_OUTCOME_PAYLOAD_KEY: outcome}}
    replayed = _replay_event(receipt)
    _check(replayed == {"state": DISPATCH_ACTIVE, "version": 2},
           "F2 historical replay omits retired lifetime from the current response")
    _check(outcome["expires_at"] == "legacy" and outcome["ttl_seconds"] == 1,
           "F2 historical accepted-event receipt remains unchanged")


def main() -> int:
    test_schema_keeps_nullable_columns_but_writers_omit_values()
    test_null_ttl_row_is_readable_and_never_expires()
    test_dispatch_inventory_and_events_are_tie_safe_pages()
    test_legacy_corrections_are_causal_authorized_and_evidence_bound()
    test_paged_reads_are_discoverable_processes()
    test_retired_arguments_are_absent_and_direct_calls_fail_before_side_effects()
    test_populated_legacy_values_cannot_expire_work_or_constrain_reports()
    test_session_and_dispatch_report_clocks_remain_independent()
    test_legacy_event_replay_hides_lifetime_without_rewriting_receipt()
    print(f"\nmanaged dispatch F1 bridge smoke: {_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
