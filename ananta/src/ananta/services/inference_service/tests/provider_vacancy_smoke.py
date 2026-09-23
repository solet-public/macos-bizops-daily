#!/usr/bin/env python3
"""INF-03 provider-vacancy smoke — providerless boot is a first-class state.

The guarantees this file enforces (INF-03, operator-ruled 2026-07-03):

1. ``InferenceService`` CONSTRUCTS with no bound provider and no
   ``ANANTA_INFERENCE_PROVIDER`` env override — the declared-VACANT state.
   The old constructor ``ValueError`` was the single boot-time enforcement
   of the mandatory binding (the reason ``mock_inference_plugin`` existed);
   if it ever returns, THIS SMOKE FAILS and the cloud profile is
   un-bootable without the mock hack again.
2. Any provider-touching operation on a VACANT service raises the TYPED
   vacancy error (stable token ``inference_service_vacant``) — never
   ``get_plugin(None)``, never silence.
3. Vertex turns on a VACANT service still route through the resolver:
   an untagged flow with a vacant ``sys:autonomic`` slot lands in the
   durable deferred queue exactly as with a bound provider (the INF-01
   flip is provider-independent).
4. The structural fault edge (slot unconfirmable) — which falls to the
   LOCAL default on a provider-ful box (§D.3 safe floor) — raises the
   typed vacancy error on a VACANT box: loud, never a silent turn loss.
5. Bound-name and env-override construction behave exactly as before.

Offline: fake plugin-manager / plugin / state collaborators, no live solet.

Run from repo root:
    .venv/bin/python3 ananta/src/ananta/services/inference_service/tests/provider_vacancy_smoke.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from _deferred_vertex_queue_fake import query_ordered_rows  # noqa: E402

from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.services.inference_service import (  # noqa: E402
    VACANT_PROVIDER_TOKEN,
    InferenceService,
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


class _FakeVertexProvider:
    """Live-session VertexProvider stand-in; counts routed calls."""

    def __init__(self) -> None:
        self.error_calls = 0

    def process_error(
        self, params: dict[str, object], state: dict[str, object],
    ) -> dict[str, object]:
        del params, state
        self.error_calls += 1
        return {
            "action_status": "completed",
            "data": {"delivered_to_vertex": "live-autonomic-holder"},
            "actions": [],
            "error": None,
            "timestamp": "",
        }

    def process_results(
        self, params: dict[str, object], state: dict[str, object],
    ) -> dict[str, object]:
        return self.process_error(params, state)


class _FakePlugin:
    """The messaging-plugin subset the resolver touches on the autonomic path."""

    def __init__(
        self,
        *,
        raise_autonomic: bool = False,
        autonomic_provider: _FakeVertexProvider | None = None,
    ) -> None:
        self._raise_autonomic = raise_autonomic
        self._autonomic_provider = autonomic_provider

    def resolve_role_to_instance(self, role: str) -> str | None:
        del role
        return None

    def get_inference_provider(self, agent_instance_id: str) -> object | None:
        del agent_instance_id
        return None

    def was_inference_provider_bound(self, agent_instance_id: str) -> bool:
        del agent_instance_id
        return False

    def get_autonomic_provider(self) -> object | None:
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
    """Flows read + an in-memory durable deferred-vertex table."""

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


def _vacant_service(
    *, plugin: object | None, trigger_by_flow: dict[str, object],
) -> tuple[InferenceService, _FakeState]:
    state = _FakeState(trigger_by_flow)
    svc = InferenceService(
        plugin_manager=_FakePluginManager(plugin),  # type: ignore[arg-type]
        inference_plugin_name=None,
        state_service=state,
    )
    return svc, state


_UNTAGGED = {"source_namespace": "anything"}


def _construction_cases() -> None:
    print("V1 — declared-vacant construction:")
    try:
        svc, _ = _vacant_service(plugin=_FakePlugin(), trigger_by_flow={})
    except (ValueError, FrameworkError) as exc:
        _check(False, f"V1 vacant construction must not raise (got {exc!r})")
        return
    _check(True, "V1 constructs with no binding and no env override")
    _check(
        svc.get_inference_provider() is None,
        "V1b no provider is resolved on a vacant service",
    )


def _typed_touch_cases() -> None:
    print("V2 — provider touch raises the TYPED vacancy error:")
    svc, _ = _vacant_service(plugin=_FakePlugin(), trigger_by_flow={})
    try:
        svc._ensure_provider_ready()  # noqa: SLF001 — the funnel every provider op uses
    except FrameworkError as exc:
        _check(
            VACANT_PROVIDER_TOKEN in str(exc),
            "V2 _ensure_provider_ready raises FrameworkError carrying the vacancy token",
        )
    except Exception as exc:  # noqa: BLE001
        _check(False, f"V2 wrong exception type on vacant provider touch: {exc!r}")
    else:
        _check(False, "V2 vacant provider touch must raise")


def _vertex_routing_cases() -> None:
    print("V3 — vertex turns still route durably under vacancy:")
    svc, state = _vacant_service(plugin=_FakePlugin(), trigger_by_flow={"fv": _UNTAGGED})
    out = svc.process_error({"model": {}}, {"flow_id": "fv"})
    data = out.get("data")
    _check(
        isinstance(data, dict) and data.get("vertex_deferred") is True,
        "V3 untagged flow + vacant slot + VACANT provider → vertex_deferred no-op",
    )
    _check(
        any(row.get("role") == "sys:autonomic" for row in state.deferred_rows),
        "V3b durable sys:autonomic deferred row written (NO-LOSS queue)",
    )


def _structural_fault_cases() -> None:
    print("V4 — structural fault edge is LOUD-typed under vacancy, never silent:")
    svc, _ = _vacant_service(
        plugin=_FakePlugin(raise_autonomic=True), trigger_by_flow={"ff": _UNTAGGED},
    )
    try:
        svc.process_error({"model": {}}, {"flow_id": "ff"})
    except FrameworkError as exc:
        _check(
            VACANT_PROVIDER_TOKEN in str(exc),
            "V4 fault-edge turn on VACANT provider raises the typed vacancy error",
        )
    except Exception as exc:  # noqa: BLE001
        _check(False, f"V4 wrong exception type on fault edge: {exc!r}")
    else:
        _check(False, "V4 fault edge on VACANT provider must raise (silence forbidden)")


def _bound_behavior_cases() -> None:
    print("V5 — bound-name and env-override behavior unchanged:")
    svc = InferenceService(
        plugin_manager=_FakePluginManager(_FakePlugin()),  # type: ignore[arg-type]
        inference_plugin_name="fake_inference_plugin",
        state_service=_FakeState({}),
    )
    _check(
        svc._inference_plugin_name == "fake_inference_plugin",  # noqa: SLF001
        "V5 explicit binding is stored verbatim",
    )
    os.environ["ANANTA_INFERENCE_PROVIDER"] = "env_named_plugin"
    try:
        svc_env = InferenceService(
            plugin_manager=_FakePluginManager(_FakePlugin()),  # type: ignore[arg-type]
            inference_plugin_name=None,
            state_service=_FakeState({}),
        )
        _check(
            svc_env._inference_plugin_name == "env_named_plugin",  # noqa: SLF001
            "V5b ANANTA_INFERENCE_PROVIDER fallback still wins over vacancy",
        )
    finally:
        del os.environ["ANANTA_INFERENCE_PROVIDER"]


def _goal_state_cases() -> None:
    print("V6 — GOAL STATE: vacant provider + LIVE sys:autonomic holder → forward:")
    holder = _FakeVertexProvider()
    svc, _ = _vacant_service(
        plugin=_FakePlugin(autonomic_provider=holder),
        trigger_by_flow={"fg": _UNTAGGED},
    )
    out = svc.process_error({"model": {}}, {"flow_id": "fg"})
    data = out.get("data")
    _check(
        isinstance(data, dict)
        and data.get("delivered_to_vertex") == "live-autonomic-holder",
        "V6 organism turn forwards to the live autonomic holder (no raise)",
    )
    _check(holder.error_calls == 1, "V6b holder received exactly one routed call")


def _vacant_config_cases() -> None:
    print("V7 — get_context_management_config serves the vacant-state constant (B1 pin):")
    from ananta.services.context_management.config import (
        VACANT_PROVIDER_CONTEXT_CONFIG,
    )

    svc, _ = _vacant_service(plugin=_FakePlugin(), trigger_by_flow={})
    try:
        cfg = svc.get_context_management_config()
    except Exception as exc:  # noqa: BLE001
        _check(False, f"V7 config fetch must not raise under vacancy (got {exc!r})")
        return
    _check(
        cfg is VACANT_PROVIDER_CONTEXT_CONFIG,
        "V7 the boot call (:1015 class) receives the ONE vacant-state constant",
    )
    _check(
        cfg.discovery_min_similarity_threshold == 0.5
        and cfg.supports_compaction is False
        and cfg.warming_enabled is False,
        "V7b constant carries the platform threshold + honest capability flags",
    )


def _warn_once_cases() -> None:
    print("V8 — the loud vacancy warning fires ONCE across construction sites:")
    import ananta.services.inference_service as inference_module

    inference_module._vacancy_warning_emitted = False  # noqa: SLF001 — test isolation
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture(level=logging.WARNING)
    inference_module.logger.addHandler(handler)
    try:
        _vacant_service(plugin=_FakePlugin(), trigger_by_flow={})
        _vacant_service(plugin=_FakePlugin(), trigger_by_flow={})
    finally:
        inference_module.logger.removeHandler(handler)
    vacancy_warnings = [
        r for r in records
        if r.levelno == logging.WARNING and "VACANT" in r.getMessage()
    ]
    _check(
        len(vacancy_warnings) == 1,
        f"V8 exactly one vacancy WARNING across two constructions (got {len(vacancy_warnings)})",
    )


def main() -> int:
    os.environ.pop("ANANTA_INFERENCE_PROVIDER", None)
    _construction_cases()
    _typed_touch_cases()
    _vertex_routing_cases()
    _structural_fault_cases()
    _bound_behavior_cases()
    _goal_state_cases()
    _vacant_config_cases()
    _warn_once_cases()
    print(f"\nprovider_vacancy_smoke: {_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
