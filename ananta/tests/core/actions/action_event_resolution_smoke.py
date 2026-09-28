#!/usr/bin/env python3
"""Smoke for ``fail_action_event`` — the sanctioned per-row resolution (iss_6069cf22).

Before this verb nothing compliant could resolve a stuck ``action_events`` row,
so the 2026-09-28 action-path stall (iss_30fb08fd) could only be waited out or
SIGKILLed — and a SIGKILL re-armed the orphan reaper's requeue loop.

This smoke asserts, against an in-memory store that honours the filters the
module actually sends (id, status, and the ``created_at`` evidence floor):

  (1) accept: a ``processing`` row and a ``queued`` row are each failed, with
      the reason in ``error_message`` and the previous status reported;
  (2) refuse: an empty reason, a finished (``completed``) row, a missing row,
      a preserved evidence row below ``EVIDENCE_FLOOR_CREATED_AT`` (excluded
      IN the read's filter, so it is never even returned), and a row whose
      status changed between the read and the write;
  (3) the evidence row is byte-for-byte untouched after every attempt;
  (4) wiring: the scheduling plugin exposes ``fail_action_event`` as a
      NON-discoverable EDGE ``@platform_process`` (operator tool, review N3),
      declares its ``EdgeProcessDefinition``, and ships the matching process
      JSON; the plugin method turns a refusal into an ERROR response and a
      success into COMPLETED;
  (5) review N1: the poller's failure write is guarded on the row's expected
      status, so a late failure from an old process cannot overwrite a row
      ``fail_action_event`` already failed; a failure expecting ``completed``
      fails a row this process completed, and the pre-claim oversized path
      still fails its ``queued`` row;
  (6) review R1: the REAL ``_process_action``, with result processing raising
      after its own completed write (the live over-bound bridge delivery),
      leaves the row ``failed`` with that reason -- not ``completed`` with an
      error result (the #9 split envelope).

Every fake state result is built with the postgres state plugin's own
``create_success_result`` (review B1): the verb once read ``data.updated``
where the live seam puts ``data.result.updated``, and a hand-written fake with
the wrong layer let that pass.

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:
    .venv/bin/python3 ananta/tests/core/actions/action_event_resolution_smoke.py
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_scheduling_plugin" / "src"))

from ananta.core.actions.action_event_resolution import (  # noqa: E402
    ActionEventResolutionError,
    fail_action_event,
)
from ananta.core.actions.action_queue_poller import (  # noqa: E402
    ActionQueuePoller,
    QueuedAction,
)
from ananta.core.actions.orphan_reaper import EVIDENCE_FLOOR_CREATED_AT  # noqa: E402
from ananta.core.domain.enums import ProcessorPolicyCategory  # noqa: E402
from ananta.core.plugins.plugin_contracts import ActionStatus  # noqa: E402
from default_scheduling_plugin.plugin import SchedulingPlugin  # noqa: E402


def _load_real_result_helpers() -> Any:
    """The postgres state plugin's own ``create_success_result``, loaded by file.

    B1 (review uev_60f9a739): a hand-written fake envelope looser than the real
    seam hid a wrong-layer read. Building every fake result with the live
    plugin's constructor keeps this smoke on the real shape
    ``{action_status, data: {namespace, result: {updated}}}``. Loaded by path so
    the plugin package's ``__init__`` (psycopg, pools) is never imported.
    """
    path = (
        REPO_ROOT
        / "plugins/postgres_state_management_plugin/src/postgres_state_management_plugin"
        / "result_helpers.py"
    )
    spec = importlib.util.spec_from_file_location("_pg_result_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_RESULTS = _load_real_result_helpers()

_failures: list[str] = []


def _check(condition: object, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL {label}")


def _matches(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    for column, expected in filters.items():
        value = row.get(column)
        if isinstance(expected, dict):
            op, bound = expected["op"], expected["value"]
            if op == "gt" and not value > bound:
                return False
            if op not in {"gt"}:
                raise AssertionError(f"unexpected filter op {op!r}")
        elif value != expected:
            return False
    return True


class _Store:
    """In-memory ``action_events`` honouring the module's filters."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = {row["id"]: row for row in rows}
        self.flip_before_update: tuple[str, str] | None = None
        self.reads: list[dict[str, Any]] = []

    def query_ordered(self, namespace: str, data: dict[str, object]) -> dict[str, object]:
        assert namespace == "core" and data["table"] == "action_events"
        filters = data["filters"]
        assert isinstance(filters, dict)
        self.reads.append(filters)
        found = [copy.deepcopy(r) for r in self.rows.values() if _matches(r, filters)]
        return _RESULTS.create_success_result({"records": found})

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object],
    ) -> dict[str, object]:
        assert namespace == "core" and query["table"] == "action_events"
        if self.flip_before_update is not None:
            action_id, status = self.flip_before_update
            self.rows[action_id]["status"] = status
        filters = query["filters"]
        assert isinstance(filters, dict)
        hit = [r for r in self.rows.values() if _matches(r, filters)]
        for row in hit:
            row.update(updates)
        return _RESULTS.create_success_result(
            {"namespace": namespace, "result": {"updated": len(hit)}},
        )


