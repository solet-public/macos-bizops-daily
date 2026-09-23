"""Snowflake account resolution from the address book.

The single "snowflake_account" entry carries literal fields
(account/user/warehouse/database/schema/role/auth_method) plus a
``vault::`` private-key reference the resolver swaps in via
``resolve_with_secrets`` (chain-consumed — see
:func:`constants.vault_key_for_private_key`). ``resolve`` returns a frozen
:class:`SnowflakeAccountConfig` whose ``repr`` redacts the private key.

The private key is a multi-line PEM travelling through a ``vault::``
reference. Newline handling is the one real risk (Rev-A F4b): a flattened key
must fail loudly HERE (at config resolution), not at first connect — so
``resolve`` parses the PEM eagerly via ``cryptography`` and raises
``SnowflakeConfigError`` on a malformed key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import serialization

from .constants import (
    ACCOUNT_ENTRY_NAME,
    AUTH_METHOD_KEY_PAIR,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
    ERROR_NOT_CONFIGURED,
    FIELD_ACCOUNT,
    FIELD_AUTH_METHOD,
    FIELD_DATABASE,
    FIELD_PRIVATE_KEY,
    FIELD_ROLE,
    FIELD_SCHEMA,
    FIELD_USER,
    FIELD_WAREHOUSE,
)

_PASSWORD_REDACTION = "***"


class SnowflakeConfigError(RuntimeError):
    """Raised when the Snowflake account cannot be resolved or is misconfigured."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, repr=False)
class SnowflakeAccountConfig:
    """A resolved Snowflake service-account configuration.

    ``private_key_der`` is the DER-encoded, unencrypted private key bytes
    ready for ``snowflake.connector.connect(private_key=...)``. ``repr``
    deliberately redacts it so logging the object never leaks the secret.
    """

    account: str
    user: str
    warehouse: str
    database: str
    schema: str
    role: str
    auth_method: str
    private_key_der: bytes

    def __repr__(self) -> str:
        return (
            f"SnowflakeAccountConfig(account={self.account!r}, user={self.user!r}, "
            f"warehouse={self.warehouse!r}, database={self.database!r}, "
            f"schema={self.schema!r}, role={self.role!r}, "
            f"auth_method={self.auth_method!r}, private_key_der={_PASSWORD_REDACTION!r})"
        )


class AppConfigLoader:
    """Resolve the Snowflake account from ``service_interface::address_book_service``.

    The service is duck-typed so this plugin never imports from
    ``default_address_book_plugin``.
    """

    def __init__(self, address_book_service: Any) -> None:
        if address_book_service is None:
            raise SnowflakeConfigError(
                ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
                "address_book_service is required for AppConfigLoader",
            )
        self._address_book = address_book_service

    def resolve(self) -> SnowflakeAccountConfig:
        """Return the resolved Snowflake account config (vault private key swapped + parsed)."""
        result = self._address_book.resolve_with_secrets(name=ACCOUNT_ENTRY_NAME)
        entries = self._extract_entries(result)
        return self._build_config(entries)

    def _extract_entries(self, result: Any) -> list[dict[str, Any]]:
        if not isinstance(result, dict) or result.get("action_status") != "completed":
            raise SnowflakeConfigError(
                ERROR_NOT_CONFIGURED,
                f"no address-book entry named '{ACCOUNT_ENTRY_NAME}' is registered",
            )
        data = result.get("data") or {}
        entries = data.get("entries", []) if isinstance(data, dict) else []
        if not isinstance(entries, list):
            raise SnowflakeConfigError(
                ERROR_NOT_CONFIGURED, f"'{ACCOUNT_ENTRY_NAME}' entry has no fields"
            )
        return [entry for entry in entries if isinstance(entry, dict)]

    def _build_config(self, entries: list[dict[str, Any]]) -> SnowflakeAccountConfig:
        account = _first_value(entries, FIELD_ACCOUNT)
        user = _first_value(entries, FIELD_USER)
        warehouse = _first_value(entries, FIELD_WAREHOUSE)
        database = _first_value(entries, FIELD_DATABASE)
        schema = _first_value(entries, FIELD_SCHEMA)
        role = _first_value(entries, FIELD_ROLE)
        auth_method = _first_value(entries, FIELD_AUTH_METHOD) or AUTH_METHOD_KEY_PAIR
        private_key_pem = _first_value(entries, FIELD_PRIVATE_KEY)
        missing = [
            label
            for label, value in (
                (FIELD_ACCOUNT, account),
                (FIELD_USER, user),
                (FIELD_PRIVATE_KEY, private_key_pem),
            )
            if not value
        ]
        if missing:
            raise SnowflakeConfigError(
                ERROR_NOT_CONFIGURED,
                f"'{ACCOUNT_ENTRY_NAME}' entry is incomplete: missing {missing}. "
                "Register account, user, and a vault:: private_key reference "
                "(warehouse/database/schema/role are session defaults).",
            )
        private_key_der = _parse_pem_private_key(private_key_pem)
        return SnowflakeAccountConfig(
            account=account,
            user=user,
            warehouse=warehouse,
            database=database,
            schema=schema,
            role=role,
            auth_method=auth_method,
            private_key_der=private_key_der,
        )


def _parse_pem_private_key(pem_text: str) -> bytes:
    """Parse + re-encode a PEM private key to unencrypted DER bytes.

    Eager parsing here (config-resolution time, not first-connect) is the
    deliberate fail-loud boundary (Rev-A F4b): a flattened/corrupted PEM
    (newlines lost in transit through the vault chain) raises
    ``SnowflakeConfigError`` immediately with a clear diagnostic, rather than
    surfacing as a confusing driver-level auth failure later.
    """
    try:
        private_key = serialization.load_pem_private_key(
            pem_text.encode("utf-8"), password=None
        )
    except ValueError as exc:
        raise SnowflakeConfigError(
            ERROR_NOT_CONFIGURED,
            "the registered private_key does not parse as a PEM private key "
            "(newlines may have been flattened in transit) — re-register with "
            "the PEM's exact multi-line text",
        ) from exc
    return private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _first_value(entries: list[dict[str, Any]], field_type: str) -> str:
    for entry in entries:
        if entry.get("field_type") == field_type:
            value = entry.get("value", "")
            return value if isinstance(value, str) else ""
    return ""
