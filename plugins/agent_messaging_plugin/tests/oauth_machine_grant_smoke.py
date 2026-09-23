#!/usr/bin/env python3
"""M5.A vault foundation smoke (no pytest; standalone hand-rolled fixtures).

Run with:

    .venv/bin/python3 plugins/agent_messaging_plugin/tests/oauth_machine_grant_smoke.py

Covers the M5 vault additions per spec §13.4 and §14.4:

1. ``oauth_client`` schema includes the two new BOOLEAN columns
   (``operator_equivalent`` + ``machine_grant_enabled``), both DEFAULT
   FALSE, both NOT NULL.
2. :func:`project_oauth_client_metadata` projects the two new fields
   with the same strict ``is True`` check as ``operator_approved`` —
   missing / non-bool values are False.
3. :meth:`VaultOAuthRegistry.mint_internal_machine_client` persists a
   record with ``operator_approved=False``, ``operator_equivalent=False``,
   ``machine_grant_enabled=True``, ``grant_types=["client_credentials"]``,
   ``redirect_uris=[]``, and returns ``(client_id, client_secret)`` once.
4. :meth:`VaultOAuthRegistry.is_operator_equivalent` returns ``True`` iff
   the client carries ``operator_equivalent=True``; absent / missing
   client returns ``False``.
5. :func:`_require_grant_eligible` accepts ``operator_approved=True``
   alone, accepts ``machine_grant_enabled=True`` alone, rejects when
   both are False, and rejects on missing / malformed metadata.
6. ``mint_internal_machine_client`` input validation: empty
   ``client_label`` / empty ``scopes`` / ``deliver_secret_to_caller=False``
   each raise ``ValueError``.
"""

from __future__ import annotations

# ruff: noqa: E402
import base64
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ananta.vault_core import (
    VaultOAuthRegistry,
    project_oauth_client_metadata,
)
from macos_vault_plugin.schema import get_oauth_client_schema

from agent_messaging_plugin.mcp_streamable.oauth import _require_grant_eligible

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


# ─── Fixtures ───────────────────────────────────────────────────────────────


class _FakeOauthStore:
    """Minimal OAuthClientStorage + RefreshTokenStorage impl for the registry."""

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


def _make_registry() -> tuple[VaultOAuthRegistry, _FakeOauthStore]:
    store = _FakeOauthStore()
    registry = VaultOAuthRegistry(
        client_storage=store,
        refresh_store=store,
        b64_encode=lambda b: base64.b64encode(b).decode("ascii"),
        b64_decode=lambda s: base64.b64decode(s.encode("ascii")),
        logger=logging.getLogger("oauth_machine_grant_smoke"),
    )
    return registry, store


# ─── Cases ──────────────────────────────────────────────────────────────────


def test_schema_includes_new_columns() -> None:
    schema = get_oauth_client_schema()
    cols = schema.columns
    _check(
        "operator_equivalent" in cols,
        "schema declares operator_equivalent column",
    )
    _check(
        "machine_grant_enabled" in cols,
        "schema declares machine_grant_enabled column",
    )
    if "operator_equivalent" in cols:
        opeq = cols["operator_equivalent"]
        _check(
            opeq.type.value == "BOOLEAN" and opeq.not_null and opeq.default == "false",
            "operator_equivalent is BOOLEAN NOT NULL DEFAULT false",
        )
    if "machine_grant_enabled" in cols:
        mge = cols["machine_grant_enabled"]
        _check(
            mge.type.value == "BOOLEAN" and mge.not_null and mge.default == "false",
            "machine_grant_enabled is BOOLEAN NOT NULL DEFAULT false",
        )


def test_projection_includes_new_fields_strict_is_true() -> None:
    row_both: dict[str, object] = {
        "client_name": "x",
        "operator_equivalent": True,
        "machine_grant_enabled": True,
    }
    p = project_oauth_client_metadata("cid", row_both)
    _check(
        p.get("operator_equivalent") is True,
        "operator_equivalent=True projects through",
    )
    _check(
        p.get("machine_grant_enabled") is True,
        "machine_grant_enabled=True projects through",
    )

    row_truthy_non_bool: dict[str, object] = {
        "client_name": "x",
        "operator_equivalent": 1,  # truthy but NOT bool True
        "machine_grant_enabled": "true",
    }
    p = project_oauth_client_metadata("cid", row_truthy_non_bool)
    _check(
        p.get("operator_equivalent") is False,
        "operator_equivalent=1 (truthy non-bool) projects False (strict is-True)",
    )
    _check(
        p.get("machine_grant_enabled") is False,
        "machine_grant_enabled='true' (str) projects False (strict is-True)",
    )

    row_missing: dict[str, object] = {"client_name": "x"}
    p = project_oauth_client_metadata("cid", row_missing)
    _check(
        p.get("operator_equivalent") is False
        and p.get("machine_grant_enabled") is False,
        "missing fields both project False",
    )


