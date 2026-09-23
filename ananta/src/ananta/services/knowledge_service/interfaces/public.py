"""KnowledgeServiceAPI — aggregate ABC re-exporting the 5 W5.Q domain sub-ABCs.

The W5.Q decomposition split the original 996-LOC, 19-verb
``KnowledgeServiceAPI`` god class into 5 domain ABCs co-located under
``ananta/src/ananta/services/knowledge_service/interfaces/``:

- ``KnowledgeLifecycleAPI`` (lifecycle.py) — install / uninstall / update /
  list_installed / activate / deactivate.
- ``KnowledgeSearchAPI`` (search.py) — search / search_planning_references /
  test_retrieval / audit_retrieval_corpus.
- ``KnowledgeFileOpsAPI`` (file_ops.py) — browse / read_file / edit_file /
  create_file / delete_file.
- ``KnowledgeRefreshAPI`` (refresh.py) — refresh_plugin_processes /
  refresh_plugin_process.
- ``KnowledgeMaintenanceAPI`` (maintenance.py) — purge_orphaned_chunks (the
  operator-fired W5.P verb; the companion purge_orphan_vectors was retired per
  the kb-cohort forks-ruling FORK C).

This file preserves the ``KnowledgeServiceAPI`` class name for backward
import compatibility — every existing ``from ...interfaces.public import
KnowledgeServiceAPI`` continues to resolve via MI re-export.

**Honest framing of what W5.Q delivers:** decorator-surface organization +
import-path compatibility + 1-line allowlist remediation (line 119
``KnowledgeServiceAPI``). Lines 118 (``KnowledgeService`` delegating
wrapper) and 120 (``KnowledgeServiceInterface``) are deferred to W5.S
and W5.R respectively. As of W5.Q, NO class in the tree inherits from
``KnowledgeServiceAPI`` (verified via tree-wide ``grep -rn
"KnowledgeServiceAPI"``). The bound plugin ``DefaultKnowledgePlugin``
inherits from the parallel ABC ``KnowledgeServiceInterface`` at
``ananta/src/ananta/interfaces/knowledge_service_interface.py`` (W5.R
scope). The aggregate ABC exists today as documentation-as-type carrier
for the ``@service_interface_process(...)`` decorators that register
process keys at module-import time; a future W5.R closes the KSA-KSI
drift and could migrate the bound plugin to inherit from this aggregate.
"""

from __future__ import annotations

from abc import ABC

from .file_ops import KnowledgeFileOpsAPI
from .lifecycle import KnowledgeLifecycleAPI
from .maintenance import KnowledgeMaintenanceAPI
from .refresh import KnowledgeRefreshAPI
from .search import KnowledgeSearchAPI


class KnowledgeServiceAPI(
    KnowledgeLifecycleAPI,
    KnowledgeSearchAPI,
    KnowledgeFileOpsAPI,
    KnowledgeRefreshAPI,
    KnowledgeMaintenanceAPI,
    ABC,
):
    """Aggregate ABC: re-exports the 5 W5.Q domain sub-ABCs via MI.

    Preserves the ``KnowledgeServiceAPI`` class name for backward import
    compatibility. The 19 abstract verbs originally declared directly on
    this class are now declared across the 5 sub-ABCs; this aggregate's
    MI inheritance preserves the import path so callers using
    ``from ...interfaces.public import KnowledgeServiceAPI`` continue to
    resolve unchanged.

    MRO is deterministic since all 5 sub-ABCs inherit from ``ABC`` alone
    (no diamond).
    """


__all__ = [
    "KnowledgeFileOpsAPI",
    "KnowledgeLifecycleAPI",
    "KnowledgeMaintenanceAPI",
    "KnowledgeRefreshAPI",
    "KnowledgeSearchAPI",
    "KnowledgeServiceAPI",
]
