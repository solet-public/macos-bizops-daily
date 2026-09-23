"""KnowledgeServiceInterface — aggregate ABC re-exporting the 5 W5.R domain sub-Interfaces.

The W5.R decomposition (2026-06-14) split the original 183-LOC, 16-method
``KnowledgeServiceInterface`` god class into 5 domain sub-Interfaces co-located
under ``ananta/src/ananta/interfaces/`` and closed the 3-verb drift vs the
W5.Q-decomposed ``KnowledgeServiceAPI``:

- ``KnowledgeLifecycleInterface`` (..._lifecycle.py) — install / uninstall /
  update / list_installed / activate / deactivate.
- ``KnowledgeSearchInterface`` (..._search.py) — search /
  ``search_planning_references`` (W5.R drift-closure addition) / test_retrieval /
  audit_retrieval_corpus.
- ``KnowledgeFileOpsInterface`` (..._file_ops.py) — browse / read_file /
  edit_file / create_file / delete_file.
- ``KnowledgeRefreshInterface`` (..._refresh.py) — refresh_plugin_processes /
  refresh_plugin_process.
- ``KnowledgeMaintenanceInterface`` (..._maintenance.py) — ``purge_orphaned_chunks``
  (W5.R drift-closure addition, implemented by ``DefaultKnowledgePlugin`` per W5.P;
  the companion ``purge_orphan_vectors`` was RETIRED per the 2026-06-22 kb-cohort
  forks-ruling FORK C — superseded by ``memory_service::cleanup_orphaned_vectors``).

This file preserves the ``KnowledgeServiceInterface`` class name and the
``INTERFACE_VERSION`` class variable for backward compatibility — every
existing ``from ananta.interfaces.knowledge_service_interface import
KnowledgeServiceInterface`` continues to resolve, and every existing
``isinstance(x, KnowledgeServiceInterface)`` check continues to work.

INTERFACE_VERSION was bumped 3.0.0 → 3.1.0 (W5.R) to signal the additive 3-verb
expansion. The 2026-06-22 FORK C retirement of ``purge_orphan_vectors`` removed
one of those verbs again; the version constant is NOT re-bumped because the bound
plugin declares ``supported_interface_versions`` AS this constant (auto-lockstep,
so the binding check never mismatches) and the value is a compat token, not a
changelog. ``DefaultKnowledgePlugin`` implements the remaining surface; the
ABC-method removal and the plugin-method removal land in the same change.
"""

from __future__ import annotations

from abc import ABC
from typing import ClassVar

from .knowledge_service_interface_file_ops import KnowledgeFileOpsInterface
from .knowledge_service_interface_lifecycle import KnowledgeLifecycleInterface
from .knowledge_service_interface_maintenance import KnowledgeMaintenanceInterface
from .knowledge_service_interface_refresh import KnowledgeRefreshInterface
from .knowledge_service_interface_search import KnowledgeSearchInterface


class KnowledgeServiceInterface(
    KnowledgeLifecycleInterface,
    KnowledgeSearchInterface,
    KnowledgeFileOpsInterface,
    KnowledgeRefreshInterface,
    KnowledgeMaintenanceInterface,
    ABC,
):
    """Aggregate ABC: concrete-via-MI composition of the 5 W5.R domain sub-Interfaces.

    Implementers (the bound plugin ``DefaultKnowledgePlugin``, the
    service-class wrapper ``KnowledgeService``, the mock ``MockKnowledgePlugin``)
    continue to inherit from this single aggregate; the 19 abstract methods
    they implement now formally satisfy 5 sub-contracts. The forward-compat
    win pattern is now available: ``isinstance(plugin, KnowledgeMaintenanceInterface)``
    returns True, enabling per-domain dependency-injection use cases.

    MRO is deterministic since all 5 sub-Interfaces inherit from ``ABC``
    alone (no diamond).
    """

    INTERFACE_VERSION: ClassVar[str] = "3.1.0"


__all__ = [
    "KnowledgeFileOpsInterface",
    "KnowledgeLifecycleInterface",
    "KnowledgeMaintenanceInterface",
    "KnowledgeRefreshInterface",
    "KnowledgeSearchInterface",
    "KnowledgeServiceInterface",
]
