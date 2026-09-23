#!/usr/bin/env python3
"""Slice-A smoke for role-model v4 (no pytest, no DB).

Slice A is the ADDITIVE foundation of the first-class role model (design
workbench/2026-07-02_first_class_role_model_design.md): the two fresh tables
(`role` entity + discriminated `role_binding`), the v4 resolver with the §4.6
typed per-holder_kind parse + the §4.3 ResolvedRole projection (holder_kind /
holder_identity / agent_session_id), and the §5.5/§7 entity upsert + best-effort
memory ingest. NON-BREAKING: the live `resolve_role_binding` + core readers stay
on `agent_role_binding` until the §9 migration (slice-D) — this only builds +
proves the new paths.

Covers: both tables standardize/install; a session holder resolves with the
typed identity + projected agent_session_id; typed parse FAILS LOUD (missing
required field, unknown holder_kind); a provider holder resolves with empty
session fields; vacant raises; a JSON-string holder_identity (SQLite path) is
coerced; the entity upsert round-trips + is idempotent; the ingest is best-effort
(never gates); role names are OPAQUE throughout.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/role_model_v4_slice_a_smoke.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.interfaces.state_management_interface import (  # noqa: E402
    StateManagementInterface,
)
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
    COL_AGENT_INSTANCE_ID,
    COL_AGENT_SESSION_ID,
    COL_CLAIM_EPOCH,
    COL_HOLDER_IDENTITY,
    COL_HOLDER_KIND,
    COL_ROLE,
    HOLDER_KIND_INFERENCE_PROVIDER,
    HOLDER_KIND_SESSION,
    TABLE_ROLE,
    TABLE_ROLE_BINDING,
    role_binding_external_id,
)
from ananta.services.store import open_store  # noqa: E402

from agent_messaging_plugin.role_binding_store import (  # noqa: E402
    RoleBindingMalformedError,
    RoleBindingVacantError,
    ingest_role_entity,
    resolve_role_binding_v4,
    upsert_role_entity,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    get_role_binding_schema,
    get_role_schema,
)

# Arbitrary, operator-defined-shaped role — proves opacity (never special-cased).
_ARBITRARY_ROLE = "zz-Ω arbitrary/role #7!"

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


def _state() -> StateManagementInterface:
    return cast(StateManagementInterface, RealShapeState())


def _seed_binding(
    state: StateManagementInterface,
    *,
    role: str,
    holder_kind: str,
    agent_instance_id: str = "",
    agent_session_id: str = "",
    holder_identity: object = None,
    claim_epoch: int = 0,
) -> None:
    state.upsert_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_ROLE_BINDING,
            "record": {
                "external_id": role_binding_external_id(role),
                COL_ROLE: role,
                COL_HOLDER_KIND: holder_kind,
                COL_AGENT_INSTANCE_ID: agent_instance_id,
                COL_AGENT_SESSION_ID: agent_session_id,
                COL_HOLDER_IDENTITY: {} if holder_identity is None else holder_identity,
                COL_CLAIM_EPOCH: claim_epoch,
            },
            "conflict_columns": ["external_id"],
        },
    )


def test_schemas_install() -> None:
    for label, sch in [("role", get_role_schema()), ("role_binding", get_role_binding_schema())]:
        open_store(sch, namespace=AGENT_ROLE_BINDING_NAMESPACE, backend="in_memory")
        _check(True, f"schema {label} standardizes + installs (in_memory)")


def test_resolve_session_holder() -> None:
    state = _state()
    _seed_binding(
        state,
        role=_ARBITRARY_ROLE,
        holder_kind=HOLDER_KIND_SESSION,
        agent_instance_id="agi-1",
        agent_session_id="sess-1",
        holder_identity={"agent_id": "claude_code", "session_label": "lbl"},
    )
    r = resolve_role_binding_v4(state, _ARBITRARY_ROLE)
    _check(r.holder_kind == HOLDER_KIND_SESSION, "session resolve: holder_kind=session")
    _check(
        r.agent_id == "claude_code" and r.session_label == "lbl",
        "session resolve: agent_id/session_label typed-parsed from holder_identity",
    )
    _check(
        r.agent_instance_id == "agi-1" and r.agent_session_id == "sess-1",
        "session resolve: agent_instance_id + PROJECTED agent_session_id (makes §5.0 re-check constructible)",
    )
    _check(r.holder_identity.get("agent_id") == "claude_code", "session resolve: raw holder_identity carried")


def test_resolve_provider_holder() -> None:
    state = _state()
    _seed_binding(
        state,
        role="sys:autonomic",
        holder_kind=HOLDER_KIND_INFERENCE_PROVIDER,
        holder_identity={"provider_kind": "anthropic", "provider_ref": "claude-opus", "display_name": "Opus"},
    )
    r = resolve_role_binding_v4(state, "sys:autonomic")
    _check(r.holder_kind == HOLDER_KIND_INFERENCE_PROVIDER, "provider resolve: holder_kind=inference_provider")
    _check(
        r.agent_id == "" and r.session_label == "" and r.agent_instance_id == "",
        "provider resolve: session-facing fields empty (a provider is not a session)",
    )
    _check(
        r.holder_identity.get("provider_ref") == "claude-opus",
        "provider resolve: provider identity carried in holder_identity",
    )


def test_typed_parse_fail_loud() -> None:
    state = _state()
    _seed_binding(state, role="R", holder_kind=HOLDER_KIND_SESSION, holder_identity={"session_label": "x"})
    raised = False
    try:
        resolve_role_binding_v4(state, "R")
    except RoleBindingMalformedError:
        raised = True
    _check(raised, "typed parse: session holder missing agent_id → RoleBindingMalformedError (no silent '')")

    state2 = _state()
    _seed_binding(state2, role="R2", holder_kind="wormhole", holder_identity={"agent_id": "x"})
    raised2 = False
    try:
        resolve_role_binding_v4(state2, "R2")
    except RoleBindingMalformedError:
        raised2 = True
    _check(raised2, "typed parse: unknown holder_kind → FATAL RoleBindingMalformedError (no fall-through to session)")


def test_vacant() -> None:
    state = _state()
    raised = False
    try:
        resolve_role_binding_v4(state, "nobody")
    except RoleBindingVacantError:
        raised = True
    _check(raised, "vacant role → RoleBindingVacantError")


def test_json_string_holder_identity_coerced() -> None:
    state = _state()
    _seed_binding(
        state,
        role="R",
        holder_kind=HOLDER_KIND_SESSION,
        holder_identity=json.dumps({"agent_id": "codex", "session_label": "L"}),
    )
    r = resolve_role_binding_v4(state, "R")
    _check(
        r.agent_id == "codex" and r.session_label == "L",
        "holder_identity as a JSON STRING (SQLite path) is coerced + typed-parsed",
    )


def test_entity_upsert_roundtrip() -> None:
    state = _state()
    upsert_role_entity(state, name=_ARBITRARY_ROLE, description="the coordinator", properties={"k": "v"})
    rows = cast(RealShapeState, state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_ROLE)
    _check(len(rows) == 1 and rows[0].get(COL_ROLE) == _ARBITRARY_ROLE, "entity upsert writes one role row (opaque name)")
    _check(rows[0].get("origin") == "user", "entity upsert: default origin=user")
    _check(rows[0].get("properties") == json.dumps({"k": "v"}), "entity upsert: properties serialized")
    upsert_role_entity(state, name=_ARBITRARY_ROLE, description="updated")
    rows2 = cast(RealShapeState, state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_ROLE)
    _check(len(rows2) == 1, "entity upsert is idempotent on external_id (one row per role)")


class _RememberingService:
    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def remember(self, **kwargs: Any) -> dict[str, Any]:
        if self.raises:
            raise RuntimeError("memory backend not ready")
        self.calls.append(kwargs)
        return {"memory_id": "mem-role-1"}


def test_ingest_best_effort() -> None:
    _check(ingest_role_entity(None, name="R") is None, "ingest: no memory_service → None (never gates)")
    svc = _RememberingService()
    mid = ingest_role_entity(svc, name=_ARBITRARY_ROLE, description="d", properties={"a": 1})
    _check(mid == "mem-role-1", "ingest: live service → memory_id returned")
    _check(
        svc.calls and any(t.startswith("role:") for t in svc.calls[0].get("tags", [])),
        "ingest: tags include the opaque role name (discoverable via recall)",
    )
    faulting = _RememberingService(raises=True)
    _check(
        ingest_role_entity(faulting, name="R") is None,
        "ingest: remember() fault → None (best-effort, never gates the claim)",
    )


def main() -> int:
    print("=== role-model v4 slice-A smoke ===")
    test_schemas_install()
    test_resolve_session_holder()
    test_resolve_provider_holder()
    test_typed_parse_fail_loud()
    test_vacant()
    test_json_string_holder_identity_coerced()
    test_entity_upsert_roundtrip()
    test_ingest_best_effort()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
