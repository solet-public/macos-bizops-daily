#!/usr/bin/env python3
"""Typed failure detail survives to the stored action result (no pytest, no DB).

`_mark_action_failed` stored a CONSTANT `code="action_failed"` on every failure,
so every `/process/call` consumer platform-wide saw the same code and could not
discriminate failure classes — against the fast-fail / no-silent-fallback rules.

The constant was only the SYMPTOM. The typed detail was already destroyed
upstream: `_mark_action_failed` accepted `error_message: str`, and all three
call sites stringified before calling it — including one that had a full
`ErrorDetail` dict in hand (`result.get("error")`) and passed it through
`str()`. Fixing the constant alone would have had nothing typed left to write.

So this drives the REAL `_mark_action_failed` with genuine typed inputs and
asserts on what is actually STORED:

  * a plugin-shaped `ErrorDetail` mapping keeps its `code`, `type` and `details`;
  * an `AnantaError` subclass contributes its `error_code` via `to_dict()`;
  * an untyped failure still degrades to the historical constant (no regression
    for callers that never had typing to lose);
  * the human-readable `message` is preserved in every case.

The stubs replace only the DB seams (`_update_action_status_to_failed`,
`_retrieve_failed_action_details`, `_store_action_result`) and the downstream
template/dispatch hop; the method under test is the shipped one.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        ananta/tests/core/actions/action_failure_error_typing_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.actions.action_queue_poller import (  # noqa: E402
    ActionQueuePoller,
    _typed_error_detail,
)
from ananta.error_handling import FrameworkError  # noqa: E402

_PROCESS_KEY = "plugin::agent_messaging_plugin::peer_send_by_name"

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


def _poller_capturing_stored_result() -> tuple[ActionQueuePoller, dict[str, Any]]:
    """A real poller with only its DB / downstream seams stubbed."""
    captured: dict[str, Any] = {}
    poller = object.__new__(ActionQueuePoller)
    poller._update_action_status_to_failed = lambda *_a, **_k: None
    poller._retrieve_failed_action_details = lambda _aid: (
        _PROCESS_KEY, "{}", None, None, "ses-1", "flow-1", None, None,
    )
    poller._parse_failed_arguments = lambda _raw: {}
    poller._process_error_processor_template = lambda *_a, **_k: None

    class _Dispatcher:
        def dispatch_execution_failure(self, **_k: Any) -> None:
            return None

    poller._get_error_dispatcher = lambda: _Dispatcher()

    def _store(action_id: str, result: dict[str, Any], process_key: str) -> None:
        captured["action_id"] = action_id
        captured["result"] = result
        captured["process_key"] = process_key

    poller._store_action_result = _store
    return poller, captured


def _stored_error(**kwargs: Any) -> dict[str, Any]:
    poller, captured = _poller_capturing_stored_result()
    try:
        poller._mark_action_failed("ae-typed", "boom", **kwargs)
    except Exception as exc:  # noqa: BLE001 — surface a wiring break as a failure
        return {"__raised__": f"{type(exc).__name__}: {exc}"}
    result = captured.get("result") or {}
    error = result.get("error")
    return error if isinstance(error, dict) else {"__missing__": result}


def test_plugin_error_detail_keeps_its_code() -> None:
    """A plugin's ErrorDetail mapping survives to the stored row.

    Mutation that turns this red: drop the `error_detail` parameter and
    reinstate the hardcoded `{"message": ..., "code": "action_failed"}`.
    """
    error = _stored_error(error_detail={
        "type": "agent_messaging_error",
        "code": "peer_role_vacant",
        "message": "boom",
        "details": {"role": "Claude-C"},
        "severity": "error",
    })
    _check(
        error.get("code") == "peer_role_vacant",
        f"a plugin ErrorDetail's code reaches the stored result (got {error.get('code')!r})",
    )
    _check(
        error.get("type") == "agent_messaging_error",
        "the error TYPE survives too — consumers discriminate on both",
    )
    _check(
        error.get("details") == {"role": "Claude-C"},
        "structured details survive (this is what str() destroyed upstream)",
    )


def test_framework_error_contributes_its_code() -> None:
    """An AnantaError subclass types the failure via its own to_dict()."""
    error = _stored_error(
        error_detail=FrameworkError(
            "boom", error_code="plugin.not_running", details={"plugin": "x"},
        ).to_dict(),
    )
    _check(
        error.get("code") == "plugin.not_running",
        f"a FrameworkError's error_code reaches the row (got {error.get('code')!r})",
    )


def test_untyped_failure_still_degrades_to_the_constant() -> None:
    """No typed input → the historical constant. Not a regression, a fallback.

    This is the control that keeps the fix honest: it must ADD typing where
    typing exists, not invent it where none does.
    """
    error = _stored_error()
    _check(
        error.get("code") == "action_failed",
        f"an untyped failure keeps the generic code (got {error.get('code')!r})",
    )
    _check(
        error.get("message") == "boom",
        "the human-readable message is preserved on the untyped path",
    )


def test_extractor_refuses_to_synthesize_a_code() -> None:
    """★ The extractor must return None for untyped input, never a stub.

    Found by mutation, not by design: mutating `_typed_error_detail` to return
    `{"code": "synthesized"}` left the suite GREEN, because the untyped case
    above passes `error_detail=None` by DEFAULT and never reaches the
    extractor. So the "no typing in, no typing out" guarantee was asserted at
    the wrong seam — the default parameter, not the code that decides.

    A fabricated code is indistinguishable from a real one downstream, which is
    the exact failure this change exists to end, so it gets its own assertion
    at the deciding function.
    """
    for untyped in ("a bare string", ValueError("no typing"), {"message": "no code"}, None):
        _check(
            _typed_error_detail(untyped) is None,
            f"extractor returns None for untyped input ({type(untyped).__name__})",
        )
    _check(
        _typed_error_detail({"code": "real", "message": "m"}) is not None,
        "control: the extractor DOES accept genuinely typed input",
    )


def test_message_is_preserved_alongside_typing() -> None:
    """Typing must not cost the operator-facing message."""
    error = _stored_error(error_detail={"code": "c", "message": "boom"})
    _check(
        error.get("message") == "boom",
        "the message survives on the typed path too",
    )


def main() -> None:
    print("typed failure detail survives to the stored action result")
    for name, obj in sorted(globals().items()):
        if name.startswith("test_") and callable(obj):
            print(f"\n{name}")
            obj()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
