"""Address Book Service Public API.

AI-discoverable address registry operations with @service_interface_process decorators.
All methods in this interface are indexed for process discovery.

Discoverability Policy (Task #47, 2026-05-24):
- EVERY method declares ``is_discoverable=True`` explicitly. The base decorator
  default for ``@service_interface_process`` is ``is_discoverable=False`` (service
  methods are presumed internal); address-book operations are end-user / agent
  -facing (register, resolve, resolve_with_secrets, add_entry, update, search,
  list_types, list_tags, ...), so the per-method flag overrides the default.
- Adding a new method without ``is_discoverable=True`` will SILENTLY exclude it
  from ``process_search`` and the agent will not be able to find it.

Data Model:
- address: Parent record (name, address_type, description, tags, memory_id)
- address_entry: Child records (field_type, description, value)

Features:
- One-to-many relationship (address -> entries)
- Automatic memory integration for semantic search
- Vault integration for secret references (local://category/key)
"""

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.services.service_interface_decorator import service_interface_process


class AddressBookServiceAPI(ABC):
    """Public address book operations - AI-discoverable via process registry.

    This interface defines address registry operations that can be discovered
    and invoked by the AI orchestration system and action templates.

    Access via: service_interface::address_book_service::{method_name}
    """

    @service_interface_process(
        name="register",
        is_discoverable=True,
        provider="address_book_service",
        parameters={
            "name": ParameterMetadata(
                description="Unique lookup key (e.g., 'openai_api', 'postgres_production')",
                required=True,
                type=ParameterType.STRING,
            ),
            "address_type": ParameterMetadata(
                description="Type classification (url, endpoint, path, service, database, api)",
                required=True,
                type=ParameterType.STRING,
            ),
            "description": ParameterMetadata(
                description="Human-readable description (used for memory/search)",
                required=True,
                type=ParameterType.STRING,
            ),
            "entries": ParameterMetadata(
                description="List of entry dicts with field_type, description, value",
                required=True,
                type=ParameterType.LIST,
            ),
            "tags": ParameterMetadata(
                description="Optional tags for organization",
                required=False,
                type=ParameterType.LIST,
                default=[],
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Registration result with address_id and memory_id",
            type=ParameterType.OBJECT,
            properties={
                "address_id": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Database ID of created address"
                ),
                "memory_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="UUID of associated memory for semantic search",
                ),
                "name": ParameterMetadata(
                    type=ParameterType.STRING, description="The registered name"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
    )
    @abstractmethod
    def register(
        self,
        name: str,
        address_type: str,
        description: str,
        entries: list[dict[str, str]],
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create address with entries. Returns address_id, memory_id."""
        pass

    @service_interface_process(
        name="resolve",
        is_discoverable=True,
        provider="address_book_service",
        parameters={
            "name": ParameterMetadata(
                description="The registered name to resolve",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Full address with all entries",
            type=ParameterType.OBJECT,
            properties={
                "address": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Address record with id, name, type, description, tags",
                ),
                "entries": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="List of entry records with id, field_type, description, value",
                ),
            },
        ),
    )
    @abstractmethod
    def resolve(self, name: str) -> dict[str, Any]:
        """Get address by name with all entries. Strengthens memory."""
        pass

    @service_interface_process(
        name="resolve_with_secrets",
        is_discoverable=True,
        provider="address_book_service",
        parameters={
            "name": ParameterMetadata(
                description="The registered name to resolve",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Full address with secrets resolved",
            type=ParameterType.OBJECT,
            properties={
                "address": ParameterMetadata(
                    type=ParameterType.OBJECT, description="Address record"
                ),
                "entries": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Entries with vault references replaced by actual values",
                ),
            },
        ),
    )
    @abstractmethod
    def resolve_with_secrets(self, name: str) -> dict[str, Any]:
        """Get address with vault references resolved."""
        pass

    @service_interface_process(
        name="add_entry",
        is_discoverable=True,
        provider="address_book_service",
        parameters={
            "name": ParameterMetadata(
                description="The address name to add entry to",
                required=True,
                type=ParameterType.STRING,
            ),
            "field_type": ParameterMetadata(
                description="Type of entry (url, note, port, host, password, api_key, etc.)",
                required=True,
                type=ParameterType.STRING,
            ),
            "description": ParameterMetadata(
                description="Human-readable description of the entry",
                required=True,
                type=ParameterType.STRING,
            ),
            "value": ParameterMetadata(
                description="Entry value (can reference vault: 'local://category/key')",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Entry creation result",
            type=ParameterType.OBJECT,
            properties={
                "entry_id": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Database ID of created entry"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
    )
    @abstractmethod
    def add_entry(self, name: str, field_type: str, description: str, value: str) -> dict[str, Any]:
        """Add entry to existing address. Returns entry_id."""
        pass

    @service_interface_process(
        name="update_entry",
        is_discoverable=True,
        provider="address_book_service",
        parameters={
            "entry_id": ParameterMetadata(
                description="The entry ID to update", required=True, type=ParameterType.STRING
            ),
            "field_type": ParameterMetadata(
                description="New field type (optional)",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "description": ParameterMetadata(
                description="New description (optional)",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "value": ParameterMetadata(
                description="New value (optional)",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Updated entry",
            type=ParameterType.OBJECT,
            properties={
                "entry": ParameterMetadata(
                    type=ParameterType.OBJECT, description="The updated entry record"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
    )
    @abstractmethod
    def update_entry(
        self,
        entry_id: str,
        field_type: str | None = None,
        description: str | None = None,
        value: str | None = None,
    ) -> dict[str, Any]:
        """Update specific entry by id."""
        pass

    @service_interface_process(
        name="delete_entry",
        is_discoverable=True,
        provider="address_book_service",
        parameters={
            "entry_id": ParameterMetadata(
                description="The entry ID to delete", required=True, type=ParameterType.STRING
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Delete confirmation",
            type=ParameterType.OBJECT,
            properties={
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
    )
    @abstractmethod
    def delete_entry(self, entry_id: str) -> dict[str, Any]:
        """Delete specific entry."""
        pass

    @service_interface_process(
        name="update",
        is_discoverable=True,
        provider="address_book_service",
        parameters={
            "name": ParameterMetadata(
                description="The address name to update (cannot be changed)",
                required=True,
                type=ParameterType.STRING,
            ),
            "address_type": ParameterMetadata(
                description="New address type (optional)",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "description": ParameterMetadata(
                description="New description (optional)",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "tags": ParameterMetadata(
                description="New tags (replaces existing, optional)",
                required=False,
                type=ParameterType.LIST,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Updated address",
            type=ParameterType.OBJECT,
            properties={
                "address": ParameterMetadata(
                    type=ParameterType.OBJECT, description="The updated address record"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
    )
    @abstractmethod
    def update(
        self,
        name: str,
        address_type: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update address metadata (not entries)."""
        pass

    @service_interface_process(
        name="delete",
        is_discoverable=True,
        provider="address_book_service",
        parameters={
            "name": ParameterMetadata(
                description="The address name to delete", required=True, type=ParameterType.STRING
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Delete confirmation",
            type=ParameterType.OBJECT,
            properties={
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
    )
    @abstractmethod
    def delete(self, name: str) -> dict[str, Any]:
        """Delete address and all entries. Archives memory."""
        pass

    @service_interface_process(
        name="search",
        is_discoverable=True,
        provider="address_book_service",
        parameters={
            "query": ParameterMetadata(
                description="Text search in name/description (optional)",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "address_type": ParameterMetadata(
                description="Filter by address type (optional)",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "tag": ParameterMetadata(
                description="Filter by tag (optional)",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "limit": ParameterMetadata(
                description="Maximum results",
                required=False,
                type=ParameterType.INTEGER,
                default=20,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="List of matching addresses (without entries)",
            type=ParameterType.OBJECT,
            properties={
                "addresses": ParameterMetadata(
                    type=ParameterType.LIST, description="List of address records (without entries)"
                ),
                "count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of results"
                ),
            },
        ),
    )
    @abstractmethod
    def search(
        self,
        query: str | None = None,
        address_type: str | None = None,
        tag: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search addresses. Returns list without entries."""
        pass

    @service_interface_process(
        name="list_types",
        is_discoverable=True,
        provider="address_book_service",
        parameters={},
        return_value_schema=ReturnValueSchema(
            description="Address types with counts",
            type=ParameterType.OBJECT,
            properties={
                "types": ParameterMetadata(
                    type=ParameterType.LIST, description="List of {type, count} records"
                ),
            },
        ),
    )
    @abstractmethod
    def list_types(self) -> dict[str, Any]:
        """List address_types with counts."""
        pass

    @service_interface_process(
        name="list_tags",
        is_discoverable=True,
        provider="address_book_service",
        parameters={},
        return_value_schema=ReturnValueSchema(
            description="Tags with counts",
            type=ParameterType.OBJECT,
            properties={
                "tags": ParameterMetadata(
                    type=ParameterType.LIST, description="List of {tag, count} records"
                ),
            },
        ),
    )
    @abstractmethod
    def list_tags(self) -> dict[str, Any]:
        """List tags with counts."""
        pass
