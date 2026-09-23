"""Knowledge-base file CRUD plugin-contract methods (W5.R).

Browse, read, edit, create, and delete files within a knowledge base
directory (living KBs). Lifted byte-for-byte from the W5.R-pre-
decomposition ``KnowledgeServiceInterface``.
"""

from abc import ABC, abstractmethod
from typing import Any


class KnowledgeFileOpsInterface(ABC):
    """KB file CRUD abstract methods — browse / read / edit / create / delete."""

    @abstractmethod
    def browse(self, name: str, path: str = "") -> dict[str, Any]:
        """List directory contents of a knowledge base."""
        ...

    @abstractmethod
    def read_file(self, name: str, path: str) -> dict[str, Any]:
        """Read a file from a knowledge base."""
        ...

    @abstractmethod
    def edit_file(
        self, name: str, path: str, content: str,
        expected_content_hash: str | None = None,
    ) -> dict[str, Any]:
        """Write content to an existing file. For git KBs: writes to managed
        branch, commits. For local KBs: direct filesystem write. Re-chunks the
        affected file and updates memories.

        When ``expected_content_hash`` is supplied it must equal the target's
        current content hash (from ``read_file``'s ``content_sha256``) or the
        edit fails loud — optimistic concurrency that kills the silent
        lost-update. Some write postures require it; ``FULL`` keeps the legacy
        behavior when it is omitted.
        """
        ...

    @abstractmethod
    def create_file(self, name: str, path: str, content: str) -> dict[str, Any]:
        """Create a new file in the KB. For git KBs: writes to managed branch,
        commits. For local KBs: direct filesystem write. Chunks new file and
        creates memories.
        """
        ...

    @abstractmethod
    def delete_file(self, name: str, path: str) -> dict[str, Any]:
        """Delete a file from the KB. For git KBs: removes from managed branch,
        commits. For local KBs: direct filesystem delete. Forgets associated
        chunks.
        """
        ...

    @abstractmethod
    def archive_file(
        self, name: str, path: str, superseded_by: str | None = None,
    ) -> dict[str, Any]:
        """Retire a document by moving it under the KB's configured archive
        subdirectory (preserving relative structure), stamping its §4 metadata
        block (``Archived:``, optionally ``Superseded_by:`` / ``Status:
        superseded``), and re-keying its index chunk so archived docs stay
        discoverable and readable at the new path. Pure filesystem move — never
        a git verb. Fail-louds when the KB has no archive subdirectory
        configured, the path is already archived, or the destination collides.
        """
        ...

