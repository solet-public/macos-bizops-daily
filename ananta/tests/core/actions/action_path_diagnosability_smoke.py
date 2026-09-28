#!/usr/bin/env python3
"""Smoke for making the next action-path stall diagnosable (iss_30fb08fd, iss_e0648481).

Asserts:

  (1) ``install_stack_dump_handler`` registers SIGUSR1 against a dedicated
      ``data/logs/faulthandler_stacks.log`` under the app home: sending the
      signal to this process writes every thread's Python stack there, and a
      second install is idempotent. ``ananta.cli.sync_main`` calls it.
  (2) The ``ACTION_PATH_LIVENESS`` snapshot carries ``in_flight_action_id``,
      ``in_flight_process_key``, ``in_flight_started_at`` (and the age) while
      ``_poll_once`` is inside an action, and clears them afterwards -- driven
      through the real ``ActionQueuePoller._poll_once`` drain loop, including
      a handler that raises.
  (3) A single action longer than ``SLOW_ACTION_THRESHOLD_SECONDS`` is logged
      loudly (``SLOW_ACTION`` at ERROR); a fast one is not.

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:
    .venv/bin/python3 ananta/tests/core/actions/action_path_diagnosability_smoke.py
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.actions import action_queue_poller as poller_module  # noqa: E402
from ananta.core.actions.action_path_liveness import (  # noqa: E402
    ACTION_PATH_LIVENESS,
    SLOW_ACTION_THRESHOLD_SECONDS,
)
from ananta.core.actions.action_queue_poller import (  # noqa: E402
    ActionQueuePoller,
    QueuedAction,
)
from ananta.core.runtime.stack_dump import (  # noqa: E402
    STACK_DUMP_FILENAME,
    install_stack_dump_handler,
)

_failures: list[str] = []


def _check(condition: object, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL {label}")


def _wait_for(path: Path, needle: str) -> str:
    """The dump file's text once it contains ``needle`` (or after ~2 s)."""
    text = ""
    for _ in range(200):
        text = path.read_text(encoding="utf-8")
        if needle in text:
            break
        time.sleep(0.01)
    return text


def _check_markers(lines: list[str]) -> None:
    pid = f"pid={os.getpid()} at "
    _check(
        lines[0].startswith("=== stack-dump handler installed ") and pid in lines[0],
        f"install writes a header naming the pid and UTC time ({lines[0]!r})",
    )
    trailer = [ln for ln in lines if ln.startswith("=== SIGUSR1 stack dump above:")]
    _check(
        len(trailer) == 1 and pid in trailer[0] and trailer[0].rstrip(" =").endswith("+00:00"),
        f"each dump is marked with the pid and a UTC timestamp ({trailer})",
    )


def test_stack_dump_handler() -> None:
    print("\n[1] SIGUSR1 dumps every Python thread's stack to data/logs")
    with tempfile.TemporaryDirectory() as tmp:
        app_home = Path(tmp)
        path = install_stack_dump_handler(app_home)
        _check(
            path == app_home / "data" / "logs" / STACK_DUMP_FILENAME,
            f"dump file is the dedicated data/logs file ({path})",
        )
        _check(install_stack_dump_handler(app_home) == path, "a second install is idempotent")
        os.kill(os.getpid(), signal.SIGUSR1)
        text = _wait_for(path, "SIGUSR1 stack dump above")
        _check_markers(text.splitlines())
        _check("Current thread" in text, "the dump names the current thread")
        _check(
            "test_stack_dump_handler" in text,
            "the dump carries this smoke's own Python frame",
        )


