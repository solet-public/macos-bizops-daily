#!/usr/bin/env python3
"""``trigger_poll`` dispatch-offload smoke (no solet/Postgres/LM Studio).

Locks the 2026-07-10 fleet-wide-dispatch-wedge fix: ``trigger_poll`` is the
cron HEARTBEAT for the singleton importer-poll drainer, NOT an inline pass. On
the action queue's serial ``_poll_once`` loop every dispatched handler is
awaited to completion, so an inline ``self._importer.poll_once()`` parks
*fleet-wide* ``process_call`` dispatch for the whole pass — and the first pass
after a new filesystem source becomes eligible is the source's ENTIRE history
(~1.2M events / ~18.9k sessions on 2026-07-10). The fix makes ``trigger_poll``
submit the whole pass to a single-slot :class:`BoundedSummaryExecutor`
(``_poll_executor``) and return in milliseconds — the third instance of the
already-reviewed heartbeat/drainer pattern (auto-summarize + LED-01
event-embedding are the other two).

Coverage:

  * STATIC (AST, deterministic — the primary red anchor): ``trigger_poll`` does
    NO inline ``poll_once`` and delegates via ``start_importer_poll_drain``;
    the inline ``poll_once`` lives in ``run_importer_poll_drain`` (drain
    thread only).
  * fast-return: with the importer's ``poll_once`` blocked, ``trigger_poll``
    still returns quickly with ``{"poller": "started"}`` (RED pre-fix: the call
    blocks until ``poll_once`` returns, and returns a count dict).
  * off-thread: the poll runs on a background daemon thread, never the thread
    that called ``trigger_poll`` (the action-queue thread in production).
  * singleton: a second fire while a drainer holds the single slot no-ops with
    ``{"poller": "already_running"}``.
  * slot-release-on-exception: a raising ``poll_once`` still frees the slot
    (executor ``finally``) so the next fire starts a fresh drainer.

Run:

    .venv/bin/python3 ananta/tests/llm/session_ledger/trigger_poll_offloads_drain_smoke.py
"""

from __future__ import annotations

import ast
import inspect
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

# The slot-release-on-exception case deliberately raises inside the drain; the
# single-slot executor logs that at its top-level background boundary. Silence it
# so the smoke output is clean — the assertions, not the logs, prove the behavior.
logging.getLogger(
    "ananta.services.session_ledger_service.summary_executor",
).setLevel(logging.CRITICAL)

from ananta.llm.session_ledger.types import ImporterReport  # noqa: E402
from ananta.services.session_ledger_service import (  # noqa: E402
    poll_drain as poll_drain_mod,
)
from ananta.services.session_ledger_service import service as svc_mod  # noqa: E402
from ananta.services.session_ledger_service.service import (  # noqa: E402
    SessionLedgerService,
)
from ananta.services.session_ledger_service.summary_executor import (  # noqa: E402
    BoundedSummaryExecutor,
)

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


class _BlockingImporter:
    """Importer stand-in whose ``poll_once`` blocks on an Event and records the
    thread it ran on — models the ~1.2M-event first pass that parked dispatch.

    ``raises=True`` makes the (released) pass raise, exercising the
    slot-release-on-exception contract of the single-slot executor.
    """

    def __init__(self, *, raises: bool = False) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.poll_threads: list[int] = []
        self.calls = 0
        self._raises = raises

    def poll_once(self) -> ImporterReport:
        self.calls += 1
        self.poll_threads.append(threading.get_ident())
        self.entered.set()
        self.release.wait(timeout=5.0)
        if self._raises:
            raise RuntimeError("simulated importer poll failure")
        return ImporterReport(
            sources_polled=1, sessions_seen=0, events_persisted=0, batches_failed=0,
        )


def _make_service(*, importer: Any, poll_executor: Any) -> SessionLedgerService:
    """Construct a SessionLedgerService stand-in bypassing heavy __init__.

    ``trigger_poll`` reads only ``_poll_executor`` + ``_importer``, so those are
    the only two seams the offload path touches.
    """
    instance = SessionLedgerService.__new__(SessionLedgerService)
    instance._importer = importer  # type: ignore[assignment]
    instance._poll_executor = poll_executor  # type: ignore[assignment]
    return instance


def _fire_async(service: SessionLedgerService) -> tuple[dict[str, Any], threading.Event]:
    """Call ``trigger_poll`` on a background thread so a pre-fix INLINE pass
    (which blocks, and may raise) surfaces as a bounded-wait timeout (a clean
    FAIL) instead of hanging the smoke or crashing the main thread. Records the
    caller thread + the return value; any exception is captured, never raised
    (a released pre-fix inline pass can raise on this wrapper's thread)."""
    holder: dict[str, Any] = {}
    returned = threading.Event()

    def _call() -> None:
        holder["caller_ident"] = threading.get_ident()
        try:
            holder["result"] = service.trigger_poll()
        except Exception as exc:  # noqa: BLE001 - test harness: record, never crash
            holder["error"] = exc
        finally:
            returned.set()

    threading.Thread(target=_call, daemon=True, name="trigger-poll-caller").start()
    return holder, returned


def _await_slot_free(service: SessionLedgerService) -> bool:
    """Bounded async retry until a fresh fire reports ``started`` (slot freed)."""
    for _ in range(40):
        holder, returned = _fire_async(service)
        if returned.wait(timeout=0.2) and holder.get("result") == {"poller": "started"}:
            return True
        time.sleep(0.05)
    return False


