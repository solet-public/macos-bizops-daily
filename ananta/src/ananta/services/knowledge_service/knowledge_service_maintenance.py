"""Knowledge-service wrapper sub-mixin for operator-fired cleanup delegates (W5.S).

One delegate satisfying the W5.R-decomposed :class:`KnowledgeMaintenanceInterface`
— the W5.P verb ``purge_orphaned_chunks``. Lifted byte-for-byte from the post-W5.R
``KnowledgeService.__init__.py``. (The companion ``purge_orphan_vectors`` delegate
was RETIRED per the 2026-06-22 kb-cohort forks-ruling FORK C — superseded by
``service_interface::memory_service::cleanup_orphaned_vectors``.)
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ananta.interfaces.knowledge_service_interface_maintenance import (
        KnowledgeMaintenanceInterface,
    )


class KnowledgeMaintenanceWrapper:
    """Operator-fired cleanup delegate methods. Inherited via MI."""

    if TYPE_CHECKING:
        def _get_backend(self) -> "KnowledgeMaintenanceInterface": ...

    def purge_orphaned_chunks(
        self,
        confirm: bool = False,
        batch_size: int = 5000,
        max_batches: int | None = None,
    ) -> dict[str, Any]:
        return self._get_backend().purge_orphaned_chunks(
            confirm=confirm, batch_size=batch_size, max_batches=max_batches,
        )