_RECENT = datetime(2026, 9, 28, 3, 24, 43)
_EVIDENCE = EVIDENCE_FLOOR_CREATED_AT - timedelta(days=3)


def _rows() -> list[dict[str, Any]]:
    return [
        {"id": "ae-proc", "status": "processing", "created_at": _RECENT,
         "process_key": "service_interface::knowledge_service::audit_retrieval_corpus"},
        {"id": "ae-queued", "status": "queued", "created_at": _RECENT, "process_key": "p::q"},
        {"id": "ae-done", "status": "completed", "created_at": _RECENT, "process_key": "p::d"},
        {"id": "ae-2m3q4msgmiekh", "status": "processing", "created_at": _EVIDENCE,
         "process_key": "plugin::agent_messaging_plugin::deliver_result", "parameters": "x" * 64},
    ]


def _refused(store: _Store, action_id: str, reason: str, needle: str, label: str) -> None:
    try:
        fail_action_event(store, action_id=action_id, reason=reason)
    except ActionEventResolutionError as exc:
        _check(needle in str(exc), f"{label} ({exc})")
    else:
        _check(False, f"{label} — was accepted")


def test_accept_paths() -> None:
    print("\n[1] accept: processing and queued rows are failed with the reason")
    store = _Store(_rows())
    result = fail_action_event(store, action_id="ae-proc", reason="  requeue loop, iss_30fb08fd ")
    _check(result["previous_status"] == "processing", f"reports previous status ({result})")
    _check(store.rows["ae-proc"]["status"] == "failed", "processing row is now failed")
    _check(
        store.rows["ae-proc"]["error_message"]
        == "failed by fail_action_event (was processing): requeue loop, iss_30fb08fd",
        f"error_message carries the stripped reason ({store.rows['ae-proc']['error_message']!r})",
    )
    queued = fail_action_event(store, action_id="ae-queued", reason="never run this")
    _check(
        queued["previous_status"] == "queued" and store.rows["ae-queued"]["status"] == "failed",
        "queued row is now failed",
    )
    _check(
        all(read.get("created_at", {}).get("value") == EVIDENCE_FLOOR_CREATED_AT for read in store.reads),
        "every read carries the evidence floor IN the filter",
    )


def test_refuse_paths() -> None:
    print("\n[2] refuse: reason, finished, missing, evidence, concurrent change")
    store = _Store(_rows())
    evidence_before = copy.deepcopy(store.rows["ae-2m3q4msgmiekh"])
    _refused(store, "ae-proc", "   ", "reason is required", "an empty reason is refused")
    _refused(store, "ae-done", "x", "not queued or processing", "a completed row is refused")
    _refused(store, "ae-nope", "x", "not found above the evidence floor", "a missing row is refused")
    _refused(
        store, "ae-2m3q4msgmiekh", "x", "preserved incident-evidence row",
        "a preserved evidence row is refused (read as not found)",
    )
    _check(store.rows["ae-proc"]["status"] == "processing", "a refused call wrote nothing")
    store.flip_before_update = ("ae-proc", "completed")
    _refused(store, "ae-proc", "x", "changed state after it was read", "a concurrent change is refused")
    _check(store.rows["ae-proc"]["status"] == "completed", "the concurrent writer's status stands")
    _check(
        store.rows["ae-2m3q4msgmiekh"] == evidence_before,
        "the evidence row is untouched after every attempt",
    )


def test_plugin_wiring() -> None:
    print("\n[3] wiring: non-discoverable EDGE platform process, edge definition, JSON, responses")
    method = SchedulingPlugin.fail_action_event
    meta: Any = None
    for attr in ("_platform_process_metadata", "_process_metadata", "_action_metadata"):
        meta = getattr(method, attr, None)
        if meta is not None:
            break
    meta_dict = meta if isinstance(meta, dict) else getattr(meta, "__dict__", {})
    _check(bool(meta_dict), "fail_action_event carries @platform_process metadata")
    _check(
        meta_dict.get("processor_policy_category") == ProcessorPolicyCategory.EDGE,
        f"it is EDGE (got {meta_dict.get('processor_policy_category')!r})",
    )
    _check(
        meta_dict.get("is_discoverable") is False,
        "it is NOT discoverable: an operator recovery tool called by key (review N3)",
    )

    plugin = SchedulingPlugin()
    _check(
        "fail_action_event" in plugin.get_edge_process_definitions(),
        "an EdgeProcessDefinition is declared for it",
    )
    json_path = (
        REPO_ROOT / "plugins/default_scheduling_plugin/knowledge_base/processes/fail_action_event.json"
    )
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    _check(
        doc.get("process_key") == "plugin::default_scheduling_plugin::fail_action_event",
        "the process JSON names the canonical key",
    )

    store = _Store(_rows())
    plugin.state_service = store  # type: ignore[assignment]
    ok = plugin.fail_action_event(params={"action_id": "ae-queued", "reason": "operator"}, state={})
    _check(
        ok.get("action_status") == ActionStatus.COMPLETED.value and ok.get("data", {}).get("status") == "failed",
        f"success is a COMPLETED response carrying the resolution ({ok})",
    )
    bad = plugin.fail_action_event(params={"action_id": "ae-done", "reason": "x"}, state={})
    _check(
        bad.get("action_status") == ActionStatus.ERROR.value
        and "not queued or processing" in str(bad.get("error", {}).get("message")),
        f"a refusal is an ERROR response with the reason ({bad})",
    )


