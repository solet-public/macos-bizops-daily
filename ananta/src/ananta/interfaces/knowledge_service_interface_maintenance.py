"""Operator-fired KB-orphan cleanup plugin-contract method (W5.R + W5.P).

``purge_orphaned_chunks`` is a W5.R drift-closure addition — it exists on the
decorator-rich ``KnowledgeMaintenanceAPI`` (post-W5.Q) but was missing from the
plugin contract before W5.R. ``DefaultKnowledgePlugin`` implements it per W5.P;
the mock plugin gets a stub that raises NotImplementedError. (The companion
``purge_orphan_vectors`` method was RETIRED per the 2026-06-22 kb-cohort
forks-ruling FORK C — superseded by
``service_interface::memory_service::cleanup_orphaned_vectors``.)
"""

from abc import ABC, abstractmethod
from typing import Any


class KnowledgeMaintenanceInterface(ABC):
    """Operator-fired KB-orphan cleanup abstract method — W5.R drift-closure addition."""

    @abstractmethod
    def purge_orphaned_chunks(
        self,
        confirm: bool = False,
        batch_size: int = 5000,
        max_batches: int | None = None,
    ) -> dict[str, Any]:
        """Operator-fired one-shot orphan-cleanup sweeper (W5.P).

        Hard-deletes ``knowledge:official`` memory rows whose IDs are not
        referenced by any active ``default_knowledge_plugin__knowledge_install``
        record. NEVER auto-fired on the spawn path — replaces the prior
        spawn-time auto-purge that blew the 600s router-registration budget
        on 2026-06-13. Default ``confirm=False`` returns a dry-run with the
        orphan count plus the first 10 sample IDs; ``confirm=True`` deletes
        in per-batch state-service transactions of ``batch_size`` rows.
        Optional ``max_batches`` caps the run when chunking work across
        maintenance windows.
        """
        ...

