#!/usr/bin/env python3
"""Phase 5 (Seam B) — inference vertex resolver + InferenceService routing smoke.

Exercises the ◆R2 three-way discriminator (role-resolvability, sharpened by
Coordinator-Day to explicit-binding-this-lifetime) and the InferenceService
short-circuit above ``_execute_transaction`` — entirely offline, with fake
plugin / state / provider collaborators.

Governing rule under test: NEVER route an explicitly-bound-this-lifetime
vertex to the default local model; DEFAULT only for never-bound-this-lifetime
or untagged flows.

Resolver cases:
  R1  role tag resolves to a LIVE provider → PROVIDER (role wins over a stale
      instance tag — proves resolve-by-role, not by ephemeral instance).
  R2  instance-only tag with a live provider → PROVIDER (case 3a).
  R3  role tag, binding resolvable, holder has NO live provider → DEFER (case 2).
  R4  role tag, binding VACANT → DEFER (role-tagged never falls to default).
  R5  instance-only tag, no provider, tombstoned (bound-then-gone) → DEFER (3b).
  R6  instance-only tag, no provider, never bound → DEFAULT (case 3c; the
      v4 §8 streamable / post-restart shape).
  R7  no vertex tags (source_namespace only) → DEFAULT (case 3d).
  R8  no flow_id in state → DEFAULT.
  R9  flow row absent (no trigger_data) → DEFAULT.
  R10 agent_messaging_plugin unavailable → DEFAULT (degrade, don't black-hole).
  R11 trigger_data stored as a JSON string parses → resolves (PROVIDER).
  R12 role-tagged, role→instance lookup RAISES → DEFER (N3; role-bound never
      crashes or falls to default).
  R13 instance-only, provider lookup RAISES → DEFAULT (N3; roleless binding
      unconfirmable, don't black-hole never-bound flows).

InferenceService routing cases:
  IS1 process_error, PROVIDER → provider.process_error called; result carries
      delivered_to_vertex; the default transaction is NOT run.
  IS2 process_results, PROVIDER → provider.process_results called.
  IS3 process_error, DEFER → no-op COMPLETED result (vertex_deferred, empty
      actions); get_deferred_vertices() records the deferral keyed by role.
  IS4 ★FLIP: untagged flow + vacant sys:autonomic → DEFER (durable), never
      the local default (sub-slice-2 killed the vacant→LOCAL interim).
  IS4b messaging plugin unreachable → _route_vertex returns None (LOCAL
      safe floor — the §D.3 hard edge survives the flip).

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run from repo root:
    .venv/bin/python3 ananta/src/ananta/services/inference_service/tests/inference_vertex_resolver_smoke.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from _deferred_vertex_queue_fake import query_ordered_rows  # noqa: E402

from ananta.services.inference_service import InferenceService  # noqa: E402
from ananta.services.inference_service.vertex_resolver import (  # noqa: E402
    InferenceProviderResolver,
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


class _FakeProvider:
    """Stand-in for SessionInferenceProvider (VertexProvider structural type)."""

    def __init__(self, instance: str) -> None:
        self.instance = instance
        self.error_calls = 0
        self.result_calls = 0

    def process_error(
        self, params: dict[str, object], state: dict[str, object],
    ) -> dict[str, object]:
        del params, state
        self.error_calls += 1
        return self._envelope("bridge_delivery_error")

    def process_results(
        self, params: dict[str, object], state: dict[str, object],
    ) -> dict[str, object]:
        del params, state
        self.result_calls += 1
        return self._envelope("bridge_delivery_result")

    def _envelope(self, event_type: str) -> dict[str, object]:
        return {
            "action_status": "completed",
            "data": {"delivered_to_vertex": self.instance, "event_type": event_type},
            "actions": [],
            "error": None,
            "timestamp": "",
        }


class _FakePlugin:
    """The AgentMessagingPlugin subset the resolver depends on."""

    def __init__(
        self,
        *,
        role_map: dict[str, str] | None = None,
        providers: dict[str, _FakeProvider] | None = None,
        tombstones: set[str] | None = None,
        raise_role: bool = False,
        raise_provider: bool = False,
        autonomic_provider: _FakeProvider | None = None,
        raise_autonomic: bool = False,
    ) -> None:
        self._role_map = role_map or {}
        self._providers = providers or {}
        self._tombstones = tombstones or set()
        self._raise_role = raise_role
        self._raise_provider = raise_provider
        self._autonomic_provider = autonomic_provider
        self._raise_autonomic = raise_autonomic

    def resolve_role_to_instance(self, role: str) -> str | None:
        if self._raise_role:
            raise RuntimeError("simulated agent_role_binding read fault")
        return self._role_map.get(role)

    def get_inference_provider(self, agent_instance_id: str) -> _FakeProvider | None:
        if self._raise_provider:
            raise RuntimeError("simulated sidecar lookup fault")
        return self._providers.get(agent_instance_id)

    def was_inference_provider_bound(self, agent_instance_id: str) -> bool:
        return agent_instance_id in self._tombstones

    def get_autonomic_provider(self) -> _FakeProvider | None:
        if self._raise_autonomic:
            raise RuntimeError("simulated sys:autonomic resolution fault")
        return self._autonomic_provider


class _FakePluginManager:
    def __init__(self, plugin: object | None) -> None:
        self._plugin = plugin

    def get_plugin(self, plugin_name: str) -> object:
        if plugin_name == "agent_messaging_plugin" and self._plugin is not None:
            return self._plugin
        raise RuntimeError(f"plugin not found: {plugin_name}")


class _FakeState:
    """read_state over a flows-row map + an in-memory durable deferred-vertex
    table (upsert_state / query_state) that models the NO-LOSS queue.

    ``upsert_state`` is idempotent on the conflict columns (``flow_id``);
    ``query_state`` applies the inner equality filters. Both return the real
    ActionResult envelope shapes (``action_status=completed`` + ``data.records``
    / ``data.result``) so ``require_records`` / ``require_completed`` exercise
    the same fail-loud path they take against the postgres provider.
    """

    def __init__(self, trigger_by_flow: dict[str, object]) -> None:
        self._trigger_by_flow = trigger_by_flow
        self._deferred_rows: list[dict[str, object]] = []
        self._next_id = 0

    def read_state(
        self, *, namespace: str, query: dict[str, object],
    ) -> dict[str, object]:
        del namespace
        filters = query.get("filters")
        flow_id = filters.get("id") if isinstance(filters, dict) else None
        if flow_id not in self._trigger_by_flow:
            return {"data": {"records": []}}
        trigger = self._trigger_by_flow[flow_id]
        return {"data": {"records": [{"trigger_data": trigger}]}}

    def upsert_state(self, namespace: str, data: dict[str, object]) -> dict[str, object]:
        del namespace
        record = data.get("record")
        conflict = data.get("conflict_columns")
        if not isinstance(record, dict) or not isinstance(conflict, list):
            return {"action_status": "failed", "data": {}}
        self._deferred_rows = [
            row for row in self._deferred_rows
            if not all(row.get(col) == record.get(col) for col in conflict)
        ]
        stored: dict[str, object] = {
            **record,
            "is_deleted": 0,
            "created_at": f"2026-09-15T00:00:00.{self._next_id:06d}+00:00",
            "id": f"dfv-{self._next_id}",
        }
        self._next_id += 1
        self._deferred_rows.append(stored)
        return {"action_status": "completed", "data": {"result": {"upserted": 1}}}

    def query_state(self, namespace: str, filters: dict[str, object]) -> dict[str, object]:
        del namespace
        inner = filters.get("filters")
        want = inner if isinstance(inner, dict) else {}
        rows = [
            dict(row) for row in self._deferred_rows
            if all(row.get(key) == value for key, value in want.items())
        ]
        return {"action_status": "completed", "data": {"records": rows}}

    def query_ordered(self, namespace: str, data: dict[str, object]) -> dict[str, object]:
        """Model the bounded-read helper's ordered live-row pages."""
        del namespace
        return query_ordered_rows(self._deferred_rows, data)

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object],
    ) -> dict[str, object]:
        del namespace
        inner = query.get("filters")
        want = inner if isinstance(inner, dict) else {}
        n = 0
        for row in self._deferred_rows:
            if all(row.get(k) == v for k, v in want.items()):
                row.update(updates)
                n += 1
        return {"action_status": "completed", "data": {"result": {"updated": n}}}

    def delete_records(self, namespace: str, query: dict[str, object]) -> dict[str, object]:
        del namespace
        inner = query.get("filters")
        want = inner if isinstance(inner, dict) else {}
        before = len(self._deferred_rows)
        self._deferred_rows = [
            r for r in self._deferred_rows
            if not all(r.get(k) == v for k, v in want.items())
        ]
        return {
            "action_status": "completed",
            "data": {"result": {"deleted": before - len(self._deferred_rows)}},
        }


