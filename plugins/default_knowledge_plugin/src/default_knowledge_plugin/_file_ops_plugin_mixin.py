"""DefaultKnowledgePlugin file CRUD sub-Mixin (W5.T).

Five KSI file CRUD methods (browse / read_file / edit_file / create_file /
delete_file) lifted byte-for-byte from the W5.T-pre-decomposition
``DefaultKnowledgePlugin``. Inherited via MI from the residual class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .kb_file_ops import (
    archive_file_kb,
    browse_kb,
    create_file_kb,
    delete_file_kb,
    edit_file_kb,
    read_file_kb,
)


class KnowledgeFileOpsPluginMixin:
    """KB file CRUD verb implementations. Inherited via MI from DefaultKnowledgePlugin."""

    if TYPE_CHECKING:
        # Service-state attributes owned by DefaultKnowledgePlugin.__init__ + prepare_for_readiness.
        _memory_service: Any
        _state_service: Any

    def browse(self, name: str, path: str = "") -> dict[str, Any]:
        """List directory contents within a knowledge base."""
        return browse_kb(name, path, self._state_service)

    def read_file(self, name: str, path: str) -> dict[str, Any]:
        """Read a file from a knowledge base. Path traversal protected."""
        return read_file_kb(name, path, self._state_service)

    def edit_file(
        self, name: str, path: str, content: str,
        expected_content_hash: str | None = None,
    ) -> dict[str, Any]:
        """Overwrite an existing file (optionally hash-preconditioned), re-chunk."""
        return edit_file_kb(
            name, path, content,
            self._state_service, self._memory_service,
            expected_content_hash=expected_content_hash,
        )

    def create_file(self, name: str, path: str, content: str) -> dict[str, Any]:
        """Create a new file in a knowledge base, chunk and index it."""
        return create_file_kb(
            name, path, content,
            self._state_service, self._memory_service,
        )

    def delete_file(self, name: str, path: str) -> dict[str, Any]:
        """Delete a file from a knowledge base and hard-delete its chunks."""
        return delete_file_kb(
            name, path,
            self._state_service, self._memory_service,
        )

    def archive_file(
        self, name: str, path: str, superseded_by: str | None = None,
    ) -> dict[str, Any]:
        """Retire a doc: move under archive_subdir, stamp §4 block, re-key chunk."""
        return archive_file_kb(
            name, path, superseded_by,
            self._state_service, self._memory_service,
        )
