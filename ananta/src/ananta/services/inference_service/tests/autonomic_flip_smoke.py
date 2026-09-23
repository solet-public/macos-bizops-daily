#!/usr/bin/env python3
"""INF-01 sub-slice-2 ★ FLIP-ASSERTION smoke — vacancy must DEFER, never LOCAL.

The one guarantee this file exists to enforce (Day-ruled, the sub-slice-2
review centerpiece): once the ``sys:autonomic`` auto-assignment lifecycle
exists, a VACANT (or gone-holder) slot routes the organism's own
error/result turns into the durable NO-LOSS deferred queue — it must NEVER
fall through to the local default model. The sub-slice-1 interim
(vacant → DEFAULT → local) was the permanent-silent-qwen-on-vacancy defect
INF-01 exists to kill; if any future change reverts it, THIS SMOKE FAILS.

What stays LOCAL (the §D.3 safe floor, asserted here so the flip cannot
overreach either): the two STRUCTURAL fault edges where the slot is
unconfirmable — the agent_messaging plugin unreachable, and the slot
lookup RAISING. Deferring those would black-hole the turn.

Offline: fake plugin-manager / plugin / state collaborators, no live solet.

Run from repo root:
    .venv/bin/python3 ananta/src/ananta/services/inference_service/tests/autonomic_flip_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from _deferred_vertex_queue_fake import query_ordered_rows  # noqa: E402

from ananta.services.inference_service import InferenceService  # noqa: E402
from ananta.services.inference_service.vertex_resolver import (  # noqa: E402
    InferenceProviderResolver,
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


class _FakeProvider:
    """VertexProvider structural stand-in; counts routed calls."""

    def __init__(self, instance: str) -> None:
        self.instance = instance
        self.error_calls = 0
        self.result_calls = 0
        self.seen_states: list[dict[str, object]] = []

    def process_error(
        self, params: dict[str, object], state: dict[str, object],
    ) -> dict[str, object]:
        del params
        self.error_calls += 1
        self.seen_states.append(state)
        return self._envelope()

    def process_results(
        self, params: dict[str, object], state: dict[str, object],
    ) -> dict[str, object]:
        del params
        self.result_calls += 1
        self.seen_states.append(state)
        return self._envelope()

    def _envelope(self) -> dict[str, object]:
        return {
            "action_status": "completed",
            "data": {"delivered_to_vertex": self.instance},
            "actions": [],
            "error": None,
            "timestamp": "",
        }


class _FakePlugin:
    """The messaging-plugin subset the resolver touches on the autonomic path."""

    def __init__(
        self,
        *,
        autonomic_provider: _FakeProvider | None = None,
        raise_autonomic: bool = False,
    ) -> None:
        self._autonomic_provider = autonomic_provider
        self._raise_autonomic = raise_autonomic

    def resolve_role_to_instance(self, role: str) -> str | None:
        del role
        return None

    def get_inference_provider(self, agent_instance_id: str) -> _FakeProvider | None:
        del agent_instance_id
        return None

    def was_inference_provider_bound(self, agent_instance_id: str) -> bool:
        del agent_instance_id
        return False

    def get_autonomic_provider(self) -> _FakeProvider | None:
        if self._raise_autonomic:
            raise RuntimeError("simulated sys:autonomic lookup fault")
        return self._autonomic_provider


class _FakePluginManager:
    def __init__(self, plugin: object | None) -> None:
        self._plugin = plugin

    def get_plugin(self, plugin_name: str) -> object:
        if plugin_name == "agent_messaging_plugin" and self._plugin is not None:
            return self._plugin
        raise RuntimeError(f"plugin not found: {plugin_name}")


class _FakeState:
    """Flows read + an in-memory durable deferred-vertex table (real envelopes)."""

    def __init__(self, trigger_by_flow: dict[str, object]) -> None:
        self._trigger_by_flow = trigger_by_flow
        self.deferred_rows: list[dict[str, object]] = []

    def read_state(
        self, *, namespace: str, query: dict[str, object],
    ) -> dict[str, object]:
        del namespace
        filters = query.get("filters")
        flow_id = filters.get("id") if isinstance(filters, dict) else None
        if flow_id not in self._trigger_by_flow:
            return {"data": {"records": []}}
        return {"data": {"records": [{"trigger_data": self._trigger_by_flow[flow_id]}]}}

    def upsert_state(self, namespace: str, data: dict[str, object]) -> dict[str, object]:
        del namespace
        record = data.get("record")
        conflict = data.get("conflict_columns")
        if not isinstance(record, dict) or not isinstance(conflict, list):
            return {"action_status": "failed", "data": {}}
        self.deferred_rows = [
            row for row in self.deferred_rows
            if not all(row.get(col) == record.get(col) for col in conflict)
        ]
        self.deferred_rows.append({
            **record,
            "is_deleted": 0,
            "created_at": f"2026-09-15T00:00:00.{len(self.deferred_rows):06d}+00:00",
            "id": f"deferred-{len(self.deferred_rows)}",
        })
        return {"action_status": "completed", "data": {"result": {"upserted": 1}}}

    def query_state(self, namespace: str, filters: dict[str, object]) -> dict[str, object]:
        del namespace
        inner = filters.get("filters")
        want = inner if isinstance(inner, dict) else {}
        rows = [
            dict(row) for row in self.deferred_rows
            if all(row.get(key) == value for key, value in want.items())
        ]
        return {"action_status": "completed", "data": {"records": rows}}

    def query_ordered(self, namespace: str, data: dict[str, object]) -> dict[str, object]:
        """Model the bounded-read helper's ordered live-row pages."""
        del namespace
        return query_ordered_rows(self.deferred_rows, data)

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object],
    ) -> dict[str, object]:
        del namespace
        inner = query.get("filters")
        want = inner if isinstance(inner, dict) else {}
        n = 0
        for row in self.deferred_rows:
            if all(row.get(k) == v for k, v in want.items()):
                row.update(updates)
                n += 1
        return {"action_status": "completed", "data": {"result": {"updated": n}}}

    def delete_records(self, namespace: str, query: dict[str, object]) -> dict[str, object]:
        del namespace
        inner = query.get("filters")
        want = inner if isinstance(inner, dict) else {}
        before = len(self.deferred_rows)
        self.deferred_rows = [
            r for r in self.deferred_rows
            if not all(r.get(k) == v for k, v in want.items())
        ]
        return {
            "action_status": "completed",
            "data": {"result": {"deleted": before - len(self.deferred_rows)}},
        }