def _resolver(
    *, plugin: object | None, trigger_by_flow: dict[str, object],
) -> InferenceProviderResolver:
    return InferenceProviderResolver(
        plugin_manager=_FakePluginManager(plugin),  # type: ignore[arg-type]
        state_service=_FakeState(trigger_by_flow),
    )


def _resolver_cases() -> None:
    print("Resolver discriminator:")

    # R1 — role resolves to a live provider; stale instance tag ignored.
    live = _FakeProvider("agi-current")
    res = _resolver(
        plugin=_FakePlugin(
            role_map={"Claude-B": "agi-current"}, providers={"agi-current": live},
        ),
        trigger_by_flow={
            "f1": {"inference_vertex_role": "Claude-B",
                   "inference_vertex_session_id": "agi-stale"},
        },
    ).resolve({"flow_id": "f1"})
    _check(
        res.routing is VertexRouting.PROVIDER
        and res.provider is live
        and res.agent_instance_id == "agi-current",
        "R1 role→live provider (role wins over stale instance tag)",
    )

    # R2 — instance-only tag, live provider → PROVIDER (3a).
    p2 = _FakeProvider("agi-2")
    res = _resolver(
        plugin=_FakePlugin(providers={"agi-2": p2}),
        trigger_by_flow={"f2": {"inference_vertex_session_id": "agi-2"}},
    ).resolve({"flow_id": "f2"})
    _check(
        res.routing is VertexRouting.PROVIDER and res.provider is p2,
        "R2 instance-only→live provider (3a)",
    )

    # R3 — role resolvable, holder absent → DEFER (2).
    res = _resolver(
        plugin=_FakePlugin(role_map={"Claude-B": "agi-3"}, providers={}),
        trigger_by_flow={
            "f3": {"inference_vertex_role": "Claude-B",
                   "inference_vertex_session_id": "agi-old"},
        },
    ).resolve({"flow_id": "f3"})
    _check(
        res.routing is VertexRouting.DEFER and res.agent_instance_id == "agi-3",
        "R3 role holder absent→DEFER (never Qwen)",
    )

    # R4 — role tag, binding vacant → DEFER (role-tagged never defaults).
    res = _resolver(
        plugin=_FakePlugin(role_map={}, providers={}),
        trigger_by_flow={
            "f4": {"inference_vertex_role": "Ghost",
                   "inference_vertex_session_id": "agi-4"},
        },
    ).resolve({"flow_id": "f4"})
    _check(
        res.routing is VertexRouting.DEFER and res.role == "Ghost",
        "R4 role tag vacant binding→DEFER (not default)",
    )

    # R5 — instance-only, no provider, tombstoned → DEFER (3b).
    res = _resolver(
        plugin=_FakePlugin(providers={}, tombstones={"agi-5"}),
        trigger_by_flow={"f5": {"inference_vertex_session_id": "agi-5"}},
    ).resolve({"flow_id": "f5"})
    _check(
        res.routing is VertexRouting.DEFER and res.agent_instance_id == "agi-5",
        "R5 instance bound-then-gone (tombstone)→DEFER (3b)",
    )

    # R6 — instance-only, no provider, never bound → DEFAULT (3c / streamable).
    res = _resolver(
        plugin=_FakePlugin(providers={}, tombstones=set()),
        trigger_by_flow={"f6": {"inference_vertex_session_id": "agi-6"}},
    ).resolve({"flow_id": "f6"})
    _check(
        res.routing is VertexRouting.DEFAULT,
        "R6 instance never-bound-this-lifetime→DEFAULT (3c, v4 §8 streamable)",
    )

    # R7 — no vertex tags → DEFAULT (3d).
    res = _resolver(
        plugin=_FakePlugin(),
        trigger_by_flow={"f7": {"source_namespace": "agent_messaging_plugin"}},
    ).resolve({"flow_id": "f7"})
    _check(res.routing is VertexRouting.DEFAULT, "R7 untagged flow→DEFAULT (3d)")

    # R8 — no flow_id in state → DEFAULT.
    res = _resolver(plugin=_FakePlugin(), trigger_by_flow={}).resolve({})
    _check(res.routing is VertexRouting.DEFAULT, "R8 no flow_id→DEFAULT")

    # R9 — flow row absent → DEFAULT.
    res = _resolver(plugin=_FakePlugin(), trigger_by_flow={}).resolve(
        {"flow_id": "missing"},
    )
    _check(res.routing is VertexRouting.DEFAULT, "R9 flow row absent→DEFAULT")

    # R10 — messaging plugin unavailable → DEFAULT (degrade, not black-hole).
    res = _resolver(
        plugin=None,
        trigger_by_flow={"f10": {"inference_vertex_role": "Claude-B"}},
    ).resolve({"flow_id": "f10"})
    _check(
        res.routing is VertexRouting.DEFAULT,
        "R10 messaging plugin unavailable→DEFAULT",
    )

    # R11 — trigger_data stored as a JSON string parses and resolves.
    p11 = _FakeProvider("agi-11")
    res = _resolver(
        plugin=_FakePlugin(providers={"agi-11": p11}),
        trigger_by_flow={
            "f11": json.dumps({"inference_vertex_session_id": "agi-11"}),
        },
    ).resolve({"flow_id": "f11"})
    _check(
        res.routing is VertexRouting.PROVIDER and res.provider is p11,
        "R11 JSON-string trigger_data parses→PROVIDER",
    )

    # R12 — N3: role-tagged flow, role→instance lookup RAISES → DEFER (loud),
    # never crash/default (role-bound path).
    res = _resolver(
        plugin=_FakePlugin(raise_role=True),
        trigger_by_flow={"f12": {"inference_vertex_role": "Claude-B"}},
    ).resolve({"flow_id": "f12"})
    _check(
        res.routing is VertexRouting.DEFER and res.role == "Claude-B",
        "R12 role lookup raises→DEFER (N3, role-bound never crashes/defaults)",
    )

    # R13 — N3: instance-only flow, provider lookup RAISES → DEFAULT (loud),
    # binding unconfirmable for a roleless instance.
    res = _resolver(
        plugin=_FakePlugin(raise_provider=True),
        trigger_by_flow={"f13": {"inference_vertex_session_id": "agi-13"}},
    ).resolve({"flow_id": "f13"})
    _check(
        res.routing is VertexRouting.DEFAULT,
        "R13 instance lookup raises→DEFAULT (N3, roleless unconfirmable)",
    )


