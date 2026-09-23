"""Shared stdlib-only types for the bootstrap operation adapter."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

Runner = Callable[..., subprocess.CompletedProcess[str]]
Which = Callable[[str], str | None]
Clock = Callable[[], datetime]
Sleep = Callable[[float], None]

SUPPORTED_POSTGRES_MAJOR = 17
PROBE_TIMEOUT_SECONDS = 10
INSTALL_TIMEOUT_SECONDS = 300
FORMULA_KEG_MARKER = "/Cellar/solet/"


class AdapterError(RuntimeError):
    """A host action or invariant could not complete safely."""


class AdapterRequestError(ValueError):
    """The caller supplied a request outside the frozen closed envelope."""


@dataclass(frozen=True)
class AdapterRuntime:
    """Injectable public host seams used by all hermetic route fixtures."""

    run: Runner
    which: Which
    now: Clock
    name: str
    target: Path
    base_python: str | None = None
    sleep: Sleep = time.sleep


@dataclass(frozen=True)
class PostgresObservation:
    """Public PostgreSQL installation and readiness facts."""

    brew_present: bool
    brew_path: str | None
    psql_present: bool
    homebrew_managed: bool
    major: int | None
    ready: bool
    pgvector_available: bool | None


@dataclass(frozen=True)
class RolePolicyObservation:
    """Public per-solet PostgreSQL policy facts."""

    role_exists: bool
    database_exists: bool
    role_safe: bool | None
    database_owner_matches: bool | None
    schema_exists: bool | None
    schema_owner_matches: bool | None
    public_connect_revoked: bool | None
    vector_installed: bool | None
    hba_path: Path | None
    hba_safe: bool
    hba_layout_recognized: bool
    scram_present: bool
    error_kind: str | None = None
