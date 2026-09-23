#!/usr/bin/env python3
"""Slice-C smoke — system-slot declarations + the §6.1 reserved-keyspace claim gate
(no pytest, no DB).

Slice C builds the system-slot SUBSTRATE (§6/§6.1/§D.2/§D.6): the reserved ``sys:``
keyspace, the slot-declaration registry, the `sys:autonomic` constant (session-filled,
consumed by INF-01's §D.9 auto-assignment lane), and the claim/release gates on the
general `peer_claim_role` / `peer_release_role`. The auto-assignment lifecycle itself
is INF-01's lane (seam: workbench/2026-07-03_inf01_slicec_seam_boundary.md).

Covers:

  GATE (evaluate_system_slot_claim — pure, fixture plugin-owned slot):
    * a normal user role → NOT_SYSTEM (proceeds with the ordinary claim);
    * `sys:autonomic` (session-filled) → REJECT (assigned via §D.9, not this verb);
    * an undeclared `sys:` name → REJECT (unknown slot in the reserved keyspace);
    * a PLUGIN-OWNED slot: declared owner (plugin principal) → ALLOW;
      wrong plugin / operator principal / MISSING context → REJECT (fail-closed).
  DECLARATION INTEGRITY (validate_system_slot_declarations):
    * the canonical registry passes; key≠slot_name / non-`sys:` name / bad
      holder_kind each raise SystemSlotDeclarationError (fail-startup).
  is_system_role: `sys:` prefix True; an opaque user name False.
  VERB WIRING (peer_claim_role / peer_release_role, canonical registry):
    * peer_claim_role(`sys:autonomic`) → system_slot_claim_denied (session-filled);
    * peer_claim_role(undeclared `sys:`) → system_slot_claim_denied;
    * a caller-supplied `params['call_context']` spoof does NOT grant a system claim
      (the gate reads the server-built `state`, never `params`);
    * peer_claim_role(normal role) → NOT denied (passes the gate → state path);
    * peer_release_role(`sys:*`) → system_slot_release_denied (no-vacant-release);
    * peer_release_role(normal role) → NOT denied (passes the guard).

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/system_slots_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.core.services.call_context import CallContext  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    HOLDER_KIND_SESSION,
    SYS_AUTONOMIC_SLOT,
    SYSTEM_ROLE_PREFIX,
    is_system_role,
)

from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.system_slots import (  # noqa: E402
    SystemSlotClaimDecision,
    SystemSlotDeclaration,
    SystemSlotDeclarationError,
    evaluate_system_slot_claim,
    validate_system_slot_declarations,
)

# An opaque, operator-defined user role — proves the gate never special-cases it.
_ARBITRARY_ROLE = "zz-Ω arbitrary/role #7!"

# A FIXTURE plugin-owned slot (no production plugin-owned slot exists yet — the
# gate machinery is exercised here, per the minimal-realization decision).
_FIXTURE_OWNER = "widget_owner_plugin"
_FIXTURE_SLOT = f"{SYSTEM_ROLE_PREFIX}widget"
_FIXTURE_DECLS = {
    _FIXTURE_SLOT: SystemSlotDeclaration(
        slot_name=_FIXTURE_SLOT, owner_plugin=_FIXTURE_OWNER, holder_kind=HOLDER_KIND_SESSION,
    ),
}

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


def _decision(name: str, ctx: CallContext | None, decls: Any = None) -> SystemSlotClaimDecision:
    if decls is None:
        return evaluate_system_slot_claim(name, ctx).decision
    return evaluate_system_slot_claim(name, ctx, decls).decision


# ---------------------------------------------------------------------------
# Gate (pure) — the §6.1 decision cases, incl. the fixture plugin-owned slot
# ---------------------------------------------------------------------------


def test_gate_user_role_not_system() -> None:
    _check(
        _decision(_ARBITRARY_ROLE, None) is SystemSlotClaimDecision.NOT_SYSTEM,
        "a normal user role → NOT_SYSTEM (gate defers to the ordinary claim)",
    )


def test_gate_autonomic_session_filled_rejected() -> None:
    _check(
        _decision(SYS_AUTONOMIC_SLOT, CallContext.for_operator()) is SystemSlotClaimDecision.REJECT,
        "sys:autonomic (session-filled) → REJECT via peer_claim_role (assigned by §D.9, not here)",
    )


def test_gate_undeclared_sys_rejected() -> None:
    _check(
        _decision(f"{SYSTEM_ROLE_PREFIX}not_a_real_slot", CallContext.for_operator())
        is SystemSlotClaimDecision.REJECT,
        "an undeclared sys: name → REJECT (reserved keyspace, not a declared slot)",
    )


def test_gate_plugin_owned_owner_allowed() -> None:
    _check(
        _decision(_FIXTURE_SLOT, CallContext.for_plugin(_FIXTURE_OWNER), _FIXTURE_DECLS)
        is SystemSlotClaimDecision.ALLOW,
        "plugin-owned slot + declared owner (plugin principal) → ALLOW",
    )


def test_gate_plugin_owned_wrong_owner_rejected() -> None:
    _check(
        _decision(_FIXTURE_SLOT, CallContext.for_plugin("other_plugin"), _FIXTURE_DECLS)
        is SystemSlotClaimDecision.REJECT,
        "plugin-owned slot + a DIFFERENT plugin → REJECT (only the declared owner)",
    )


def test_gate_plugin_owned_operator_rejected() -> None:
    _check(
        _decision(_FIXTURE_SLOT, CallContext.for_operator(), _FIXTURE_DECLS)
        is SystemSlotClaimDecision.REJECT,
        "plugin-owned slot + an operator principal → REJECT (not principal_kind='plugin')",
    )


def test_gate_plugin_owned_no_context_fails_closed() -> None:
    _check(
        _decision(_FIXTURE_SLOT, None, _FIXTURE_DECLS) is SystemSlotClaimDecision.REJECT,
        "plugin-owned slot + MISSING call_context → REJECT (fail-closed, never allow)",
    )


# ---------------------------------------------------------------------------
# Declaration integrity + prefix helper
# ---------------------------------------------------------------------------


def test_validate_canonical_passes() -> None:
    try:
        validate_system_slot_declarations()
        ok = True
    except SystemSlotDeclarationError:
        ok = False
    _check(ok, "the canonical system-slot registry validates (well-formed)")


def _raises_declaration_error(decls: dict[str, SystemSlotDeclaration]) -> bool:
    try:
        validate_system_slot_declarations(decls)
    except SystemSlotDeclarationError:
        return True
    return False


def test_validate_malformed_raise() -> None:
    _check(
        _raises_declaration_error(
            {"sys:x": SystemSlotDeclaration("sys:y", None, HOLDER_KIND_SESSION)},
        ),
        "declaration key ≠ slot_name → SystemSlotDeclarationError",
    )
    _check(
        _raises_declaration_error(
            {"plainname": SystemSlotDeclaration("plainname", None, HOLDER_KIND_SESSION)},
        ),
        "a non-sys: slot name → SystemSlotDeclarationError (reserved-keyspace check)",
    )
    _check(
        _raises_declaration_error(
            {"sys:z": SystemSlotDeclaration("sys:z", None, "wormhole")},
        ),
        "an unknown holder_kind → SystemSlotDeclarationError",
    )


def test_is_system_role() -> None:
    _check(is_system_role(SYS_AUTONOMIC_SLOT), "is_system_role: sys: prefix → True")
    _check(not is_system_role(_ARBITRARY_ROLE), "is_system_role: an opaque user name → False")


# ---------------------------------------------------------------------------
# Verb wiring — peer_claim_role / peer_release_role (canonical registry)
# ---------------------------------------------------------------------------


class _GatePlugin(AgentMessagingPlugin):
    """Bare plugin for the gate/guard path — no orchestrator, so _get_state_service
    returns None (a role that PASSES the gate then surfaces state_service_unavailable,
    which proves it got past the gate).

    The bridge collaborators are set to None exactly as the real ``__init__``
    leaves them before ``start_bridge``: ``peer_claim_role`` now hands them to
    the shared claim body, so an unbound-attribute fixture would fail on
    construction rather than on the gate this file is about."""

    def __init__(self) -> None:
        self._service = None
        self._bridge_manager = None
        self._peer_registry = None


def _claim(plugin: _GatePlugin, name: str, params_extra: dict[str, Any] | None = None,
           state: dict[str, Any] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name, "agent_id": "claude_code", "agent_instance_id": "agi-x"}
    if params_extra:
        params.update(params_extra)
    return plugin.peer_claim_role(params, state or {})


def _code(result: dict[str, Any]) -> str:
    err = result.get("error")
    if isinstance(err, dict):
        return str(err.get("code") or err.get("error_code") or "")
    return str(result.get("code") or "")


def test_verb_claim_autonomic_denied() -> None:
    result = _claim(_GatePlugin(), SYS_AUTONOMIC_SLOT)
    _check(
        _code(result) == "system_slot_claim_denied",
        "peer_claim_role(sys:autonomic) → system_slot_claim_denied (session-filled, canonical)",
    )


def test_verb_claim_undeclared_sys_denied() -> None:
    result = _claim(_GatePlugin(), f"{SYSTEM_ROLE_PREFIX}bogus")
    _check(
        _code(result) == "system_slot_claim_denied",
        "peer_claim_role(sys:bogus) → system_slot_claim_denied (unknown slot)",
    )


def test_verb_params_call_context_spoof_ignored() -> None:
    # A caller planting a plugin-principal call_context in PARAMS must NOT influence
    # the gate — it reads the server-built state, never params.
    spoof = {"call_context": CallContext.for_plugin("attacker_plugin")}
    result = _claim(_GatePlugin(), SYS_AUTONOMIC_SLOT, params_extra=spoof, state={})
    _check(
        _code(result) == "system_slot_claim_denied",
        "a params-injected call_context does NOT grant a system claim (gate reads state, not params)",
    )


def test_verb_claim_user_role_passes_gate() -> None:
    result = _claim(_GatePlugin(), _ARBITRARY_ROLE)
    _check(
        _code(result) != "system_slot_claim_denied",
        "peer_claim_role(user role) → NOT gate-denied (proceeds; surfaces state_service_unavailable)",
    )
    _check(
        _code(result) == "state_service_unavailable",
        "the user-role claim got PAST the gate (reached the state path)",
    )


def test_verb_release_system_denied() -> None:
    result = _GatePlugin().peer_release_role({"name": SYS_AUTONOMIC_SLOT}, {})
    _check(
        _code(result) == "system_slot_release_denied",
        "peer_release_role(sys:autonomic) → system_slot_release_denied (no-vacant-release)",
    )


def test_verb_release_user_role_passes_guard() -> None:
    result = _GatePlugin().peer_release_role({"name": _ARBITRARY_ROLE}, {})
    _check(
        _code(result) != "system_slot_release_denied",
        "peer_release_role(user role) → NOT guard-denied (proceeds to the state path)",
    )


def main() -> int:
    print("=== slice-C system-slots + §6.1 claim gate smoke ===")
    test_gate_user_role_not_system()
    test_gate_autonomic_session_filled_rejected()
    test_gate_undeclared_sys_rejected()
    test_gate_plugin_owned_owner_allowed()
    test_gate_plugin_owned_wrong_owner_rejected()
    test_gate_plugin_owned_operator_rejected()
    test_gate_plugin_owned_no_context_fails_closed()
    test_validate_canonical_passes()
    test_validate_malformed_raise()
    test_is_system_role()
    test_verb_claim_autonomic_denied()
    test_verb_claim_undeclared_sys_denied()
    test_verb_params_call_context_spoof_ignored()
    test_verb_claim_user_role_passes_gate()
    test_verb_release_system_denied()
    test_verb_release_user_role_passes_guard()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
