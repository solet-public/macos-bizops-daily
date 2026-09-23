#!/usr/bin/env python3
"""INF-02 completion-request queue + routing smoke (no pytest).

Proves the durable request/response queue behind
``InferenceService.submit_completion_request`` — the session-PRIMARY
routing precedence (operator-ruled: the live ``sys:autonomic`` holder is
the planning-completion lane; the bound provider is an OPTIONAL,
default-OFF fallback), the predicated-CAS lifecycle, and the INF-03
vacancy interplay:

* durable row shape (all declared columns; ``pending`` unassigned;
  schema-aware phantom-column rejection active for the NEW table — the
  slice-D fix-round class);
* size-capped ``messages`` payload — typed rejection, never truncation;
* stamp CAS wins exactly once; serve CAS is the idempotency gate
  (``served`` once, ``already_served`` after, ``unknown_request`` typed);
* requeue transitions: attempts increment + stamp clear; concurrent-serve
  ``lost_race``; terminal ``failed`` (+ reason) at the attempts cap — a
  completion never hangs pending forever;
* routing precedence: live capable holder → ``session`` (stamped +
  forwarded); forward fault → ``deferred`` with the stamp CLEARED (durable,
  never lost); capability-less holder → ``deferred``; vacant slot →
  ``deferred``; vacant slot + fallback env ON + bound provider →
  ``provider_fallback`` (NO row — the caller runs its own sync path);
  fallback ON but provider VACANT (INF-03) → still ``deferred``;
  structural fault (DEFAULT verdict) → ``deferred`` — never silent.

Offline: the shared REAL-SHAPE state fake (real provider envelopes,
mutations nested under ``data.result``), fake resolver/forwarder. No live
solet / LM Studio / Postgres.

Run from repo root:
    .venv/bin/python3 ananta/src/ananta/services/inference_service/tests/completion_request_smoke.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[6]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "tests"))

from _real_state_fake import (  # noqa: E402
    _STANDARDIZER_COLUMNS,
    RealShapeState,
)

from ananta.core.domain.types import ActionResult  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.services.inference_service import InferenceService  # noqa: E402
from ananta.services.inference_service.completion_request_queue import (  # noqa: E402
    REQUEUE_FAILED_TERMINAL,
    REQUEUE_LOST_RACE,
    REQUEUE_REQUEUED,
    SERVE_ALREADY_SERVED,
    SERVE_SERVED,
    SERVE_UNKNOWN_REQUEST,
    UNASSIGNED_HOLDER,
    forwarded_before,
    insert_completion_request,
    pending_stamped_requests,
    pending_unassigned_requests,
    requeue_stale_assignment,
    serve_completion_request,
    stamp_for_forward,
)
from ananta.services.inference_service.completion_request_schema import (  # noqa: E402
    COL_ATTEMPTS,
    COL_FAILURE_REASON,
    COL_HOLDER_AGENT_INSTANCE_ID,
    COL_REQUEST_ID,
    COL_RESULT_TEXT,
    COL_STATUS,
    INFERENCE_COMPLETION_REQUEST_NAMESPACE,
    MAX_REQUEUE_ATTEMPTS,
    MESSAGES_PAYLOAD_MAX_CHARS,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SERVED,
    TABLE_INFERENCE_COMPLETION_REQUEST,
    get_inference_completion_request_schema,
)
from ananta.services.inference_service.completion_routing import (  # noqa: E402
    COMPLETION_PROVIDER_FALLBACK_ENV,
    COMPLETION_ROUTED_DEFERRED,
    COMPLETION_ROUTED_PROVIDER_FALLBACK,
    COMPLETION_ROUTED_SESSION,
)
from ananta.services.inference_service.vertex_resolver import (  # noqa: E402
    VertexResolution,
    VertexRouting,
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


def _store() -> RealShapeState:
    """The shared real-shape fake, schema-enforced for the NEW table."""
    fake = RealShapeState()
    declared = frozenset(
        get_inference_completion_request_schema()
        .tables[TABLE_INFERENCE_COMPLETION_REQUEST]
        .columns,
    )
    fake._enforced_columns[TABLE_INFERENCE_COMPLETION_REQUEST] = (  # noqa: SLF001 — smoke wiring
        declared | _STANDARDIZER_COLUMNS
    )
    return fake


MESSAGES = [{"role": "system", "content": "plan"}, {"role": "user", "content": "go"}]
CORRELATION = {"context_id": "ctx-1", "playbook_id": "pb-1"}
RESUME_KEY = "service_interface::thinking_service::resume_thinking_completion"


def _insert(store: RealShapeState) -> str:
    return insert_completion_request(
        store,
        purpose="playbook_planning",
        resume_process_key=RESUME_KEY,
        correlation=CORRELATION,
        messages=MESSAGES,
    )


class _IncapableProvider:
    """A vertex provider WITHOUT the completion-forward capability.

    Satisfies ``VertexProvider`` structurally (the resolution dataclass's
    field type); the vertex methods are unreachable on the completion path.
    """

    def process_error(
        self, params: dict[str, object], state: dict[str, object],
    ) -> ActionResult:
        raise AssertionError("not routed here")

    def process_results(
        self, params: dict[str, object], state: dict[str, object],
    ) -> ActionResult:
        raise AssertionError("not routed here")


class _RecordingForwarder(_IncapableProvider):
    """CompletionForwarder + VertexProvider stand-in; records forwards."""

    def __init__(self, instance: str, *, raise_on_forward: bool = False) -> None:
        self._instance = instance
        self._raise = raise_on_forward
        self.forwards: list[dict[str, object]] = []

    @property
    def agent_instance_id(self) -> str:
        return self._instance

    def forward_completion_request(
        self,
        *,
        request_id: str,
        purpose: str,
        messages: list[dict[str, str]],
        correlation: dict[str, str],
    ) -> None:
        if self._raise:
            raise RuntimeError("bridge append raced disconnect")
        self.forwards.append(
            {
                "request_id": request_id,
                "purpose": purpose,
                "messages": messages,
                "correlation": correlation,
            },
        )


class _StubResolver:
    """Vertex resolver stub returning a crafted autonomic resolution."""

    def __init__(self, resolution: VertexResolution) -> None:
        self._resolution = resolution

    def resolve_autonomic(self) -> VertexResolution:
        return self._resolution


class _FakePluginManager:
    def get_plugin(self, name: str) -> None:
        return None


def _service(
    store: RealShapeState,
    resolution: VertexResolution,
    *,
    provider_name: str | None = "fake_provider",
) -> InferenceService:
    service = InferenceService(
        plugin_manager=_FakePluginManager(),  # type: ignore[arg-type]
        inference_plugin_name=provider_name,
        state_service=store,
    )
    service._vertex_resolver = _StubResolver(resolution)  # type: ignore[assignment]  # noqa: SLF001 — smoke seam
    return service


def _submit(service: InferenceService) -> dict[str, object]:
    return service.submit_completion_request(
        purpose="playbook_planning",
        messages=MESSAGES,
        resume_process_key=RESUME_KEY,
        correlation=CORRELATION,
    )


def _case_1() -> None:
    print("\n[1] durable row shape + schema-aware enforcement")
    store = _store()
    request_id = _insert(store)
    rows = store.rows(
        INFERENCE_COMPLETION_REQUEST_NAMESPACE, TABLE_INFERENCE_COMPLETION_REQUEST,
    )
    _check(len(rows) == 1, "insert lands exactly one durable row")
    row = rows[0]
    _check(row[COL_REQUEST_ID] == request_id, "row keyed by the minted request_id")
    _check(row[COL_STATUS] == STATUS_PENDING, "row starts pending")
    _check(
        row[COL_HOLDER_AGENT_INSTANCE_ID] == UNASSIGNED_HOLDER,
        "row starts unassigned",
    )
    _check(row[COL_ATTEMPTS] == 0, "attempts start at 0")
    phantom = store.write_state(
        INFERENCE_COMPLETION_REQUEST_NAMESPACE,
        {
            "table": TABLE_INFERENCE_COMPLETION_REQUEST,
            "record": {COL_REQUEST_ID: "icr-phantom", "no_such_column": 1},
        },
    )
    _check(
        phantom.get("action_status") != "completed",
        "schema-aware fake REJECTS a phantom column on the new table",
    )


def _case_2() -> None:
    print("\n[2] messages payload size cap — typed, never truncated")
    try:
        insert_completion_request(
            _store(),
            purpose="playbook_planning",
            resume_process_key=RESUME_KEY,
            correlation=CORRELATION,
            messages=[{"role": "user", "content": "x" * MESSAGES_PAYLOAD_MAX_CHARS}],
        )
        _check(False, "over-cap payload raises FrameworkError")
    except FrameworkError:
        _check(True, "over-cap payload raises FrameworkError")


def _case_3() -> None:
    """Sections [3] + [4] share one store — the serve CAS runs on the stamped row."""
    print("\n[3] stamp CAS wins exactly once")
    store = _store()
    request_id = _insert(store)
    _check(
        stamp_for_forward(store, request_id=request_id, holder_agent_instance_id="agi-a"),
        "first stamp wins",
    )
    _check(
        not stamp_for_forward(
            store, request_id=request_id, holder_agent_instance_id="agi-b",
        ),
        "second stamp loses (row already stamped)",
    )

    print("\n[4] serve CAS idempotency + unknown-request rejection")
    verdict, served_row = serve_completion_request(
        store, request_id=request_id, result_text="the plan",
    )
    _check(verdict == SERVE_SERVED, "first serve wins the CAS")
    _check(
        served_row is not None
        and served_row[COL_STATUS] == STATUS_SERVED
        and served_row[COL_RESULT_TEXT] == "the plan",
        "served row carries the completion text",
    )
    verdict, _ = serve_completion_request(
        store, request_id=request_id, result_text="different text",
    )
    _check(verdict == SERVE_ALREADY_SERVED, "second serve reports already_served")
    verdict, missing = serve_completion_request(
        store, request_id="icr-nope", result_text="x",
    )
    _check(
        verdict == SERVE_UNKNOWN_REQUEST and missing is None,
        "unknown request id is a typed rejection",
    )


def _case_5() -> None:
    print("\n[5] requeue transitions: clear / lost-race / terminal fail")
    store = _store()
    request_id = _insert(store)
    stamp_for_forward(store, request_id=request_id, holder_agent_instance_id="agi-dead")
    row = pending_stamped_requests(store)[0]
    outcome = requeue_stale_assignment(store, row=row, reason="holder death")
    _check(outcome == REQUEUE_REQUEUED, "stamped row re-queues")
    requeued = pending_unassigned_requests(store)[0]
    _check(
        requeued[COL_ATTEMPTS] == 1
        and requeued[COL_HOLDER_AGENT_INSTANCE_ID] == UNASSIGNED_HOLDER,
        "requeue clears the stamp and increments attempts",
    )
    stale_snapshot = dict(row)  # as-read copy from BEFORE the requeue
    outcome = requeue_stale_assignment(store, row=stale_snapshot, reason="race")
    _check(
        outcome == REQUEUE_LOST_RACE,
        "a stale as-read snapshot loses the requeue race cleanly",
    )
    for attempt in range(1, MAX_REQUEUE_ATTEMPTS):
        stamp_for_forward(
            store, request_id=request_id, holder_agent_instance_id=f"agi-{attempt}",
        )
        row = pending_stamped_requests(store)[0]
        outcome = requeue_stale_assignment(store, row=row, reason="serve timeout")
    _check(outcome == REQUEUE_FAILED_TERMINAL, "attempts cap fails terminally")
    failed_row = store.rows(
        INFERENCE_COMPLETION_REQUEST_NAMESPACE, TABLE_INFERENCE_COMPLETION_REQUEST,
    )[0]
    _check(
        failed_row[COL_STATUS] == STATUS_FAILED
        and bool(failed_row[COL_FAILURE_REASON]),
        "terminal row is failed WITH a failure_reason (loud, auditable)",
    )
    _check(
        forwarded_before({"forwarded_at": ""}, cutoff_iso="2026-01-01T00:00:00+00:00"),
        "a stamp with no forward time counts as timed out (anomaly must not pin)",
    )


def _case_6() -> None:
    print("\n[6] routing precedence — session-PRIMARY")
    os.environ.pop(COMPLETION_PROVIDER_FALLBACK_ENV, None)

    store = _store()
    forwarder = _RecordingForwarder("agi-holder")
    service = _service(
        store,
        VertexResolution(VertexRouting.PROVIDER, forwarder, "sys:autonomic", None),
    )
    verdict_map = _submit(service)
    _check(
        verdict_map.get("routing") == COMPLETION_ROUTED_SESSION,
        "live capable holder → session verdict",
    )
    _check(len(forwarder.forwards) == 1, "request forwarded exactly once")
    fwd = forwarder.forwards[0]
    _check(
        fwd["purpose"] == "playbook_planning"
        and fwd["messages"] == MESSAGES
        and fwd["correlation"] == CORRELATION,
        "forward carries purpose + messages + correlation verbatim",
    )
    stamped = pending_stamped_requests(store)
    _check(
        len(stamped) == 1
        and stamped[0][COL_HOLDER_AGENT_INSTANCE_ID] == "agi-holder",
        "durable row is stamped to the holder",
    )

    store = _store()
    raising = _RecordingForwarder("agi-holder", raise_on_forward=True)
    service = _service(
        store,
        VertexResolution(VertexRouting.PROVIDER, raising, "sys:autonomic", None),
    )
    verdict_map = _submit(service)
    _check(
        verdict_map.get("routing") == COMPLETION_ROUTED_DEFERRED,
        "forward fault → deferred verdict (durable, never lost)",
    )
    _check(
        len(pending_unassigned_requests(store)) == 1
        and not pending_stamped_requests(store),
        "forward fault clears the stamp — row waits unassigned",
    )

    store = _store()
    service = _service(
        store,
        VertexResolution(
            VertexRouting.PROVIDER, _IncapableProvider(), "sys:autonomic", None,
        ),
    )
    verdict_map = _submit(service)
    _check(
        verdict_map.get("routing") == COMPLETION_ROUTED_DEFERRED
        and len(pending_unassigned_requests(store)) == 1,
        "capability-less holder → deferred durable row",
    )

    store = _store()
    service = _service(
        store, VertexResolution(VertexRouting.DEFER, None, "sys:autonomic", None),
    )
    verdict_map = _submit(service)
    _check(
        verdict_map.get("routing") == COMPLETION_ROUTED_DEFERRED
        and len(pending_unassigned_requests(store)) == 1,
        "vacant slot → deferred durable row (fallback OFF by default)",
    )


def _case_7() -> None:
    print("\n[7] provider fallback — operator-enabled, provider-bound ONLY")
    os.environ[COMPLETION_PROVIDER_FALLBACK_ENV] = "1"
    try:
        store = _store()
        service = _service(
            store, VertexResolution(VertexRouting.DEFER, None, "sys:autonomic", None),
        )
        verdict_map = _submit(service)
        _check(
            verdict_map.get("routing") == COMPLETION_ROUTED_PROVIDER_FALLBACK,
            "fallback ON + bound provider → provider_fallback verdict",
        )
        _check(
            not store.rows(
                INFERENCE_COMPLETION_REQUEST_NAMESPACE,
                TABLE_INFERENCE_COMPLETION_REQUEST,
            ),
            "provider_fallback enqueues NO row (the caller serves synchronously)",
        )

        store = _store()
        service = _service(
            store,
            VertexResolution(VertexRouting.DEFER, None, "sys:autonomic", None),
            provider_name=None,  # INF-03 declared-VACANT provider
        )
        verdict_map = _submit(service)
        _check(
            verdict_map.get("routing") == COMPLETION_ROUTED_DEFERRED
            and len(pending_unassigned_requests(store)) == 1,
            "fallback ON but provider VACANT (INF-03) → still deferred durable",
        )

        store = _store()
        forwarder = _RecordingForwarder("agi-holder")
        service = _service(
            store,
            VertexResolution(VertexRouting.PROVIDER, forwarder, "sys:autonomic", None),
        )
        verdict_map = _submit(service)
        _check(
            verdict_map.get("routing") == COMPLETION_ROUTED_SESSION,
            "fallback ON never outranks a live holder (session stays PRIMARY)",
        )
    finally:
        os.environ.pop(COMPLETION_PROVIDER_FALLBACK_ENV, None)


def _case_8() -> None:
    print("\n[8] structural fault (DEFAULT verdict) → deferred, never silent")
    store = _store()
    service = _service(store, VertexResolution.default())
    verdict_map = _submit(service)
    _check(
        verdict_map.get("routing") == COMPLETION_ROUTED_DEFERRED
        and len(pending_unassigned_requests(store)) == 1,
        "unconfirmable slot → deferred durable row",
    )


class _SweeperRacingStore(RealShapeState):
    """Simulates the Reviewer-A F1 race: the serve-timeout sweeper thread
    stamps a fresh unassigned row in the window between the submit path's
    insert and its own stamp CAS (the drain forwarded the row first)."""

    def write_state(self, namespace: str, data: dict[str, object]) -> dict[str, object]:
        result = super().write_state(namespace, data)
        record = data.get("record")
        if (
            isinstance(record, dict)
            and str(data.get("table")) == TABLE_INFERENCE_COMPLETION_REQUEST
        ):
            stamp_for_forward(
                self,
                request_id=str(record[COL_REQUEST_ID]),
                holder_agent_instance_id="agi-sweeper",
            )
        return result


def _case_9() -> None:
    print("\n[9] F1 pin: a concurrent drain winning the fresh row's stamp "
          "never fails the submit")
    store = _SweeperRacingStore()
    declared = frozenset(
        get_inference_completion_request_schema()
        .tables[TABLE_INFERENCE_COMPLETION_REQUEST]
        .columns,
    )
    store._enforced_columns[TABLE_INFERENCE_COMPLETION_REQUEST] = (  # noqa: SLF001 — smoke wiring
        declared | _STANDARDIZER_COLUMNS
    )
    forwarder = _RecordingForwarder("agi-holder")
    service = _service(
        store,
        VertexResolution(VertexRouting.PROVIDER, forwarder, "sys:autonomic", None),
    )
    try:
        verdict_map = _submit(service)
    except FrameworkError:
        _check(False, "lost stamp race never raises (F1)")
        return
    _check(True, "lost stamp race never raises (F1)")
    _check(
        verdict_map.get("routing") == COMPLETION_ROUTED_SESSION
        and verdict_map.get("holder_agent_instance_id") == "agi-sweeper",
        "submit reports session with the DRAIN's holder (row is in flight)",
    )
    _check(
        not forwarder.forwards,
        "the losing submit path does NOT double-forward",
    )


def main() -> int:
    print("INF-02 completion-request queue + routing smoke")
    _case_1()
    _case_2()
    _case_3()
    _case_5()
    _case_6()
    _case_7()
    _case_8()
    _case_9()

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
