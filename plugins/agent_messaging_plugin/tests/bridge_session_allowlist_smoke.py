#!/usr/bin/env python3
"""M5.B principal-propagation smoke (no pytest; standalone fixtures).

Run with:

    .venv/bin/python3 plugins/agent_messaging_plugin/tests/bridge_session_allowlist_smoke.py

Spec §14 + §17.5 — covers the per-session policy + bearer-claim
propagation surface end-to-end against in-memory fixtures.

Cases:

1. `BearerClaim` carries `client_id`; `_payload_to_bearer_claim` REQUIRES
   non-empty client_id; absent / empty raises `BearerAuthError`.
2. `_issue_access_token` emits `client_id` in the JWT payload, and a
   roundtrip through `BearerVerifier.verify` recovers it on the claim.
3. `BearerVerifier._check_client_exists` rejects claims whose client_id
   the registry callback flags as unknown (revoked / orphan).
4. `BridgeSessionManager.open_bridge(claim)` pre-populates
   `bridge.client_id` and `bridge.process_export_allowlist` from the
   resolved policy (vs `open()` which leaves them as defaults).
5. `_resolve_session_policy` returns `EMPTY_ALLOWLIST` (fail-closed) when
   no resolver is wired.
6. Policy resolver routes claims to `_UNRESTRICTED` (operator-equivalent),
   `SHIPPER_ALLOWLIST` (paired shipper), and `EMPTY_ALLOWLIST` (neither).
7. `_validate_process_against_session_policy` honours the
   `_UNRESTRICTED` sentinel (allows everything) AND membership-checks
   against a concrete allowlist.
8. `_filter_payload_against_session_policy` drops out-of-allowlist
   process_keys from search results; `_UNRESTRICTED` passes the payload
   through untouched.
9. `_build_process_call_trigger_data` includes `authenticated_principal`
   when the bridge carries a client_id; omits it for stdio bridges with
   empty client_id (fail-loud at handler via
   `extract_authenticated_principal`).
10. Cross-deployment isolation: a shipper bridge bound to client A can't
    access a process that's gated by shipper B's allowlist; per-bridge
    state is independent.
"""

from __future__ import annotations

# ruff: noqa: E402
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jwt as pyjwt

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agent_messaging_plugin.bridge_sessions import (
    _UNRESTRICTED,
    EMPTY_ALLOWLIST,
    SHIPPER_ALLOWLIST,
    BridgeSessionManager,
)
from agent_messaging_plugin.mcp_streamable.auth import (
    HMAC_KEY_BYTE_LENGTH,
    HMAC_SIGNING_ALGORITHM,
    BearerAuthError,
    BearerClaim,
    BearerVerifier,
    _payload_to_bearer_claim,
)
from agent_messaging_plugin.models import BridgeSessionState
from agent_messaging_plugin.platform_surface import (
    _filter_payload_against_session_policy,
)

if TYPE_CHECKING:
    from collections.abc import Callable

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


_HMAC_KEY = b"\xab" * HMAC_KEY_BYTE_LENGTH


# ─── Fixtures ───────────────────────────────────────────────────────────────


