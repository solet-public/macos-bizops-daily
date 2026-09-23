"""DefaultKnowledgePlugin operator-fired W5.P cleanup sub-Mixin (W5.T).

The operator-fired ``purge_orphaned_chunks`` verb, lifted byte-for-byte from
the W5.T-pre-decomposition ``DefaultKnowledgePlugin``. Inherited via MI from
the residual class. (The companion ``purge_orphan_vectors`` verb was RETIRED
per the 2026-06-22 kb-cohort forks-ruling FORK C — superseded by
``service_interface::memory_service::cleanup_orphaned_vectors``, which rebuilds
the shared vector namespace covering KB chunks too; cascade-vector-first
deletes prevent new orphans.)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .kb_lifecycle import purge_orphaned_chunks


class KnowledgeMaintenancePluginMixin:
    """Operator-fired W5.P orphan-cleanup verb implementations. Inherited via MI."""

    if TYPE_CHECKING:
        # Service-state attributes owned by DefaultKnowledgePlugin.__init__ + prepare_for_readiness.
        _state_service: Any
        _memory_service: Any

    def purge_orphaned_chunks(
        self,
        confirm: bool = False,
        batch_size: int = 5000,
        max_batches: int | None = None,
    ) -> dict[str, Any]:
        """Operator-fired KB-chunk orphan-cleanup (W5.P §4.6).

        Default dry-run; pass confirm=True to actually delete. Each batch
        hard-deletes its orphan chunk ids through the owning
        ``memory_service.delete_memories_by_ids`` verb (no state-service
        transaction — SQL-lockdown cohort; the verb cascades vectors first).
        """
        return purge_orphaned_chunks(
            self._state_service,
            self._memory_service,
            confirm=confirm,
            batch_size=batch_size,
            max_batches=max_batches,
        )
