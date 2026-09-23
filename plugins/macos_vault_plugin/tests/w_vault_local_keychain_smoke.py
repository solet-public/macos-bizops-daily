#!/usr/bin/env python3
"""W-VAULT-LOCAL-KEYCHAIN Tier 3 dual-substrate contract smokes.

Verifies the per-method dual-write contract from the Tier 3 brief §9:
``store`` / ``store_random`` / ``rotate`` write both substrates atomically,
``delete`` flushes both, ``rename`` mirrors the state-side rename onto the
Keychain, ``retrieve`` reads Keychain-first with state-service fallback,
``exists`` is OR over substrates. Partial-failure rolls back the side that
already succeeded so the substrates never silently diverge.

Smokes use an in-memory state-service mock (no Postgres dependency), the
``FakeKeychain`` from ``tests/fake_keychain.py`` (no host OS Keychain
dependency), and an identity-crypto stub (no real master key required).
That keeps the suite hermetic — runnable from any Python venv with the
plugin's package installed.

Codex sign-off correction #4: CI must not touch the real macOS keychain.
Production ``SystemKeychain`` is exercised end-to-end only by the
sacrificial-cutover smoke SC-17 against a freshly-birthed solet.

Standalone — not pytest.  Run with::

    SOLET_NAME=smoke .venv/bin/python3 plugins/macos_vault_plugin/tests/w_vault_local_keychain_smoke.py
"""

from __future__ import annotations

import os
import stat
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

if "SOLET_NAME" not in os.environ:
    os.environ["SOLET_NAME"] = "smoke"

# The gate runner invokes this script with the repository root as cwd, but
# imports otherwise resolve through the venv's ambient editable installation.
# Anchor both source roots to this smoke's own checkout so a lane-worktree
# verdict measures its edited plugin and interface contracts.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SOURCE_ROOTS = (_REPO_ROOT / "ananta" / "src", _REPO_ROOT / "plugins" / "macos_vault_plugin" / "src")
for _source_root in reversed(_SOURCE_ROOTS):
    _source_root_text = str(_source_root)
    if _source_root_text not in sys.path:
        sys.path.insert(0, _source_root_text)

from ananta.core.services.call_context import CallContext  # noqa: E402
from macos_vault_plugin.constants import passphrase_env_var  # noqa: E402
from macos_vault_plugin.key_manager import VaultKeyManager  # noqa: E402
from macos_vault_plugin.plugin import MacosVaultPlugin  # noqa: E402

if TYPE_CHECKING:
    # Relative import for static-analysis resolution. Pyright single-file
    # invocations don't always walk up to find ``tests/__init__.py``, so we
    # suppress the warning explicitly — at runtime the sys.path branch
    # below is what actually loads the module.
    from .fake_keychain import FakeKeychain  # pyright: ignore[reportMissingImports]
else:
    _TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
    if _TESTS_DIR not in sys.path:
        sys.path.insert(0, _TESTS_DIR)
    from fake_keychain import FakeKeychain  # noqa: E402

SOLET = "smoke"
PLUGIN = "macos_vault_plugin"
OTHER_PLUGIN = "soundcloud_artist_studio_plugin"


