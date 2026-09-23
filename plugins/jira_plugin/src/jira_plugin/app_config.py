"""Jira site config loaded from the address book.

The plugin's base_url / service-account email / api_token / expires_at /
scope_note live in a single address-book entry named ``jira_site``. The
``api_token`` field stores a
``vault::<solet>.default_address_book_plugin.jira_api_token`` reference
(the secret lives in the RESOLVER's namespace so the address book reads it under
its own identity — see :data:`constants.VAULT_KEY_API_TOKEN`); every other field
is a literal. ``resolve_with_secrets`` swaps the vault reference before
returning, so the plugin never reads a raw vault key for its credentials.

``expires_at`` is validated to a timezone-aware datetime AT CONFIG-LOAD (fail
loud), not at first use — a malformed expiry surfaces immediately in
``address_book`` resolution rather than as a mystery downstream failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .constants import (
    ADDRESS_BOOK_ENTRY_DESCRIPTION,
    ADDRESS_BOOK_ENTRY_NAME,
    ADDRESS_BOOK_ENTRY_TYPE,
    ADDRESS_BOOK_FIELD_API_TOKEN,
    ADDRESS_BOOK_FIELD_BASE_URL,
    ADDRESS_BOOK_FIELD_EMAIL,
    ADDRESS_BOOK_FIELD_EXPIRES_AT,
    ADDRESS_BOOK_FIELD_SCOPE_NOTE,
    ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE,
    ERROR_ADDRESS_BOOK_ENTRY_MISSING,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
    ERROR_EXPIRES_AT_INVALID,
    VAULT_KEY_API_TOKEN,
)


class AppConfigError(RuntimeError):
    """Raised when the ``jira_site`` address-book entry cannot supply config."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class JiraAppConfig:
    """Resolved Jira Cloud site identity + credentials."""

    base_url: str
    email: str
    api_token: str
    expires_at: datetime
    scope_note: str


class AppConfigLoader:
    """Resolve the Jira site config from ``service_interface::address_book_service``.

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

    def load(self) -> JiraAppConfig:
        """Return the resolved Jira site config (vault reference swapped)."""
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

    def _build_config(self, entries: list[dict[str, Any]]) -> JiraAppConfig:
        base_url = _first_value(entries, ADDRESS_BOOK_FIELD_BASE_URL)
        email = _first_value(entries, ADDRESS_BOOK_FIELD_EMAIL)
        api_token = _first_value(entries, ADDRESS_BOOK_FIELD_API_TOKEN)
        expires_at_raw = _first_value(entries, ADDRESS_BOOK_FIELD_EXPIRES_AT)
        scope_note = _first_value(entries, ADDRESS_BOOK_FIELD_SCOPE_NOTE)
        missing = [
            label
            for label, value in (
                (ADDRESS_BOOK_FIELD_BASE_URL, base_url),
                (ADDRESS_BOOK_FIELD_EMAIL, email),
                (ADDRESS_BOOK_FIELD_API_TOKEN, api_token),
                (ADDRESS_BOOK_FIELD_EXPIRES_AT, expires_at_raw),
            )
            if not value
        ]
        if missing:
            raise AppConfigError(
                ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE,
                (
                    f"Address book entry '{ADDRESS_BOOK_ENTRY_NAME}' is missing "
                    f"field_type values: {missing}. Register with base_url, email, "
                    "api_token (vault-ref), and expires_at (ISO-8601)."
                ),
            )
        return JiraAppConfig(
            base_url=base_url,
            email=email,
            api_token=api_token,
            expires_at=_parse_expires_at(expires_at_raw),
            scope_note=scope_note,
        )


def _parse_expires_at(raw: str) -> datetime:
    """Parse the recorded token expiry as a timezone-aware datetime (fail loud).

    Accepts ISO-8601 with a ``Z`` suffix or an explicit offset. A store-naive
    value is coerced to UTC so downstream expiry math is always aware-vs-aware
    (mirrors the deaf-wake _parse_iso coercion convention). A value that does
    not parse raises ``AppConfigError`` at config-load — never a silent skew.
    """
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise AppConfigError(
            ERROR_EXPIRES_AT_INVALID,
            (
                f"Address book entry '{ADDRESS_BOOK_ENTRY_NAME}' field "
                f"'{ADDRESS_BOOK_FIELD_EXPIRES_AT}' value {raw!r} is not ISO-8601 "
                "(expected e.g. '2027-01-15T00:00:00Z')."
            ),
        ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _first_value(entries: list[dict[str, Any]], field_type: str) -> str:
    for entry in entries:
        if entry.get("field_type") == field_type:
            value = entry.get("value", "")
            return value if isinstance(value, str) else ""
    return ""


def _missing_entry_message() -> str:
    secret_reference = f"vault::{VAULT_KEY_API_TOKEN}"
    return (
        f"Address book entry '{ADDRESS_BOOK_ENTRY_NAME}' not found. "
        "Register it before running any jira verb. Example:\n"
        "  process_call service_interface::address_book_service::register {\n"
        f'    "name": "{ADDRESS_BOOK_ENTRY_NAME}",\n'
        f'    "address_type": "{ADDRESS_BOOK_ENTRY_TYPE}",\n'
        f'    "description": "{ADDRESS_BOOK_ENTRY_DESCRIPTION}",\n'
        '    "entries": [\n'
        f'      {{"field_type": "{ADDRESS_BOOK_FIELD_BASE_URL}", '
        '"value": "https://<org>.atlassian.net"}},\n'
        f'      {{"field_type": "{ADDRESS_BOOK_FIELD_EMAIL}", '
        '"value": "<service-account-email>"}},\n'
        f'      {{"field_type": "{ADDRESS_BOOK_FIELD_API_TOKEN}", '
        f'"value": "{secret_reference}"}},\n'
        f'      {{"field_type": "{ADDRESS_BOOK_FIELD_EXPIRES_AT}", '
        '"value": "<token-expiry-ISO-8601>"}},\n'
        f'      {{"field_type": "{ADDRESS_BOOK_FIELD_SCOPE_NOTE}", '
        '"value": "<fixed-at-creation scope note>"}}\n'
        '    ]\n'
        '  }'
    )
