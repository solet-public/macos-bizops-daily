"""Knowledge-service wrapper sub-mixin for KB file-ops delegates (W5.S).

Five delegates satisfying the W5.R-decomposed :class:`KnowledgeFileOpsInterface`:
browse, read_file, edit_file, create_file, and delete_file. Lifted byte-for-byte
from the W5.S-pre-decomposition ``KnowledgeService.__init__.py``.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ananta.interfaces.knowledge_service_interface_file_ops import (
        KnowledgeFileOpsInterface,
    )


class KnowledgeFileOpsWrapper:
    """KB file-ops delegate methods. Inherited via MI."""

    if TYPE_CHECKING:
        def _get_backend(self) -> "KnowledgeFileOpsInterface": ...

    def browse(self, name: str, path: str = "") -> dict[str, Any]:
        return self._get_backend().browse(name=name, path=path)

    def read_file(self, name: str, path: str) -> dict[str, Any]:
        return self._get_backend().read_file(name=name, path=path)

    def edit_file(
        self, name: str, path: str, content: str,
        expected_content_hash: str | None = None,
    ) -> dict[str, Any]:
        return self._get_backend().edit_file(
            name=name, path=path, content=content,
            expected_content_hash=expected_content_hash,
        )

    def create_file(self, name: str, path: str, content: str) -> dict[str, Any]:
        return self._get_backend().create_file(name=name, path=path, content=content)

    def delete_file(self, name: str, path: str) -> dict[str, Any]:
        return self._get_backend().delete_file(name=name, path=path)

    def archive_file(
        self, name: str, path: str, superseded_by: str | None = None,
    ) -> dict[str, Any]:
        return self._get_backend().archive_file(
            name=name, path=path, superseded_by=superseded_by,
        )