def _service(
    *, plugin: object | None, trigger_by_flow: dict[str, object],
) -> tuple[InferenceService, _FakeState]:
    state = _FakeState(trigger_by_flow)
    svc = InferenceService(
        plugin_manager=_FakePluginManager(plugin),  # type: ignore[arg-type]
        inference_plugin_name="fake_inference_plugin",
        state_service=state,
    )
    return svc, state


_UNTAGGED = {"source_namespace": "anything"}


def _flip_cases() -> None:
    print("★ FLIP — vacancy DEFERs, never LOCAL:")

    # F1 (resolver layer) — reachable plugin, NO holder → DEFER, role stamped.
    res = InferenceProviderResolver(
        plugin_manager=_FakePluginManager(_FakePlugin()),  # type: ignore[arg-type]
        state_service=_FakeState({}),
    ).resolve_autonomic()
    _check(
        res.routing is VertexRouting.DEFER and res.role == "sys:autonomic",
        "F1 resolve_autonomic vacant→DEFER(role=sys:autonomic) — NOT DEFAULT",
    )
    _check(
        res.routing is not VertexRouting.DEFAULT,
        "F1b the sub-slice-1 vacant→DEFAULT interim is DEAD",
    )

    # F2 (service layer, process_error) — untagged flow + vacant slot →
    # deferred no-op with a durable sys:autonomic row; the local default
    # transaction is NOT reached (it would raise on the fake plugin manager).
    svc, state = _service(plugin=_FakePlugin(), trigger_by_flow={"fe": _UNTAGGED})
    out = svc.process_error({"model": {}}, {"flow_id": "fe"})
    data = out.get("data")
    _check(
        isinstance(data, dict) and data.get("vertex_deferred") is True,
        "F2 process_error untagged+vacant→vertex_deferred no-op (never local)",
    )
    _check(
        any(
            row.get("role") == "sys:autonomic" and row.get("flow_id") == "fe"
            for row in state.deferred_rows
        ),
        "F2b deferral recorded DURABLY under role=sys:autonomic (drain key)",
    )

    # F3 (service layer, process_results) — same guarantee on the result edge.
    svc, state = _service(plugin=_FakePlugin(), trigger_by_flow={"fr": _UNTAGGED})
    out = svc.process_results({"model": {}}, {"flow_id": "fr"})
    data = out.get("data")
    _check(
        isinstance(data, dict) and data.get("vertex_deferred") is True
        and any(row.get("flow_id") == "fr" for row in state.deferred_rows),
        "F3 process_results untagged+vacant→vertex_deferred (durable)",
    )

    # F4 — a LIVE holder still routes PROVIDER (the flip does not overreach).
    holder = _FakeProvider("agi-holder")
    svc, _ = _service(
        plugin=_FakePlugin(autonomic_provider=holder),
        trigger_by_flow={"fp": _UNTAGGED},
    )
    svc.process_error({"model": {}}, {"flow_id": "fp"})
    _check(
        holder.error_calls == 1,
        "F4 live holder→PROVIDER routing unchanged by the flip",
    )