# ─── Cases ───────────────────────────────────────────────────────────────────


def test_static_trigger_poll_offloads_without_inline_poll() -> None:
    """AST guard (deterministic): the cron entry ``trigger_poll`` (service.py) must
    delegate to ``start_importer_poll_drain`` and contain NO inline ``poll_once``;
    the inline ``poll_once`` lives in ``run_importer_poll_drain`` (poll_drain module,
    drain thread)."""
    svc_src = Path(inspect.getfile(svc_mod)).read_text(encoding="utf-8")
    drain_src = Path(inspect.getfile(poll_drain_mod)).read_text(encoding="utf-8")
    bodies: dict[str, str] = {}
    for src in (svc_src, drain_src):
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.FunctionDef) and node.name in (
                "trigger_poll", "start_importer_poll_drain", "run_importer_poll_drain",
            ):
                bodies[node.name] = ast.get_source_segment(src, node) or ""

    entry = bodies.get("trigger_poll", "")
    starter = bodies.get("start_importer_poll_drain", "")
    drain = bodies.get("run_importer_poll_drain", "")
    _check(entry, "trigger_poll located in service.py")
    _check(starter, "start_importer_poll_drain located (poll_drain module)")
    _check(drain, "run_importer_poll_drain located (poll_drain module)")
    _check(
        "start_importer_poll_drain" in entry,
        "trigger_poll delegates to start_importer_poll_drain",
    )
    _check(
        "poll_once" not in entry,
        "trigger_poll does NO inline poll_once (never parks the action queue)",
    )
    _check(
        ".submit(" in starter,
        "start_importer_poll_drain submits the pass to the single-slot executor",
    )
    _check(
        "poll_once" in drain,
        "the inline importer poll_once lives in run_importer_poll_drain (drain thread)",
    )


def test_trigger_poll_returns_fast_and_reports_started() -> None:
    """With ``poll_once`` blocked, the cron entry still returns quickly with
    ``{"poller": "started"}`` and the pass runs on a NON-caller daemon thread."""
    importer = _BlockingImporter()
    service = _make_service(
        importer=importer, poll_executor=BoundedSummaryExecutor(name="ledger-poll-test"),
    )
    holder, returned = _fire_async(service)
    try:
        _check(
            returned.wait(timeout=1.0),
            "trigger_poll returns fast while poll_once is still blocked "
            "(off the dispatch path)",
        )
        _check(
            holder.get("result") == {"poller": "started"},
            f"first fire returns {{'poller': 'started'}} (got {holder.get('result')!r})",
        )
        _check(
            importer.entered.wait(timeout=2.0),
            "the importer poll actually started (on the drain thread)",
        )
        caller_ident = holder.get("caller_ident")
        _check(
            importer.poll_threads
            and all(t != caller_ident for t in importer.poll_threads),
            "poll_once ran on a background daemon thread, never the calling "
            "(action-queue) thread",
        )
    finally:
        importer.release.set()


def test_second_fire_is_already_running_singleton() -> None:
    """A second fire while a drainer holds the single slot no-ops with
    ``already_running``; the slot frees once the drain completes."""
    importer = _BlockingImporter()
    service = _make_service(
        importer=importer,
        poll_executor=BoundedSummaryExecutor(name="ledger-poll-singleton-test"),
    )
    h1, r1 = _fire_async(service)
    _check(
        r1.wait(timeout=1.0) and h1.get("result") == {"poller": "started"},
        f"first fire started the drainer (got {h1.get('result')!r})",
    )
    _check(
        importer.entered.wait(timeout=2.0), "drainer thread entered poll_once (holds slot)",
    )
    h2, r2 = _fire_async(service)
    _check(
        r2.wait(timeout=1.0) and h2.get("result") == {"poller": "already_running"},
        f"second fire no-ops while a drainer holds the slot (got {h2.get('result')!r})",
    )

    importer.release.set()
    _check(_await_slot_free(service), "slot frees for a fresh drainer once the drain completes")
    importer.release.set()


def test_slot_releases_after_poll_raises() -> None:
    """A raising ``poll_once`` still frees the slot (executor ``finally``) so the
    next fire starts a fresh drainer — failure isolation, no permanent wedge."""
    importer = _BlockingImporter(raises=True)
    service = _make_service(
        importer=importer,
        poll_executor=BoundedSummaryExecutor(name="ledger-poll-raise-test"),
    )
    h1, r1 = _fire_async(service)
    _check(
        r1.wait(timeout=1.0) and h1.get("result") == {"poller": "started"},
        f"first fire started the drainer (got {h1.get('result')!r})",
    )
    _check(importer.entered.wait(timeout=2.0), "drainer entered poll_once before raising")
    importer.release.set()  # let poll_once raise on the drain thread

    _check(
        _await_slot_free(service),
        "slot frees after poll_once raised — the executor never wedges at 0",
    )
    importer.release.set()


def main() -> int:
    print("=== trigger_poll_offloads_drain_smoke (dispatch offload, 2026-07-10) ===")
    test_static_trigger_poll_offloads_without_inline_poll()
    test_trigger_poll_returns_fast_and_reports_started()
    test_second_fire_is_already_running_singleton()
    test_slot_releases_after_poll_raises()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
