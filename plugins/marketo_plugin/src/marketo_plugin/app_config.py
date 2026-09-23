"""Marketo instance config loaded from the address book.

The plugin's base_url / client_id / client_secret live in a single
address-book entry named ``marketo_instance``. ``base_url`` is the operator's
Marketo REST endpoint (e.g. ``https://123-ABC-456.mktorest.com`` — found in
Admin > Integration > Web Services); the identity/token endpoint lives under
the same host, so no separate identity_url field is needed. The
``client_secret`` field stores a
``vault::<solet>.default_address_book_plugin.marketo_client_secret``
reference (chain-consumed — see :data:`constants.VAULT_KEY_CLIENT_SECRET`);
every other field is a literal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .constants import (
    ADDRESS_BOOK_ENTRY_DESCRIPTION,
    ADDRESS_BOOK_ENTRY_NAME,
    ADDRESS_BOOK_ENTRY_TYPE,
    ADDRESS_BOOK_FIELD_BASE_URL,
    ADDRESS_BOOK_FIELD_CLIENT_ID,
    ADDRESS_BOOK_FIELD_CLIENT_SECRET,
    ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE,
    ERROR_ADDRESS_BOOK_ENTRY_MISSING,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
    VAULT_KEY_CLIENT_SECRET,
)

_SECRET_REDACTION = "***"


class AppConfigError(RuntimeError):
    """Raised when the ``marketo_instance`` address-book entry cannot supply config."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, repr=False)
class MarketoInstanceConfig:
    """Resolved Marketo instance identity + credentials.

    ``repr`` redacts the client secret so logging the object never leaks it.
    """

    base_url: str
    client_id: str
    client_secret: str

    def __repr__(self) -> str:
        return (
            f"MarketoInstanceConfig(base_url={self.base_url!r}, "
            f"client_id={self.client_id!r}, client_secret={_SECRET_REDACTION!r})"
        )


class AppConfigLoader:
    """Resolve the Marketo instance config from ``service_interface::address_book_service``.

    The service is duck-typed so this plugin never imports from
    ``default_address_book_plugin``.
    """

    def __init__(self, address_book_service: Any) -> None:
        if address_book_service is None:
            raise AppConfigError(
                ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
                "address_book_service is required for AppConfigLoader",
            )
        self._address_book = address_book_service

    def load(self) -> MarketoInstanceConfig:
        """Return the resolved Marketo instance config (vault reference swapped)."""
        result = self._address_book.resolve_with_secrets(name=ADDRESS_BOOK_ENTRY_NAME)
        entries = self._extract_entries(result)
        return self._build_config(entries)

    def _extract_entries(self, result: Any) -> list[dict[str, Any]]:
        if not isinstance(result, dict) or result.get("action_status") != "completed":
            raise AppConfigError(ERROR_ADDRESS_BOOK_ENTRY_MISSING, _missing_entry_message())
        data = result.get("data") or {}
        if not isinstance(data, dict):
            raise AppConfigError(
                ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE,
                f"Address book entry '{ADDRESS_BOOK_ENTRY_NAME}' returned no data",
            )
        entries = data.get("entries", [])
        if not isinstance(entries, list):
            raise AppConfigError(
                ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE,
                f"Address book entry '{ADDRESS_BOOK_ENTRY_NAME}' has no entries list",
            )
        return [entry for entry in entries if isinstance(entry, dict)]

    def _build_config(self, entries: list[dict[str, Any]]) -> MarketoInstanceConfig:
        base_url = _first_value(entries, ADDRESS_BOOK_FIELD_BASE_URL)
        client_id = _first_value(entries, ADDRESS_BOOK_FIELD_CLIENT_ID)
        client_secret = _first_value(entries, ADDRESS_BOOK_FIELD_CLIENT_SECRET)
        missing = [
            label
            for label, value in (
                (ADDRESS_BOOK_FIELD_BASE_URL, base_url),
                (ADDRESS_BOOK_FIELD_CLIENT_ID, client_id),
                (ADDRESS_BOOK_FIELD_CLIENT_SECRET, client_secret),
            )
            if not value
        ]
        if missing:
            raise AppConfigError(
                ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE,
                (
                    f"Address book entry '{ADDRESS_BOOK_ENTRY_NAME}' is missing "
                    f"field_type values: {missing}. Register with base_url, "
                    "client_id, and a client_secret vault-ref."
                ),
            )
        return MarketoInstanceConfig(
            base_url=base_url.rstrip("/"), client_id=client_id, client_secret=client_secret
        )


def _first_value(entries: list[dict[str, Any]], field_type: str) -> str:
    for entry in entries:
        if entry.get("field_type") == field_type:
            value = entry.get("value", "")
            return value if isinstance(value, str) else ""
    return ""


def _missing_entry_message() -> str:
    secret_reference = f"vault::{VAULT_KEY_CLIENT_SECRET}"
    return (
        f"Address book entry '{ADDRESS_BOOK_ENTRY_NAME}' not found. "
        "Register it before running any marketo verb. Example:\n"
        "  process_call service_interface::address_book_service::register {\n"
        f'    "name": "{ADDRESS_BOOK_ENTRY_NAME}",\n'
        f'    "address_type": "{ADDRESS_BOOK_ENTRY_TYPE}",\n'
        f'    "description": "{ADDRESS_BOOK_ENTRY_DESCRIPTION}",\n'
        '    "entries": [\n'
        f'      {{"field_type": "{ADDRESS_BOOK_FIELD_BASE_URL}", '
        '"value": "https://123-ABC-456.mktorest.com"}},\n'
        f'      {{"field_type": "{ADDRESS_BOOK_FIELD_CLIENT_ID}", '
        '"value": "<oauth-client-id>"}},\n'
        f'      {{"field_type": "{ADDRESS_BOOK_FIELD_CLIENT_SECRET}", '
        f'"value": "{secret_reference}"}}\n'
        '    ]\n'
        '  }'
    )
