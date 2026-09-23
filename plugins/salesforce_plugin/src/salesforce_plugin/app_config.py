"""Salesforce org binding loaded from the address book.

The plugin's ``target_org`` (the sf CLI alias or username every verb is
invoked against, via `--target-org`) and ``instance_host`` (the pinned
my-domain host that alias must resolve to) live in a single address-book
entry named ``salesforce_org``. Both fields are literals — this plugin
stores NO secret anywhere; the durable credential is the sf CLI's own
keychain-backed refresh token, and no access token of any kind ever enters
this process (full CLI delegation — see `client.py`).

``instance_host`` is the foreign-target invariant made explicit: the CLI's
alias cache can hold many orgs, and the pin guarantees the plugin only ever
talks to the one the operator registered (the work scripts' hardcoded
host-substring guard, promoted to operator config).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .constants import (
    ADDRESS_BOOK_ENTRY_DESCRIPTION,
    ADDRESS_BOOK_ENTRY_NAME,
    ADDRESS_BOOK_ENTRY_TYPE,
    ADDRESS_BOOK_FIELD_INSTANCE_HOST,
    ADDRESS_BOOK_FIELD_TARGET_ORG,
    ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE,
    ERROR_ADDRESS_BOOK_ENTRY_MISSING,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
)


class AppConfigError(RuntimeError):
    """Raised when the ``salesforce_org`` address-book entry cannot supply config."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SalesforceOrgConfig:
    """Resolved Salesforce org binding — no secrets, both fields literal."""

    target_org: str
    instance_host: str


class AppConfigLoader:
    """Resolve the Salesforce org binding from ``service_interface::address_book_service``.

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

    def load(self) -> SalesforceOrgConfig:
        """Return the resolved org binding."""
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

    def _build_config(self, entries: list[dict[str, Any]]) -> SalesforceOrgConfig:
        target_org = _first_value(entries, ADDRESS_BOOK_FIELD_TARGET_ORG)
        instance_host = _first_value(entries, ADDRESS_BOOK_FIELD_INSTANCE_HOST)
        missing = [
            label
            for label, value in (
                (ADDRESS_BOOK_FIELD_TARGET_ORG, target_org),
                (ADDRESS_BOOK_FIELD_INSTANCE_HOST, instance_host),
            )
            if not value
        ]
        if missing:
            raise AppConfigError(
                ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE,
                (
                    f"Address book entry '{ADDRESS_BOOK_ENTRY_NAME}' is missing "
                    f"field_type values: {missing}. Register with target_org (the "
                    "sf CLI alias or username) and instance_host (the org's "
                    "my-domain host)."
                ),
            )
        return SalesforceOrgConfig(target_org=target_org, instance_host=instance_host)


def _first_value(entries: list[dict[str, Any]], field_type: str) -> str:
    for entry in entries:
        if entry.get("field_type") == field_type:
            value = entry.get("value", "")
            return value if isinstance(value, str) else ""
    return ""


def _missing_entry_message() -> str:
    return (
        f"Address book entry '{ADDRESS_BOOK_ENTRY_NAME}' not found. "
        "Register it before running any salesforce verb. Example:\n"
        "  process_call service_interface::address_book_service::register {\n"
        f'    "name": "{ADDRESS_BOOK_ENTRY_NAME}",\n'
        f'    "address_type": "{ADDRESS_BOOK_ENTRY_TYPE}",\n'
        f'    "description": "{ADDRESS_BOOK_ENTRY_DESCRIPTION}",\n'
        '    "entries": [\n'
        f'      {{"field_type": "{ADDRESS_BOOK_FIELD_TARGET_ORG}", '
        '"value": "<sf-cli-alias-or-username>"}},\n'
        f'      {{"field_type": "{ADDRESS_BOOK_FIELD_INSTANCE_HOST}", '
        '"value": "<org>.my.salesforce.com"}}\n'
        "    ]\n"
        "  }"
    )
