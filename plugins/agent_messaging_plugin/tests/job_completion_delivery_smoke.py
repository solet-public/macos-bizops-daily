#!/usr/bin/env python3
"""Lane W — the push half: deliver_job_completion + the completion_route_role stamp.

Core decides WHETHER to push (see
`ananta/tests/core/actions/job_completion_role_routing_smoke.py`); this smoke
covers the two halves core cannot: the dispatch-time stamp that makes a push
possible, and the verb that performs it.

Sections, each naming the mutation that reds it:

  1. STAMP (A2). `_build_process_call_trigger_data` writes
     `completion_route_role` when — and only when — a durable role actually
     resolved. Absent means unaddressable; "" would be a destination that
     resolves to nothing. (Mutation: stamp it unconditionally -> the roleless
     case FAILs.) Also asserts the stamp is its own key, NOT a member of the
     `caller_attribution_*` family, which the §34.6 design keeps
     single-consumer so an attributed CLI call can never re-point a flow's
     inference vertex.
  2. ENVELOPE. The delivered message carries job_id, provider, status and the
     payload, so the recipient can act without a second lookup. (Mutation: drop
     the payload from the rendered body -> FAIL.)
  3. TRUNCATION. An oversized payload is clipped WITH disclosure and the exact
     verb that returns the whole thing — a silently clipped payload reads as a
     complete one. (Mutation: truncate without the notice -> FAIL.)
  4. SENDER. The verb uses the system job-completion sentinel and NOT the
     REL-01 resolution ladder. This is the entire reason it is not just
     `peer_send_by_name`: that verb's ladder reaches a caller-attribution rung
     which, for a bridge-origin flow, resolves to the job's ORIGINATOR — i.e.
     the recipient — and would stamp the completion as sent by the very session
     being delivered to. (Mutation: swap the hardcoded sentinel for
     `_resolve_role_send_sender(state, state_service)` -> FAIL.)
  5. STAMP-ON-SUCCESS. The reach upgrade is written only after a measured
     hand-off, and a stamp that cannot be written returns False and leaves the
     unreached marker standing — over-reporting an unreached job is
     recoverable, losing one is not. (Mutation: return True from the failure
     branch -> FAIL.)

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/job_completion_delivery_smoke.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.core.state.job_completion_reach import (  # noqa: E402
    COMPLETION_REACH_KEY,
    REACH_ROLE_INBOX_DELIVERED,
)

from agent_messaging_plugin import plugin as plugin_module  # noqa: E402
from agent_messaging_plugin.platform_surface import (  # noqa: E402
    COMPLETION_ROUTE_ROLE_KEY,
    CallerAttribution,
    PlatformSurface,
)

_ROLE = "lane-w-recipient"
_JOB = "job-smoke-w"

_failures: list[str] = []


def _check(condition: bool, message: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {message}")
    if not condition:
        _failures.append(message)


class _FakeBridge:
    """The bridge shape `_build_process_call_trigger_data` reads."""

    bridge_id = "agc-smoke"
    session_id = "ses-smoke"
    agent_instance_id = ""
    client_id = ""
    caller_agent_session_id = "ases-smoke"


def _trigger_for(role: str) -> dict[str, object]:
    return PlatformSurface._build_process_call_trigger_data(
        bridge=_FakeBridge(),  # type: ignore[arg-type]
        process_key="plugin::g_suite_plugin::sheets_create_from_files",
        reason="smoke",
        inference_vertex_role="",
        operator_equivalent=False,
        caller_attribution=CallerAttribution(
            agent_id="claude_code",
            agent_instance_id="agi-smoke",
            session_label="Lane W Recipient",
            role=role,
        ),
    )


def test_stamp_is_written_only_when_a_role_resolved() -> None:
    """Section 1: presence is permission; absence is honest."""
    print("\ntest_stamp_is_written_only_when_a_role_resolved")
    with_role = _trigger_for(_ROLE)
    without_role = _trigger_for("")

    _check(
        with_role.get(COMPLETION_ROUTE_ROLE_KEY) == _ROLE,
        "a resolved durable role is stamped as the completion route",
    )
    _check(
        COMPLETION_ROUTE_ROLE_KEY not in without_role,
        "a roleless caller leaves the key ABSENT (not empty) — no guessed destination",
    )
    _check(
        not COMPLETION_ROUTE_ROLE_KEY.startswith("caller_attribution"),
        "the route key is NOT a member of the single-consumer attribution family",
    )
    _check(
        without_role.get("caller_attribution_role") == "",
        "the attribution family itself is unchanged by this lane",
    )


def test_envelope_carries_what_the_recipient_needs() -> None:
    """Section 2: names route, content binds."""
    print("\ntest_envelope_carries_what_the_recipient_needs")
    text = plugin_module._format_job_completion_message(
        job_id=_JOB,
        provider_name="g_suite_plugin.sheets_create_from_files",
        status="completed",
        payload={"spreadsheet_url": "https://docs.example/abc"},
    )
    _check(_JOB in text, "the message names the job_id")
    _check("g_suite_plugin.sheets_create_from_files" in text, "the message names the originating verb")
    _check("completed" in text, "the message names the terminal status")
    _check(
        "https://docs.example/abc" in text,
        "the message embeds the payload — no second lookup required",
    )

    err = plugin_module._format_job_completion_message(
        job_id=_JOB, provider_name="", status="error", payload={"message": "boom"},
    )
    _check("boom" in err and "error" in err, "an error payload is rendered the same way")


def test_truncation_discloses_itself() -> None:
    """Section 3: a clipped payload must never read as a complete one."""
    print("\ntest_truncation_discloses_itself")
    big = {"rows": ["x" * 200 for _ in range(60)]}
    text = plugin_module._format_job_completion_message(
        job_id=_JOB, provider_name="p.v", status="completed", payload=big,
    )
    _check("TRUNCATED" in text, "truncation is disclosed in the message body")
    _check(
        "job_service::get_job" in text and _JOB in text,
        "and it names the exact verb (with the job_id) that returns the whole payload",
    )
    _check(
        len(text) < len(json.dumps(big, indent=2, sort_keys=True)),
        "the delivered message really is smaller than the raw payload",
    )


class _CapturedSend:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def __call__(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs

        class _Outcome:
            @staticmethod
            def to_payload() -> dict[str, object]:
                return {"delivery": "queued_wake", "message_id": "arm-smoke"}

        return _Outcome()


class _FakeStateService:
    def __init__(self, *, job_readable: bool = True, update_raises: bool = False) -> None:
        self._job_readable = job_readable
        self._update_raises = update_raises
        self.updates: list[dict[str, object]] = []

    def read_state(self, namespace: str, query: dict[str, object]) -> dict[str, object]:
        if not self._job_readable:
            return {"action_status": "completed", "data": {"records": []}}
        return {
            "action_status": "completed",
            "data": {"records": [{"id": _JOB, "metadata": json.dumps({"flow_id": "f1"})}]},
        }

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object]
    ) -> dict[str, object]:
        if self._update_raises:
            raise RuntimeError("smoke: metadata write failed")
        self.updates.append(updates)
        return {"action_status": "completed"}


class _Harness:
    """Minimal duck-typed stand-in the verb body is bound onto."""

    def __init__(self, state_service: _FakeStateService) -> None:
        self._peer_registry = object()
        self._bridge_manager = object()
        self._state_service = state_service

    def _get_state_service(self) -> _FakeStateService:
        return self._state_service

    def _require_service(self) -> object:
        return object()


class _ResolvedRole:
    agent_instance_id = "agi-recipient"
    agent_id = "claude_code"
    session_label = "Lane W Recipient"


def _run_verb(state_service: _FakeStateService, state: dict[str, Any]) -> tuple[Any, _CapturedSend]:
    captured = _CapturedSend()
    real_send = plugin_module.dispatch_role_send
    real_resolve = plugin_module.resolve_role_binding
    plugin_module.dispatch_role_send = captured  # type: ignore[assignment]
    plugin_module.resolve_role_binding = lambda _svc, _name: _ResolvedRole()  # type: ignore[assignment]
    try:
        result = plugin_module.AgentMessagingPlugin.deliver_job_completion(
            _Harness(state_service),  # type: ignore[arg-type]
            {
                "name": _ROLE,
                "job_id": _JOB,
                "provider_name": "g_suite_plugin.sheets_create_from_files",
                "status": "completed",
                "payload": {"ok": True},
            },
            state,
        )
    finally:
        plugin_module.dispatch_role_send = real_send  # type: ignore[assignment]
        plugin_module.resolve_role_binding = real_resolve  # type: ignore[assignment]
    return result, captured


def test_sender_is_the_sentinel_never_the_recipient() -> None:
    """Section 4: the reason this verb is not peer_send_by_name."""
    print("\ntest_sender_is_the_sentinel_never_the_recipient")
    # A flow state that WOULD drive the REL-01 ladder to the recipient's own
    # role — exactly the bridge-origin shape this lane routes.
    state = {
        "inference_vertex_role": "",
        "inference_vertex_session_id": "",
        "caller_attribution_role": _ROLE,
        "caller_attribution_instance_id": "agi-recipient",
        "caller_attribution_agent_id": "claude_code",
        "caller_attribution_label": "Lane W Recipient",
    }
    svc = _FakeStateService()
    _result, captured = _run_verb(svc, state)

    _check(
        captured.kwargs.get("sender_agent_instance_id")
        == plugin_module.SYSTEM_JOB_COMPLETION_ID,
        "the sender is the job-completion sentinel",
    )
    _check(
        captured.kwargs.get("sender_agent_instance_id") != _ROLE
        and captured.kwargs.get("sender_session_label") != "Lane W Recipient",
        "and is NOT the attributed caller — a completion is never 'from' its recipient",
    )
    _check(
        captured.kwargs.get("sender_bridge_id") == plugin_module.SYSTEM_JOB_COMPLETION_ID,
        "completions thread on their own sentinel, not the scheduler's",
    )
    _check(
        captured.kwargs.get("sender_bridge_id") != plugin_module.SYSTEM_SCHEDULER_ID,
        "a completion is not labelled scheduler-originated (false provenance)",
    )
    _check(captured.kwargs.get("role_name") == _ROLE, "it is addressed to the routed role")


def test_reach_is_stamped_only_on_a_measured_handoff() -> None:
    """Section 5: the stamp attests delivery, and never fabricates one."""
    print("\ntest_reach_is_stamped_only_on_a_measured_handoff")
    svc = _FakeStateService()
    result, _captured = _run_verb(svc, {})
    stamped = [
        json.loads(str(u.get("metadata")))
        for u in svc.updates
        if isinstance(u.get("metadata"), str)
    ]
    _check(
        any(s.get(COMPLETION_REACH_KEY) == REACH_ROLE_INBOX_DELIVERED for s in stamped),
        "a delivered completion upgrades completion_reach to role_inbox_delivered",
    )
    data = result.get("data") if isinstance(result, dict) else None
    data = data if isinstance(data, dict) else {}
    _check(data.get("reach_stamped") is True, "and the verb reports the stamp it wrote")

    unreadable = _FakeStateService(job_readable=False)
    result2, _c2 = _run_verb(unreadable, {})
    data2 = result2.get("data") if isinstance(result2, dict) else None
    data2 = data2 if isinstance(data2, dict) else {}
    _check(
        not unreadable.updates,
        "an unreadable job writes NO reach value (it stays unreached for the drain)",
    )
    _check(
        data2.get("reach_stamped") is False,
        "and the verb reports honestly that it could not stamp",
    )

    # The other failure shape: the job reads fine but the metadata WRITE throws.
    # Exercised explicitly because it is a different branch from the unreadable
    # case above, and an unexercised branch is an unproven one.
    raising = _FakeStateService(update_raises=True)
    result3, captured3 = _run_verb(raising, {})
    data3 = result3.get("data") if isinstance(result3, dict) else None
    data3 = data3 if isinstance(data3, dict) else {}
    _check(
        data3.get("reach_stamped") is False,
        "a throwing metadata write reports reach_stamped=False, never a fabricated stamp",
    )
    _check(
        bool(captured3.kwargs),
        "and the DELIVERY still happened — a failed stamp never undoes a real send",
    )


def main() -> int:
    print("Lane W: job completion delivery + route stamp")
    test_stamp_is_written_only_when_a_role_resolved()
    test_envelope_carries_what_the_recipient_needs()
    test_truncation_discloses_itself()
    test_sender_is_the_sentinel_never_the_recipient()
    test_reach_is_stamped_only_on_a_measured_handoff()

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
