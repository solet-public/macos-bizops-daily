#!/usr/bin/env python3
"""DEP-01 Phase-2b — B/C inference re-home smoke (no pytest).

Proves the extended-plan engine (class B: ``create_task`` /
``continue_task``) and the planning vertex (class C:
``process_planning_results``) are re-homed off the private qwen path and
onto the platform inference service, and that the private path is GONE:

* the shared seam ``_generate_thinking_completion`` resolves
  ``inference_service`` via the orchestrator and calls
  ``generate_completion`` with a plain-prose ``InferenceRequest``
  (``use_structured_output=False``, purpose in ``context_metadata``) —
  asserted with a RECORDING fake service. The smoke asserts the call
  REACHES the service with the right shape. Code-truth (Rev-B trace,
  builder-verified): ``generate_completion`` is a plain delegation to
  the BOUND provider — the vacancy→DEFER flip exists only on the
  ``_route_vertex`` paths, so B/C serve on the bound provider (interim,
  operator-D.1-consistent; INF-02 tracks an autonomic-routed
  completion surface);
* any unusable envelope per the ActionResult contract — an error
  payload, a non-completed status, or empty completion text — raises
  the typed ``default_thinking_plugin.inference_unusable`` fail-loud
  (the live LM Studio provider RAISES on failure instead; those
  exceptions propagate loud untouched). A missing inference_service
  fails loud too. No silent fallback exists;
* structural (AST) pins: ``create_task``, ``continue_task``, and
  ``process_planning_results`` each CALL the seam, and the module
  carries NO ``_invoke_thinking_model``, NO ``_strip_think_block``, no
  ``chat/completions`` endpoint, and no qwen model literal — the
  private transport cannot silently return (TMO-02's subject is gone).

Offline: recording fakes; real plugin seam code; static ast over the
real source.

Run:
    .venv/bin/python3 plugins/default_thinking_plugin/tests/inference_rehome_smoke.py
"""

from __future__ import annotations

import ast
import importlib
import inspect
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "default_thinking_plugin" / "src"))

importlib.import_module("ananta.core.config")  # pre-warm; avoids a latent cycle

from ananta.error_handling import FrameworkError  # noqa: E402
from default_thinking_plugin.constants import ErrorCode  # noqa: E402
from default_thinking_plugin.plugin import DefaultThinkingPlugin  # noqa: E402

# ---------------------------------------------------------------------------
# Recording fakes
# ---------------------------------------------------------------------------


class RecordingInferenceService:
    """Records generate_completion requests; returns a canned envelope."""

    def __init__(self, envelope: dict[str, Any] | None = None) -> None:
        self.requests: list[Any] = []
        self._envelope = envelope

    def generate_completion(self, request: Any) -> dict[str, Any] | None:
        self.requests.append(request)
        return self._envelope


class FakeOrchestrator:
    """Orchestrator exposing only the recording inference service."""

    def __init__(self, service: RecordingInferenceService | None) -> None:
        self._service = service

    def get_service(self, name: str) -> object | None:
        return self._service if name == "inference_service" else None


def build_plugin(service: RecordingInferenceService | None) -> Any:
    plugin: Any = DefaultThinkingPlugin.__new__(DefaultThinkingPlugin)
    plugin.orchestrator_ref = FakeOrchestrator(service)
    import logging

    plugin.logger = logging.getLogger("inference_rehome_smoke")
    return plugin


COMPLETED_ENVELOPE: dict[str, Any] = {
    "action_status": "completed",
    "data": {"result": {"completion": "authored planning output"}},
}

# ActionResult-contract shape a provider MAY return per the interface
# (the live LM Studio provider raises instead of returning this — the
# case pins the seam's contract adherence, not live provider behavior).
ERROR_ENVELOPE: dict[str, Any] = {
    "action_status": "error",
    "data": {},
    "error": {"code": "inference.provider_error", "message": "boom"},
}

EMPTY_ENVELOPE: dict[str, Any] = {
    "action_status": "completed",
    "data": {"result": {"completion": ""}},
}

MESSAGES = [
    {"role": "system", "content": "frame"},
    {"role": "user", "content": "plan the next move"},
]


# ---------------------------------------------------------------------------
# Checker (standalone pass/fail accumulator — project policy: no pytest)
# ---------------------------------------------------------------------------


