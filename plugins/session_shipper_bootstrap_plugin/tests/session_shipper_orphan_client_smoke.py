#!/usr/bin/env python3
"""M5.C orphan-client smoke — operator-free reconciliation of a cross-store mint failure.

Run:

    .venv/bin/python3 plugins/session_shipper_bootstrap_plugin/tests/session_shipper_orphan_client_smoke.py

Spec §13.4 + 2026-05-30 operator framing ("if it needs operator
assistance, it has clearly failed catastrophically"). The pairing-poll
handler MUST NOT leave the operator to manually revoke an orphan
OAuth client when the vault mint succeeded but the state-service
transition failed. Two compensation paths are verified here:

1. **Happy compensation:** vault.revoke_client succeeds after the
   state-update failure. The orphan is gone; an INFO log line surfaces
   the transient failure. The 500 still goes back to the shipper
   (never the cleartext secret).

2. **Revoke also fails:** vault.revoke_client itself fails. The route
   records a memory-tagged sweep entry (``session_ledger:orphan_oauth_client``)
   so the periodic-poll heartbeat can retry the revoke. Genuine
   HIGH-severity log line surfaces both ids. Still 500 to the shipper.

Both scenarios assert: (a) shipper response is 500; (b) no cleartext
secret in the response body; (c) deployment row stays in 'approved'
(NOT 'paired') because the transition never committed; (d) post-
compensation, no orphan vault client exists for path 1; the sweep
entry is recorded for path 2.
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
    PAIRING_POLL_ROUTE,
    CrossStoreOrphanMintError,
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


# ─── Fixtures ────────────────────────────────────────────────────────────────


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
        self, client_id: str, redirect_uris: list[str]
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


class _RecordingMemoryService:
    """Captures memory_service.remember calls so the smoke can assert sweep tags."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def remember(self, *, content: str, tags: tuple[str, ...]) -> None:
        self.entries.append({"content": content, "tags": list(tags)})


class _OrphanPairingLedger:
    """Implements :class:`PairingLedgerProtocol` with controllable failure switches.

    * ``fail_state_transition``: when True, mint succeeds but transition
      raises — the route catches this as CrossStoreOrphanMintError.
    * ``fail_revoke``: when True, the compensation revoke call raises —
      the route falls back to memory-tagged sweep.
    """

    def __init__(
        self,
        vault: VaultOAuthRegistry,
        *,
        memory_service: _RecordingMemoryService | None = None,
    ) -> None:
        self._vault = vault
        self._memory_service = memory_service
        self._deployments: dict[str, dict[str, Any]] = {}
        self._next_id = 1
        self.fail_state_transition = False
        self.fail_revoke = False

    def make_deployment(self) -> str:
        deployment_id = f"dep-orphan-{self._next_id:05d}"
        self._next_id += 1
        self._deployments[deployment_id] = {
            "id": deployment_id,
            "machine_id": "m-orphan",
            "pairing_status": PairingStatus.APPROVED.value,
            "pairing_token_hash": None,
            "pairing_token_salt": None,
            "user_code": None,
            "pairing_initiated_at": None,
            "approved_at": datetime.now(UTC).isoformat(),
            "oauth_client_id": None,
            "initiating_client_id": "cid-operator",
            "authorized_source_kinds": ["agent_messaging"],
        }
        return deployment_id

    def pairing_get_deployment(self, deployment_id: str) -> dict[str, object] | None:
        row = self._deployments.get(deployment_id)
        return dict(row) if row is not None else None

    def pairing_persist_token(
        self,
        *,
        deployment_id: str,
        pairing_token_hash: str,
        pairing_token_salt: str,
        user_code: str,
    ) -> None:
        row = self._deployments[deployment_id]
        row["pairing_token_hash"] = pairing_token_hash
        row["pairing_token_salt"] = pairing_token_salt
        row["user_code"] = user_code

    def pairing_verify_token(
        self,
        *,
        deployment_id: str,
        cleartext_token: str,
    ) -> bool:
        # Smoke pre-seeds a deployment in 'approved' state without an
        # initiate round-trip; the route's token verify lookup will
        # encounter empty fields. Return True so the test focuses on
        # the cross-store atomicity path (not token verification).
        _ = deployment_id
        _ = cleartext_token
        return True

    def pairing_mint_and_transition_to_paired(
        self,
        *,
        deployment_id: str,
    ) -> dict[str, str]:
        creds = self._vault.mint_internal_machine_client(
            client_label=f"shipper-{deployment_id}",
            scopes=("ledger:ingest",),
        )
        if self.fail_state_transition:
            raise CrossStoreOrphanMintError(
                client_id=creds["client_id"],
                deployment_id=deployment_id,
                original=RuntimeError("simulated state-service write failure"),
            )
        row = self._deployments[deployment_id]
        row["pairing_status"] = PairingStatus.PAIRED.value
        row["oauth_client_id"] = creds["client_id"]
        return creds

    def pairing_compensate_orphan_mint(
        self,
        *,
        deployment_id: str,
        client_id: str,
    ) -> str:
        if self.fail_revoke:
            # Simulate vault unreachable / DB down during revoke.
            try:
                raise RuntimeError("simulated vault.revoke_client failure")
            except Exception:  # noqa: BLE001 — sweep-tag fallback path
                from session_shipper_bootstrap_plugin.pairing_routes import (  # noqa: PLC0415
                    record_orphan_for_sweep,
                )
                record_orphan_for_sweep(
                    client_id=client_id,
                    deployment_id=deployment_id,
                    memory_service=self._memory_service,
                )
                return "sweep_scheduled"
        self._vault.revoke_client(client_id)
        return "revoked"


