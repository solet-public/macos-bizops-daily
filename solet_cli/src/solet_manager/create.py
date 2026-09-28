"""Compatibility façade for manager-owned create orchestration."""

from __future__ import annotations

from pathlib import Path

from .completion_verifier import run_completion_probes
from .config import CreateConfig
from .contract_reconciliation import (
    reconciliation_recovery_pending,
    recover_contract_reconciliation,
)
from .create_enrollment import enroll_created_instance
from .create_execution import execute_create
from .models import CommandResult, JsonValue
from .operation_records import normalize_apply_result as _normalize_apply_result
from .paths import ManagerPaths
from .preview_engine import preview_create
from .registry import InstanceRegistry
from .state_io import instance_lock

__all__ = ["CreateManager", "_normalize_apply_result", "run_completion_probes"]


class CreateManager:
    """Preserve the public manager surface while delegating coherent workflows."""

    def __init__(
        self,
        *,
        paths: ManagerPaths,
        contract_directory: Path | None,
        seed_lock_path: Path,
    ) -> None:
        self.paths = paths
        self.contract_directory = contract_directory
        self.seed_lock_path = seed_lock_path
        self.registry = InstanceRegistry(
            paths.registry_path,
            maintenance_inventory_path=paths.maintenance_inventory_path,
            registry_lock_path=paths.registry_lock_path,
        )

    def preview(
        self,
        config: CreateConfig,
        *,
        decision_selections: dict[str, JsonValue] | None = None,
        decision_source: str = "flag",
        decision_sources: dict[str, str] | None = None,
    ) -> CommandResult:
        """Return the exact no-write preview currently knowable by the manager."""

        if reconciliation_recovery_pending(self.paths, config.name):
            with instance_lock(self.paths.lock_path(config.name), create=True):
                recover_contract_reconciliation(self.paths, config.name)
        return preview_create(
            paths=self.paths,
            contract_directory=self.contract_directory,
            seed_lock_path=self.seed_lock_path,
            registry=self.registry,
            config=config,
            decision_selections=decision_selections,
            decision_source=decision_source,
            decision_sources=decision_sources,
        )

    def create(
        self,
        config: CreateConfig,
        *,
        approved_fingerprint: str,
        decision_selections: dict[str, JsonValue] | None = None,
        decision_source: str = "flag",
        decision_sources: dict[str, str] | None = None,
        stop_after_stage: str | None = None,
    ) -> CommandResult:
        """Execute the current approved preview through the resumable transaction.

        A verified create is then enrolled for ``solet-manager update`` (iss_836499b3).
        """

        result = execute_create(
            paths=self.paths,
            contract_directory=self.contract_directory,
            seed_lock_path=self.seed_lock_path,
            registry=self.registry,
            preview_call=self.preview,
            config=config,
            approved_fingerprint=approved_fingerprint,
            decision_selections=decision_selections,
            decision_source=decision_source,
            decision_sources=decision_sources,
            stage_limit=stop_after_stage,
        )
        if result.kind != "create" or result.status != "verified":
            return result
        return enroll_created_instance(self.paths, config.name, config.target, self.seed_lock_path, result)