def _autonomic_cases() -> None:
    print("sys:autonomic fault-edge resolver:")

    # RA1 — no session holds sys:autonomic → DEFER (★ sub-slice-2 FLIP:
    # auto-assignment keeps the slot filled, so a vacancy is a transient
    # window that queues durably; vacant→LOCAL was the sub-slice-1 interim
    # and is DEAD — the dedicated flip-assertion smoke guards this too).
    res = _resolver(plugin=_FakePlugin(), trigger_by_flow={}).resolve_autonomic()
    _check(
        res.routing is VertexRouting.DEFER and res.role == "sys:autonomic",
        "RA1 sys:autonomic vacant→DEFER (★ sub-slice-2 flip, never LOCAL)",
    )

    # RA2 — a session holds sys:autonomic with a live provider → PROVIDER (the
    # dormant fault-edge live path; sub-slice-2 assignment activates it).
    holder = _FakeProvider("agi-autonomic")
    res = _resolver(
        plugin=_FakePlugin(autonomic_provider=holder), trigger_by_flow={},
    ).resolve_autonomic()
    _check(
        res.routing is VertexRouting.PROVIDER
        and res.provider is holder
        and res.role == "sys:autonomic",
        "RA2 sys:autonomic live holder→PROVIDER (role=sys:autonomic)",
    )

    # RA3 — messaging plugin unreachable → DEFAULT (§D.3 HARD EDGE: resolving
    # the slot needs the same plugin; stays LOCAL, never black-holes).
    res = _resolver(plugin=None, trigger_by_flow={}).resolve_autonomic()
    _check(
        res.routing is VertexRouting.DEFAULT,
        "RA3 messaging plugin unreachable→DEFAULT (hard edge: stays LOCAL)",
    )

    # RA4 — sys:autonomic resolution RAISES → DEFAULT (loud degrade; the
    # fault-edge safe floor never crashes the organism's turn).
    res = _resolver(
        plugin=_FakePlugin(raise_autonomic=True), trigger_by_flow={},
    ).resolve_autonomic()
    _check(
        res.routing is VertexRouting.DEFAULT,
        "RA4 sys:autonomic lookup raises→DEFAULT (loud degrade, safe floor)",
    )


