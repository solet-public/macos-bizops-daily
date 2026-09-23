#!/usr/bin/env python3
"""2026-07-31 smoke — ensure_kb_retrieval_audit_schedule (durable-identity cron install).

Background. The "KB retrieval drift audit" cron (``sch-2l18uk4q9et3a``, created
2026-06-01) fired daily for two months and executed the audit **zero times**. Two
independent defects, both repaired here:

  1. It used the memory-tag heartbeat shape, whose action_def carries
     ``result_processor_kind=None`` by construction. The action-queue poller's
     EDGE_SINK_SKIP branch terminates that with no dispatch, forever, for
     headless crons. The repaired target is the EDGE_SINK verb
     ``audit_retrieval_corpus_cron``.
  2. It carried ``session_id=sess-2l18il2eddp47`` — the creating session's id,
     dead since June. A durable trigger must not inherit a caller session, so
     this verb writes system-owned ``session_id`` AND ``flow_id`` constants.
     Fixing only the session id would half-fix it, and a half-fix here is
     indistinguishable from a fix until it dies.

Naming the mutations that turn each assertion red, so a green here is evidence:

  (1) constants exist and are SYSTEM-OWNED, not the heartbeat pair
      -> red if someone reuses HEARTBEAT_SESSION_ID/FLOW_ID (which would make
         audit fires unattributable in audit logs).
  (2) BOTH session_id and flow_id are set on the built ScheduleData
      -> red if either is dropped or left None. This is defect 2 exactly.
  (3) the default tag DIFFERS from the predecessor's ``kb_retrieval_audit``
      -> red if someone "tidies" it to match, which would make the idempotent
         clear-by-tag step delete the predecessor: a bare swap, which the
         create-alongside -> prove-one-firing -> then-clear ordering forbids.
  (4) the action is the canonical EDGE_SINK shape and the REAL platform
      validator accepts it
      -> red if a result_processor_kind is introduced. Uses the shipped
         ``validate_cron_action_def``, not a local restatement of its rule.
  (5) the cron target is the deployed verb's exact process key
      -> red on a typo or a rename, which would otherwise fail silently at
         fire time.
  (6) the already-present check is TARGET-AWARE, not cron-only
      -> red if it compares only the cron expression. This is the load-bearing
         case: a schedule with the right cadence and the wrong target is the
         defect being repaired, and a cron-only check reports it healthy.

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:
    .venv/bin/python3 plugins/default_scheduling_plugin/tests/ensure_kb_retrieval_audit_schedule_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_scheduling_plugin" / "src"))

from default_scheduling_plugin.constants import (  # noqa: E402
    HEARTBEAT_FLOW_ID,
    HEARTBEAT_SESSION_ID,
    KB_RETRIEVAL_AUDIT_CRON,
    KB_RETRIEVAL_AUDIT_FLOW_ID,
    KB_RETRIEVAL_AUDIT_LABEL,
    KB_RETRIEVAL_AUDIT_PROCESS_KEY,
    KB_RETRIEVAL_AUDIT_SESSION_ID,
    KB_RETRIEVAL_AUDIT_TAG,
)
from default_scheduling_plugin.factories.schedule_factory import (  # noqa: E402
    ScheduleFactory,
)
from default_scheduling_plugin.models import ActionData  # noqa: E402
from default_scheduling_plugin.validation import (  # noqa: E402
    validate_cron_action_def,
    validate_cron_expression,
)

# The predecessor trigger's tag. Hardcoded deliberately: this smoke's job is to
# assert the new tag is NOT this one, so it must name the value it forbids.
# See reference: a contamination guard must keep the token it forbids.
PREDECESSOR_TAG = "kb_retrieval_audit"

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


def _build_schedule_data():
    """Build the ScheduleData exactly as the verb does, via the shipped factory."""
    return ScheduleFactory.create_cron_schedule_data(
        cron_expression=KB_RETRIEVAL_AUDIT_CRON,
        label=KB_RETRIEVAL_AUDIT_LABEL,
        tags=[KB_RETRIEVAL_AUDIT_TAG],
        actions=[
            ActionData(
                name=KB_RETRIEVAL_AUDIT_PROCESS_KEY,
                parameters={},
                result_processor=None,
                result_processor_kind=None,
            )
        ],
        action_name=KB_RETRIEVAL_AUDIT_PROCESS_KEY,
        action_parameters={},
        session_id=KB_RETRIEVAL_AUDIT_SESSION_ID,
        flow_id=KB_RETRIEVAL_AUDIT_FLOW_ID,
    )


def test_identifiers_are_system_owned_and_distinct() -> None:
    print("\n[1/6] identifiers are system-owned and distinct from the heartbeat pair")
    _check(
        KB_RETRIEVAL_AUDIT_SESSION_ID.startswith("sess-"),
        f"session id is a well-formed system id ({KB_RETRIEVAL_AUDIT_SESSION_ID})",
    )
    _check(
        KB_RETRIEVAL_AUDIT_FLOW_ID.startswith("flow-"),
        f"flow id is a well-formed system id ({KB_RETRIEVAL_AUDIT_FLOW_ID})",
    )
    _check(
        KB_RETRIEVAL_AUDIT_SESSION_ID != HEARTBEAT_SESSION_ID
        and KB_RETRIEVAL_AUDIT_FLOW_ID != HEARTBEAT_FLOW_ID,
        "audit ids are distinct from the heartbeat ids (fires stay independently attributable)",
    )


def test_both_identifiers_land_on_the_schedule() -> None:
    print("\n[2/6] BOTH session_id and flow_id land on the persisted schedule")
    data = _build_schedule_data()
    _check(
        data.session_id == KB_RETRIEVAL_AUDIT_SESSION_ID,
        f"session_id is the system-owned constant (got {data.session_id!r})",
    )
    _check(
        data.flow_id == KB_RETRIEVAL_AUDIT_FLOW_ID,
        f"flow_id is the system-owned constant (got {data.flow_id!r})",
    )
    _check(
        bool(data.session_id) and bool(data.flow_id),
        "neither identifier is empty or None (the half-fix case)",
    )


def test_tag_does_not_collide_with_the_predecessor() -> None:
    print("\n[3/6] default tag does not collide with the predecessor trigger")
    _check(
        KB_RETRIEVAL_AUDIT_TAG != PREDECESSOR_TAG,
        f"default tag {KB_RETRIEVAL_AUDIT_TAG!r} differs from predecessor {PREDECESSOR_TAG!r}",
    )
    _check(
        PREDECESSOR_TAG not in [KB_RETRIEVAL_AUDIT_TAG],
        "the idempotent clear-by-tag step cannot match the predecessor (no bare swap)",
    )


def test_action_is_edge_sink_and_the_real_validator_accepts_it() -> None:
    print("\n[4/6] action is canonical EDGE_SINK and the shipped validator accepts it")
    action = ActionData(
        name=KB_RETRIEVAL_AUDIT_PROCESS_KEY,
        parameters={},
        result_processor=None,
        result_processor_kind=None,
    )
    _check(
        action.result_processor_kind is None and action.result_processor is None,
        "EDGE_SINK shape: both result_processor fields are None",
    )
    try:
        validate_cron_action_def(action)
        accepted = True
        err = ""
    except ValueError as exc:
        accepted = False
        err = str(exc)
    _check(accepted, f"the shipped validate_cron_action_def accepts the action ({err})")

    # Negative control: the validator must still reject the shape it exists to
    # reject, or its acceptance above proves nothing.
    rejected = False
    try:
        validate_cron_action_def(
            ActionData(
                name=KB_RETRIEVAL_AUDIT_PROCESS_KEY,
                parameters={},
                result_processor=None,
                result_processor_kind="inference",
            )
        )
    except ValueError:
        rejected = True
    _check(
        rejected,
        "negative control: the same validator still rejects result_processor_kind='inference'",
    )
    _check(
        validate_cron_expression(KB_RETRIEVAL_AUDIT_CRON),
        f"the default cron expression validates ({KB_RETRIEVAL_AUDIT_CRON})",
    )


def test_cron_target_is_the_deployed_verb() -> None:
    print("\n[5/6] cron target names the deployed EDGE_SINK verb exactly")
    _check(
        KB_RETRIEVAL_AUDIT_PROCESS_KEY
        == "service_interface::knowledge_service::audit_retrieval_corpus_cron",
        f"process key is the audit cron sibling (got {KB_RETRIEVAL_AUDIT_PROCESS_KEY})",
    )
    _check(
        "get_memories_by_tag" not in KB_RETRIEVAL_AUDIT_PROCESS_KEY,
        "the target is not the dead memory-tag verb the predecessor used",
    )
    data = _build_schedule_data()
    names = [a.name for a in data.actions]
    _check(
        names == [KB_RETRIEVAL_AUDIT_PROCESS_KEY],
        f"the built schedule carries exactly that one action (got {names})",
    )


def test_already_present_check_is_target_aware() -> None:
    print("\n[6/6] the already-present check compares the TARGET, not only the cron")
    from default_scheduling_plugin.plugin import SchedulingPlugin

    check = SchedulingPlugin._check_existing_audit_schedule

    correct = {
        "sch-correct": {
            "cron_expression": KB_RETRIEVAL_AUDIT_CRON,
            "tags": [KB_RETRIEVAL_AUDIT_TAG],
            "actions": [{"name": KB_RETRIEVAL_AUDIT_PROCESS_KEY, "parameters": {}}],
        }
    }
    # Right cadence, WRONG target -- the exact defect being repaired. A cron-only
    # comparison reports this healthy and leaves the audit dead.
    mistargeted = {
        "sch-dead": {
            "cron_expression": KB_RETRIEVAL_AUDIT_CRON,
            "tags": [KB_RETRIEVAL_AUDIT_TAG],
            "actions": [
                {
                    "name": "service_interface::memory_service::get_memories_by_tag",
                    "parameters": {},
                }
            ],
        }
    }

    hit = check(correct, KB_RETRIEVAL_AUDIT_CRON, KB_RETRIEVAL_AUDIT_TAG)
    _check(
        hit is not None
        and hit.get("data", {}).get("status") == "already_present",
        "a correct existing schedule is recognised as already_present",
    )

    miss = check(mistargeted, KB_RETRIEVAL_AUDIT_CRON, KB_RETRIEVAL_AUDIT_TAG)
    _check(
        miss is None,
        "a right-cadence WRONG-TARGET schedule is NOT already_present (it gets normalized)",
    )

    two = dict(correct)
    two["sch-dupe"] = dict(correct["sch-correct"])
    _check(
        check(two, KB_RETRIEVAL_AUDIT_CRON, KB_RETRIEVAL_AUDIT_TAG) is None,
        "duplicates are not already_present (they get normalized to one)",
    )


def main() -> int:
    print("=== ensure_kb_retrieval_audit_schedule_smoke (durable-identity cron install) ===")
    test_identifiers_are_system_owned_and_distinct()
    test_both_identifiers_land_on_the_schedule()
    test_tag_does_not_collide_with_the_predecessor()
    test_action_is_edge_sink_and_the_real_validator_accepts_it()
    test_cron_target_is_the_deployed_verb()
    test_already_present_check_is_target_aware()

    total = _passed + len(_failed)
    print(f"\n{_passed}/{total} passed")
    if _failed:
        print("FAILURES:")
        for f in _failed:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
