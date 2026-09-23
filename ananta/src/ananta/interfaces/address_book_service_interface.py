"""Address Book Service Interface - Address registry with one-to-many entries."""

from abc import ABC, abstractmethod
from typing import ClassVar

from ananta.core.domain.types import ActionResult


class AddressBookServiceInterface(ABC):
    """Address registry with one-to-many entries. Auto-ingests to memory.

    Data model:
    - address: parent record (name, address_type, description, tags, memory_id)
    - address_entry: child records (field_type, description, value)

    Plugins implementing this interface should:
    1. Define service_interfaces property returning tuple containing AddressBookServiceInterface
    2. Define supported_interface_versions property with version mapping
    3. Integrate with memory_service for automatic ingestion
    4. Return ActionResult TypedDict from all operations
    """

    INTERFACE_VERSION: ClassVar[str] = "1.0.0"

    @abstractmethod
    def register(
        self,
        name: str,
        address_type: str,
        description: str,
        entries: list[dict[str, str]],
        tags: list[str] | None = None,
    ) -> ActionResult:
        """Create address with entries. Returns address_id, memory_id.

        Args:
            name: Unique lookup key (e.g., "openai_api")
            address_type: Type classification (url, endpoint, path, service, etc.)
            description: Human-readable description (used for memory/search)
            entries: List of entry dicts with field_type, description, value
            tags: Optional tags for organization

        Returns:
            ActionResult with address_id and memory_id
        """
        ...

    @abstractmethod
    def resolve(self, name: str) -> ActionResult:
        """Get address by name with all entries. Strengthens memory.

        Args:
            name: The registered name

        Returns:
            ActionResult with full address and entries
        """
        ...

    @abstractmethod
    def add_entry(
        self,
        name: str,
        field_type: str,
        description: str,
        value: str,
    ) -> ActionResult:
        """Add entry to existing address. Returns entry_id.

        Args:
            name: The address name
            field_type: Type of entry (url, note, port, host, etc.)
            description: Human-readable description
            value: The entry value (can be arbitrarily long)

        Returns:
            ActionResult with entry_id
        """
        ...

    @abstractmethod
    def update_entry(
        self,
        entry_id: str,
        field_type: str | None = None,
        description: str | None = None,
        value: str | None = None,
    ) -> ActionResult:
        """Update specific entry by id.

        Args:
            entry_id: The entry id (as string)
            field_type: New field type (optional)
            description: New description (optional)
            value: New value (optional)

        Returns:
            ActionResult with updated entry
        """
        ...

    @abstractmethod
    def delete_entry(self, entry_id: str) -> ActionResult:
        """Delete specific entry.

        Args:
            entry_id: The entry id (as string)

        Returns:
            ActionResult indicating success
        """
        ...

    @abstractmethod
    def update(
        self,
        name: str,
        address_type: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> ActionResult:
        """Update address metadata (not entries).

        Args:
            name: The registered name (cannot be changed)
            address_type: New address type (optional)
            description: New description (optional)
            tags: New tags (replaces existing, optional)

        Returns:
            ActionResult with updated address
        """
        ...

    @abstractmethod
    def delete(self, name: str) -> ActionResult:
        """Delete address and all entries. Archives memory.

        Args:
            name: The registered name

        Returns:
            ActionResult indicating success
        """
        ...

    @abstractmethod
    def search(
        self,
        query: str | None = None,
        address_type: str | None = None,
        tag: str | None = None,
        limit: int = 20,
    ) -> ActionResult:
        """Search addresses. Returns list without entries (use resolve for full data).

        Args:
            query: Optional text search in name/description
            address_type: Filter by type
            tag: Filter by tag
            limit: Maximum results

        Returns:
            ActionResult with list of matching addresses (without entries)
        """
        ...

    @abstractmethod
    def list_types(self) -> ActionResult:
        """List address_types with counts.

        Returns:
            ActionResult with types and counts
        """
        ...

    @abstractmethod
    def list_tags(self) -> ActionResult:
        """List tags with counts.

        Returns:
            ActionResult with tags and counts
        """
        ...

    @abstractmethod
    def resolve_with_secrets(self, name: str) -> ActionResult:
        """Get address by name with vault references resolved.

        Same as resolve() but automatically fetches secrets for entries
        where value starts with 'vault::'. The vault reference is replaced
        with the actual secret value.

        Example entry value: 'vault::api_keys::openai' -> actual API key

        Args:
            name: The registered name

        Returns:
            ActionResult with full address and entries (secrets resolved)

        Raises:
            VaultLockedError: If vault is locked and secrets are referenced
            SecretNotFoundError: If a vault reference doesn't exist
        """
        ...