def _inference_service(
    *, plugin: object | None, trigger_by_flow: dict[str, object],
) -> InferenceService:
    svc = InferenceService(
        plugin_manager=_FakePluginManager(plugin),  # type: ignore[arg-type]
        inference_plugin_name="fake_inference_plugin",
        state_service=_FakeState(trigger_by_flow),
    )
    return svc


def _routing_case_is1_provider_error() -> None:
    prov = _FakeProvider("agi-a")
    svc = _inference_service(
        plugin=_FakePlugin(
            role_map={"Claude-B": "agi-a"}, providers={"agi-a": prov},
        ),
        trigger_by_flow={"e1": {"inference_vertex_role": "Claude-B"}},
    )
    out = svc.process_error({"model": {}}, {"flow_id": "e1"})
    data1 = out.get("data")
    delivered = data1.get("delivered_to_vertex") if isinstance(data1, dict) else None
    _check(
        prov.error_calls == 1 and delivered == "agi-a",
        "IS1 process_error PROVIDER→provider.process_error, delivered_to_vertex",
    )


def _routing_case_is2_provider_results() -> None:
    prov2 = _FakeProvider("agi-b")
    svc = _inference_service(
        plugin=_FakePlugin(providers={"agi-b": prov2}),
        trigger_by_flow={"r1": {"inference_vertex_session_id": "agi-b"}},
    )
    out = svc.process_results({"model": {}}, {"flow_id": "r1"})
    data2 = out.get("data")
    delivered = data2.get("delivered_to_vertex") if isinstance(data2, dict) else None
    _check(
        prov2.result_calls == 1 and delivered == "agi-b",
        "IS2 process_results PROVIDER→provider.process_results",
    )


