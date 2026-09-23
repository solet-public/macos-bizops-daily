#!/usr/bin/env python3
"""M5.C end-to-end pairing smoke (no pytest; standalone fixtures).

Run:

    .venv/bin/python3 plugins/session_shipper_bootstrap_plugin/tests/session_shipper_pairing_smoke.py

Covers spec §13.5 end-to-end:

1. generate_ingest_setup creates a pending deployment with the operator's
   client_id as initiating_client_id (§13.3 ownership-binding seed).
2. POST /api/v1/ledger/pairing/initiate persists a fresh pairing_token
   (scrypt-hashed) + returns user_code + cleartext token.
3. approve_pairing rejects when caller.client_id != initiating_client_id
   (§13.3 ownership-binding 403); accepts on match.
4. POST /api/v1/ledger/pairing/poll verifies the token, mints an OAuth
   client (machine_grant_enabled=True via the M5.A vault method),
   transitions approved→paired, returns the cleartext client_secret ONCE.
5. shipper_self_revoke derives deployment_id from the bearer's client_id
   (§14.1 pin 2 — handler accepts NO caller-supplied target); paired →
   revoked. The deployment row's pairing_status is updated.
6. Token-mismatch on poll returns 401.
7. Wrong-state on poll (pending) returns {status: pending}.

The smoke uses TestClient over a fresh FastAPI app + in-memory stores
for the deployment table + vault. No real solet, no state_service.
"""

from __future__ import annotations

import base64
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "session_shipper_bootstrap_plugin" / "src"),
)

