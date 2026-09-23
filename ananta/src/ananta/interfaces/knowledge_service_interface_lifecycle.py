"""Knowledge-base lifecycle plugin-contract abstract methods (W5.R).

Install, uninstall, update, list, activate, and deactivate the KB
install records that drive the knowledge_service's directory-backed
reference library. Lifted byte-for-byte from the W5.R-pre-decomposition
``KnowledgeServiceInterface`` god class (183 LOC, 16 abstract methods).
"""

from abc import ABC, abstractmethod
from typing import Any


class KnowledgeLifecycleInterface(ABC):
    """KB lifecycle abstract methods — install / uninstall / update / list / activate / deactivate."""

    @abstractmethod
    def install(self, name: str, source: str | None = None) -> dict[str, Any]:
        """Index knowledge_base/{name}/. Resolution order:
        1. If source provided: clone from URL (no credentials)
        2. If knowledge_base/{name}/ exists locally: index it
        3. Else: resolve source URL + credentials from address_book_service
        Idempotent: re-installing archives old chunks via forget(), creates new.
        """
        ...

    @abstractmethod
    def ingest(self, name: str) -> dict[str, Any]:
        """Content-hash-gated idempotent (re)ingest of one KB, or all when name='all'.

        Single KB: skip if content-current (returned under 'unchanged'), else
        reindex (returned under 'ingested'); fails loud on error. 'all': scan the
        KB root and apply the same per-KB gate — install pass only (no orphan
        uninstall); per-KB failures collected under 'failed' with status='partial'.
        Returns {status, mode, ingested, unchanged, failed, total_chunks}.
        """
        ...

    @abstractmethod
    def uninstall(self, name: str, remove_files: bool = False) -> dict[str, Any]:
        """Archive chunks via forget(), delete install record.
        remove_files=True ONLY deletes directories with source_type='git'.
        Never deletes local/symlinked. Chunks are archived (not hard-deleted) —
        vector embeddings are removed, memory records persist as archived.
        Does NOT delete the address book entry — source registry is independent.
        """
        ...

    @abstractmethod
    def update(self, name: str) -> dict[str, Any]:
        """Git KBs: git pull (credentials from address book if needed) + reindex changed.
        Local KBs: reindex by mtime.
        """
        ...

    @abstractmethod
    def list_installed(self, active_only: bool = False) -> dict[str, Any]:
        """List indexed knowledge bases with metadata."""
        ...

    @abstractmethod
    def activate(self, name: str) -> dict[str, Any]:
        """Activate a deactivated knowledge base for search."""
        ...

    @abstractmethod
    def deactivate(self, name: str) -> dict[str, Any]:
        """Deactivate a knowledge base (excluded from search)."""
        ...