def _routing_case_is3_defer() -> None:
    svc = _inference_service(
        plugin=_FakePlugin(role_map={"Claude-B": "agi-gone"}, providers={}),
        trigger_by_flow={
            "e2": {"inference_vertex_role": "Claude-B"},
            "e2b": {"inference_vertex_role": "Claude-B"},
        },
    )
    out = svc.process_error({"model": {}}, {"flow_id": "e2"})
    deferred = svc.get_deferred_vertices()
    data3 = out.get("data")
    record = deferred.get("e2", {})
    deferred_ok = (
        isinstance(data3, dict)
        and data3.get("vertex_deferred") is True
        and record.get("method") == "process_error"
    )
    _check(
        deferred_ok and out.get("actions") == [] and out.get("action_status") == "completed",
        "IS3 DEFER→no-op COMPLETED + durably recorded (keyed by flow_id)",
    )
    _check(record.get("flow_id") == "e2", "IS3 DEFER record captures flow_id (N2 re-drive hook)")
    _check(record.get("role") == "Claude-B", "IS3 DEFER record captures the bound role")
    stamp = out.get("timestamp")
    _check(
        isinstance(stamp, str) and stamp != "",
        "IS3 DEFER result carries a real (non-empty) timestamp (N4)",
    )
    # NO-LOSS (INF-01 §D.9): a SECOND distinct flow deferred to the SAME role
    # must NOT evict the first — the pre-INF-01 last-writer-per-role register
    # lost N−1. Durable queue → all N retained + enumerable.
    svc.process_error({"model": {}}, {"flow_id": "e2b"})
    deferred2 = svc.get_deferred_vertices()
    _check(
        "e2" in deferred2 and "e2b" in deferred2 and len(deferred2) == 2,
        "IS3 NO-LOSS: two flows deferred to one role both retained (N flows → N rows)",
    )
    # Idempotent: re-defer of the SAME flow_id upserts (no duplicate row).
    svc.process_error({"model": {}}, {"flow_id": "e2"})
    deferred3 = svc.get_deferred_vertices()
    _check(
        len(deferred3) == 2,
        "IS3 idempotent: re-defer of same flow_id → upsert, not a duplicate row",
    )