class Checker:
    """Minimal pass/fail accumulator."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.passed = 0
        self.failed: list[str] = []

    def check(self, condition: object, label: str) -> None:
        if condition:
            self.passed += 1
            print(f"  PASS  {label}")
        else:
            self.failed.append(label)
            print(f"  FAIL  {label}")

    def summary(self) -> int:
        print(f"\n{self.passed} passed, {len(self.failed)} failed")
        for label in self.failed:
            print(f"  FAILED: {label}")
        return 1 if self.failed else 0


def _unusable_error(exc: FrameworkError) -> bool:
    return getattr(exc, "error_code", None) == ErrorCode.INFERENCE_UNUSABLE


# ---------------------------------------------------------------------------
# Cases — the seam reaches inference_service with the right shape
# ---------------------------------------------------------------------------


def test_seam_reaches_service_and_extracts(c: Checker) -> None:
    service = RecordingInferenceService(COMPLETED_ENVELOPE)
    plugin = build_plugin(service)
    text = plugin._generate_thinking_completion(MESSAGES, purpose="smoke_case")
    c.check(text == "authored planning output", f"canonical completion extracted ({text!r})")
    c.check(len(service.requests) == 1, "exactly one generate_completion call")
    request = service.requests[0]
    c.check(request.messages == MESSAGES, "messages pass through unmodified")
    c.check(
        request.use_structured_output is False,
        "plain-prose request (no action-JSON schema forced)",
    )
    c.check(
        request.context_metadata.get("purpose") == "smoke_case",
        f"purpose stamped in context_metadata ({request.context_metadata!r})",
    )


def test_error_envelope_fails_loud(c: Checker) -> None:
    """An ActionResult-contract error envelope raises the typed error."""
    plugin = build_plugin(RecordingInferenceService(ERROR_ENVELOPE))
    try:
        plugin._generate_thinking_completion(MESSAGES, purpose="smoke_case")
    except FrameworkError as exc:
        c.check(_unusable_error(exc), f"error envelope raises inference_unusable ({exc})")
    else:
        c.check(False, "error envelope must raise FrameworkError")


def test_empty_completion_fails_loud(c: Checker) -> None:
    plugin = build_plugin(RecordingInferenceService(EMPTY_ENVELOPE))
    try:
        plugin._generate_thinking_completion(MESSAGES, purpose="smoke_case")
    except FrameworkError as exc:
        c.check(_unusable_error(exc), f"empty completion raises inference_unusable ({exc})")
    else:
        c.check(False, "empty completion must raise FrameworkError")


def test_missing_service_fails_loud(c: Checker) -> None:
    plugin = build_plugin(None)
    try:
        plugin._generate_thinking_completion(MESSAGES, purpose="smoke_case")
    except FrameworkError as exc:
        c.check(
            getattr(exc, "error_code", None) == ErrorCode.BACKEND_NOT_AVAILABLE,
            f"missing inference_service raises backend_not_available ({exc})",
        )
    else:
        c.check(False, "missing inference_service must raise FrameworkError")


# ---------------------------------------------------------------------------
# Cases — structural routing + private-path absence (AST over real source)
# ---------------------------------------------------------------------------


def _plugin_source() -> str:
    return Path(inspect.getfile(DefaultThinkingPlugin)).read_text(encoding="utf-8")


def _method_calls_seam(tree: ast.Module, method: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == method:
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "_generate_thinking_completion"
                ):
                    return True
            return False
    return False


def test_b_and_c_route_through_the_seam(c: Checker) -> None:
    tree = ast.parse(_plugin_source())
    for method in ("create_task", "continue_task", "process_planning_results"):
        c.check(
            _method_calls_seam(tree, method),
            f"{method} calls _generate_thinking_completion",
        )


def test_private_path_is_gone(c: Checker) -> None:
    source = _plugin_source()
    c.check(
        "_invoke_thinking_model" not in source,
        "no _invoke_thinking_model anywhere in the module",
    )
    c.check(
        "_strip_think_block" not in source,
        "no _strip_think_block anywhere in the module",
    )
    c.check(
        "chat/completions" not in source,
        "no private chat/completions endpoint",
    )
    c.check(
        "qwen3-30b" not in source.lower(),
        "no qwen model-name literal (prose mentions of the retirement are fine)",
    )
    c.check(
        '"timeout_seconds"' not in source and '"max_tokens"' not in source,
        "private-path knobs gone from config surface (TMO-02 subject deleted)",
    )


def main() -> int:
    c = Checker("DEP-01 Phase-2b inference re-home")
    cases: list[Callable[[Checker], None]] = [
        test_seam_reaches_service_and_extracts,
        test_error_envelope_fails_loud,
        test_empty_completion_fails_loud,
        test_missing_service_fails_loud,
        test_b_and_c_route_through_the_seam,
        test_private_path_is_gone,
    ]
    for case in cases:
        print(f"\n{case.__name__}")
        case(c)
    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
