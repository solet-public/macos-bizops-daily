#!/usr/bin/env python3
"""INF-02 planning completion routing + resume smoke (no pytest).

Proves the thinking-side half of the autonomic completion surface — the
ONLY live ``_generate_thinking_completion`` consumer per the INF-02 caller
sweep (``process_planning_results``; create_task/continue_task are dormant
and excluded):

  P1  ``_route_planning_completion`` submits with the right shape (purpose
      playbook_planning, the resume process key, correlation carrying the
      planning context + playbook ids) and a ``session``/``deferred``
      verdict returns the ``awaiting_completion`` ActionResult — the flow
      turn terminates cleanly with NO actions.
  P2  a ``provider_fallback`` verdict returns ``None`` → the caller runs
      the synchronous bound-provider path (operator-opted only).
  P3  a missing inference_service fails loud (typed).
  P4  ``resume_thinking_completion`` re-enters with the served row: the
      OUTPUT context event is appended with the served text and the parsed
      planning actions are submitted with the ``result_processor_target``
      override intact (the loop continues); a no-actions completion
      finalizes the playbook.
  P5  typed rejections: missing request_id; unknown request; a row that is
      NOT served; a purpose this consumer does not own; an incomplete row.
  P6  structural (AST) pins: ``process_planning_results`` routes through
      ``_route_planning_completion`` BEFORE the sync seam, and both the
      vertex and the resume share ``_continue_planning_with_completion``
      (one post-completion path, no drift).

Offline: recording fakes over the real plugin seam code + static ast over
the real source. No live solet / LM Studio / Postgres.

Run from repo root:
    .venv/bin/python3 plugins/default_thinking_plugin/tests/planning_completion_resume_smoke.py
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "default_thinking_plugin" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"))

from ananta.error_handling import FrameworkError  # noqa: E402
from default_thinking_plugin.plugin import DefaultThinkingPlugin  # noqa: E402

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


MESSAGES = [{"role": "system", "content": "plan"}, {"role": "user", "content": "go"}]
RESUME_KEY = "service_interface::thinking_service::resume_thinking_completion"


class _RecordingCompletionService:
    """Records submit_completion_request calls; serves canned rows on read."""

    def __init__(self, verdict: dict[str, object]) -> None:
        self.verdict = verdict
        self.submits: list[dict[str, object]] = []
        self.rows: dict[str, dict[str, object]] = {}

    def submit_completion_request(self, **kwargs: object) -> dict[str, object]:
        self.submits.append(kwargs)
        return self.verdict

    def get_completion_request(self, request_id: str) -> dict[str, object] | None:
        return self.rows.get(request_id)


class _FakeOrchestrator:
    def __init__(self, service: object | None) -> None:
        self._service = service

    def get_service(self, name: str) -> object | None:
        return self._service if name == "inference_service" else None


def _build_plugin(service: object | None) -> Any:
    plugin: Any = DefaultThinkingPlugin.__new__(DefaultThinkingPlugin)
    plugin.orchestrator_ref = _FakeOrchestrator(service)
    plugin.logger = logging.getLogger("planning_completion_resume_smoke")
    plugin.appended_events = []

    def _record_event(
        context_id: str, content: str, event_type: object, actor_type: object,
    ) -> None:
        plugin.appended_events.append(
            (context_id, content, getattr(event_type, "value", event_type)),
        )

    plugin._append_context_event = _record_event  # noqa: SLF001 — harness seam
    return plugin


def _served_row(request_id: str, *, completion: str) -> dict[str, object]:
    return {
        "request_id": request_id,
        "purpose": "playbook_planning",
        "resume_process_key": RESUME_KEY,
        "correlation": json.dumps({"context_id": "ctx-1", "playbook_id": "pb-1"}),
        "status": "served",
        "result_text": completion,
    }


ACTION_COMPLETION = (
    "Next step:\n```json\n"
    + json.dumps({"actions": [{"name": "search", "process": {"provider_type": "service_interface", "provider": "knowledge_service", "function_name": "search"}, "arguments": {"query": "x"}}]})
    + "\n```"
)


def _case_1() -> None:
    print("\n[P1] routed submit shape + awaiting verdict")
    service = _RecordingCompletionService(
        {"routing": "session", "request_id": "icr-1"},
    )
    plugin = _build_plugin(service)
    result = plugin._route_planning_completion(MESSAGES, "ctx-1", "pb-1")  # noqa: SLF001 — drives the seam directly
    _check(result is not None, "P1 session verdict returns an ActionResult")
    _check(
        result["action_status"] == "completed"
        and result["data"]["planning_status"] == "awaiting_completion"
        and result["data"]["request_id"] == "icr-1"
        and result["actions"] == [],
        "P1 awaiting_completion terminates the turn with NO actions",
    )
    submit = service.submits[0]
    _check(
        submit["purpose"] == "playbook_planning"
        and submit["resume_process_key"] == RESUME_KEY
        and submit["messages"] == MESSAGES,
        "P1 submit carries purpose + resume key + assembled messages",
    )
    _check(
        submit["correlation"]
        == {"context_id": "ctx-1", "playbook_id": "pb-1"},
        "P1 correlation carries the planning context + playbook ids",
    )
    deferred_service = _RecordingCompletionService(
        {"routing": "deferred", "request_id": "icr-2"},
    )
    plugin = _build_plugin(deferred_service)
    result = plugin._route_planning_completion(MESSAGES, "ctx-1", "pb-1")  # noqa: SLF001
    _check(
        result is not None and result["data"]["routing"] == "deferred",
        "P1 deferred (vacant-slot) verdict also returns awaiting_completion",
    )


def _case_2() -> None:
    print("\n[P2] provider_fallback verdict → sync path (returns None)")
    fallback_service = _RecordingCompletionService({"routing": "provider_fallback"})
    plugin = _build_plugin(fallback_service)
    result = plugin._route_planning_completion(MESSAGES, "ctx-1", "pb-1")  # noqa: SLF001
    _check(result is None, "P2 provider_fallback → caller runs the sync seam")


def _case_3() -> None:
    print("\n[P3] missing inference_service fails loud")
    plugin = _build_plugin(None)
    try:
        plugin._route_planning_completion(MESSAGES, "ctx-1", "pb-1")  # noqa: SLF001
        _check(False, "P3 missing service raises FrameworkError")
    except FrameworkError:
        _check(True, "P3 missing service raises FrameworkError")


def _case_4() -> None:
    print("\n[P4] resume re-enters the loop with the served text")
    service = _RecordingCompletionService({"routing": "session"})
    service.rows["icr-act"] = _served_row("icr-act", completion=ACTION_COMPLETION)
    plugin = _build_plugin(service)
    result = plugin.resume_thinking_completion({"request_id": "icr-act"}, {})
    _check(
        plugin.appended_events
        and plugin.appended_events[0][0] == "ctx-1"
        and plugin.appended_events[0][1] == ACTION_COMPLETION,
        "P4 OUTPUT context event appended with the served completion",
    )
    _check(
        result["action_status"] == "completed"
        and result["data"]["planning_status"] == "in_progress"
        and len(result["actions"]) == 1,
        "P4 parsed planning actions submitted (loop continues)",
    )
    submitted = result["actions"][0]
    _check(
        submitted["result_processor_target"]
        == "service_interface::thinking_service::process_planning_results"
        and submitted["context_id"] == "ctx-1",
        "P4 submitted actions carry the vertex override + planning context",
    )

    finalize_service = _RecordingCompletionService({"routing": "session"})
    finalize_service.rows["icr-fin"] = _served_row(
        "icr-fin",
        completion="```playbook\nthe playbook\n```\n```plan\nthe plan\n```",
    )
    plugin = _build_plugin(finalize_service)
    finalized: list[tuple[str, str]] = []
    plugin._finalize_playbook = (  # noqa: SLF001 — heavy artifact path, recorded
        lambda text, playbook_id, session_id: finalized.append((text, playbook_id))
        or {"action_status": "completed", "data": {"planning_status": "completed"},
            "actions": [], "error": None, "timestamp": ""}
    )
    result = plugin.resume_thinking_completion({"request_id": "icr-fin"}, {})
    _check(
        finalized and finalized[0][1] == "pb-1"
        and result["data"]["planning_status"] == "completed",
        "P4 a no-actions completion finalizes the playbook",
    )


def _case_5() -> None:
    print("\n[P5] typed rejections")
    plugin = _build_plugin(_RecordingCompletionService({"routing": "session"}))
    for label, params in (
        ("P5 missing request_id", {}),
        ("P5 unknown request", {"request_id": "icr-nope"}),
    ):
        try:
            plugin.resume_thinking_completion(params, {})
            _check(False, f"{label} raises FrameworkError")
        except FrameworkError:
            _check(True, f"{label} raises FrameworkError")

    pending_service = _RecordingCompletionService({"routing": "session"})
    pending_service.rows["icr-pend"] = {
        **_served_row("icr-pend", completion=""),
        "status": "pending",
        "result_text": "",
    }
    plugin = _build_plugin(pending_service)
    try:
        plugin.resume_thinking_completion({"request_id": "icr-pend"}, {})
        _check(False, "P5 a non-served row raises FrameworkError")
    except FrameworkError:
        _check(True, "P5 a non-served row raises FrameworkError")

    foreign_service = _RecordingCompletionService({"routing": "session"})
    foreign_service.rows["icr-for"] = {
        **_served_row("icr-for", completion="text"),
        "purpose": "some_other_surface",
    }
    plugin = _build_plugin(foreign_service)
    try:
        plugin.resume_thinking_completion({"request_id": "icr-for"}, {})
        _check(False, "P5 a foreign purpose raises FrameworkError")
    except FrameworkError:
        _check(True, "P5 a foreign purpose raises FrameworkError")

    incomplete_service = _RecordingCompletionService({"routing": "session"})
    incomplete_service.rows["icr-inc"] = {
        **_served_row("icr-inc", completion="text"),
        "correlation": json.dumps({"context_id": ""}),
    }
    plugin = _build_plugin(incomplete_service)
    try:
        plugin.resume_thinking_completion({"request_id": "icr-inc"}, {})
        _check(False, "P5 an incomplete row raises FrameworkError")
    except FrameworkError:
        _check(True, "P5 an incomplete row raises FrameworkError")


def _case_6() -> None:
    print("\n[P6] structural pins (ast over the real source)")
    source = inspect.getsource(DefaultThinkingPlugin)
    tree = ast.parse(source)

    def _method(name: str) -> ast.FunctionDef:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        raise AssertionError(f"method {name} not found")

    def _calls(node: ast.FunctionDef) -> set[str]:
        return {
            n.func.attr
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }

    vertex_calls = _calls(_method("process_planning_results"))
    _check(
        "_route_planning_completion" in vertex_calls,
        "P6 process_planning_results routes via _route_planning_completion",
    )
    _check(
        "_generate_thinking_completion" in vertex_calls,
        "P6 the sync fallback seam remains in process_planning_results",
    )
    _check(
        "_continue_planning_with_completion" in vertex_calls
        and "_continue_planning_with_completion"
        in _calls(_method("resume_thinking_completion")),
        "P6 vertex + resume share ONE post-completion path",
    )


def main() -> int:
    print("INF-02 planning completion routing + resume smoke")
    _case_1()
    _case_2()
    _case_3()
    _case_4()
    _case_5()
    _case_6()

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
