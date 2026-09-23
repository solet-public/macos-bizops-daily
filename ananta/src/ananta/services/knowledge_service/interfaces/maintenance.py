"""Operator-fired KB-orphan cleanup service-interface verbs (W5.P + W5.Q).

``purge_orphaned_chunks`` hard-deletes knowledge:official memory rows
that no active install record references. It is operator-fired only —
NEVER auto-fired on the spawn path (the W5.P incident anchor). Lifted
byte-for-byte from the W5.Q-pre-decomposition ``KnowledgeServiceAPI``
where it landed via W5.P (2026-06-14).

The former companion verb ``purge_orphan_vectors`` was RETIRED per the
2026-06-22 kb-cohort forks-ruling FORK C: its cross-namespace
pgvector⟕actr anti-join is un-expressible on the state interface AND is
superseded by ``service_interface::memory_service::cleanup_orphaned_vectors``
(the owner rebuilds the shared vector namespace, which covers KB chunks
too; cascade-vector-first deletes prevent new orphans).
"""

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.services.service_interface_decorator import service_interface_process


class KnowledgeMaintenanceAPI(ABC):
    """Operator-fired KB-orphan cleanup verb — purge_orphaned_chunks."""

    @service_interface_process(
        name="purge_orphaned_chunks",
        provider="knowledge_service",
        is_discoverable=True,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "confirm": ParameterMetadata(
                description=(
                    "When False (default), runs a dry-run that returns the orphan "
                    "count + a sample of 10 IDs. When True, performs the batched "
                    "hard-delete."
                ),
                required=False,
                type=ParameterType.BOOLEAN,
                default=False,
            ),
            "batch_size": ParameterMetadata(
                description=(
                    "Rows per transaction batch when confirm=True. Bounded so a "
                    "single batch's transaction stays short. Default 5000."
                ),
                required=False,
                type=ParameterType.INTEGER,
                default=5000,
            ),
            "max_batches": ParameterMetadata(
                description=(
                    "When set, caps the run at this many batches. Useful for "
                    "operator-controlled chunked cleanup across maintenance windows."
                ),
                required=False,
                type=ParameterType.INTEGER,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Orphan-purge result. Per W5.P §4.6: operator-fired only — NEVER "
                "auto-fired on spawn path. Modes: dry_run / completed."
            ),
            type=ParameterType.OBJECT,
            properties={
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="'dry_run' or 'completed'.",
                ),
                "orphan_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "On dry_run: total orphans found. On completed: orphans "
                        "actually deleted (may be < total if max_batches bounded)."
                    ),
                ),
                "sample_ids": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="On dry_run only: first 10 orphan IDs for inspection.",
                ),
                "batches_run": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="On completed only: number of batches actually run.",
                ),
                "active_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Count of memory IDs referenced by active install records.",
                ),
                "total_chunk_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Total knowledge:official chunks (active + orphan).",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
    )
    @abstractmethod
    def purge_orphaned_chunks(
        self,
        confirm: bool = False,
        batch_size: int = 5000,
        max_batches: int | None = None,
    ) -> dict[str, Any]: ...