def _safe_floor_cases() -> None:
    print("§D.3 safe floor — structural faults stay LOCAL:")

    # F5 — messaging plugin UNREACHABLE → _route_vertex None (local floor).
    svc, state = _service(plugin=None, trigger_by_flow={"fu": _UNTAGGED})
    routed = svc._route_vertex(  # noqa: SLF001 — the smoke asserts the routing seam
        is_error=True, state={"flow_id": "fu"}, params={"model": {}},
    )
    _check(
        routed is None and state.deferred_rows == [],
        "F5 plugin unreachable→None (LOCAL floor; nothing falsely queued)",
    )

    # F6 — slot lookup RAISES → None (local floor) + fault-degrade counted.
    svc, state = _service(
        plugin=_FakePlugin(raise_autonomic=True), trigger_by_flow={"fx": _UNTAGGED},
    )
    routed = svc._route_vertex(  # noqa: SLF001 — the smoke asserts the routing seam
        is_error=False, state={"flow_id": "fx"}, params={"model": {}},
    )
    _check(
        routed is None and state.deferred_rows == [],
        "F6 slot lookup raises→None (LOCAL floor; unconfirmable ≠ vacant)",
    )
    _check(
        getattr(svc, "_autonomic_fault_degrade_turns", 0) == 1,
        "F6b fault-degrade turn COUNTED (telemetry)",
    )


def _cold_context_cases() -> None:
    print("§D.4 cold-context forward:")

    # F7 — offline the pipeline factory is unbuildable (no orchestrator) →
    # LOUD degrade: the forward still happens, WITHOUT the assembled key.
    holder = _FakeProvider("agi-h")
    svc, _ = _service(
        plugin=_FakePlugin(autonomic_provider=holder),
        trigger_by_flow={"fc": _UNTAGGED},
    )
    svc.process_error({"model": {}}, {"flow_id": "fc"})
    _check(
        holder.error_calls == 1
        and "autonomic_assembled_context" not in holder.seen_states[0],
        "F7 assembly fault→forward proceeds WITHOUT assembled context (floor)",
    )

    # F7b — when assembly succeeds, the assembled messages ride the forward
    # state under the documented key (wiring; assembly fidelity is live-path).
    holder2 = _FakeProvider("agi-h2")
    svc2, _ = _service(
        plugin=_FakePlugin(autonomic_provider=holder2),
        trigger_by_flow={"fd": _UNTAGGED},
    )
    assembled = {"messages": [{"role": "system", "content": "ctx"}]}
    svc2._assemble_cold_context = (  # type: ignore[method-assign]  # noqa: SLF001 — smoke stubs the seam
        lambda **_kw: assembled
    )
    svc2.process_results({"model": {}}, {"flow_id": "fd"})
    forwarded = holder2.seen_states[0]
    _check(
        forwarded.get("autonomic_assembled_context") == assembled
        and forwarded.get("flow_id") == "fd",
        "F7b assembled context rides the forward state (raw refs intact)",
    )


def main() -> int:
    print("=== INF-01 ★ flip-assertion smoke (vacancy→DEFER, never LOCAL) ===")
    _flip_cases()
    _safe_floor_cases()
    _cold_context_cases()
    total = _passed + len(_failed)
    print(f"\n{_passed}/{total} checks passed")
    if _failed:
        print("FAILED:")
        for label in _failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
