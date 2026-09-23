"""Recorded lane-worktree seam for lifecycle dispatch smokes.

The lifecycle verbs own real ``git worktree`` mutation.  Smokes that exercise
dispatch wiring use this seam so their synthetic requests cannot escape the
test-owned temporary root or reach the shared checkout.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

import agent_messaging_plugin.session_lifecycle_verbs as lifecycle_verbs


@dataclass(frozen=True, slots=True)
class RecordedProvision:
    """One observed request to provision a synthetic lane worktree."""

    role_name: str
    agent_instance_id: str
    worktree: lifecycle_verbs.LaneWorktree


class RecordedLaneWorktreeFixture:
    """Replace lifecycle git mutation with a contained, assertion-friendly record."""

    def __init__(self, temp_root: Path) -> None:
        self.root = temp_root.resolve() / "recorded-lane-worktrees"
        self.provisioning_calls: list[RecordedProvision] = []
        self.retirement_calls: list[dict[str, object]] = []
        self.cleanup_calls: list[lifecycle_verbs.LaneWorktree] = []
        self._original_provision: Any = None
        self._original_adjudicate: Any = None
        self._original_retire: Any = None
        self._original_remove: Any = None

    def __enter__(self) -> RecordedLaneWorktreeFixture:
        self.root.mkdir(parents=True, exist_ok=True)
        self._original_provision = lifecycle_verbs._provision_spawn_worktree  # noqa: SLF001
        self._original_adjudicate = lifecycle_verbs._adjudicate_retire_lane_worktree  # noqa: SLF001
        self._original_retire = lifecycle_verbs._retire_lane_worktree  # noqa: SLF001
        self._original_remove = lifecycle_verbs.remove_lane_worktree  # noqa: SLF001
        lifecycle_verbs._provision_spawn_worktree = self._provision  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs._adjudicate_retire_lane_worktree = self._adjudicate  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs._retire_lane_worktree = self._retire  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs.remove_lane_worktree = self._cleanup  # type: ignore[assignment]  # noqa: SLF001
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        lifecycle_verbs._provision_spawn_worktree = self._original_provision  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs._adjudicate_retire_lane_worktree = self._original_adjudicate  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs._retire_lane_worktree = self._original_retire  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs.remove_lane_worktree = self._original_remove  # type: ignore[assignment]  # noqa: SLF001

    def contains(self, path: Path) -> bool:
        """Return whether ``path`` resolves under this fixture's temp root."""
        try:
            path.resolve().relative_to(self.root)
        except ValueError:
            return False
        return True

    def has_recorded_provisioning(self) -> bool:
        """Validate the complete role/branch/root record for every spawn."""
        return bool(self.provisioning_calls) and all(
            self.contains(call.worktree.repo_root)
            and self.contains(call.worktree.root)
            and self.contains(call.worktree.path)
            and call.worktree.branch == f"fixture/{call.role_name}/{call.agent_instance_id}"
            for call in self.provisioning_calls
        )

    def _provision(
        self,
        state: object,
        *,
        role_name: str,
        agent_instance_id: str,
        repository_root: str,
    ) -> lifecycle_verbs.LaneWorktree:
        del state, repository_root
        path = self.root / "worktrees" / agent_instance_id
        path.mkdir(parents=True, exist_ok=True)
        # Adapters validate only the worktree checkout marker before invoking
        # their recorded host doubles; never create a real repository here.
        (path / ".git").mkdir(exist_ok=True)
        worktree = lifecycle_verbs.LaneWorktree(
            repo_root=self.root,
            root=self.root,
            path=path,
            branch=f"fixture/{role_name}/{agent_instance_id}",
        )
        if not all(self.contains(target) for target in (worktree.repo_root, worktree.root, worktree.path)):
            raise AssertionError("recorded worktree target escaped its test-owned temporary root")
        self.provisioning_calls.append(RecordedProvision(role_name, agent_instance_id, worktree))
        return worktree

    def _adjudicate(self, row: Mapping[str, object]) -> None:
        del row

    def _retire(self, row: Mapping[str, object]) -> str:
        self.retirement_calls.append(dict(row))
        return "removed"

    def _cleanup(self, worktree: lifecycle_verbs.LaneWorktree) -> None:
        if not all(self.contains(target) for target in (worktree.repo_root, worktree.root, worktree.path)):
            raise AssertionError("recorded cleanup target escaped its test-owned temporary root")
        self.cleanup_calls.append(worktree)
