"""DTOs for root-manifest drift classification.

See ``workbench/2026-06-16_root_manifest_yaml_design.md`` §3 + §6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

MANIFEST_FILENAME: Final[str] = "root_manifest.yaml"
SCHEMA_VERSION: Final[int] = 1
ENV_EXTRA_IGNORE_PATTERNS: Final[str] = "ANANTA_ROOT_MANIFEST_IGNORE_PATTERNS"


@dataclass(frozen=True)
class SunsetOverdue:
    """A sanctioned/overrides entry whose date-string sunset has passed.

    Milestone-string sunsets never produce this record per §4.4 — they
    require coordinator-side resolution.  Only YYYY-MM-DD sunsets that
    have elapsed are enforceable.
    """

    path: str
    sunset_target: str
    days_overdue: int


@dataclass(frozen=True)
class Manifest:
    """Parsed manifest payload (post-schema-validation)."""

    schema_version: int
    solet_name: str
    universal_files: tuple[str, ...]
    universal_directories: tuple[str, ...]
    platform_managed_directories: tuple[str, ...]
    sanctioned_entries: tuple[dict[str, str], ...]
    overrides_entries: tuple[dict[str, str], ...]
    diagnostic_report_categories: tuple[str, ...]
    diagnostic_ignore_patterns: tuple[str, ...]


@dataclass(frozen=True)
class Classification:
    """Root-directory drift summary.

    Same shape for every consumer.  Severity escalation is the
    consumer's call — see the design memo §6.1 / §6.4.
    """

    manifest_path: Path
    repo_root: Path
    solet_name: str | None
    unknown_entries: tuple[str, ...] = field(default_factory=tuple)
    untracked_entries: tuple[str, ...] = field(default_factory=tuple)
    git_index_consulted: bool = False
    missing_universal: tuple[str, ...] = field(default_factory=tuple)
    missing_sanctioned: tuple[str, ...] = field(default_factory=tuple)
    sunset_overdue: tuple[SunsetOverdue, ...] = field(default_factory=tuple)
    cleanup_overdue: tuple[str, ...] = field(default_factory=tuple)
    schema_validation_error: str | None = None

    @property
    def tracked_unknown_entries(self) -> tuple[str, ...]:
        """C6.1 entries the repository actually CARRIES (iss_ce45713b).

        ``unknown_entries`` is every undeclared root entry the filesystem
        scan found.  ``untracked_entries`` is the subset that Git's index
        does not cover at all — a lane's scratch note, a handover file
        written thirty seconds ago — which is only known when
        ``git_index_consulted`` is true.  A landing-time consumer blocks on
        this difference: the manifest polices what the repository holds,
        not what happens to be lying beside it.  With no Git index to ask,
        the two sets are identical and the check is exactly as strict as
        the pure filesystem scan it grew from.
        """
        untracked = set(self.untracked_entries)
        return tuple(name for name in self.unknown_entries if name not in untracked)

    @property
    def has_blocking_violations(self) -> bool:
        """``True`` if a BLOCKING-layer consumer should fail-closed.

        BLOCKING surfaces per §6.4: C6.1 unknown entries, C6.2 missing
        universal entries, C6.5 date-sunset overdue, C6.6 schema failure.
        INFO-only surfaces (C6.3 missing sanctioned, cleanup-overdue
        per Patch P3) do NOT trigger blocking.
        """
        return (
            bool(self.unknown_entries)
            or bool(self.missing_universal)
            or bool(self.sunset_overdue)
            or self.schema_validation_error is not None
        )

    @property
    def has_any_drift(self) -> bool:
        """``True`` if the diagnostic consumer should log a report."""
        return (
            self.has_blocking_violations
            or bool(self.missing_sanctioned)
            or bool(self.cleanup_overdue)
        )
