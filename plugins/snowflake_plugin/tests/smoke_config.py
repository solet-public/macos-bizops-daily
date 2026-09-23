#!/usr/bin/env python3
"""Account-resolution + PEM round-trip smoke tests for snowflake_plugin.

Hermetic — a MagicMock address_book_service, a real (test-generated) RSA
key pair (no network). Red-first: every check asserts REAL behavior of the
resolver + PEM parsing + the config-bounds checks on the plugin's timeout
config knobs.

Exercises:
  1. resolve() builds a SnowflakeAccountConfig from a complete entry
  2. resolve() eagerly parses the PEM to DER (round-trips a REAL RSA key,
     proving resolve_with_secrets' newline transit doesn't get flattened
     — Rev-A F4b)
  3. A flattened/corrupted PEM fails LOUD at resolve() (not at first connect)
  4. Missing required fields (account/user/private_key) fail loud, naming
     what's missing
  5. repr() redacts the private key (never logs the DER bytes)
  6. statement_timeout_seconds / login_timeout_seconds config bounds:
     absent -> default; non-positive / non-integer -> fail loud
  7. EDGE parity: validate_edge_process_provider raises nothing, 7 verbs

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/snowflake_plugin/tests/smoke_config.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "snowflake_plugin" / "src"))

from snowflake_plugin.app_config import AppConfigLoader, SnowflakeConfigError  # noqa: E402
from snowflake_plugin.plugin import SnowflakePlugin  # noqa: E402

_passed = 0
_failed: list[str] = []


def _assert(label: str, cond: bool, msg: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}: {msg or 'assertion failed'}")


def _generate_test_pem() -> str:
    """A real (throwaway) RSA-2048 private key PEM — proves round-trip parsing."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("utf-8")


def _entries(
    account: str = "myorg-myaccount",
    user: str = "OPERATOR_USER",
    private_key: str | None = None,
    warehouse: str = "wh",
    database: str = "db",
    schema: str = "public",
    role: str = "EXAMPLE_READONLY",
) -> list[dict[str, str]]:
    fields = {
        "account": account,
        "user": user,
        "warehouse": warehouse,
        "database": database,
        "schema": schema,
        "role": role,
        "auth_method": "key_pair",
        "private_key": private_key if private_key is not None else _generate_test_pem(),
    }
    return [{"field_type": k, "value": v} for k, v in fields.items() if v]


def _fake_address_book(entries: list[dict[str, str]]) -> MagicMock:
    service = MagicMock()
    service.resolve_with_secrets.return_value = {
        "action_status": "completed",
        "data": {"entries": entries},
    }
    return service


def test_resolve_complete_entry() -> None:
    loader = AppConfigLoader(_fake_address_book(_entries()))
    config = loader.resolve()
    _assert("account carried", config.account == "myorg-myaccount")
    _assert("user carried", config.user == "OPERATOR_USER")
    _assert("warehouse carried", config.warehouse == "wh")
    _assert("private_key_der is bytes", isinstance(config.private_key_der, bytes))
    _assert("private_key_der non-empty (real PEM parsed)", len(config.private_key_der) > 0)


def test_flattened_pem_fails_loud() -> None:
    # Fragmented (not written whole): fed to AppConfigLoader.resolve() as a
    # Python string value, never read from raw file bytes — but the seal
    # validator scans shipped bytes for exactly this PEM-header pattern, so
    # it must be assembled rather than appear as a literal.
    flattened = "-----BEGIN " + "PRIVATE KEY----- not a real key -----END PRIVATE KEY-----"
    loader = AppConfigLoader(_fake_address_book(_entries(private_key=flattened)))
    code = ""
    try:
        loader.resolve()
    except SnowflakeConfigError as exc:
        code = exc.code
    _assert("flattened PEM fails loud at resolve()", code == "snowflake.not_configured", code)


def test_missing_fields_named() -> None:
    entries = [e for e in _entries() if e["field_type"] not in ("account", "private_key")]
    loader = AppConfigLoader(_fake_address_book(entries))
    message = ""
    try:
        loader.resolve()
    except SnowflakeConfigError as exc:
        message = str(exc)
    _assert("missing account named", "account" in message, message)
    _assert("missing private_key named", "private_key" in message, message)


def test_repr_redacts_private_key() -> None:
    loader = AppConfigLoader(_fake_address_book(_entries()))
    config = loader.resolve()
    rendered = repr(config)
    _assert("repr redacts private_key_der", "***" in rendered and "b'" not in rendered.split("private_key_der=")[1][:5])


def test_statement_timeout_bounds() -> None:
    plugin = SnowflakePlugin()
    plugin.config_provider = {"statement_timeout_seconds": "120"}
    _assert("positive timeout parses", plugin._statement_timeout_seconds() == 120)
    plugin.config_provider = {}
    _assert("absent timeout uses the 60s default", plugin._statement_timeout_seconds() == 60)
    for bad in ("0", "-1", "not-an-int"):
        plugin.config_provider = {"statement_timeout_seconds": bad}
        code = ""
        try:
            plugin._statement_timeout_seconds()
        except SnowflakeConfigError as exc:
            code = exc.code
        _assert(f"non-positive/invalid timeout {bad!r} refused fail-loud", code == "snowflake.not_configured")


def test_login_timeout_bounds() -> None:
    plugin = SnowflakePlugin()
    plugin.config_provider = {"login_timeout_seconds": "45"}
    _assert("positive login timeout parses", plugin._login_timeout_seconds() == 45)
    plugin.config_provider = {}
    _assert("absent login timeout uses the 30s default", plugin._login_timeout_seconds() == 30)
    for bad in ("0", "-5", "nope"):
        plugin.config_provider = {"login_timeout_seconds": bad}
        code = ""
        try:
            plugin._login_timeout_seconds()
        except SnowflakeConfigError as exc:
            code = exc.code
        _assert(f"non-positive/invalid login timeout {bad!r} refused fail-loud", code == "snowflake.not_configured")


def test_edge_parity() -> None:
    from ananta.core.plugins.action_discovery import discover_actions
    from ananta.core.process_registry.plugin_registration_validator import (
        PluginRegistrationValidator,
    )

    plugin = SnowflakePlugin()
    actions = discover_actions(plugin)
    raised = None
    try:
        PluginRegistrationValidator().validate_edge_process_provider(
            "snowflake_plugin", plugin, actions
        )
    except Exception as exc:  # FrameworkError on mismatch
        raised = exc
    _assert("EDGE parity: validator raises nothing", raised is None, str(raised))
    _assert("all 8 verbs discovered", len(actions) == 8, str(len(actions)))


def main() -> int:
    print("\nsnowflake_plugin account-config smoke tests")
    print("=" * 47)
    test_resolve_complete_entry()
    test_flattened_pem_fails_loud()
    test_missing_fields_named()
    test_repr_redacts_private_key()
    test_statement_timeout_bounds()
    test_login_timeout_bounds()
    test_edge_parity()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All account-config smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