def _routing_case_is3b_defer_no_flow_id() -> None:
    # A DEFER with NO flow_id is unrecoverable (no re-drive key) → logged loud +
    # dropped from the durable queue; the flow still terminates cleanly.
    svc = _inference_service(plugin=_FakePlugin(), trigger_by_flow={})
    out = svc._record_deferred_vertex(  # noqa: SLF001 — asserts the None-flow_id guard
        is_error=True,
        resolution=VertexResolution(VertexRouting.DEFER, None, "sys:autonomic", None),
        flow_id=None,
    )
    _check(
        out.get("action_status") == "completed" and out.get("actions") == [],
        "IS3b DEFER no flow_id → no-op COMPLETED (flow still terminates)",
    )
    _check(
        svc.get_deferred_vertices() == {},
        "IS3b DEFER no flow_id → UNRECOVERABLE, dropped from durable queue (not silent-persisted)",
    )


def _routing_case_is4_flip_vacant_defers() -> None:
    # ★ SUB-SLICE-2 FLIP: an untagged (DEFAULT-verdict) flow with a REACHABLE
    # messaging plugin and a VACANT sys:autonomic slot DEFERs durably — it
    # must NEVER fall to the local default model (the dead sub-slice-1
    # interim). The dedicated autonomic_flip_smoke guards this too.
    svc = _inference_service(
        plugin=_FakePlugin(),
        trigger_by_flow={"e3": {"source_namespace": "agent_messaging_plugin"}},
    )
    out = svc.process_error({"model": {}}, {"flow_id": "e3"})
    data4 = out.get("data")
    deferred = svc.get_deferred_vertices().get("e3", {})
    _check(
        isinstance(data4, dict)
        and data4.get("vertex_deferred") is True
        and deferred.get("role") == "sys:autonomic",
        "IS4 ★FLIP untagged + vacant sys:autonomic→DEFER (durable, never LOCAL)",
    )


def _routing_case_is4b_unreachable_stays_local() -> None:
    # §D.3 HARD EDGE (unchanged by the flip): messaging plugin UNREACHABLE →
    # _route_vertex returns None (caller runs the LOCAL default) — the slot
    # is structurally unconfirmable, so deferring would black-hole the turn.
    svc = _inference_service(
        plugin=None,
        trigger_by_flow={"e4": {"source_namespace": "agent_messaging_plugin"}},
    )
    routed = svc._route_vertex(  # noqa: SLF001 — smoke asserts the routing seam
        is_error=True, state={"flow_id": "e4"}, params={"model": {}},
    )
    _check(
        routed is None,
        "IS4b plugin unreachable→None (LOCAL safe floor, §D.3 hard edge)",
    )


def _routing_case_is5_default_to_autonomic() -> None:
    # DEFAULT flow (untagged) + a LIVE sys:autonomic holder → _route_vertex
    # routes the fault-edge to the autonomic provider. Proves the (dormant)
    # call-site works once a holder exists; with no holder it returns None (IS4).
    holder = _FakeProvider("agi-autonomic")
    svc = _inference_service(
        plugin=_FakePlugin(autonomic_provider=holder),
        trigger_by_flow={"a1": {"source_namespace": "x"}},  # untagged → DEFAULT
    )
    out = svc.process_error({"model": {}}, {"flow_id": "a1"})
    data5 = out.get("data")
    delivered = data5.get("delivered_to_vertex") if isinstance(data5, dict) else None
    _check(
        holder.error_calls == 1 and delivered == "agi-autonomic",
        "IS5 DEFAULT + live sys:autonomic holder→routed to autonomic provider",
    )


def _routing_cases() -> None:
    print("InferenceService routing:")
    _routing_case_is1_provider_error()
    _routing_case_is2_provider_results()
    _routing_case_is3_defer()
    _routing_case_is3b_defer_no_flow_id()
    _routing_case_is4_flip_vacant_defers()
    _routing_case_is4b_unreachable_stays_local()
    _routing_case_is5_default_to_autonomic()


def main() -> int:
    print("=== inference vertex resolver + routing smoke ===")
    _resolver_cases()
    _autonomic_cases()
    _routing_cases()
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