def test_cli_installs_handler() -> None:
    cli_tree = ast.parse((REPO_ROOT / "ananta/src/ananta/cli.py").read_text(encoding="utf-8"))
    sync_main = next(
        node for node in ast.walk(cli_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "sync_main"
    )
    called = {
        node.func.id for node in ast.walk(sync_main)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    _check("install_stack_dump_handler" in called, "ananta.cli.sync_main installs the handler")


def _action(action_id: str, process_key: str) -> QueuedAction:
    return QueuedAction(
        id=action_id, process_key=process_key, parameters="{}", notes="", created_at="",
    )


def _poller(handler: Any) -> ActionQueuePoller:
    """A real ``ActionQueuePoller`` with only its I/O seams replaced."""
    poller = ActionQueuePoller.__new__(ActionQueuePoller)
    queue = [_action("ae-audit", "service_interface::knowledge_service::audit_retrieval_corpus"),
             _action("ae-boom", "plugin::default_scheduling_plugin::fail_action_event")]

    async def get_queued() -> list[QueuedAction]:
        return list(queue)

    failed: list[str] = []
    poller._get_queued_actions = get_queued  # type: ignore[method-assign]
    poller._mark_action_processing = lambda _action_id: True  # type: ignore[method-assign]
    poller._process_action = handler  # type: ignore[method-assign]
    poller._mark_action_failed = (  # type: ignore[method-assign]
        lambda action_id, _msg, error_detail=None: failed.append(action_id)
    )
    poller.total_actions_processed = 0
    poller._last_observed_queue_depth = 2
    return poller


def test_in_flight_fields() -> None:
    print("\n[2] health snapshot names the in-flight action, and clears it after")
    seen: dict[str, dict[str, object]] = {}

    async def handler(action: QueuedAction) -> None:
        seen[action.id] = ACTION_PATH_LIVENESS.snapshot()
        if action.id == "ae-boom":
            raise RuntimeError("handler failed")

    asyncio.run(_poller(handler)._poll_once())
    during = seen.get("ae-audit", {})
    _check(during.get("in_flight_action_id") == "ae-audit", f"in_flight_action_id set ({during})")
    _check(
        during.get("in_flight_process_key")
        == "service_interface::knowledge_service::audit_retrieval_corpus",
        "in_flight_process_key set",
    )
    _check(isinstance(during.get("in_flight_started_at"), str), "in_flight_started_at set")
    _check(isinstance(during.get("in_flight_age_seconds"), float), "in_flight_age_seconds set")
    _check(
        seen.get("ae-boom", {}).get("in_flight_action_id") == "ae-boom",
        "the next action replaces it",
    )
    after = ACTION_PATH_LIVENESS.snapshot()
    _check(
        after["in_flight_action_id"] is None
        and after["in_flight_process_key"] is None
        and after["in_flight_started_at"] is None
        and after["in_flight_age_seconds"] is None,
        f"cleared after the drain, even after a raising handler ({after})",
    )


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_slow_action_logged() -> None:
    print("\n[3] a slow single action is logged loudly; a fast one is not")
    capture = _Capture()
    poller_module.logger.addHandler(capture)
    real_monotonic = time.monotonic
    try:
        clock = [1000.0]
        time.monotonic = lambda: clock[0]  # type: ignore[assignment]

        async def handler(action: QueuedAction) -> None:
            clock[0] += SLOW_ACTION_THRESHOLD_SECONDS + 5 if action.id == "ae-audit" else 0.01

        asyncio.run(_poller(handler)._poll_once())
    finally:
        time.monotonic = real_monotonic
        poller_module.logger.removeHandler(capture)
    slow = [r.getMessage() for r in capture.records if r.getMessage().startswith("SLOW_ACTION")]
    _check(len(slow) == 1, f"exactly one SLOW_ACTION line ({slow})")
    _check(
        bool(slow) and "ae-audit" in slow[0] and "audit_retrieval_corpus" in slow[0],
        "it names the slow action and its process key",
    )


def main() -> int:
    print("Action-path diagnosability smoke (iss_30fb08fd, iss_e0648481)")
    test_stack_dump_handler()
    test_cli_installs_handler()
    test_in_flight_fields()
    test_slow_action_logged()
    if _failures:
        print(f"\nFAIL: {len(_failures)} check(s) failed")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("\nPASS: SIGUSR1 dumps stacks; health names the in-flight action; slow actions log loudly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
