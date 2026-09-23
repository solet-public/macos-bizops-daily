"""OAuth app identity loaded from the address book.

The plugin's client_id / client_secret / redirect_uri live in a single address
book entry named ``google_oauth_app``. The ``client_secret`` field stores a
``vault::<solet>.default_address_book_plugin.google_client_secret``
reference (the secret lives in the RESOLVER's namespace so the address book can
read it under its own identity — see :data:`constants.VAULT_KEY_CLIENT_SECRET`);
every other field is a literal value. ``resolve_with_secrets`` swaps the vault
reference before returning, so the plugin never reads raw vault keys for
app-wide config.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..constants import (
    ADDRESS_BOOK_ENTRY_DESCRIPTION,
    ADDRESS_BOOK_ENTRY_NAME,
    ADDRESS_BOOK_ENTRY_TYPE,
    ADDRESS_BOOK_FIELD_CLIENT_ID,
    ADDRESS_BOOK_FIELD_CLIENT_SECRET,
    ADDRESS_BOOK_FIELD_REDIRECT_URI,
    ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE,
    ERROR_ADDRESS_BOOK_ENTRY_MISSING,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
    VAULT_KEY_CLIENT_SECRET,
)


class AppConfigError(RuntimeError):
    """Raised when the address book entry cannot supply OAuth app config."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class OAuthAppConfig:
    """Resolved OAuth 2.0 application identity for Google Workspace."""

    client_id: str
    client_secret: str
    redirect_uri: str


class AppConfigLoader:
    """Resolve the OAuth app config from ``service_interface::address_book_service``.

    The address book service is duck-typed so this plugin never imports from
    ``default_address_book_plugin``.
    """

    def __init__(self, address_book_service: Any) -> None:
        if address_book_service is None:
            raise AppConfigError(
                ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
                "address_book_service is required for AppConfigLoader",
            )
        self._address_book = address_book_service

    def load(self) -> OAuthAppConfig:
        """Return the resolved OAuth app config (vault references swapped)."""
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

    def _build_config(self, entries: list[dict[str, Any]]) -> OAuthAppConfig:
        client_id = _first_value(entries, ADDRESS_BOOK_FIELD_CLIENT_ID)
        client_secret = _first_value(entries, ADDRESS_BOOK_FIELD_CLIENT_SECRET)
        redirect_uri = _first_value(entries, ADDRESS_BOOK_FIELD_REDIRECT_URI)
        missing = [
            label
            for label, value in (
                (ADDRESS_BOOK_FIELD_CLIENT_ID, client_id),
                (ADDRESS_BOOK_FIELD_CLIENT_SECRET, client_secret),
                (ADDRESS_BOOK_FIELD_REDIRECT_URI, redirect_uri),
            )
            if not value
        ]
        if missing:
            raise AppConfigError(
                ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE,
                (
                    f"Address book entry '{ADDRESS_BOOK_ENTRY_NAME}' is missing "
                    f"field_type values: {missing}. Register with client_id, "
                    "client_secret (vault-ref), and redirect_uri."
                ),
            )
        return OAuthAppConfig(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
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
        "Register it before running connect_account. Example:\n"
        "  process_call service_interface::address_book_service::register {\n"
        f'    "name": "{ADDRESS_BOOK_ENTRY_NAME}",\n'
        f'    "address_type": "{ADDRESS_BOOK_ENTRY_TYPE}",\n'
        f'    "description": "{ADDRESS_BOOK_ENTRY_DESCRIPTION}",\n'
        '    "entries": [\n'
        f'      {{"field_type": "{ADDRESS_BOOK_FIELD_CLIENT_ID}", '
        '"value": "<google-client-id>"}},\n'
        f'      {{"field_type": "{ADDRESS_BOOK_FIELD_CLIENT_SECRET}", '
        f'"value": "{secret_reference}"}},\n'
        f'      {{"field_type": "{ADDRESS_BOOK_FIELD_REDIRECT_URI}", '
        '"value": "https://<solet-fqdn>/oauth/google/callback"}}\n'
        '    ]\n'
        '  }'
    )