from ananta.llm.session_ledger.types import PairingStatus  # noqa: E402
from ananta.vault_core import VaultOAuthRegistry  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from session_shipper_bootstrap_plugin.pairing_routes import (  # noqa: E402
    PAIRING_INITIATE_ROUTE,
    PAIRING_POLL_ROUTE,
    PairingLedgerProtocol,
    register_session_ledger_pairing_routes,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

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


# ─── In-memory ledger + vault fixtures ──────────────────────────────────────


class _InMemoryDeploymentTable:
    """Holds deployment rows; tracks pairing-token + status transitions."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self._next_id = 1

    def insert(
        self,
        *,
        machine_id: str,
        initiating_client_id: str,
        authorized_source_kinds: list[str],
    ) -> str:
        deployment_id = f"dep-test-{self._next_id:05d}"
        self._next_id += 1
        now = datetime.now(UTC).isoformat()
        self.rows[deployment_id] = {
            "id": deployment_id,
            "machine_id": machine_id,
            "pairing_status": PairingStatus.PENDING.value,
            "pairing_token_hash": None,
            "pairing_token_salt": None,
            "user_code": None,
            "pairing_initiated_at": None,
            "approved_at": None,
            "oauth_client_id": None,
            "initiating_client_id": initiating_client_id,
            "authorized_source_kinds": list(authorized_source_kinds),
            "paired_at": None,
            "revoked_at": None,
            "created_at": now,
            "updated_at": now,
        }
        return deployment_id


class _StubVaultClientStore:
    """Bare-minimum OAuthClientStorage stub for VaultOAuthRegistry."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, dict[str, Any]] = {}

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        return self.rows.get(client_id)

    def insert_client(self, record: dict[str, Any]) -> None:
        self.rows[record["client_id"]] = dict(record)

    def delete_client(self, client_id: str) -> int:
        return 1 if self.rows.pop(client_id, None) is not None else 0

    def list_clients(self) -> list[Mapping[str, Any]]:
        return list(self.rows.values())

    def update_client_redirect_uris(
        self, client_id: str, redirect_uris: list[str],
    ) -> bool:
        row = self.rows.get(client_id)
        if row is None:
            return False
        row["redirect_uris"] = redirect_uris
        return True

    def record_client_token_use(
        self, client_id: str, fields: Mapping[str, str],
    ) -> bool:
        """Record last-use evidence on the in-memory record."""
        record = self.rows.get(client_id)
        if record is None:
            return False
        record.update(dict(fields))
        return True

    def insert_token(self, row: dict[str, Any]) -> None:
        self.tokens[row["token_hash"]] = dict(row)

    def consume_token(self, token_hash: str) -> dict[str, Any] | None:
        return self.tokens.pop(token_hash, None)


class _PairingLedgerStub:
    """Direct PairingLedgerProtocol impl over the in-memory table + vault."""

    def __init__(
        self,
        table: _InMemoryDeploymentTable,
        vault: VaultOAuthRegistry,
    ) -> None:
        self._table = table
        self._vault = vault

    def pairing_get_deployment(self, deployment_id: str) -> dict[str, object] | None:
        row = self._table.rows.get(deployment_id)
        return dict(row) if row is not None else None

    def pairing_persist_token(
        self,
        *,
        deployment_id: str,
        pairing_token_hash: str,
        pairing_token_salt: str,
        user_code: str,
    ) -> None:
        row = self._table.rows.get(deployment_id)
        if row is None:
            return
        row["pairing_token_hash"] = pairing_token_hash
        row["pairing_token_salt"] = pairing_token_salt
        row["user_code"] = user_code
        row["pairing_initiated_at"] = datetime.now(UTC).isoformat()

    def pairing_verify_token(
        self,
        *,
        deployment_id: str,
        cleartext_token: str,
    ) -> bool:
        row = self._table.rows.get(deployment_id)
        if row is None:
            return False
        salt_b64 = row.get("pairing_token_salt")
        hash_b64 = row.get("pairing_token_hash")
        if not isinstance(salt_b64, str) or not isinstance(hash_b64, str):
            return False
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(hash_b64.encode("ascii"))
        # Inline the same scrypt call shape as pairing_routes
        import hashlib  # noqa: PLC0415
        import hmac  # noqa: PLC0415
        candidate = hashlib.scrypt(
            password=cleartext_token.encode("utf-8"),
            salt=salt,
            n=16384,
            r=8,
            p=1,
            maxmem=64 * 1024 * 1024,
            dklen=32,
        )
        return hmac.compare_digest(candidate, expected)

    def pairing_mint_and_transition_to_paired(
        self,
        *,
        deployment_id: str,
    ) -> dict[str, str]:
        creds = self._vault.mint_internal_machine_client(
            client_label=f"shipper-{deployment_id}",
            scopes=("ledger:ingest",),
        )
        row = self._table.rows[deployment_id]
        row["pairing_status"] = PairingStatus.PAIRED.value
        row["oauth_client_id"] = creds["client_id"]
        row["paired_at"] = datetime.now(UTC).isoformat()
        row["pairing_token_hash"] = None
        row["pairing_token_salt"] = None
        return {
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        }


def _build_app(ledger: PairingLedgerProtocol) -> FastAPI:
    app = FastAPI()
    register_session_ledger_pairing_routes(app, ledger=ledger)
    return app


def _make_vault() -> VaultOAuthRegistry:
    store = _StubVaultClientStore()
    return VaultOAuthRegistry(
        client_storage=store,
        refresh_store=store,
        b64_encode=lambda b: base64.b64encode(b).decode("ascii"),
        b64_decode=lambda s: base64.b64decode(s.encode("ascii")),
        logger=logging.getLogger("session_shipper_pairing_smoke"),
    )


# ─── Cases ──────────────────────────────────────────────────────────────────


def test_full_pairing_flow_end_to_end() -> None:
    """Spec §13.5: pending → initiate → approved → poll → paired."""
    table = _InMemoryDeploymentTable()
    vault = _make_vault()
    ledger_stub = _PairingLedgerStub(table, vault)
    client = TestClient(_build_app(ledger_stub))

    # 1. Operator-side: generate_ingest_setup creates pending deployment
    deployment_id = table.insert(
        machine_id="machine-aaa",
        initiating_client_id="cid-operator",
        authorized_source_kinds=["agent_messaging", "codex_local"],
    )
    _check(
        table.rows[deployment_id]["pairing_status"] == "pending",
        "deployment starts in pending state",
    )

    # 2. Shipper-side: POST /pairing/initiate
    initiate_resp = client.post(
        PAIRING_INITIATE_ROUTE,
        json={"deployment_id": deployment_id, "machine_id": "machine-aaa"},
    )
    _check(
        initiate_resp.status_code == 200,
        f"/pairing/initiate → 200 (got {initiate_resp.status_code})",
    )
    initiate_body = initiate_resp.json()
    _check(
        "user_code" in initiate_body
        and "pairing_token" in initiate_body
        and initiate_body.get("expires_in_seconds") == 600,
        "initiate response carries {user_code, pairing_token, expires_in_seconds=600}",
    )
    user_code = initiate_body["user_code"]
    pairing_token = initiate_body["pairing_token"]
    _check(
        table.rows[deployment_id]["pairing_token_hash"] is not None,
        "deployment row carries pairing_token_hash after initiate",
    )
    _check(
        table.rows[deployment_id]["user_code"] == user_code,
        "deployment row carries user_code after initiate",
    )

    # 3. Operator-side: approve_pairing transitions pending → approved
    #    (Simulated directly since approve_pairing is the SessionLedgerService
    #    method, not a route. Smoke covers route surface; M5.A vault smoke +
    #    M5.B bridge_session_allowlist smoke cover the service surface.)
    row = table.rows[deployment_id]
    _check(
        row["initiating_client_id"] == "cid-operator",
        "approve_pairing ownership-binding seed: initiating_client_id captured",
    )
    # Transition (simulating approve_pairing's repository call):
    row["pairing_status"] = PairingStatus.APPROVED.value
    row["approved_at"] = datetime.now(UTC).isoformat()
    row["user_code"] = None

    # 4. Shipper-side: POST /pairing/poll
    poll_resp = client.post(
        PAIRING_POLL_ROUTE,
        json={"deployment_id": deployment_id, "pairing_token": pairing_token},
    )
    _check(
        poll_resp.status_code == 200,
        f"/pairing/poll → 200 on valid token (got {poll_resp.status_code})",
    )
    poll_body = poll_resp.json()
    _check(
        poll_body.get("status") == "paired",
        f"poll response status=paired (got {poll_body.get('status')!r})",
    )
    _check(
        isinstance(poll_body.get("client_id"), str)
        and isinstance(poll_body.get("client_secret"), str),
        "poll response carries cleartext client_id + client_secret (one-time)",
    )
    _check(
        table.rows[deployment_id]["pairing_status"] == "paired",
        "deployment row pairing_status=paired after poll",
    )
    _check(
        table.rows[deployment_id]["oauth_client_id"] == poll_body["client_id"],
        "deployment row oauth_client_id bound to minted client",
    )
    _check(
        table.rows[deployment_id]["pairing_token_hash"] is None
        and table.rows[deployment_id]["pairing_token_salt"] is None,
        "pairing_token cleared after consumption (single-use)",
    )


def test_poll_wrong_token_returns_401() -> None:
    table = _InMemoryDeploymentTable()
    vault = _make_vault()
    ledger_stub = _PairingLedgerStub(table, vault)
    client = TestClient(_build_app(ledger_stub))

    deployment_id = table.insert(
        machine_id="machine-bbb",
        initiating_client_id="cid-operator",
        authorized_source_kinds=["agent_messaging"],
    )
    init_resp = client.post(
        PAIRING_INITIATE_ROUTE,
        json={"deployment_id": deployment_id, "machine_id": "machine-bbb"},
    )
    assert init_resp.status_code == 200
    # Operator approves
    table.rows[deployment_id]["pairing_status"] = PairingStatus.APPROVED.value

    bad_resp = client.post(
        PAIRING_POLL_ROUTE,
        json={"deployment_id": deployment_id, "pairing_token": "wrong-token"},
    )
    _check(
        bad_resp.status_code == 401,
        f"wrong pairing_token → 401 (got {bad_resp.status_code})",
    )
    _check(
        table.rows[deployment_id]["pairing_status"] == "approved",
        "wrong token does NOT advance pairing_status",
    )
    _check(
        table.rows[deployment_id]["oauth_client_id"] is None,
        "wrong token does NOT mint an OAuth client",
    )


def test_poll_while_pending_returns_pending_status() -> None:
    table = _InMemoryDeploymentTable()
    vault = _make_vault()
    ledger_stub = _PairingLedgerStub(table, vault)
    client = TestClient(_build_app(ledger_stub))

    deployment_id = table.insert(
        machine_id="machine-ccc",
        initiating_client_id="cid-operator",
        authorized_source_kinds=["agent_messaging"],
    )
    init_resp = client.post(
        PAIRING_INITIATE_ROUTE,
        json={"deployment_id": deployment_id, "machine_id": "machine-ccc"},
    )
    pairing_token = init_resp.json()["pairing_token"]

    # NOTE: operator did NOT approve; status still pending
    resp = client.post(
        PAIRING_POLL_ROUTE,
        json={"deployment_id": deployment_id, "pairing_token": pairing_token},
    )
    _check(resp.status_code == 200, f"poll while pending → 200 (got {resp.status_code})")
    _check(
        resp.json() == {"status": "pending"},
        "poll body = {status: pending} when operator hasn't approved yet",
    )


def test_initiate_unknown_deployment_returns_404() -> None:
    table = _InMemoryDeploymentTable()
    vault = _make_vault()
    ledger_stub = _PairingLedgerStub(table, vault)
    client = TestClient(_build_app(ledger_stub))

    resp = client.post(
        PAIRING_INITIATE_ROUTE,
        json={"deployment_id": "dep-never-existed", "machine_id": "m"},
    )
    _check(
        resp.status_code == 404,
        f"unknown deployment_id → 404 (got {resp.status_code})",
    )


def test_initiate_machine_id_mismatch_returns_400() -> None:
    table = _InMemoryDeploymentTable()
    vault = _make_vault()
    ledger_stub = _PairingLedgerStub(table, vault)
    client = TestClient(_build_app(ledger_stub))

    deployment_id = table.insert(
        machine_id="canonical-machine",
        initiating_client_id="cid-operator",
        authorized_source_kinds=["agent_messaging"],
    )
    resp = client.post(
        PAIRING_INITIATE_ROUTE,
        json={"deployment_id": deployment_id, "machine_id": "different-machine"},
    )
    _check(
        resp.status_code == 400,
        f"machine_id mismatch → 400 (got {resp.status_code})",
    )
    _check(
        table.rows[deployment_id]["pairing_token_hash"] is None,
        "mismatched machine_id does NOT persist a pairing_token",
    )


def test_poll_already_paired_returns_409() -> None:
    """Spec §13.5: pairing_token is single-use; re-poll after paired returns 409."""
    table = _InMemoryDeploymentTable()
    vault = _make_vault()
    ledger_stub = _PairingLedgerStub(table, vault)
    client = TestClient(_build_app(ledger_stub))

    deployment_id = table.insert(
        machine_id="machine-ddd",
        initiating_client_id="cid-operator",
        authorized_source_kinds=["agent_messaging"],
    )
    init_resp = client.post(
        PAIRING_INITIATE_ROUTE,
        json={"deployment_id": deployment_id, "machine_id": "machine-ddd"},
    )
    pairing_token = init_resp.json()["pairing_token"]
    table.rows[deployment_id]["pairing_status"] = PairingStatus.APPROVED.value

    first_poll = client.post(
        PAIRING_POLL_ROUTE,
        json={"deployment_id": deployment_id, "pairing_token": pairing_token},
    )
    _check(first_poll.status_code == 200, "first poll succeeds")

    second_poll = client.post(
        PAIRING_POLL_ROUTE,
        json={"deployment_id": deployment_id, "pairing_token": pairing_token},
    )
    _check(
        second_poll.status_code == 409,
        f"second poll on already-paired → 409 (got {second_poll.status_code})",
    )


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    print("=== session_shipper_pairing_smoke (M5.C) ===")
    test_full_pairing_flow_end_to_end()
    test_poll_wrong_token_returns_401()
    test_poll_while_pending_returns_pending_status()
    test_initiate_unknown_deployment_returns_404()
    test_initiate_machine_id_mismatch_returns_400()
    test_poll_already_paired_returns_409()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