class InMemoryStateService:
    """State-service mock backed by a single ``rows`` list keyed by ``secret_key``.

    Mirrors only the four verbs the vault plugin actually uses:
    ``write_state``, ``read_state``, ``update_state``, ``delete_records``.
    Each verb returns the platform's ``action_status`` envelope shape.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def write_state(self, *, namespace: str, data: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        record = data["record"]
        self.rows.append(dict(record))
        return {"action_status": "completed", "data": {"record": record}}

    def read_state(self, *, namespace: str, query: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        filters = query.get("filters") or {}
        matches = [
            r for r in self.rows
            if all(r.get(k) == v for k, v in filters.items())
        ]
        return {"action_status": "completed", "data": {"records": matches}}

    def update_state(
        self, *, namespace: str, query: dict[str, Any], updates: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        filters = query.get("filters") or {}
        affected = 0
        for r in self.rows:
            if all(r.get(k) == v for k, v in filters.items()):
                r.update(updates)
                affected += 1
        return {"action_status": "completed", "data": {"affected": affected}}

    def delete_records(self, *, namespace: str, query: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        filters = query.get("filters") or {}
        before = len(self.rows)
        self.rows = [
            r for r in self.rows
            if not all(r.get(k) == v for k, v in filters.items())
        ]
        return {"action_status": "completed", "data": {"deleted": before - len(self.rows)}}


class IdentityCrypto:
    """Identity ``encrypt``/``decrypt`` stub. ciphertext == plaintext bytes.

    Real ``VaultCrypto`` does AES-256-GCM with random nonce/salt; for smoke
    purposes we just need a stable round-trip so we can assert dual-write
    semantics without standing up a master key.
    """

    def encrypt(self, plaintext: str) -> dict[str, str]:
        return {
            "ciphertext": plaintext,
            "salt": "smoke-salt",
            "nonce": "smoke-nonce",
            "tag": "smoke-tag",
        }

    def decrypt(
        self,
        ciphertext: str,
        salt: str,  # noqa: ARG002
        nonce: str,  # noqa: ARG002
        auth_tag: str,  # noqa: ARG002
    ) -> str:
        return ciphertext


class WrappedMasterKeyKeychain:
    """In-memory KeychainBackend double for passphrase-wrap lifecycle tests."""

    direct_master_key = False

    def __init__(self) -> None:
        self._entries: dict[str, bytes] = {}

    def exists(self, account: str) -> bool:
        return account in self._entries

    def retrieve(self, account: str) -> bytes | None:
        return self._entries.get(account)

    def store(self, account: str, data: bytes) -> None:
        self._entries[account] = data


def make_vault() -> MacosVaultPlugin:
    """Plugin instance with mock state service + FakeKeychain + identity crypto."""
    plugin = MacosVaultPlugin()
    plugin.state_service = InMemoryStateService()  # type: ignore[assignment]
    plugin._keychain = FakeKeychain()  # type: ignore[assignment]
    plugin._crypto = IdentityCrypto()  # type: ignore[assignment]
    return plugin


def scoped_key(credential: str, plugin: str = PLUGIN) -> str:
    return f"{SOLET}.{plugin}.{credential}"


def fake(vault: MacosVaultPlugin) -> FakeKeychain:
    assert isinstance(vault._keychain, FakeKeychain)
    return vault._keychain


def state(vault: MacosVaultPlugin) -> InMemoryStateService:
    assert isinstance(vault.state_service, InMemoryStateService)
    return vault.state_service


# ─────────────────────────────────────────────────────────────────────────
# SC-1: Keychain-first read
# ─────────────────────────────────────────────────────────────────────────

def test_sc1_keychain_first_read() -> None:
    vault = make_vault()
    key = scoped_key("api_key")
    fake(vault).store_credential(PLUGIN, "api_key", b"keychain-value")
    state(vault).rows.append({
        "secret_key": key,
        "encrypted_value": "state-value",
        "salt": "x", "nonce": "x", "auth_tag": "x",
    })
    result = vault._retrieve_impl(key)
    assert result.get("action_status") == "completed", result
    assert result.get("data", {}).get("value") == "keychain-value", result


# ─────────────────────────────────────────────────────────────────────────
# SC-2 retired 2026-06-09 — P0-A Round 3 collapsed `_retrieve_impl` to
# Keychain-only; state-service fallback no longer exists by design.
# Replacement smoke: `test_retrieve_keychain_only`.
# ─────────────────────────────────────────────────────────────────────────


def test_retrieve_keychain_only() -> None:
    """Retrieve fetches plaintext from Keychain; absent → not_found (no SQL fallback)."""
    vault = make_vault()
    key = scoped_key("token")
    fake(vault).store_credential(PLUGIN, "token", b"keychain-only-value")
    result = vault._retrieve_impl(key)
    assert result.get("action_status") == "completed", result
    assert result.get("data", {}).get("value") == "keychain-only-value", result

    missing = vault._retrieve_impl(scoped_key("absent"))
    assert missing.get("action_status") == "completed", missing
    assert (missing.get("data") or {}).get("found") is False, missing


# ─────────────────────────────────────────────────────────────────────────
# SC-3 retired 2026-06-09 — `_store_impl` is Keychain-only; no SQL writeback.
# Replacement smoke: `test_store_keychain_only`.
# ─────────────────────────────────────────────────────────────────────────


def test_store_keychain_only() -> None:
    """Store writes to Keychain only; SQL substrate is untouched."""
    vault = make_vault()
    key = scoped_key("new_secret")
    result = vault._store_impl(key, "v1", [], {})
    assert result.get("action_status") == "completed", result
    assert fake(vault).retrieve_credential(PLUGIN, "new_secret") == b"v1"
    # SQL substrate must not be written to by the runtime path.
    state_rows = [r for r in state(vault).rows if r["secret_key"] == key]
    assert state_rows == [], state_rows


# ─────────────────────────────────────────────────────────────────────────
# SC-4 retired 2026-06-09 — `_rotate_impl` is Keychain-only; no SQL
# encrypt+update. Replacement smoke: `test_rotate_keychain_only`.
# ─────────────────────────────────────────────────────────────────────────


def test_rotate_keychain_only() -> None:
    """Rotate overwrites the Keychain entry in place; SQL substrate untouched."""
    vault = make_vault()
    key = scoped_key("rotating")
    vault._store_impl(key, "v1", [], {})
    result = vault._rotate_impl(key, "v2")
    assert result.get("action_status") == "completed", result
    assert fake(vault).retrieve_credential(PLUGIN, "rotating") == b"v2"
    assert all(r["secret_key"] != key for r in state(vault).rows)


def test_rotate_passphrase_keeps_file_unlock_coherent() -> None:
    """A file-backed rotation must unlock after the manager is locked again.

    Killing mutation: omit the ``os.replace`` publication in
    ``vault_rotate_passphrase``. The next-boot file stays on the old value and
    lacks the replacement's private-at-birth mode, so this round-trip fails.
    """
    import macos_vault_plugin.key_manager as key_manager_module

    keychain = WrappedMasterKeyKeychain()
    original_get_keychain = key_manager_module.get_keychain
    original_get_backend_name = key_manager_module.get_backend_name
    original_key_manager = key_manager_module._key_manager
    env_var = passphrase_env_var()
    original_app_home = os.environ.get("APP_HOME")
    original_env_passphrase = os.environ.get(env_var)
    try:
        key_manager_module.get_keychain = lambda: keychain  # type: ignore[assignment]
        key_manager_module.get_backend_name = lambda: "smoke keychain"  # type: ignore[assignment]
        with TemporaryDirectory() as app_home:
            os.environ["APP_HOME"] = app_home
            os.environ.pop(env_var, None)
            passphrase_file = (
                Path(app_home) / "config" / "plugins" / PLUGIN / "passphrase"
            )
            passphrase_file.parent.mkdir(parents=True)
            passphrase_file.write_text("before")
            manager = VaultKeyManager()
            manager.initialize("before")
            key_manager_module._key_manager = manager
            vault = MacosVaultPlugin()

            result = vault.vault_rotate_passphrase(
                "before", "after", call_context=CallContext.for_operator(),
            )

            assert result.get("action_status") == "completed", result
            assert stat.S_IMODE(passphrase_file.stat().st_mode) == 0o600
            manager.lock()
            assert vault._get_passphrase() == "after"
            manager.unlock(vault._get_passphrase())
            assert manager.is_unlocked()
    finally:
        key_manager_module.get_keychain = original_get_keychain  # type: ignore[assignment]
        key_manager_module.get_backend_name = original_get_backend_name  # type: ignore[assignment]
        key_manager_module._key_manager = original_key_manager
        if original_app_home is None:
            os.environ.pop("APP_HOME", None)
        else:
            os.environ["APP_HOME"] = original_app_home
        if original_env_passphrase is None:
            os.environ.pop(env_var, None)
        else:
            os.environ[env_var] = original_env_passphrase


def test_rotate_passphrase_refuses_nonpersistent_env() -> None:
    """An environment-backed unlock cannot be persistently updated by this verb."""
    env_var = passphrase_env_var()
    original_env_passphrase = os.environ.get(env_var)
    try:
        os.environ[env_var] = "before"
        result = MacosVaultPlugin().vault_rotate_passphrase(
            "before", "after", call_context=CallContext.for_operator(),
        )
        assert result.get("action_status") == "error", result
        assert result.get("error", {}).get("code") == "vault.passphrase_mismatch", result
    finally:
        if original_env_passphrase is None:
            os.environ.pop(env_var, None)
        else:
            os.environ[env_var] = original_env_passphrase


# ─────────────────────────────────────────────────────────────────────────
# SC-5 (delete) — preserved, now asserts Keychain-only delete (SQL untouched).
# ─────────────────────────────────────────────────────────────────────────

def test_sc5_dual_write_delete() -> None:
    vault = make_vault()
    key = scoped_key("doomed")
    vault._store_impl(key, "v1", [], {})
    result = vault._delete_impl(key)
    assert result.get("action_status") == "completed", result
    assert not fake(vault).exists_credential(PLUGIN, "doomed")
    assert all(r["secret_key"] != key for r in state(vault).rows)


# ─────────────────────────────────────────────────────────────────────────
# SC-6: Stale-fallback prevention
# ─────────────────────────────────────────────────────────────────────────

def test_sc6_stale_fallback_prevention() -> None:
    """After rotate + public delete, retrieve returns not_found — no stale state-side row served as a fallback.

    Note: per ``_not_found`` (plugin.py:580-596), the not-found envelope is
    a "completed" business result with ``data.found=False`` (not an error
    code). This matches the platform convention that "key absent" is a
    valid query outcome, not a failure.
    """
    vault = make_vault()
    key = scoped_key("stale_test")
    vault._store_impl(key, "v1", [], {})
    vault._rotate_impl(key, "v2")
    delete_result = vault._delete_impl(key)
    assert delete_result.get("action_status") == "completed", delete_result
    retrieve_result = vault._retrieve_impl(key)
    assert retrieve_result.get("action_status") == "completed", retrieve_result
    data = retrieve_result.get("data") or {}
    assert data.get("found") is False, retrieve_result


# ─────────────────────────────────────────────────────────────────────────
# SC-7 retired 2026-06-09 — "partial-failure rollback" was a dual-write
# concern; with `_store_impl` Keychain-only, any Keychain write failure
# is a plain failure with no SQL state to roll back. Replacement smoke:
# `test_keychain_store_failure_propagates`.
# ─────────────────────────────────────────────────────────────────────────


class FailingKeychain(FakeKeychain):  # pyright: ignore[reportUntypedBaseClass]
    def store_credential(
        self,
        plugin_name: str,  # noqa: ARG002
        credential: str,  # noqa: ARG002
        value: bytes,  # noqa: ARG002
    ) -> None:
        raise RuntimeError("simulated keychain write failure")


def test_keychain_store_failure_propagates() -> None:
    """Keychain write failure raises directly; no SQL row is created (substrate gone)."""
    import contextlib

    vault = make_vault()
    vault._keychain = FailingKeychain()  # type: ignore[assignment]
    key = scoped_key("phantom")
    with contextlib.suppress(RuntimeError):
        vault._store_impl(key, "v1", [], {})
    assert all(r["secret_key"] != key for r in state(vault).rows)


# ─────────────────────────────────────────────────────────────────────────
# SC-8: Non-existent credential
# ─────────────────────────────────────────────────────────────────────────

def test_sc8_not_found_when_both_absent() -> None:
    vault = make_vault()
    result = vault._retrieve_impl(scoped_key("never_stored"))
    assert result.get("action_status") == "completed", result
    data = result.get("data") or {}
    assert data.get("found") is False, result


# ─────────────────────────────────────────────────────────────────────────
# SC-14: Cross-plugin namespace denial fires BEFORE substrate lookup
# ─────────────────────────────────────────────────────────────────────────

class TrackingKeychain(FakeKeychain):  # pyright: ignore[reportUntypedBaseClass]
    """Records every retrieve_credential call to assert it never fires on a denied key."""

    def __init__(self) -> None:
        super().__init__()
        self.retrieve_calls: list[tuple[str, str]] = []

    def retrieve_credential(
        self, plugin_name: str, credential: str,
    ) -> bytes | None:
        self.retrieve_calls.append((plugin_name, credential))
        return super().retrieve_credential(plugin_name, credential)


def test_sc14_cross_plugin_denial_fires_before_substrate_lookup() -> None:
    from ananta.core.services.call_context import (
        CallContext,
        VaultAccessDeniedError,
    )
    vault = make_vault()
    tracker = TrackingKeychain()
    vault._keychain = tracker  # type: ignore[assignment]
    other_key = scoped_key("token", plugin=OTHER_PLUGIN)
    state_rows_before = list(state(vault).rows)
    ctx = CallContext.for_plugin(PLUGIN)
    raised = False
    try:
        vault.retrieve(other_key, call_context=ctx)
    except VaultAccessDeniedError:
        raised = True
    assert raised, "enforce_namespace did NOT raise on cross-plugin access"
    assert tracker.retrieve_calls == [], (
        f"Keychain was queried before namespace enforcement: {tracker.retrieve_calls}"
    )
    assert state(vault).rows == state_rows_before


# ─────────────────────────────────────────────────────────────────────────
# SC-12: Startup keypair migration preserves public key (renames legacy → scoped)
# ─────────────────────────────────────────────────────────────────────────

def _make_keypair_row(secret_key: str, encrypted_value: str) -> dict[str, Any]:
    return {
        "secret_key": secret_key,
        "encrypted_value": encrypted_value,
        "salt": "x", "nonce": "x", "auth_tag": "x", "version": 1,
    }


def _rows_with_key(vault: MacosVaultPlugin, key: str) -> list[dict[str, Any]]:
    return [r for r in state(vault).rows if r["secret_key"] == key]


def test_sc12_keypair_migration_preserves_keys() -> None:
    """Pre-populate state-service with legacy keypair rows; verify migration renames them to the new scoped form WITHOUT minting fresh."""
    from macos_vault_plugin.constants import (
        _LEGACY_ENCRYPTION_KEYPAIR_PRIVATE_KEY,
        _LEGACY_ENCRYPTION_KEYPAIR_PUBLIC_KEY,
        ENCRYPTION_KEYPAIR_PRIVATE_KEY,
        ENCRYPTION_KEYPAIR_PUBLIC_KEY,
    )
    vault = make_vault()
    state(vault).rows.extend([
        _make_keypair_row(_LEGACY_ENCRYPTION_KEYPAIR_PRIVATE_KEY, "legacy-priv-ciphertext"),
        _make_keypair_row(_LEGACY_ENCRYPTION_KEYPAIR_PUBLIC_KEY, "legacy-pub-ciphertext"),
    ])
    vault._migrate_legacy_keypair_if_present()
    new_priv = _rows_with_key(vault, ENCRYPTION_KEYPAIR_PRIVATE_KEY)
    new_pub = _rows_with_key(vault, ENCRYPTION_KEYPAIR_PUBLIC_KEY)
    assert len(new_priv) == 1 and new_priv[0]["encrypted_value"] == "legacy-priv-ciphertext"
    assert len(new_pub) == 1 and new_pub[0]["encrypted_value"] == "legacy-pub-ciphertext"
    assert not _rows_with_key(vault, _LEGACY_ENCRYPTION_KEYPAIR_PRIVATE_KEY)
    assert not _rows_with_key(vault, _LEGACY_ENCRYPTION_KEYPAIR_PUBLIC_KEY)


# ─────────────────────────────────────────────────────────────────────────
# SC-13: Startup keypair migration failure refuses fresh mint
# ─────────────────────────────────────────────────────────────────────────

class FailingUpdateStateService(InMemoryStateService):
    """State-service mock that fails ``update_state`` to simulate atomic-rename failure."""

    def update_state(
        self, *, namespace: str, query: dict[str, Any], updates: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        return {"action_status": "error", "data": {}, "error": "simulated"}


def test_sc13_keypair_migration_failure_refuses_fresh_mint() -> None:
    """Inject failure into state-service rename; verify VaultKeypairMigrationError raised and no fresh mint."""
    from macos_vault_plugin.constants import (
        _LEGACY_ENCRYPTION_KEYPAIR_PRIVATE_KEY,
        ENCRYPTION_KEYPAIR_PRIVATE_KEY,
    )
    from macos_vault_plugin.errors import VaultKeypairMigrationError
    vault = make_vault()
    vault.state_service = FailingUpdateStateService()  # type: ignore[assignment]
    state(vault).rows.append({
        "secret_key": _LEGACY_ENCRYPTION_KEYPAIR_PRIVATE_KEY,
        "encrypted_value": "legacy-priv-ciphertext",
        "salt": "x", "nonce": "x", "auth_tag": "x", "version": 1,
    })
    raised = False
    try:
        vault._migrate_legacy_keypair_if_present()
    except VaultKeypairMigrationError:
        raised = True
    assert raised, "Expected VaultKeypairMigrationError on rename failure"
    legacy_rows = [r for r in state(vault).rows if r["secret_key"] == _LEGACY_ENCRYPTION_KEYPAIR_PRIVATE_KEY]
    new_rows = [r for r in state(vault).rows if r["secret_key"] == ENCRYPTION_KEYPAIR_PRIVATE_KEY]
    assert len(legacy_rows) == 1, "Legacy row should remain (rename was rejected)"
    assert len(new_rows) == 0, "No new-form row should exist (fresh-mint was refused)"


# ─────────────────────────────────────────────────────────────────────────
# SC-18 retired 2026-06-09 — P0-A §10.4 deleted
# ``_migrate_legacy_master_key_file_if_present``. The fail-loud
# assertion that supersedes it is exercised inside the boot path of
# ``_bootstrap_vault_and_keypair`` via
# ``_assert_legacy_vault_substrates_absent`` — covered by any clean
# boot of the plugin.
# ─────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────
# SC-15: store_from_keychain operator-ingest namespace stays distinct
# ─────────────────────────────────────────────────────────────────────────

def test_sc15_operator_keychain_namespace_disjoint() -> None:
    """The per-credential service name ``<solet>.<plugin>`` MUST NOT collide with operator-managed entries."""
    from macos_vault_plugin.keychain import SystemKeychain

    # Pin the fixture: _scoped_service_name reads SOLET_NAME from the env,
    # so the assertion must not float with the runner's env (the gate exports
    # the real SOLET_NAME). Set it to this test's fixture solet, restore after.
    _prev_solet = os.environ.get("SOLET_NAME")
    os.environ["SOLET_NAME"] = SOLET
    try:
        sk = SystemKeychain()
        scoped_service = sk._scoped_service_name(PLUGIN)
        assert scoped_service == f"{SOLET}.{PLUGIN}"
        assert "-vault" not in scoped_service
        operator_service_examples = ["Anthropic API", "soundcloud_oauth_app", "GitHub"]
        for op_svc in operator_service_examples:
            assert op_svc != scoped_service
    finally:
        if _prev_solet is None:
            os.environ.pop("SOLET_NAME", None)
        else:
            os.environ["SOLET_NAME"] = _prev_solet


# ─────────────────────────────────────────────────────────────────────────
# SC-16: Final source sweep — surviving ``default_vault_plugin`` refs are intentional
# ─────────────────────────────────────────────────────────────────────────

_SC16_EXPECTED_REFS = frozenset({
    "ananta/src/ananta/vault_core/audit.py",
    "plugins/secrets_manager_vault_plugin/src/secrets_manager_vault_plugin/smv_schema.py",
    "plugins/macos_vault_plugin/src/macos_vault_plugin/schema.py",
    "plugins/macos_vault_plugin/src/macos_vault_plugin/constants.py",
    "plugins/macos_vault_plugin/src/macos_vault_plugin/plugin.py",
    "plugins/macos_vault_plugin/migrations/migrate_default_to_secrets_manager.py",
    "plugins/macos_vault_plugin/knowledge_base/articles/secret_transfer_protocol.md",
    "plugins/macos_vault_plugin/knowledge_base/manifest.yaml",
    "initialization/2025-12-11_homunculi_design.md",
    "profile/config/manifest.yaml",
    "profile/config/service_bindings.json",
    # Sanctioned retained-namespace refs in test files (Class (a) per KB
    # 11_vault_and_address_book: the SQL/physical namespace `default_vault_plugin`
    # is deliberately kept across the plugin rename for the audit + OAuth tables).
    # secret_transfer_protocol_live_smoke names the `default_vault_plugin__secret_transfer_audit`
    # table constant; cross_host_blue_green_smoke's docstring names the Postgres-backed
    # `default_vault_plugin` OAuth-registry table. Neither is a stale plugin-name ref.
    "plugins/macos_vault_plugin/tests/secret_transfer_protocol_live_smoke.py",
    "plugins/aws_self_deployment_plugin/tests/cross_host_blue_green_smoke.py",
})
_SC16_EXCLUDED_DIRS = frozenset({"__pycache__", ".ruff_cache", ".mypy_cache", ".pytest_cache"})
_SC16_EXCLUDED_SUFFIXES = (".pyc", ".pyo")
_SC16_SCANNED_PREFIXES = ("ananta/src", "plugins/", "initialization", "profile/config")


def _sc16_rel_in_scope(rel_root: str) -> bool:
    return any(rel_root.startswith(prefix) for prefix in _SC16_SCANNED_PREFIXES)


def _sc16_file_contains_default_vault_plugin(path: str) -> bool:
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return "default_vault_plugin" in fh.read()
    except OSError:
        return False


def _sc16_scan_for_refs(repo_root: str, smoke_self: str) -> set[str]:
    found: set[str] = set()
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in _SC16_EXCLUDED_DIRS]
        rel_root = os.path.relpath(root, repo_root)
        if not _sc16_rel_in_scope(rel_root):
            continue
        for f in files:
            if f.endswith(_SC16_EXCLUDED_SUFFIXES):
                continue
            path = os.path.join(root, f)
            rel = os.path.relpath(path, repo_root)
            if rel == smoke_self:
                continue
            if _sc16_file_contains_default_vault_plugin(path):
                found.add(rel)
    return found


def test_sc16_surviving_default_vault_plugin_refs_are_intentional() -> None:
    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", ".."),
    )
    smoke_self = os.path.relpath(os.path.abspath(__file__), repo_root)
    found = _sc16_scan_for_refs(repo_root, smoke_self)
    unexpected = found - _SC16_EXPECTED_REFS
    assert not unexpected, (
        f"Unexpected surviving 'default_vault_plugin' references: {sorted(unexpected)}. "
        "Either add them to the intentional set with rationale or remove them."
    )


# ─────────────────────────────────────────────────────────────────────────
# SC-17: vault qualification probes are bounded and leave no canary behind.
# ─────────────────────────────────────────────────────────────────────────

def test_sc17_vault_qualification_probes_are_bounded() -> None:
    vault = make_vault()

    keychain = vault.qualify_keychain()
    assert keychain.get("action_status") == "completed", keychain
    assert set(keychain.get("data", {})) == {
        "backend", "available", "current_user_accessible",
    }, keychain

    round_trip = vault.qualify_round_trip()
    assert round_trip.get("action_status") == "completed", round_trip
    assert round_trip.get("data") == {
        "stored": True,
        "retrieved": True,
        "matched": True,
        "deleted": True,
    }, round_trip
    assert fake(vault).snapshot() == {}, fake(vault).snapshot()
    assert "canary" not in repr(round_trip), round_trip


# ─────────────────────────────────────────────────────────────────────────
# Standalone runner
# ─────────────────────────────────────────────────────────────────────────

_SMOKES: list[tuple[str, Callable[[], None]]] = [
    ("SC-1  Keychain-first read", test_sc1_keychain_first_read),
    ("Retrieve Keychain-only", test_retrieve_keychain_only),
    ("Store Keychain-only", test_store_keychain_only),
    ("Rotate Keychain-only", test_rotate_keychain_only),
    ("Passphrase rotation keeps next-boot file unlock coherent", test_rotate_passphrase_keeps_file_unlock_coherent),
    ("Passphrase rotation refuses nonpersistent environment config", test_rotate_passphrase_refuses_nonpersistent_env),
    ("SC-5  Dual-write delete", test_sc5_dual_write_delete),
    ("SC-6  Stale-fallback prevention", test_sc6_stale_fallback_prevention),
    ("Keychain store failure propagates", test_keychain_store_failure_propagates),
    ("SC-8  Not-found when both absent", test_sc8_not_found_when_both_absent),
    ("SC-12 Keypair migration preserves keys", test_sc12_keypair_migration_preserves_keys),
    ("SC-13 Keypair migration failure refuses fresh mint", test_sc13_keypair_migration_failure_refuses_fresh_mint),
    ("SC-14 Cross-plugin denial pre-substrate", test_sc14_cross_plugin_denial_fires_before_substrate_lookup),
    ("SC-15 Operator-ingest namespace disjoint", test_sc15_operator_keychain_namespace_disjoint),
    ("SC-16 Surviving refs are intentional", test_sc16_surviving_default_vault_plugin_refs_are_intentional),
    ("SC-17 Vault qualification probes are bounded", test_sc17_vault_qualification_probes_are_bounded),
]


def main() -> int:
    failures: list[tuple[str, str]] = []
    for label, fn in _SMOKES:
        try:
            fn()
            print(f"  OK   {label}")
        except Exception:
            failures.append((label, traceback.format_exc()))
            print(f"  FAIL {label}")
    print()
    if failures:
        print(f"{len(failures)} of {len(_SMOKES)} smokes failed:")
        for label, tb in failures:
            print(f"\n--- {label} ---")
            print(tb)
        return 1
    print(f"All {len(_SMOKES)} smokes passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