def _mint_token(
    *,
    client_id: str = "cid-test",
    drop_client_id: bool = False,
    aud: str = "",
    issued_at: datetime | None = None,
) -> str:
    """Mint a JWT carrying the spec-shape claim payload."""
    now = (issued_at or datetime.now(UTC)).replace(microsecond=0)
    payload: dict[str, Any] = {
        "agent_id": "oauth_client",
        "agent_instance_id": f"agi-oauth-{client_id}",
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "session_label": client_id,
        "scopes": ["mcp:read", "mcp:write"],
        "aud": aud,
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    if not drop_client_id:
        payload["client_id"] = client_id
    return pyjwt.encode(payload, _HMAC_KEY, algorithm=HMAC_SIGNING_ALGORITHM)


def _make_bridge(
    *,
    client_id: str = "cid-shipper-a",
    allowlist: tuple[str, ...] = SHIPPER_ALLOWLIST,
) -> BridgeSessionState:
    """Construct a BridgeSessionState directly (no manager)."""
    return BridgeSessionState(
        bridge_id="agc-test123",
        session_id="ses-test123",
        agent_instance_id=f"agi-oauth-{client_id}",
        session_label=client_id,
        client_id=client_id,
        process_export_allowlist=allowlist,
    )


def _make_manager(
    policy_resolver: Callable[[BearerClaim], tuple[str, ...]] | None = None,
) -> BridgeSessionManager:
    return BridgeSessionManager(
        session_id_factory=lambda _hn: "ses-test",
        idle_timeout_s=300,
        max_pending_events=100,
        long_poll_timeout_s=25,
        policy_resolver=policy_resolver,
    )


# ─── Cases ──────────────────────────────────────────────────────────────────


def test_bearer_claim_carries_client_id() -> None:
    claim = BearerClaim(
        agent_id="x",
        agent_instance_id="agi-x",
        issued_at=datetime.now(UTC),
        client_id="cid-aaa",
    )
    _check(
        claim.client_id == "cid-aaa",
        "BearerClaim accepts and exposes client_id field",
    )


def test_parse_claim_requires_client_id() -> None:
    """Spec §14.3: every accepted claim carries a non-empty client_id."""
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    base: dict[str, Any] = {
        "agent_id": "oauth_client",
        "agent_instance_id": "agi-oauth-x",
        "issued_at": now,
        "session_label": "x",
        "aud": "",
    }
    # Missing client_id
    try:
        _payload_to_bearer_claim(base)
    except BearerAuthError as exc:
        _check(
            "client_id" in exc.message,
            "parser rejects missing client_id with bearer.invalid_claim",
        )
    else:
        _check(False, "expected BearerAuthError on missing client_id")
    # Empty client_id
    try:
        _payload_to_bearer_claim({**base, "client_id": ""})
    except BearerAuthError as exc:
        _check(
            "client_id" in exc.message, "parser rejects empty client_id"
        )
    else:
        _check(False, "expected BearerAuthError on empty client_id")
    # Non-string client_id
    try:
        _payload_to_bearer_claim({**base, "client_id": 12345})
    except BearerAuthError as exc:
        _check(
            "client_id" in exc.message, "parser rejects non-string client_id"
        )
    else:
        _check(False, "expected BearerAuthError on non-string client_id")
    # Valid client_id
    claim = _payload_to_bearer_claim({**base, "client_id": "cid-ok"})
    _check(claim.client_id == "cid-ok", "parser surfaces client_id on success")


def test_token_roundtrip_preserves_client_id() -> None:
    """Spec §14.2: issuer emits client_id; verifier recovers it."""
    token = _mint_token(client_id="cid-roundtrip")
    verifier = BearerVerifier(_HMAC_KEY)
    claim = verifier.verify(f"Bearer {token}")
    _check(
        claim.client_id == "cid-roundtrip",
        "BearerVerifier roundtrips client_id end-to-end",
    )


def test_verifier_rejects_token_missing_client_id() -> None:
    """Tokens minted before M5 (no client_id) are rejected post-deploy."""
    token = _mint_token(drop_client_id=True)
    verifier = BearerVerifier(_HMAC_KEY)
    try:
        verifier.verify(f"Bearer {token}")
    except BearerAuthError as exc:
        _check(
            "client_id" in exc.message,
            "verifier rejects pre-M5 tokens with no client_id",
        )
        return
    _check(False, "expected BearerAuthError on missing client_id")


def test_verifier_cross_checks_client_exists() -> None:
    """Spec §14.3: revoked clients' tokens are rejected even if otherwise valid."""
    known: set[str] = {"cid-known"}
    verifier = BearerVerifier(
        _HMAC_KEY,
        client_exists_check=lambda cid: cid in known,
    )
    # Known client → passes
    good_token = _mint_token(client_id="cid-known")
    claim = verifier.verify(f"Bearer {good_token}")
    _check(
        claim.client_id == "cid-known",
        "verifier accepts known client_id",
    )
    # Unknown client → bearer.unknown_client
    revoked_token = _mint_token(client_id="cid-revoked")
    try:
        verifier.verify(f"Bearer {revoked_token}")
    except BearerAuthError as exc:
        _check(
            exc.code == "bearer.unknown_client",
            f"verifier rejects unknown client_id (code={exc.code})",
        )
        return
    _check(False, "expected BearerAuthError on unknown client_id")


def test_open_bridge_populates_client_id_and_allowlist() -> None:
    """Spec §14.4: open_bridge propagates claim → bridge state."""
    captured: list[BearerClaim] = []

    def resolver(claim: BearerClaim) -> tuple[str, ...]:
        captured.append(claim)
        return SHIPPER_ALLOWLIST

    mgr = _make_manager(policy_resolver=resolver)
    claim = BearerClaim(
        agent_id="oauth_client",
        agent_instance_id="agi-oauth-cid-shipper",
        issued_at=datetime.now(UTC),
        session_label="shipper-dep-aaa",
        client_id="cid-shipper",
    )
    bridge = mgr.open_bridge(claim)
    _check(
        bridge.client_id == "cid-shipper",
        "open_bridge sets bridge.client_id from claim",
    )
    _check(
        bridge.agent_instance_id == "agi-oauth-cid-shipper",
        "open_bridge sets agent_instance_id from claim",
    )
    _check(
        bridge.session_label == "shipper-dep-aaa",
        "open_bridge sets session_label from claim",
    )
    _check(
        bridge.process_export_allowlist == SHIPPER_ALLOWLIST,
        "open_bridge resolves session policy onto the bridge",
    )
    _check(
        len(captured) == 1 and captured[0] is claim,
        "policy_resolver receives the claim once during open_bridge",
    )


def test_resolve_policy_fails_closed_without_resolver() -> None:
    """No resolver wired → EMPTY_ALLOWLIST (fail-closed)."""
    mgr = _make_manager(policy_resolver=None)
    claim = BearerClaim(
        agent_id="oauth_client",
        agent_instance_id="agi-x",
        issued_at=datetime.now(UTC),
        client_id="cid-anything",
    )
    bridge = mgr.open_bridge(claim)
    _check(
        bridge.process_export_allowlist == EMPTY_ALLOWLIST,
        "no resolver wired → EMPTY_ALLOWLIST (fail-closed)",
    )


def test_resolver_routes_to_three_policies() -> None:
    """Operator-equivalent, paired-shipper, and fail-closed all reachable."""
    operator_clients = {"cid-operator-eq"}
    shipper_clients = {"cid-shipper-paired"}

    def resolver(claim: BearerClaim) -> tuple[str, ...]:
        if claim.client_id in operator_clients:
            return _UNRESTRICTED
        if claim.client_id in shipper_clients:
            return SHIPPER_ALLOWLIST
        return EMPTY_ALLOWLIST

    mgr = _make_manager(policy_resolver=resolver)

    def _open(client_id: str) -> BridgeSessionState:
        claim = BearerClaim(
            agent_id="oauth_client",
            agent_instance_id=f"agi-oauth-{client_id}",
            issued_at=datetime.now(UTC),
            client_id=client_id,
        )
        return mgr.open_bridge(claim)

    op_bridge = _open("cid-operator-eq")
    _check(
        op_bridge.process_export_allowlist is _UNRESTRICTED,
        "operator_equivalent client_id → _UNRESTRICTED sentinel (identity-checked)",
    )

    shipper_bridge = _open("cid-shipper-paired")
    _check(
        shipper_bridge.process_export_allowlist == SHIPPER_ALLOWLIST,
        "paired-shipper client_id → SHIPPER_ALLOWLIST",
    )

    unknown_bridge = _open("cid-unknown")
    _check(
        unknown_bridge.process_export_allowlist == EMPTY_ALLOWLIST,
        "unknown client_id → EMPTY_ALLOWLIST (fail-closed)",
    )


def test_filter_payload_unrestricted_passthrough() -> None:
    """_UNRESTRICTED bridges get the search payload untouched."""
    bridge = _make_bridge(client_id="cid-op", allowlist=_UNRESTRICTED)
    payload: dict[str, Any] = {
        "processes": [
            {"process_key": "plugin::foo::bar"},
            {"process_key": "service_interface::session_ledger_service::ingest_raw_chunk"},
            {"process_key": "plugin::baz::qux"},
        ],
        "process_keys": [
            "plugin::foo::bar",
            "service_interface::session_ledger_service::ingest_raw_chunk",
            "plugin::baz::qux",
        ],
        "process_count": 3,
    }
    filtered = _filter_payload_against_session_policy(payload, bridge)
    _check(
        filtered is payload,
        "_UNRESTRICTED bridge → payload returned untouched (no filter)",
    )


def test_filter_payload_drops_out_of_allowlist_entries() -> None:
    """Non-unrestricted bridges keep only allowlisted process_keys."""
    bridge = _make_bridge(
        client_id="cid-shipper-a", allowlist=SHIPPER_ALLOWLIST
    )
    payload: dict[str, Any] = {
        "processes": [
            {"process_key": "plugin::evil::ransomware"},
            {"process_key": "service_interface::session_ledger_service::ingest_raw_chunk"},
            {"process_key": "plugin::foo::bar"},
            {"process_key": "service_interface::session_ledger_service::shipper_self_revoke"},
        ],
        "process_keys": [
            "plugin::evil::ransomware",
            "service_interface::session_ledger_service::ingest_raw_chunk",
            "plugin::foo::bar",
            "service_interface::session_ledger_service::shipper_self_revoke",
        ],
        "process_count": 4,
    }
    filtered = _filter_payload_against_session_policy(payload, bridge)
    kept_keys = filtered["process_keys"]
    _check(
        "plugin::evil::ransomware" not in kept_keys,
        "out-of-allowlist process_key dropped",
    )
    _check(
        "service_interface::session_ledger_service::ingest_raw_chunk" in kept_keys,
        "shipper-allowed process_key retained",
    )
    _check(
        filtered["process_count"] == 2,
        f"only 2 allowlisted keys kept (got {filtered['process_count']})",
    )


def test_filter_payload_fail_closed_empty_allowlist() -> None:
    """EMPTY_ALLOWLIST bridges see zero processes in search results."""
    bridge = _make_bridge(client_id="cid-unknown", allowlist=EMPTY_ALLOWLIST)
    payload: dict[str, Any] = {
        "processes": [
            {"process_key": "plugin::foo::bar"},
            {"process_key": "service_interface::xx::yy"},
        ],
        "process_keys": ["plugin::foo::bar", "service_interface::xx::yy"],
        "process_count": 2,
    }
    filtered = _filter_payload_against_session_policy(payload, bridge)
    _check(
        filtered["process_count"] == 0,
        "EMPTY_ALLOWLIST → 0 processes surface from search",
    )
    _check(
        filtered["processes"] == [] and filtered["process_keys"] == [],
        "EMPTY_ALLOWLIST → empty processes + process_keys lists",
    )


def test_stdio_bridge_bypasses_allowlist() -> None:
    """M5.B post-merge hot-fix: stdio bridges (empty client_id) get full passthrough.

    Stdio bridges predate the OAuth-principal model; per-session
    allowlists semantically apply only to OAuth-bound bridges. Without
    the bypass, every first-party MCP call would 403 (the EMPTY_ALLOWLIST
    default would reject everything).
    """
    from agent_messaging_plugin.platform_surface import (  # noqa: PLC0415
        BridgeError,
        PlatformSurface,
    )

    stdio_bridge = BridgeSessionState(
        bridge_id="agc-stdio-test",
        session_id="ses-stdio-test",
        client_id="",  # explicit: stdio bridges have no OAuth identity
        process_export_allowlist=EMPTY_ALLOWLIST,
    )

    # _validate_process_against_session_policy must NOT raise for stdio.
    surface_cls = PlatformSurface
    # Need an instance because the method is non-static. Skip platform
    # init by going through object.__new__; the method only touches
    # bridge state, no instance attrs.
    surface: PlatformSurface = object.__new__(surface_cls)
    try:
        surface._validate_process_against_session_policy(  # noqa: SLF001
            "plugin::anything::at_all", stdio_bridge,
        )
    except BridgeError as exc:
        _check(False, f"stdio bridge SHOULD bypass policy check; got {exc}")
        return
    _check(
        True,
        "stdio bridge (empty client_id) bypasses _validate_process_against_session_policy",
    )

    # _filter_payload_against_session_policy must return payload unchanged.
    payload: dict[str, Any] = {
        "processes": [
            {"process_key": "plugin::foo::bar"},
            {"process_key": "plugin::baz::qux"},
        ],
        "process_keys": ["plugin::foo::bar", "plugin::baz::qux"],
        "process_count": 2,
    }
    filtered = _filter_payload_against_session_policy(payload, stdio_bridge)
    _check(
        filtered is payload,
        "stdio bridge → _filter_payload_against_session_policy returns payload unchanged",
    )


def test_trigger_data_carries_authenticated_principal_when_oauth() -> None:
    """Bridge with client_id → trigger_data includes authenticated_principal."""
    # Import inside the test to keep the module-level imports tight.
    from agent_messaging_plugin.platform_surface import PlatformSurface  # noqa: PLC0415

    bridge = _make_bridge(client_id="cid-shipper-a")
    trigger = PlatformSurface._build_process_call_trigger_data(
        bridge=bridge,
        process_key="service_interface::session_ledger_service::ingest_raw_chunk",
        reason="smoke",
        # Signature drift: the builder now carries the originating session's
        # durable role + an operator-equivalence flag (the latter rides into
        # authenticated_principal for server-side authz). Pass True here to
        # assert the mechanism propagates it (policy resolution of who IS
        # operator-equivalent lives in _resolve_operator_equivalent, not here).
        inference_vertex_role="",
        operator_equivalent=True,
    )
    principal = trigger.get("authenticated_principal")
    _check(
        isinstance(principal, dict),
        "trigger_data carries authenticated_principal dict",
    )
    if isinstance(principal, dict):
        _check(
            principal["client_id"] == "cid-shipper-a",
            "authenticated_principal.client_id matches bridge.client_id",
        )
        _check(
            principal["bridge_id"] == bridge.bridge_id
            and principal["session_id"] == bridge.session_id,
            "authenticated_principal carries bridge_id + session_id",
        )
        _check(
            principal["operator_equivalent"] is True,
            "authenticated_principal propagates operator_equivalent from the builder arg",
        )


def test_trigger_data_omits_principal_for_stdio_bridges() -> None:
    """Empty client_id (stdio bridge) → no authenticated_principal in trigger."""
    from agent_messaging_plugin.platform_surface import PlatformSurface  # noqa: PLC0415

    stdio_bridge = BridgeSessionState(
        bridge_id="agc-stdio",
        session_id="ses-stdio",
        client_id="",  # explicit: stdio bridges have no OAuth identity
    )
    trigger = PlatformSurface._build_process_call_trigger_data(
        bridge=stdio_bridge,
        process_key="plugin::foo::bar",
        reason="stdio call",
        inference_vertex_role="",
        operator_equivalent=False,
    )
    _check(
        "authenticated_principal" not in trigger,
        "stdio bridge (empty client_id) → no authenticated_principal "
        "(handlers raise PermissionError per extract_authenticated_principal)",
    )


def test_cross_deployment_isolation() -> None:
    """Per-bridge state is independent: two shipper bridges, same allowlist
    membership BUT independent identity. Forging a different client_id at the
    handler level (via state.authenticated_principal mutation) would be the
    real cross-deployment attack — defended at extract_authenticated_principal,
    not here. This test confirms the bridge-state foundation.
    """
    bridge_a = _make_bridge(client_id="cid-shipper-A", allowlist=SHIPPER_ALLOWLIST)
    bridge_b = _make_bridge(client_id="cid-shipper-B", allowlist=SHIPPER_ALLOWLIST)
    _check(
        bridge_a.client_id != bridge_b.client_id,
        "two shipper bridges carry distinct client_ids",
    )
    _check(
        bridge_a.process_export_allowlist == bridge_b.process_export_allowlist,
        "both shipper bridges share SHIPPER_ALLOWLIST (membership identical)",
    )
    # Build trigger_data on bridge_a; principal MUST reflect A's client_id only.
    from agent_messaging_plugin.platform_surface import PlatformSurface  # noqa: PLC0415

    trigger_a = PlatformSurface._build_process_call_trigger_data(
        bridge=bridge_a,
        process_key="service_interface::session_ledger_service::shipper_self_revoke",
        reason="bridge A revoke",
        inference_vertex_role="",
        operator_equivalent=False,
    )
    principal_a = trigger_a["authenticated_principal"]
    assert isinstance(principal_a, dict)
    _check(
        principal_a["client_id"] == "cid-shipper-A",
        "bridge A's trigger_data carries A's client_id, NOT B's",
    )


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    print("=== bridge_session_allowlist_smoke (M5.B principal propagation) ===")
    test_bearer_claim_carries_client_id()
    test_parse_claim_requires_client_id()
    test_token_roundtrip_preserves_client_id()
    test_verifier_rejects_token_missing_client_id()
    test_verifier_cross_checks_client_exists()
    test_open_bridge_populates_client_id_and_allowlist()
    test_resolve_policy_fails_closed_without_resolver()
    test_resolver_routes_to_three_policies()
    test_filter_payload_unrestricted_passthrough()
    test_filter_payload_drops_out_of_allowlist_entries()
    test_filter_payload_fail_closed_empty_allowlist()
    test_stdio_bridge_bypasses_allowlist()
    test_trigger_data_carries_authenticated_principal_when_oauth()
    test_trigger_data_omits_principal_for_stdio_bridges()
    test_cross_deployment_isolation()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