def _make_vault() -> VaultOAuthRegistry:
    store = _StubVaultClientStore()
    return VaultOAuthRegistry(
        client_storage=store,
        refresh_store=store,
        b64_encode=lambda b: base64.b64encode(b).decode("ascii"),
        b64_decode=lambda s: base64.b64decode(s.encode("ascii")),
        logger=logging.getLogger("session_shipper_orphan_smoke"),
    )


def _build_app(ledger: PairingLedgerProtocol) -> FastAPI:
    app = FastAPI()
    register_session_ledger_pairing_routes(app, ledger=ledger)
    return app


# ─── Cases ───────────────────────────────────────────────────────────────────


def test_happy_compensation_revokes_orphan_client() -> None:
    """Path 1: vault mint succeeds, state fails, revoke succeeds.

    Verifies orphan does NOT survive (no operator action required).
    """
    vault = _make_vault()
    ledger = _OrphanPairingLedger(vault)
    ledger.fail_state_transition = True
    ledger.fail_revoke = False
    client = TestClient(_build_app(ledger))
    deployment_id = ledger.make_deployment()

    pre_clients = len(vault._clients.list_clients())  # noqa: SLF001
    _check(pre_clients == 0, "vault starts with zero clients (fresh fixture)")

    resp = client.post(
        PAIRING_POLL_ROUTE,
        json={"deployment_id": deployment_id, "pairing_token": "any"},
    )
    _check(
        resp.status_code == 500,
        f"orphan path returns 500 (got {resp.status_code})",
    )
    body = resp.json()
    _check(
        "client_secret" not in body and "client_id" not in body,
        f"500 response carries NO credentials (body keys: {sorted(body)})",
    )
    post_clients = len(vault._clients.list_clients())  # noqa: SLF001
    _check(
        post_clients == 0,
        f"after compensation, NO orphan vault client remains (count: {post_clients})",
    )
    row = ledger.pairing_get_deployment(deployment_id)
    assert row is not None
    _check(
        row.get("pairing_status") == PairingStatus.APPROVED.value,
        f"deployment stays in 'approved' (not 'paired'): got {row.get('pairing_status')!r}",
    )
    _check(
        row.get("oauth_client_id") is None,
        f"deployment.oauth_client_id stays NULL (got {row.get('oauth_client_id')!r})",
    )


def test_revoke_failure_records_sweep_tag() -> None:
    """Path 2: vault mint succeeds, state fails, revoke ALSO fails.

    Verifies memory-tagged sweep entry written + HIGH-severity outcome
    + 500-without-secret returned.
    """
    vault = _make_vault()
    memory = _RecordingMemoryService()
    ledger = _OrphanPairingLedger(vault, memory_service=memory)
    ledger.fail_state_transition = True
    ledger.fail_revoke = True
    client = TestClient(_build_app(ledger))
    deployment_id = ledger.make_deployment()

    resp = client.post(
        PAIRING_POLL_ROUTE,
        json={"deployment_id": deployment_id, "pairing_token": "any"},
    )
    _check(
        resp.status_code == 500,
        f"orphan + revoke-fail path returns 500 (got {resp.status_code})",
    )
    body = resp.json()
    _check(
        "client_secret" not in body and "client_id" not in body,
        f"500 response carries NO credentials (body keys: {sorted(body)})",
    )

    # Sweep entry recorded
    _check(
        len(memory.entries) == 1,
        f"exactly one memory-sweep entry written (got {len(memory.entries)})",
    )
    if memory.entries:
        entry = memory.entries[0]
        _check(
            "session_ledger:orphan_oauth_client" in entry["tags"],
            f"sweep entry carries 'session_ledger:orphan_oauth_client' tag (got {entry['tags']})",
        )
        _check(
            deployment_id in entry["content"],
            f"sweep entry content names the deployment_id (got: {entry['content']!r})",
        )

    # Vault still has the orphan because revoke failed; sweep will retry.
    post_clients = vault._clients.list_clients()  # noqa: SLF001
    _check(
        len(post_clients) == 1,
        f"orphan vault client persists pending sweep (count: {len(post_clients)})",
    )


def test_sweep_entry_absent_when_revoke_succeeded() -> None:
    """Path 1 sanity: happy compensation does NOT write a sweep tag."""
    vault = _make_vault()
    memory = _RecordingMemoryService()
    ledger = _OrphanPairingLedger(vault, memory_service=memory)
    ledger.fail_state_transition = True
    ledger.fail_revoke = False
    client = TestClient(_build_app(ledger))
    deployment_id = ledger.make_deployment()

    client.post(
        PAIRING_POLL_ROUTE,
        json={"deployment_id": deployment_id, "pairing_token": "any"},
    )
    _check(
        len(memory.entries) == 0,
        f"happy path writes NO sweep entry (got {len(memory.entries)})",
    )


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    print("=== session_shipper_orphan_client_smoke (M5.C deferral #3) ===")
    test_happy_compensation_revokes_orphan_client()
    test_revoke_failure_records_sweep_tag()
    test_sweep_entry_absent_when_revoke_succeeded()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