def test_mint_internal_machine_client_happy_path() -> None:
    registry, store = _make_registry()
    minted = registry.mint_internal_machine_client(
        client_label="shipper-dep-aaa",
        scopes=("ledger:ingest",),
    )
    _check(
        isinstance(minted.get("client_id"), str)
        and isinstance(minted.get("client_secret"), str),
        "mint returns {client_id, client_secret}",
    )
    cid = minted["client_id"]
    record = store.rows.get(cid)
    _check(record is not None, "record persisted in storage")
    if record is None:
        return
    _check(
        record["operator_approved"] is False,
        "minted record: operator_approved=False",
    )
    _check(
        record["operator_equivalent"] is False,
        "minted record: operator_equivalent=False (shipper is NOT operator-equivalent)",
    )
    _check(
        record["machine_grant_enabled"] is True,
        "minted record: machine_grant_enabled=True (grant eligibility signal)",
    )
    _check(
        record["grant_types"] == ["client_credentials"],
        "minted record: grant_types == ['client_credentials']",
    )
    _check(
        record["redirect_uris"] == [],
        "minted record: redirect_uris == [] (no browser flow)",
    )
    _check(
        record["client_name"] == "shipper-dep-aaa",
        "minted record: client_name == client_label",
    )


def test_mint_input_validation() -> None:
    registry, _ = _make_registry()
    try:
        registry.mint_internal_machine_client(
            client_label="", scopes=("ledger:ingest",)
        )
    except ValueError as exc:
        _check(
            "client_label" in str(exc), "empty client_label raises ValueError"
        )
    else:
        _check(False, "expected ValueError on empty client_label")

    try:
        registry.mint_internal_machine_client(client_label="x", scopes=())
    except ValueError as exc:
        _check("scopes" in str(exc), "empty scopes raises ValueError")
    else:
        _check(False, "expected ValueError on empty scopes")

    try:
        registry.mint_internal_machine_client(
            client_label="x",
            scopes=("ledger:ingest",),
            deliver_secret_to_caller=False,
        )
    except ValueError as exc:
        _check(
            "deliver_secret_to_caller" in str(exc),
            "deliver_secret_to_caller=False raises ValueError (v1 contract)",
        )
    else:
        _check(False, "expected ValueError on deliver_secret_to_caller=False")


def test_is_operator_equivalent() -> None:
    registry, store = _make_registry()

    # Unknown client → False
    _check(
        registry.is_operator_equivalent("never-registered") is False,
        "is_operator_equivalent returns False for unknown client",
    )

    # Insert an operator_equivalent=True client
    store.rows["cid-op"] = {
        "client_id": "cid-op",
        "client_name": "operator",
        "operator_approved": True,
        "operator_equivalent": True,
        "machine_grant_enabled": False,
    }
    _check(
        registry.is_operator_equivalent("cid-op") is True,
        "is_operator_equivalent returns True when row.operator_equivalent=True",
    )

    # Insert an operator_approved=True but NOT operator_equivalent
    store.rows["cid-op-not-eq"] = {
        "client_id": "cid-op-not-eq",
        "client_name": "regular operator client",
        "operator_approved": True,
        "operator_equivalent": False,
        "machine_grant_enabled": False,
    }
    _check(
        registry.is_operator_equivalent("cid-op-not-eq") is False,
        "operator_approved alone does NOT confer operator_equivalent",
    )

    # Insert a machine-grant client
    store.rows["cid-shipper"] = {
        "client_id": "cid-shipper",
        "client_name": "shipper-dep-xxx",
        "operator_approved": False,
        "operator_equivalent": False,
        "machine_grant_enabled": True,
    }
    _check(
        registry.is_operator_equivalent("cid-shipper") is False,
        "machine_grant_enabled alone does NOT confer operator_equivalent",
    )


def test_require_grant_eligible_accepts_operator_approved() -> None:
    resp = _require_grant_eligible(
        {"operator_approved": True, "machine_grant_enabled": False}
    )
    _check(
        resp is None,
        "_require_grant_eligible accepts operator_approved=True alone",
    )


def test_require_grant_eligible_accepts_machine_grant() -> None:
    resp = _require_grant_eligible(
        {"operator_approved": False, "machine_grant_enabled": True}
    )
    _check(
        resp is None,
        "_require_grant_eligible accepts machine_grant_enabled=True alone "
        "(shipper-mint path; no operator_approved required)",
    )


def test_require_grant_eligible_rejects_neither_true() -> None:
    resp = _require_grant_eligible(
        {"operator_approved": False, "machine_grant_enabled": False}
    )
    _check(
        resp is not None and resp.status_code == 401,
        "rejects when both flags are False (401 invalid_client)",
    )


def test_require_grant_eligible_rejects_missing_metadata() -> None:
    resp = _require_grant_eligible(None)
    _check(
        resp is not None and resp.status_code == 401,
        "rejects None metadata (401)",
    )
    resp = _require_grant_eligible([])  # type: ignore[arg-type]
    _check(
        resp is not None and resp.status_code == 401,
        "rejects non-dict metadata (401)",
    )


def test_require_grant_eligible_strict_is_true() -> None:
    """Strict identity check: truthy non-bool values must NOT grant eligibility."""
    resp = _require_grant_eligible(
        {"operator_approved": 1, "machine_grant_enabled": "true"}
    )
    _check(
        resp is not None and resp.status_code == 401,
        "rejects truthy-but-non-bool values (no implicit coercion)",
    )


def main() -> int:
    print("=== oauth_machine_grant_smoke (M5.A vault foundation) ===")
    test_schema_includes_new_columns()
    test_projection_includes_new_fields_strict_is_true()
    test_mint_internal_machine_client_happy_path()
    test_mint_input_validation()
    test_is_operator_equivalent()
    test_require_grant_eligible_accepts_operator_approved()
    test_require_grant_eligible_accepts_machine_grant()
    test_require_grant_eligible_rejects_neither_true()
    test_require_grant_eligible_rejects_missing_metadata()
    test_require_grant_eligible_strict_is_true()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