def test_poller_failed_write_is_guarded() -> None:
    print("\n[4] N1: the poller's failure write is guarded on the row's expected status")
    store = _Store(_rows())
    poller = ActionQueuePoller.__new__(ActionQueuePoller)
    poller.state_service = store  # type: ignore[assignment]
    # A late failure from an OLD process for a row already resolved here.
    fail_action_event(store, action_id="ae-proc", reason="orphaned by a SIGKILL")
    poller._update_action_status_to_failed("ae-proc", "late failure from the old colour")  # pyright: ignore[reportPrivateUsage]
    _check(
        store.rows["ae-proc"]["error_message"].startswith("failed by fail_action_event"),
        "a late failure expecting processing leaves a row fail_action_event failed",
    )
    # This process's own completed-then-failed path expects 'completed' (R1).
    poller._update_action_status_to_failed(  # pyright: ignore[reportPrivateUsage]
        "ae-done", "result processing raised", expected_status=ActionStatus.COMPLETED,
    )
    _check(
        store.rows["ae-done"]["status"] == "failed"
        and store.rows["ae-done"]["error_message"] == "result processing raised",
        "a failure expecting completed fails the row this process completed",
    )
    poller._update_action_status_to_failed(  # pyright: ignore[reportPrivateUsage]
        "ae-queued", "oversized", expected_status=ActionStatus.QUEUED,
    )
    _check(
        store.rows["ae-queued"]["status"] == "failed",
        "the pre-claim oversized path still fails a queued row",
    )


_OVER_BOUND = (
    "Action payload refused at enqueue: 19.0 MiB over the 16.0 MiB bound"
)


class _SuccessProcessor:
    def execute_action(self, action: QueuedAction) -> dict[str, object]:
        _ = action
        return {"success": True, "data": {"ok": True}}


def _raise_over_bound(action_id: str) -> None:
    raise RuntimeError(_OVER_BOUND)


def test_completed_then_failed_ends_failed() -> None:
    print("\n[5] R1: the REAL _process_action fails a row it completed when result processing raises")
    store = _Store(_rows())
    store.rows["ae-live"] = {
        "id": "ae-live", "status": "processing", "created_at": _RECENT,
        "process_key": "plugin::agent_messaging_plugin::deliver_result",
    }
    poller = ActionQueuePoller.__new__(ActionQueuePoller)
    poller.state_service = store  # type: ignore[assignment]
    poller.action_processor = _SuccessProcessor()  # type: ignore[assignment]
    # Only the seams around the path under test are replaced: the status
    # writes, _process_action's branch logic and _mark_action_completed's
    # step ordering are the shipped code.
    poller._is_terminated_flow_sibling = lambda _a: False  # type: ignore[method-assign]
    poller._prepare_action_for_execution = lambda _a: True  # type: ignore[method-assign]
    poller._resolve_io_context = lambda _a: None  # type: ignore[method-assign]
    poller._requires_main_thread = lambda _k: True  # type: ignore[method-assign]
    poller._read_stale_error_message = lambda _i: None  # type: ignore[method-assign]
    # Step 2 of _mark_action_completed (after the status write) raises, as the
    # live over-bound bridge delivery did (ae-2ppdpjk823r7d, 2026-09-27).
    poller._retrieve_action_details = _raise_over_bound  # type: ignore[method-assign,assignment]
    poller._retrieve_failed_action_details = lambda _i: None  # type: ignore[method-assign]
    action = QueuedAction(
        id="ae-live", process_key="plugin::agent_messaging_plugin::deliver_result",
        parameters="{}", notes="", created_at="",
    )
    asyncio.run(poller._process_action(action))  # pyright: ignore[reportPrivateUsage]
    row = store.rows["ae-live"]
    _check(row["status"] == "failed", f"the row ends failed, not completed ({row['status']!r})")
    _check(
        row.get("error_message") == _OVER_BOUND,
        f"with the result-processing failure as its reason ({row.get('error_message')!r})",
    )


def main() -> int:
    print("fail_action_event smoke (iss_6069cf22)")
    test_accept_paths()
    test_refuse_paths()
    test_plugin_wiring()
    test_poller_failed_write_is_guarded()
    test_completed_then_failed_ends_failed()
    if _failures:
        print(f"\nFAIL: {len(_failures)} check(s) failed")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("\nPASS: fail_action_event accepts queued/processing, refuses the rest, spares evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
